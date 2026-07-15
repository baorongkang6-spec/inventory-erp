"""销售订单服务（M18-2，SPEC §20）：创建/更新/进度刷新、由订单生成出库与发票。"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY, round_money, round_qty
from apps.finance.models import SalesInvoice, SalesInvoiceLine
from apps.finance.services import create_sales_invoice
from apps.purchasing.services import _line_amounts

from .models import SalesOrder, SalesOrderLine, SalesOutbound, SalesOutboundLine
from .services import create_and_post_outbound


class SalesOrderError(ValidationError):
    """销售订单业务校验失败。"""


def _progress(ordered: Decimal, done: Decimal) -> str:
    ordered = ordered or ZERO_QTY
    done = done or ZERO_QTY
    if done <= 0:
        return SalesOrder.Progress.NONE
    if done + Decimal("0.0005") >= ordered:  # 容忍数量尾差
        return SalesOrder.Progress.FULL
    return SalesOrder.Progress.PARTIAL


def qty_shipped(order_line: SalesOrderLine) -> Decimal:
    v = (SalesOutboundLine.objects
         .filter(order_line=order_line,
                 outbound__sales_type=SalesOutbound.SalesType.SALE)
         .exclude(outbound__status=SalesOutbound.Status.VOID)
         .aggregate(v=Sum("quantity"))["v"])
    return round_qty(v or ZERO_QTY)


def qty_invoiced(order_line: SalesOrderLine) -> Decimal:
    v = (SalesInvoiceLine.objects
         .filter(order_line=order_line)
         .exclude(invoice__status=SalesInvoice.Status.VOID)
         .aggregate(v=Sum("quantity"))["v"])
    return round_qty(v or ZERO_QTY)


def qty_open_ship(order_line: SalesOrderLine) -> Decimal:
    return round_qty(order_line.quantity - qty_shipped(order_line))


def qty_open_invoice(order_line: SalesOrderLine) -> Decimal:
    return round_qty(order_line.quantity - qty_invoiced(order_line))


def line_progress(order_line: SalesOrderLine) -> dict:
    shipped = qty_shipped(order_line)
    invoiced = qty_invoiced(order_line)
    return {
        "qty_shipped": shipped,
        "qty_invoiced": invoiced,
        "qty_open_ship": round_qty(order_line.quantity - shipped),
        "qty_open_invoice": round_qty(order_line.quantity - invoiced),
        "amount_invoiced": _amount_invoiced(order_line),
    }


def _amount_invoiced(order_line: SalesOrderLine) -> Decimal:
    v = (SalesInvoiceLine.objects
         .filter(order_line=order_line)
         .exclude(invoice__status=SalesInvoice.Status.VOID)
         .aggregate(v=Sum("amount_taxed"))["v"])
    return round_money(v or ZERO_MONEY)


def _amount_received(order: SalesOrder) -> Decimal:
    """订单关联发票已核销含税合计。"""
    from apps.finance.models import SalesInvoice
    ids = (SalesInvoiceLine.objects
           .filter(order_line__order=order)
           .exclude(invoice__status=SalesInvoice.Status.VOID)
           .values_list("invoice_id", flat=True).distinct())
    v = (SalesInvoice.objects.filter(pk__in=ids)
         .aggregate(v=Sum("settled_amount"))["v"])
    return round_money(v or ZERO_MONEY)


@transaction.atomic
def refresh_order_status(order: SalesOrder) -> SalesOrder:
    """按执行单汇总回写发货/开票/收款状态与表头合计（合计仍以订单行为准）。"""
    lines = list(order.lines.all())
    if not lines:
        order.ship_status = SalesOrder.Progress.NONE
        order.invoice_status = SalesOrder.Progress.NONE
        order.receipt_status = SalesOrder.Progress.NONE
        order.save(update_fields=["ship_status", "invoice_status", "receipt_status"])
        return order
    total_qty = sum((ln.quantity for ln in lines), ZERO_QTY)
    shipped = sum((qty_shipped(ln) for ln in lines), ZERO_QTY)
    invoiced = sum((qty_invoiced(ln) for ln in lines), ZERO_QTY)
    order.ship_status = _progress(total_qty, shipped)
    order.invoice_status = _progress(total_qty, invoiced)
    taxed = order.total_taxed or ZERO_MONEY
    received = _amount_received(order)
    if received <= 0:
        order.receipt_status = SalesOrder.Progress.NONE
    elif taxed and received + Decimal("0.005") >= taxed:
        order.receipt_status = SalesOrder.Progress.FULL
    else:
        order.receipt_status = SalesOrder.Progress.PARTIAL
    order.save(update_fields=["ship_status", "invoice_status", "receipt_status"])
    return order


def _build_order_line_amounts(quantity, rate, ln_in):
    untaxed, tax, taxed = _line_amounts(quantity, rate, ln_in)
    sale_price = round_money(untaxed / quantity) if quantity else ZERO_MONEY
    return sale_price, untaxed, tax, taxed


@transaction.atomic
def create_sales_order(*, company, user, doc_date, customer, lines, remark="") -> SalesOrder:
    """创建销售订单。

    lines: [{"product", "quantity", 可选 tax_inclusive_price / amount_untaxed / tax_rate / ...}, ...]
    """
    if not customer:
        raise SalesOrderError("客户必填")
    if not lines:
        raise SalesOrderError("至少一行明细")
    order = SalesOrder.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(SalesOrder, company, "SO", doc_date),
        doc_date=doc_date, customer=customer, remark=remark or "",
    )
    total_qty = ZERO_QTY
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for i, ln in enumerate(lines, start=1):
        quantity = round_qty(ln["quantity"])
        if quantity <= 0:
            raise SalesOrderError(f"第{i}行数量必须大于 0")
        rate = ln.get("tax_rate", DEFAULT_TAX_RATE)
        payload = dict(ln)
        if (payload.get("amount_untaxed") is None and payload.get("tax_inclusive_price") is None
                and payload.get("sale_unit_price") is not None):
            payload["unit_price"] = payload["sale_unit_price"]
        sale_price, untaxed, tax, taxed = _build_order_line_amounts(quantity, rate, payload)
        SalesOrderLine.objects.create(
            order=order, line_no=i * 10, product=ln["product"], quantity=quantity,
            sale_unit_price=sale_price, tax_rate=rate,
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
        summary=f"销售订单 {order.doc_no} 客户 {customer} 含税 {total_taxed}",
    )
    return order


@transaction.atomic
def update_sales_order(*, order, user, doc_date, customer, lines, remark="") -> SalesOrder:
    """修改订单：仅当尚无任何出库/发票执行时可改明细。"""
    if order.status == SalesOrder.Status.VOID:
        raise SalesOrderError("已作废订单不可修改")
    if order.outbounds.exclude(status=SalesOutbound.Status.VOID).exists():
        raise SalesOrderError("已有出库执行，不可改订单明细（可改备注日期请用有限字段；当前版本禁止改行）")
    if SalesInvoiceLine.objects.filter(order_line__order=order).exclude(
            invoice__status=SalesInvoice.Status.VOID).exists():
        raise SalesOrderError("已有发票执行，不可改订单明细")
    order.doc_date = doc_date
    order.customer = customer
    order.remark = remark or ""
    order.lines.all().delete()
    total_qty = ZERO_QTY
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for i, ln in enumerate(lines, start=1):
        quantity = round_qty(ln["quantity"])
        rate = ln.get("tax_rate", DEFAULT_TAX_RATE)
        payload = dict(ln)
        if (payload.get("amount_untaxed") is None and payload.get("tax_inclusive_price") is None
                and payload.get("sale_unit_price") is not None):
            payload["unit_price"] = payload["sale_unit_price"]
        sale_price, untaxed, tax, taxed = _build_order_line_amounts(quantity, rate, payload)
        SalesOrderLine.objects.create(
            order=order, line_no=i * 10, product=ln["product"], quantity=quantity,
            sale_unit_price=sale_price, tax_rate=rate,
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
    order.status = SalesOrder.Status.OPEN
    order.save()
    refresh_order_status(order)
    AuditLog.record(
        actor=user, company=order.company, action=AuditLog.Action.UPDATE, target=order,
        summary=f"修改销售订单 {order.doc_no}",
    )
    return order


@transaction.atomic
def void_sales_order(*, order, user) -> SalesOrder:
    if order.status == SalesOrder.Status.VOID:
        raise SalesOrderError("订单已作废")
    if order.outbounds.exclude(status=SalesOutbound.Status.VOID).exists():
        raise SalesOrderError("仍有未作废出库，不能作废订单")
    if SalesInvoiceLine.objects.filter(order_line__order=order).exclude(
            invoice__status=SalesInvoice.Status.VOID).exists():
        raise SalesOrderError("仍有未作废发票，不能作废订单")
    order.status = SalesOrder.Status.VOID
    order.save(update_fields=["status"])
    AuditLog.record(
        actor=user, company=order.company, action=AuditLog.Action.VOID, target=order,
        summary=f"作废销售订单 {order.doc_no}",
    )
    return order


@transaction.atomic
def create_outbound_from_order(*, order, user, doc_date, lines=None, remark="") -> SalesOutbound:
    """由销售订单生成并过账出库。

    lines: None=按全部待发货数量生成；
           或 [{"order_line": SalesOrderLine|id, "quantity": Decimal, 可选金额覆盖}, ...]
    """
    if order.status != SalesOrder.Status.OPEN:
        raise SalesOrderError("仅「执行中」订单可生成出库")
    if lines is None:
        lines = []
        for ol in order.lines.all():
            remain = qty_open_ship(ol)
            if remain > 0:
                lines.append({
                    "order_line": ol, "quantity": remain,
                    "amount_untaxed": round_money(ol.amount_untaxed * (remain / ol.quantity)),
                    "tax_rate": ol.tax_rate,
                })
    if not lines:
        raise SalesOrderError("没有可发货数量")
    out_lines = []
    for i, ln in enumerate(lines, start=1):
        ol = ln["order_line"]
        if isinstance(ol, int):
            ol = SalesOrderLine.objects.get(pk=ol, order=order)
        if ol.order_id != order.pk:
            raise SalesOrderError(f"第{i}行不属于本订单")
        qty = round_qty(ln["quantity"])
        if qty <= 0:
            raise SalesOrderError(f"第{i}行发货数量必须大于 0")
        remain = qty_open_ship(ol)
        if qty > remain:
            raise SalesOrderError(
                f"第{i}行发货数量 {qty} 超过待发货 {remain}（订单行 {ol.line_no}）")
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
            # 按订单行不含税比例分摊
            payload["amount_untaxed"] = round_money(ol.amount_untaxed * (qty / ol.quantity))
        out_lines.append(payload)
    doc = create_and_post_outbound(
        company=order.company, user=user, doc_date=doc_date, customer=order.customer,
        remark=remark or f"来源订单 {order.doc_no}", lines=out_lines,
        sales_order=order,
    )
    refresh_order_status(order)
    return doc


@transaction.atomic
def create_invoice_from_order(*, order, user, doc_date, lines=None, remark="",
                              invoice_no="", term_days=0) -> SalesInvoice:
    """由销售订单生成销售发票（可不先出库，支持先票后货）。

    lines: None=按全部待开票数量；
           或 [{"order_line", "quantity", 可选 source_outbound_line / 金额}, ...]
    """
    if order.status != SalesOrder.Status.OPEN:
        raise SalesOrderError("仅「执行中」订单可生成发票")
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
        raise SalesOrderError("没有可开票数量")
    inv_lines = []
    for i, ln in enumerate(lines, start=1):
        ol = ln["order_line"]
        if isinstance(ol, int):
            ol = SalesOrderLine.objects.get(pk=ol, order=order)
        if ol.order_id != order.pk:
            raise SalesOrderError(f"第{i}行不属于本订单")
        qty = round_qty(ln["quantity"])
        if qty <= 0:
            raise SalesOrderError(f"第{i}行开票数量必须大于 0")
        remain = qty_open_invoice(ol)
        if qty > remain:
            raise SalesOrderError(
                f"第{i}行开票数量 {qty} 超过待开票 {remain}（订单行 {ol.line_no}）")
        if ln.get("amount_untaxed") is not None:
            untaxed = round_money(ln["amount_untaxed"])
        else:
            untaxed = round_money(ol.amount_untaxed * (qty / ol.quantity))
        inv_lines.append({
            "product": ol.product,
            "description": "",
            "quantity": qty,
            "amount_untaxed": untaxed,
            "tax_rate": ln.get("tax_rate", ol.tax_rate),
            "source_outbound_line": ln.get("source_outbound_line"),
            "order_line": ol,
        })
    inv = create_sales_invoice(
        company=order.company, user=user, doc_date=doc_date, customer=order.customer,
        lines=inv_lines, invoice_no=invoice_no, remark=remark or f"来源订单 {order.doc_no}",
        term_days=term_days, sales_order=order,
    )
    refresh_order_status(order)
    return inv


def bind_outbound_lines_to_order(order, lines, *, exclude_outbound=None):
    """手工出库挂订单：按商品匹配订单行，校验不超过待发货数量。

    exclude_outbound: 修改本单时排除已计入的本单数量。
    返回带 order_line 的新 lines 列表。
    """
    if order.status != SalesOrder.Status.OPEN:
        raise SalesOrderError("只能关联「执行中」的销售订单")
    # product_id -> list of (order_line, remaining)
    buckets = {}
    for ol in order.lines.select_related("product"):
        remain = qty_open_ship(ol)
        if exclude_outbound is not None:
            already = (SalesOutboundLine.objects
                       .filter(order_line=ol, outbound=exclude_outbound)
                       .exclude(outbound__status=SalesOutbound.Status.VOID)
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
            raise SalesOrderError(
                f"第{i}行商品「{product}」不在订单 {order.doc_no} 的待发货明细中")
        ol, remain = pool[0]
        if qty > remain:
            raise SalesOrderError(
                f"第{i}行发货数量 {qty} 超过订单行待发货 {remain}（{order.doc_no}）")
        pool[0][1] = round_qty(remain - qty)
        if pool[0][1] <= 0:
            pool.pop(0)
        out.append({**ln, "order_line": ol})
    return out


def open_ship_initial_lines(order):
    """供出库单「载入订单待发明细」预填。"""
    rows = []
    for ol in order.lines.select_related("product"):
        remain = qty_open_ship(ol)
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

def _sales_outbound_fully_invoiced(outbound) -> bool:
    from django.db.models import Sum
    from apps.finance.models import SalesInvoice, SalesInvoiceLine
    lines = list(outbound.lines.values("pk", "quantity"))
    if not lines:
        return True
    invoiced = {r["source_outbound_line"]: (r["q"] or ZERO_QTY) for r in
                SalesInvoiceLine.objects.filter(source_outbound_line__outbound=outbound)
                .exclude(invoice__status=SalesInvoice.Status.VOID)
                .values("source_outbound_line").annotate(q=Sum("quantity"))}
    return all(invoiced.get(ln["pk"], ZERO_QTY) >= ln["quantity"] for ln in lines)


def sales_backfill_candidates(company):
    """按客户汇总「未完成且无订单」的出库/发票，供补单工具列表。"""
    from apps.finance.models import SalesInvoice

    outbounds = (SalesOutbound.objects
                 .filter(company=company, sales_order__isnull=True,
                         sales_type=SalesOutbound.SalesType.SALE, is_opening=False)
                 .exclude(status=SalesOutbound.Status.VOID)
                 .select_related("customer")
                 .prefetch_related("lines"))
    incomplete_ob = [o for o in outbounds if not _sales_outbound_fully_invoiced(o)]

    invoices = (SalesInvoice.objects
                .filter(company=company, sales_order__isnull=True, is_opening=False,
                        status=SalesInvoice.Status.REGISTERED)
                .select_related("customer")
                .prefetch_related("lines"))
    incomplete_inv = [i for i in invoices if i.outstanding > 0]

    by_cust = {}
    for o in incomplete_ob:
        if not o.customer_id:
            continue
        d = by_cust.setdefault(o.customer_id, {"customer": o.customer,
                                               "outbounds": [], "invoices": []})
        d["outbounds"].append(o)
    for inv in incomplete_inv:
        d = by_cust.setdefault(inv.customer_id, {"customer": inv.customer,
                                                 "outbounds": [], "invoices": []})
        d["invoices"].append(inv)
    rows = sorted(by_cust.values(), key=lambda r: r["customer"].code)
    for r in rows:
        r["ob_count"] = len(r["outbounds"])
        r["inv_count"] = len(r["invoices"])
    return rows


def sales_order_progress_rows(company):
    """执行中订单进度（待发/待开数量合计）。"""
    rows = []
    qs = (SalesOrder.objects.filter(company=company, status=SalesOrder.Status.OPEN)
          .select_related("customer").prefetch_related("lines"))
    for order in qs:
        open_ship = open_inv = ZERO_QTY
        for ln in order.lines.all():
            open_ship = round_qty(open_ship + qty_open_ship(ln))
            open_inv = round_qty(open_inv + qty_open_invoice(ln))
        rows.append({
            "order": order,
            "qty_open_ship": open_ship,
            "qty_open_invoice": open_inv,
        })
    return rows


@transaction.atomic
def backfill_sales_order(*, company, user, customer, outbound_ids, invoice_ids,
                         doc_date=None, remark="") -> SalesOrder:
    """为未完成出库/发票补建销售订单并回挂（不改金额与库存）。

    订单行按商品合并；数量 = max(已出库合计, 已开票合计)。
    """
    from django.utils import timezone
    from apps.finance.models import SalesInvoice

    if not customer:
        raise SalesOrderError("客户必填")
    outbound_ids = [int(x) for x in outbound_ids]
    invoice_ids = [int(x) for x in invoice_ids]
    if not outbound_ids and not invoice_ids:
        raise SalesOrderError("请至少选择一张出库单或发票")

    outbounds = list(SalesOutbound.objects.filter(
        company=company, pk__in=outbound_ids).prefetch_related("lines__product"))
    invoices = list(SalesInvoice.objects.filter(
        company=company, pk__in=invoice_ids).prefetch_related("lines__product"))
    if len(outbounds) != len(set(outbound_ids)):
        raise SalesOrderError("所选出库单不存在或不属于本账套")
    if len(invoices) != len(set(invoice_ids)):
        raise SalesOrderError("所选发票不存在或不属于本账套")

    for ob in outbounds:
        if ob.sales_order_id:
            raise SalesOrderError(f"出库单 {ob.doc_no} 已挂订单，不能再补")
        if ob.customer_id and ob.customer_id != customer.pk:
            raise SalesOrderError(f"出库单 {ob.doc_no} 客户与所选客户不一致")
        if ob.customer_id is None:
            raise SalesOrderError(f"出库单 {ob.doc_no} 无客户，无法补单")
        if ob.status == SalesOutbound.Status.VOID or ob.is_opening:
            raise SalesOrderError(f"出库单 {ob.doc_no} 状态不可补（已作废/期初）")
        if ob.sales_type != SalesOutbound.SalesType.SALE:
            raise SalesOrderError(f"出库单 {ob.doc_no} 非销售方式，不纳入补单")
    for inv in invoices:
        if inv.sales_order_id:
            raise SalesOrderError(f"发票 {inv.doc_no} 已挂订单，不能再补")
        if inv.customer_id != customer.pk:
            raise SalesOrderError(f"发票 {inv.doc_no} 客户与所选客户不一致")
        if inv.status == SalesInvoice.Status.VOID or inv.is_opening:
            raise SalesOrderError(f"发票 {inv.doc_no} 状态不可补")

    # product_id -> {ship_qty, inv_qty, ship_untaxed, inv_untaxed, rate}
    buckets = {}
    def buck(pid):
        return buckets.setdefault(pid, {
            "product": None, "ship_qty": ZERO_QTY, "inv_qty": ZERO_QTY,
            "ship_untaxed": ZERO_MONEY, "inv_untaxed": ZERO_MONEY,
            "rate": DEFAULT_TAX_RATE,
        })

    for ob in outbounds:
        for ln in ob.lines.all():
            b = buck(ln.product_id)
            b["product"] = ln.product
            b["ship_qty"] = round_qty(b["ship_qty"] + ln.quantity)
            b["ship_untaxed"] = round_money(b["ship_untaxed"] + ln.amount_untaxed)
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
        raise SalesOrderError("所选单据无商品明细，无法生成订单行")

    order_lines = []
    for b in buckets.values():
        qty = max(b["ship_qty"], b["inv_qty"])
        if qty <= 0:
            continue
        if b["ship_qty"] > 0:
            untaxed = round_money(b["ship_untaxed"] / b["ship_qty"] * qty)
        else:
            untaxed = round_money(b["inv_untaxed"] / b["inv_qty"] * qty)
        order_lines.append({
            "product": b["product"], "quantity": qty,
            "amount_untaxed": untaxed, "tax_rate": b["rate"],
        })

    doc_date = doc_date or timezone.localdate()
    order = create_sales_order(
        company=company, user=user, doc_date=doc_date, customer=customer,
        lines=order_lines,
        remark=remark or "补单回挂（未完成业务）",
    )
    # create_sales_order already audited; append note
    prod_map = {ln.product_id: ln for ln in order.lines.all()}

    for ob in outbounds:
        ob.sales_order = order
        ob.save(update_fields=["sales_order"])
        for ln in ob.lines.all():
            ol = prod_map.get(ln.product_id)
            if ol:
                ln.order_line = ol
                ln.save(update_fields=["order_line"])
    for inv in invoices:
        inv.sales_order = order
        inv.save(update_fields=["sales_order"])
        for ln in inv.lines.all():
            ol = prod_map.get(ln.product_id) if ln.product_id else None
            if ol:
                ln.order_line = ol
                ln.save(update_fields=["order_line"])

    refresh_order_status(order)
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.LINK, target=order,
        summary=(f"补单回挂 {order.doc_no}：出库 {len(outbounds)} 张、"
                 f"发票 {len(invoices)} 张（未改变入账金额）"),
    )
    return order
