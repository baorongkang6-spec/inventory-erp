"""销售出库的创建与过账（一个事务内完成）。

库存不足时 post_outbound 抛 InsufficientStockError，事务整体回滚，
单据与已处理行均不落库。
"""

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import ZERO_MONEY, ZERO_QTY, round_money, round_qty
from apps.inventory.services import InventoryError, post_outbound, reverse_move

from .models import SalesOutbound, SalesOutboundLine


@transaction.atomic
def create_and_post_outbound(*, company, user, doc_date, lines,
                             customer=None, remark="", expenses=None,
                             sales_type=SalesOutbound.SalesType.SALE,
                             borrow_counterparty="") -> SalesOutbound:
    """创建销售出库单并逐行过账减少库存（结转移动加权成本）。

    lines: [{"product": Product, "quantity": Decimal}, ...]
    expenses: 其他费用（销售出库的费用一律作期间费用，不改库存成本，SPEC §6.2）。
    """
    doc = SalesOutbound.objects.create(
        company=company,
        created_by=user,
        doc_no=next_doc_no(SalesOutbound, company, "CK", doc_date),
        doc_date=doc_date,
        customer=customer,
        sales_type=sales_type,
        remark=remark,
    )

    total_qty = ZERO_QTY
    total_cost = ZERO_MONEY
    for ln in lines:
        quantity = round_qty(ln["quantity"])
        move = post_outbound(
            company, ln["product"], quantity, date=doc_date,
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

    # 其他费用（期间费用，不计入存货成本）
    from apps.purchasing.services import _record_expenses
    _record_expenses(company, user, doc, doc_date, expenses, kind="sales")

    # 借调类出库（借出/归还）：在出库方记一笔借调往来（OUT，SPEC §4.1）
    if sales_type in (SalesOutbound.SalesType.RETURN, SalesOutbound.SalesType.LEND):
        from apps.finance.models import BorrowTransaction
        BorrowTransaction.objects.create(
            company=company, created_by=user,
            counterparty=borrow_counterparty or (str(customer) if customer else ""),
            direction=BorrowTransaction.Direction.OUT, amount=total_cost, date=doc_date,
            source_type="SalesOutbound", source_id=str(doc.pk), source_no=doc.doc_no,
        )

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=doc,
        summary=f"销售出库 {doc.doc_no} 出 {total_qty} 件 成本 {total_cost}",
    )

    # 关联交易自动联动（M4，SPEC §5）：客户指向系统内关联公司则镜像生成对方采购入库
    _mirror_to_related_company(doc, user)
    return doc


def _ensure_product_in(company_b, source_product):
    """在 B 账套按编码取/建对应商品（关联镜像需要 B 有同编码商品）。"""
    from apps.masterdata.models import Product
    prod, _ = Product.objects.get_or_create(
        company=company_b, code=source_product.code,
        defaults={
            "name": source_product.name, "spec": source_product.spec,
            "unit": source_product.unit, "category": source_product.category,
            "default_tax_rate": source_product.default_tax_rate,
        },
    )
    return prod


def _mirror_to_related_company(outbound, user):
    """若出库单客户对应系统内关联公司 B，则在 B 自动生成镜像采购入库单（外购）。

    照搬数量与移动加权结转成本（作为 B 的入库成本单价）；商品在 B 按编码自动配齐。
    在同一事务内完成：镜像失败则整笔出库回滚（完全自动、原子）。
    """
    from apps.purchasing.services import create_and_post_inbound

    customer = outbound.customer
    company_b = getattr(customer, "related_company", None) if customer else None
    if company_b is None or not company_b.is_active:
        return

    lines = [
        {
            "product": _ensure_product_in(company_b, ln.product),
            "quantity": ln.quantity,
            "unit_price": ln.unit_cost,   # B 以 A 的结转成本入库（关联调拨按成本平移）
        }
        for ln in outbound.lines.select_related("product")
    ]
    # 销售→对方外购入库；借出/归还→对方借调入库（SPEC §5）
    borrow_kind = outbound.sales_type in (
        SalesOutbound.SalesType.LEND, SalesOutbound.SalesType.RETURN)
    from apps.purchasing.models import PurchaseInbound
    inbound = create_and_post_inbound(
        company=company_b, user=user, doc_date=outbound.doc_date, lines=lines,
        remark=f"关联自动生成：源 {outbound.company.code} {outbound.doc_no}",
        purchase_type=(PurchaseInbound.PurchaseType.BORROW if borrow_kind
                       else PurchaseInbound.PurchaseType.EXTERNAL),
        borrow_counterparty=str(outbound.company) if borrow_kind else "",
    )
    inbound.source_outbound = outbound
    inbound.save(update_fields=["source_outbound"])
    outbound.mirror_inbound = inbound
    outbound.save(update_fields=["mirror_inbound"])

    AuditLog.record(
        actor=user, company=company_b, action=AuditLog.Action.LINK, target=inbound,
        summary=f"关联联动：由 {outbound.company.code} 出库 {outbound.doc_no} 自动生成入库 {inbound.doc_no}",
    )


@transaction.atomic
def void_sales_outbound(doc, user=None):
    """作废销售出库单：先联动作废镜像采购入库（若有），再反冲本公司库存。

    顺序：先作废对方镜像入库（反冲 B 库存，B 货已消耗则报错、整笔不作废），
    再把本单数量与成本加回 A 库存。整体事务，任一步失败全部回滚。
    """
    from apps.purchasing.services import void_purchase_inbound

    if doc.status == SalesOutbound.Status.VOID:
        raise InventoryError("该出库单已作废")

    mirror = doc.mirror_inbound
    if mirror is not None and mirror.status != mirror.Status.VOID:
        void_purchase_inbound(mirror, user, _from_source=True)

    for line in doc.lines.select_related("stock_move"):
        if line.stock_move_id:
            reverse_move(line.stock_move, source_type="SalesOutboundVoid",
                         source_id=doc.pk, source_no=f"作废{doc.doc_no}")
    # 归还出库作废：撤销借调往来
    from apps.finance.models import BorrowTransaction
    BorrowTransaction.objects.filter(
        company=doc.company, source_type="SalesOutbound", source_id=str(doc.pk)).delete()
    doc.status = SalesOutbound.Status.VOID
    doc.save(update_fields=["status"])
    AuditLog.record(actor=user, company=doc.company, action=AuditLog.Action.VOID, target=doc,
                    summary=f"作废销售出库 {doc.doc_no}（反冲库存"
                            + ("，并联动作废镜像入库）" if mirror else "）"))
    return doc
