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
         .filter(order_line=order_line,
                 inbound__purchase_type=PurchaseInbound.PurchaseType.EXTERNAL)
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


def bind_inbound_lines_to_order(order, lines, *, exclude_inbound=None):
    """手工入库挂订单：按商品匹配订单行，校验不超过待收货数量。"""
    if order.status != PurchaseOrder.Status.OPEN:
        raise PurchaseOrderError("只能关联「执行中」的采购订单")
    buckets = {}
    for ol in order.lines.select_related("product"):
        remain = qty_open_receive(ol)
        if exclude_inbound is not None:
            already = (PurchaseInboundLine.objects
                       .filter(order_line=ol, inbound=exclude_inbound)
                       .exclude(inbound__status=PurchaseInbound.Status.VOID)
                       .aggregate(v=Sum("quantity"))["v"] or ZERO_QTY)
            remain = round_qty(remain + already)
        if remain > 0:
            buckets.setdefault(ol.product_id, []).append([ol, remain])

    out = []
    for i, ln in enumerate(lines, start=1):
        product = ln["product"]
        qty = round_qty(ln["quantity"])
        pool = buckets.get(product.pk)
        if not pool:
            raise PurchaseOrderError(
                f"第{i}行商品「{product}」不在订单 {order.doc_no} 的待收货明细中")
        ol, remain = pool[0]
        if qty > remain:
            raise PurchaseOrderError(
                f"第{i}行收货数量 {qty} 超过订单行待收货 {remain}（{order.doc_no}）")
        pool[0][1] = round_qty(remain - qty)
        if pool[0][1] <= 0:
            pool.pop(0)
        out.append({**ln, "order_line": ol})
    return out


def open_receive_initial_lines(order):
    """供入库单「载入订单待收明细」预填。"""
    rows = []
    for ol in order.lines.select_related("product"):
        remain = qty_open_receive(ol)
        if remain <= 0:
            continue
        tip = (round_money(ol.amount_taxed / ol.quantity)
               if ol.quantity else ZERO_MONEY)
        rows.append({
            "product": ol.product,
            "quantity": remain,
            "tax_rate": ol.tax_rate,
            "tax_inclusive_price": tip,
            "amount_untaxed": round_money(ol.amount_untaxed * (remain / ol.quantity)),
            "tax_amount": round_money(ol.tax_amount * (remain / ol.quantity)),
            "amount_taxed": round_money(ol.amount_taxed * (remain / ol.quantity)),
        })
    return rows


# ============================= M18-4 补单回挂 =============================

def _purchase_inbound_fully_invoiced(inbound) -> bool:
    from django.db.models import Sum
    from apps.finance.models import PurchaseInvoice, PurchaseInvoiceLine
    lines = list(inbound.lines.values("pk", "quantity"))
    if not lines:
        return True
    invoiced = {r["source_inbound_line"]: (r["q"] or ZERO_QTY) for r in
                PurchaseInvoiceLine.objects.filter(source_inbound_line__inbound=inbound)
                .exclude(invoice__status=PurchaseInvoice.Status.VOID)
                .values("source_inbound_line").annotate(q=Sum("quantity"))}
    return all(invoiced.get(ln["pk"], ZERO_QTY) >= ln["quantity"] for ln in lines)


def purchase_backfill_candidates(company):
    from apps.finance.models import PurchaseInvoice

    inbounds = (PurchaseInbound.objects
                .filter(company=company, purchase_order__isnull=True,
                        purchase_type=PurchaseInbound.PurchaseType.EXTERNAL, is_opening=False)
                .exclude(status=PurchaseInbound.Status.VOID)
                .select_related("supplier")
                .prefetch_related("lines"))
    incomplete_ib = [o for o in inbounds if not _purchase_inbound_fully_invoiced(o)]

    invoices = (PurchaseInvoice.objects
                .filter(company=company, purchase_order__isnull=True, is_opening=False,
                        status=PurchaseInvoice.Status.REGISTERED)
                .select_related("supplier")
                .prefetch_related("lines"))
    incomplete_inv = [i for i in invoices if i.outstanding > 0]

    by_sup = {}
    for o in incomplete_ib:
        if not o.supplier_id:
            continue
        d = by_sup.setdefault(o.supplier_id, {"supplier": o.supplier,
                                              "inbounds": [], "invoices": []})
        d["inbounds"].append(o)
    for inv in incomplete_inv:
        d = by_sup.setdefault(inv.supplier_id, {"supplier": inv.supplier,
                                                "inbounds": [], "invoices": []})
        d["invoices"].append(inv)
    rows = sorted(by_sup.values(), key=lambda r: r["supplier"].code)
    for r in rows:
        r["ib_count"] = len(r["inbounds"])
        r["inv_count"] = len(r["invoices"])
    return rows


def purchase_order_progress_rows(company):
    rows = []
    qs = (PurchaseOrder.objects.filter(company=company, status=PurchaseOrder.Status.OPEN)
          .select_related("supplier").prefetch_related("lines"))
    for order in qs:
        open_recv = open_inv = ZERO_QTY
        for ln in order.lines.all():
            open_recv = round_qty(open_recv + qty_open_receive(ln))
            open_inv = round_qty(open_inv + qty_open_invoice(ln))
        rows.append({
            "order": order,
            "qty_open_receive": open_recv,
            "qty_open_invoice": open_inv,
        })
    return rows


@transaction.atomic
def backfill_purchase_order(*, company, user, supplier, inbound_ids, invoice_ids,
                            doc_date=None, remark="") -> PurchaseOrder:
    """为未完成入库/发票补建采购订单并回挂（不改金额与库存）。"""
    from django.utils import timezone
    from apps.finance.models import PurchaseInvoice

    if not supplier:
        raise PurchaseOrderError("供应商必填")
    inbound_ids = [int(x) for x in inbound_ids]
    invoice_ids = [int(x) for x in invoice_ids]
    if not inbound_ids and not invoice_ids:
        raise PurchaseOrderError("请至少选择一张入库单或发票")

    inbounds = list(PurchaseInbound.objects.filter(
        company=company, pk__in=inbound_ids).prefetch_related("lines__product"))
    invoices = list(PurchaseInvoice.objects.filter(
        company=company, pk__in=invoice_ids).prefetch_related("lines__product"))
    if len(inbounds) != len(set(inbound_ids)):
        raise PurchaseOrderError("所选入库单不存在或不属于本账套")
    if len(invoices) != len(set(invoice_ids)):
        raise PurchaseOrderError("所选发票不存在或不属于本账套")

    for ib in inbounds:
        if ib.purchase_order_id:
            raise PurchaseOrderError(f"入库单 {ib.doc_no} 已挂订单，不能再补")
        if ib.supplier_id and ib.supplier_id != supplier.pk:
            raise PurchaseOrderError(f"入库单 {ib.doc_no} 供应商与所选不一致")
        if ib.supplier_id is None:
            raise PurchaseOrderError(f"入库单 {ib.doc_no} 无供应商，无法补单")
        if ib.status == PurchaseInbound.Status.VOID or ib.is_opening:
            raise PurchaseOrderError(f"入库单 {ib.doc_no} 状态不可补")
        if ib.purchase_type != PurchaseInbound.PurchaseType.EXTERNAL:
            raise PurchaseOrderError(f"入库单 {ib.doc_no} 非外购，不纳入补单")
    for inv in invoices:
        if inv.purchase_order_id:
            raise PurchaseOrderError(f"发票 {inv.doc_no} 已挂订单，不能再补")
        if inv.supplier_id != supplier.pk:
            raise PurchaseOrderError(f"发票 {inv.doc_no} 供应商与所选不一致")
        if inv.status == PurchaseInvoice.Status.VOID or inv.is_opening:
            raise PurchaseOrderError(f"发票 {inv.doc_no} 状态不可补")

    buckets = {}
    def buck(pid):
        return buckets.setdefault(pid, {
            "product": None, "recv_qty": ZERO_QTY, "inv_qty": ZERO_QTY,
            "recv_untaxed": ZERO_MONEY, "inv_untaxed": ZERO_MONEY,
            "rate": DEFAULT_TAX_RATE,
        })

    for ib in inbounds:
        for ln in ib.lines.all():
            b = buck(ln.product_id)
            b["product"] = ln.product
            b["recv_qty"] = round_qty(b["recv_qty"] + ln.quantity)
            b["recv_untaxed"] = round_money(b["recv_untaxed"] + ln.amount_untaxed)
            b["rate"] = ln.tax_rate
    for inv in invoices:
        for ln in inv.lines.all():
            if not ln.product_id:
                continue
            b = buck(ln.product_id)
            b["product"] = ln.product
            b["inv_qty"] = round_qty(b["inv_qty"] + ln.quantity)
            b["inv_untaxed"] = round_money(b["inv_untaxed"] + ln.amount_untaxed)
            b["rate"] = ln.tax_rate

    if not buckets:
        raise PurchaseOrderError("所选单据无商品明细，无法生成订单行")

    order_lines = []
    for b in buckets.values():
        qty = max(b["recv_qty"], b["inv_qty"])
        if qty <= 0:
            continue
        if b["recv_qty"] > 0:
            untaxed = round_money(b["recv_untaxed"] / b["recv_qty"] * qty)
        else:
            untaxed = round_money(b["inv_untaxed"] / b["inv_qty"] * qty)
        order_lines.append({
            "product": b["product"], "quantity": qty,
            "amount_untaxed": untaxed, "tax_rate": b["rate"],
        })

    doc_date = doc_date or timezone.localdate()
    order = create_purchase_order(
        company=company, user=user, doc_date=doc_date, supplier=supplier,
        lines=order_lines,
        remark=remark or "补单回挂（未完成业务）",
    )
    prod_map = {ln.product_id: ln for ln in order.lines.all()}

    for ib in inbounds:
        ib.purchase_order = order
        ib.save(update_fields=["purchase_order"])
        for ln in ib.lines.all():
            ol = prod_map.get(ln.product_id)
            if ol:
                ln.order_line = ol
                ln.save(update_fields=["order_line"])
    for inv in invoices:
        inv.purchase_order = order
        inv.save(update_fields=["purchase_order"])
        for ln in inv.lines.all():
            ol = prod_map.get(ln.product_id) if ln.product_id else None
            if ol:
                ln.order_line = ol
                ln.save(update_fields=["order_line"])

    refresh_order_status(order)
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.LINK, target=order,
        summary=(f"补单回挂 {order.doc_no}：入库 {len(inbounds)} 张、"
                 f"发票 {len(invoices)} 张（未改变入账金额）"),
    )
    return order
