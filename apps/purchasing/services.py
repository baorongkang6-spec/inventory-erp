"""采购入库的创建与过账（一个事务内完成）。"""

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import ZERO_MONEY, ZERO_QTY, round_money, round_qty
from apps.inventory.services import InventoryError, post_inbound, reverse_move

from .models import PurchaseInbound, PurchaseInboundLine


@transaction.atomic
def create_and_post_inbound(*, company, user, doc_date, lines,
                            supplier=None, remark="", expenses=None) -> PurchaseInbound:
    """创建采购入库单并逐行过账增加库存。

    lines: [{"product": Product, "quantity": Decimal, "unit_price": Decimal}, ...]
    expenses: [{"category": ExpenseCategory, "amount": Decimal}, ...]（其他费用，SPEC §6.2）
      计入成本的费用按各行基础金额比例分摊抬高入库成本（影响移动加权），余数归最后一行；
      不计入成本的作期间费用记录。
    整个过程在事务内：任一行异常则全部回滚。
    """
    doc = PurchaseInbound.objects.create(
        company=company,
        created_by=user,
        doc_no=next_doc_no(PurchaseInbound, company, "RK", doc_date),
        doc_date=doc_date,
        supplier=supplier,
        remark=remark,
    )

    norm = [{"product": ln["product"], "quantity": round_qty(ln["quantity"]),
             "unit_price": round_money(ln["unit_price"])} for ln in lines]
    base = [round_money(x["quantity"] * x["unit_price"]) for x in norm]
    base_total = sum(base, ZERO_MONEY)

    # 计入成本的费用合计 → 按行基础金额比例分摊
    cost_fee = ZERO_MONEY
    for e in (expenses or []):
        if e["category"].include_in_cost:
            cost_fee += round_money(e["amount"])
    alloc = [ZERO_MONEY] * len(norm)
    if cost_fee and base_total > 0:
        running = ZERO_MONEY
        for i in range(len(norm)):
            if i < len(norm) - 1:
                a = round_money(cost_fee * base[i] / base_total)
                alloc[i] = a
                running += a
            else:
                alloc[i] = round_money(cost_fee - running)  # 余数归最后一行

    total_qty = ZERO_QTY
    total_amount = ZERO_MONEY
    for x, b, fee in zip(norm, base, alloc):
        line_amount = round_money(b + fee)
        move = post_inbound(
            company, x["product"], x["quantity"], x["unit_price"], amount=line_amount,
            source_type="PurchaseInbound", source_id=doc.pk, source_no=doc.doc_no,
        )
        PurchaseInboundLine.objects.create(
            inbound=doc, product=x["product"], quantity=x["quantity"],
            unit_price=move.unit_price, amount=move.amount, stock_move=move,
        )
        total_qty = round_qty(total_qty + x["quantity"])
        total_amount = round_money(total_amount + move.amount)

    # 记录其他费用（含计入成本与期间费用）
    _record_expenses(company, user, doc, doc_date, expenses, kind="purchase")

    doc.total_quantity = total_qty
    doc.total_amount = total_amount
    doc.save(update_fields=["total_quantity", "total_amount"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=doc,
        summary=f"采购入库 {doc.doc_no} 入 {total_qty} 件 金额 {total_amount}",
    )
    return doc


def _record_expenses(company, user, doc, doc_date, expenses, kind):
    """登记其他费用记录（ExpenseEntry）。"""
    from apps.finance.models import ExpenseEntry
    for e in (expenses or []):
        amount = round_money(e["amount"])
        if amount <= 0:
            continue
        ExpenseEntry.objects.create(
            company=company, created_by=user, date=doc_date, kind=kind,
            category=e["category"], amount=amount,
            included_in_cost=bool(e["category"].include_in_cost and kind == "purchase"),
            source_no=doc.doc_no, source_type=doc.__class__.__name__, source_id=str(doc.pk),
        )


@transaction.atomic
def void_purchase_inbound(doc, user=None, *, _from_source=False):
    """作废采购入库单：反冲库存（数量金额、移动加权重算）。

    若货已被后续出库消耗、反冲会导致负库存，则 reverse_move 抛错、整单不作废。
    若本单是关联出库自动生成的镜像（有 source_outbound），需从源出库单作废以联动，
    不允许单独作废（除非内部联动调用 _from_source=True）。
    """
    if doc.status == PurchaseInbound.Status.VOID:
        raise InventoryError("该入库单已作废")
    if doc.source_outbound_id and not _from_source:
        raise InventoryError("本入库由关联销售出库自动生成，请作废对应的源销售出库单以联动作废")

    for line in doc.lines.select_related("stock_move"):
        if line.stock_move_id:
            reverse_move(line.stock_move, source_type="PurchaseInboundVoid",
                         source_id=doc.pk, source_no=f"作废{doc.doc_no}")
    doc.status = PurchaseInbound.Status.VOID
    doc.save(update_fields=["status"])
    AuditLog.record(actor=user, company=doc.company, action=AuditLog.Action.VOID, target=doc,
                    summary=f"作废采购入库 {doc.doc_no}（反冲库存）")
    return doc
