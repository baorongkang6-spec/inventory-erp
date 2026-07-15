"""采购订单服务（M18-3，SPEC §20）：对称销售订单。"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY, round_money, round_qty
from apps.finance.models import PurchaseInvoice, PurchaseInvoiceLine
from apps.finance.services import create_purchase_invoice

from .models import PurchaseInbound, PurchaseInboundLine, PurchaseOrder, PurchaseOrderLine
from .services import _line_amounts, create_and_post_inbound


class PurchaseOrderError(ValidationError):
    """采购订单业务校验失败。"""


def _progress(ordered: Decimal, done: Decimal) -> str:
    ordered = ordered or ZERO_QTY
    done = done or ZERO_QTY
    if done <= 0:
        return PurchaseOrder.Progress.NONE
    if done + Decimal("0.0005") >= ordered:
        return PurchaseOrder.Progress.FULL
    return PurchaseOrder.Progress.PARTIAL


def qty_received(order_line: PurchaseOrderLine) -> Decimal:
    v = (PurchaseInboundLine.objects
         .filter(order_line=order_line)
         .exclude(inbound__status=PurchaseInbound.Status.VOID)
         .aggregate(v=Sum("quantity"))["v"])
    return round_qty(v or ZERO_QTY)


def qty_invoiced(order_line: PurchaseOrderLine) -> Decimal:
    v = (PurchaseInvoiceLine.objects
         .filter(order_line=order_line)
         .exclude(invoice__status=PurchaseInvoice.Status.VOID)
         .aggregate(v=Sum("quantity"))["v"])
    return round_qty(v or ZERO_QTY)


def qty_open_receive(order_line: PurchaseOrderLine) -> Decimal:
    return round_qty(order_line.quantity - qty_received(order_line))


def qty_open_invoice(order_line: PurchaseOrderLine) -> Decimal:
    return round_qty(order_line.quantity - qty_invoiced(order_line))


def line_progress(order_line: PurchaseOrderLine) -> dict:
    received = qty_received(order_line)
    invoiced = qty_invoiced(order_line)
    return {
        "qty_received": received,
        "qty_invoiced": invoiced,
        "qty_open_receive": round_qty(order_line.quantity - received),
        "qty_open_invoice": round_qty(order_line.quantity - invoiced),
    }


def _amount_paid(order: PurchaseOrder) -> Decimal:
    ids = (PurchaseInvoiceLine.objects
           .filter(order_line__order=order)
           .exclude(invoice__status=PurchaseInvoice.Status.VOID)
           .values_list("invoice_id", flat=True).distinct())
    v = (PurchaseInvoice.objects.filter(pk__in=ids)
         .aggregate(v=Sum("settled_amount"))["v"])
    return round_money(v or ZERO_MONEY)


@transaction.atomic
def refresh_order_status(order: PurchaseOrder) -> PurchaseOrder:
    lines = list(order.lines.all())
    if not lines:
        order.receive_status = PurchaseOrder.Progress.NONE
        order.invoice_status = PurchaseOrder.Progress.NONE
        order.payment_status = PurchaseOrder.Progress.NONE
        order.save(update_fields=["receive_status", "invoice_status", "payment_status"])
        return order
    total_qty = sum((ln.quantity for ln in lines), ZERO_QTY)
    received = sum((qty_received(ln) for ln in lines), ZERO_QTY)
    invoiced = sum((qty_invoiced(ln) for ln in lines), ZERO_QTY)
    order.receive_status = _progress(total_qty, received)
    order.invoice_status = _progress(total_qty, invoiced)
    taxed = order.total_taxed or ZERO_MONEY
    paid = _amount_paid(order)
    if paid <= 0:
        order.payment_status = PurchaseOrder.Progress.NONE
    elif taxed and paid + Decimal("0.005") >= taxed:
        order.payment_status = PurchaseOrder.Progress.FULL
    else:
        order.payment_status = PurchaseOrder.Progress.PARTIAL
    order.save(update_fields=["receive_status", "invoice_status", "payment_status"])
    return order


@transaction.atomic
def create_purchase_order(*, company, user, doc_date, supplier, lines, remark="") -> PurchaseOrder:
    if not supplier:
        raise PurchaseOrderError("供应商必填")
    if not lines:
        raise PurchaseOrderError("至少一行明细")
    order = PurchaseOrder.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(PurchaseOrder, company, "PO", doc_date),
        doc_date=doc_date, supplier=supplier, remark=remark or "",
    )
    total_qty = ZERO_QTY
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for i, ln in enumerate(lines, start=1):
        quantity = round_qty(ln["quantity"])
        if quantity <= 0:
            raise PurchaseOrderError(f"第{i}行数量必须大于 0")
        rate = ln.get("tax_rate", DEFAULT_TAX_RATE)
        payload = dict(ln)
        if (payload.get("amount_untaxed") is None and payload.get("tax_inclusive_price") is None
                and payload.get("unit_price") is not None):
            pass  # _line_amounts uses unit_price
        untaxed, tax, taxed = _line_amounts(quantity, rate, payload)
        unit = round_money(untaxed / quantity) if quantity else ZERO_MONEY
        PurchaseOrderLine.objects.create(
            order=order, line_no=i * 10, product=ln["product"], quantity=quantity,
            unit_price=unit, tax_rate=rate,
            amount_untaxed=untaxed, tax_amount=tax, amount_taxed=taxed,
        )
        total_qty = round_qty(total_qty + quantity)
        total_untaxed = round_money(total_untaxed + untaxed)
        total_tax = round_money(total_tax + tax)
        total_taxed = round_money(total_taxed + taxed)
    order.total_quantity = total_qty
    order.total_untaxed = total_untaxed
    order.total_tax = total_tax
    order.total_taxed = total_taxed
    order.save(update_fields=["total_quantity", "total_untaxed", "total_tax", "total_taxed"])
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=order,
        summary=f"采购订单 {order.doc_no} 供应商 {supplier} 含税 {total_taxed}",
    )
    return order


@transaction.atomic
def update_purchase_order(*, order, user, doc_date, supplier, lines, remark="") -> PurchaseOrder:
    if order.status == PurchaseOrder.Status.VOID:
        raise PurchaseOrderError("已作废订单不可修改")
    if order.inbounds.exclude(status=PurchaseInbound.Status.VOID).exists():
        raise PurchaseOrderError("已有入库执行，不可改订单明细")
    if PurchaseInvoiceLine.objects.filter(order_line__order=order).exclude(
            invoice__status=PurchaseInvoice.Status.VOID).exists():
        raise PurchaseOrderError("已有发票执行，不可改订单明细")
    order.doc_date = doc_date
    order.supplier = supplier
    order.remark = remark or ""
    order.lines.all().delete()
    total_qty = ZERO_QTY
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for i, ln in enumerate(lines, start=1):
        quantity = round_qty(ln["quantity"])
        rate = ln.get("tax_rate", DEFAULT_TAX_RATE)
        untaxed, tax, taxed = _line_amounts(quantity, rate, ln)
        unit = round_money(untaxed / quantity) if quantity else ZERO_MONEY
        PurchaseOrderLine.objects.create(
            order=order, line_no=i * 10, product=ln["product"], quantity=quantity,
            unit_price=unit, tax_rate=rate,
            amount_untaxed=untaxed, tax_amount=tax, amount_taxed=taxed,
        )
        total_qty = round_qty(total_qty + quantity)
        total_untaxed = round_money(total_untaxed + untaxed)
        total_tax = round_money(total_tax + tax)
        total_taxed = round_money(total_taxed + taxed)
    order.total_quantity = total_qty
    order.total_untaxed = total_untaxed
    order.total_tax = total_tax
    order.total_taxed = total_taxed
    order.status = PurchaseOrder.Status.OPEN
    order.save()
    refresh_order_status(order)
    AuditLog.record(
        actor=user, company=order.company, action=AuditLog.Action.UPDATE, target=order,
        summary=f"修改采购订单 {order.doc_no}",
    )
    return order


@transaction.atomic
def void_purchase_order(*, order, user) -> PurchaseOrder:
    if order.status == PurchaseOrder.Status.VOID:
        raise PurchaseOrderError("订单已作废")
    if order.inbounds.exclude(status=PurchaseInbound.Status.VOID).exists():
        raise PurchaseOrderError("仍有未作废入库，不能作废订单")
    if PurchaseInvoiceLine.objects.filter(order_line__order=order).exclude(
            invoice__status=PurchaseInvoice.Status.VOID).exists():
        raise PurchaseOrderError("仍有未作废发票，不能作废订单")
    order.status = PurchaseOrder.Status.VOID
    order.save(update_fields=["status"])
    AuditLog.record(
        actor=user, company=order.company, action=AuditLog.Action.VOID, target=order,
        summary=f"作废采购订单 {order.doc_no}",
    )
    return order


@transaction.atomic
def create_inbound_from_order(*, order, user, doc_date, lines=None, remark="") -> PurchaseInbound:
    if order.status != PurchaseOrder.Status.OPEN:
        raise PurchaseOrderError("仅「执行中」订单可生成入库")
    if lines is None:
        lines = []
        for ol in order.lines.all():
            remain = qty_open_receive(ol)
            if remain > 0:
                lines.append({
                    "order_line": ol, "quantity": remain,
                    "amount_untaxed": round_money(ol.amount_untaxed * (remain / ol.quantity)),
                    "tax_rate": ol.tax_rate,
                })
    if not lines:
        raise PurchaseOrderError("没有可收货数量")
    in_lines = []
    for i, ln in enumerate(lines, start=1):
        ol = ln["order_line"]
        if isinstance(ol, int):
            ol = PurchaseOrderLine.objects.get(pk=ol, order=order)
        if ol.order_id != order.pk:
            raise PurchaseOrderError(f"第{i}行不属于本订单")
        qty = round_qty(ln["quantity"])
        if qty <= 0:
            raise PurchaseOrderError(f"第{i}行收货数量必须大于 0")
        remain = qty_open_receive(ol)
        if qty > remain:
            raise PurchaseOrderError(
                f"第{i}行收货数量 {qty} 超过待收货 {remain}（订单行 {ol.line_no}）")
        payload = {
            "product": ol.product, "quantity": qty,
            "tax_rate": ln.get("tax_rate", ol.tax_rate),
            "order_line": ol,
        }
        if ln.get("amount_untaxed") is not None:
            payload["amount_untaxed"] = ln["amount_untaxed"]
        elif ln.get("tax_inclusive_price") is not None:
            payload["tax_inclusive_price"] = ln["tax_inclusive_price"]
        else:
            payload["amount_untaxed"] = round_money(ol.amount_untaxed * (qty / ol.quantity))
        in_lines.append(payload)
    doc = create_and_post_inbound(
        company=order.company, user=user, doc_date=doc_date, supplier=order.supplier,
        remark=remark or f"来源订单 {order.doc_no}", lines=in_lines,
        purchase_order=order,
    )
    refresh_order_status(order)
    return doc


@transaction.atomic
def create_invoice_from_order(*, order, user, doc_date, lines=None, remark="",
                              invoice_no="", term_days=0) -> PurchaseInvoice:
    if order.status != PurchaseOrder.Status.OPEN:
        raise PurchaseOrderError("仅「执行中」订单可生成发票")
    if lines is None:
        lines = []
        for ol in order.lines.all():
            remain = qty_open_invoice(ol)
            if remain > 0:
                lines.append({
                    "order_line": ol, "quantity": remain,
                    "amount_untaxed": round_money(ol.amount_untaxed * (remain / ol.quantity)),
                    "tax_rate": ol.tax_rate,
                })
    if not lines:
        raise PurchaseOrderError("没有可收票数量")
    inv_lines = []
    for i, ln in enumerate(lines, start=1):
        ol = ln["order_line"]
        if isinstance(ol, int):
            ol = PurchaseOrderLine.objects.get(pk=ol, order=order)
        if ol.order_id != order.pk:
            raise PurchaseOrderError(f"第{i}行不属于本订单")
        qty = round_qty(ln["quantity"])
        if qty <= 0:
            raise PurchaseOrderError(f"第{i}行收票数量必须大于 0")
        remain = qty_open_invoice(ol)
        if qty > remain:
            raise PurchaseOrderError(
                f"第{i}行收票数量 {qty} 超过待收票 {remain}（订单行 {ol.line_no}）")
        if ln.get("amount_untaxed") is not None:
            untaxed = round_money(ln["amount_untaxed"])
        else:
            untaxed = round_money(ol.amount_untaxed * (qty / ol.quantity))
        inv_lines.append({
            "product": ol.product, "description": "",
            "quantity": qty, "amount_untaxed": untaxed,
            "tax_rate": ln.get("tax_rate", ol.tax_rate),
            "source_inbound_line": ln.get("source_inbound_line"),
            "order_line": ol,
        })
    inv = create_purchase_invoice(
        company=order.company, user=user, doc_date=doc_date, supplier=order.supplier,
        lines=inv_lines, invoice_no=invoice_no, remark=remark or f"来源订单 {order.doc_no}",
        term_days=term_days, purchase_order=order,
    )
    refresh_order_status(order)
    return inv
