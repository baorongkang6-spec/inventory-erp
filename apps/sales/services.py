"""销售出库的创建与过账（一个事务内完成）。

库存不足时 post_outbound 抛 InsufficientStockError，事务整体回滚，
单据与已处理行均不落库。
"""

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import ZERO_MONEY, ZERO_QTY, round_money, round_qty
from apps.inventory.services import post_outbound

from .models import SalesOutbound, SalesOutboundLine


@transaction.atomic
def create_and_post_outbound(*, company, user, doc_date, lines,
                             customer=None, remark="") -> SalesOutbound:
    """创建销售出库单并逐行过账减少库存（结转移动加权成本）。

    lines: [{"product": Product, "quantity": Decimal}, ...]
    """
    doc = SalesOutbound.objects.create(
        company=company,
        created_by=user,
        doc_no=next_doc_no(SalesOutbound, company, "CK", doc_date),
        doc_date=doc_date,
        customer=customer,
        remark=remark,
    )

    total_qty = ZERO_QTY
    total_cost = ZERO_MONEY
    for ln in lines:
        quantity = round_qty(ln["quantity"])
        move = post_outbound(
            company, ln["product"], quantity,
            source_type="SalesOutbound", source_id=doc.pk, source_no=doc.doc_no,
        )
        SalesOutboundLine.objects.create(
            outbound=doc, product=ln["product"], quantity=quantity,
            unit_cost=move.unit_price, amount=move.amount, stock_move=move,
        )
        total_qty = round_qty(total_qty + quantity)
        total_cost = round_money(total_cost + move.amount)

    doc.total_quantity = total_qty
    doc.total_cost = total_cost
    doc.save(update_fields=["total_quantity", "total_cost"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=doc,
        summary=f"销售出库 {doc.doc_no} 出 {total_qty} 件 成本 {total_cost}",
    )
    return doc
