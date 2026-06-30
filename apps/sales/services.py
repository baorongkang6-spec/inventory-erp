"""销售出库的创建与过账（一个事务内完成）。

库存不足时 post_outbound 抛 InsufficientStockError，事务整体回滚，
单据与已处理行均不落库。
"""

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY, round_money, round_qty
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
    total_qty, total_cost = _apply_outbound_lines(
        doc, user, doc_date, lines, expenses, sales_type, customer, borrow_counterparty)
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=doc,
        summary=f"销售出库 {doc.doc_no} 出 {total_qty} 件 成本 {total_cost}",
    )
    # 关联交易自动联动（M4，SPEC §5）：客户指向系统内关联公司则镜像生成对方采购入库
    _mirror_to_related_company(doc, user)
    return doc


def _apply_outbound_lines(doc, user, doc_date, lines, expenses, sales_type,
                          customer, borrow_counterparty):
    """把明细行过账到已存在的出库单 doc 上（创建/修改共用）。返回 (总数量, 结转成本合计)。"""
    company = doc.company
    total_qty = ZERO_QTY
    total_cost = total_untaxed = total_tax = total_taxed = ZERO_MONEY
    from apps.purchasing.services import _line_amounts
    for ln in lines:
        quantity = round_qty(ln["quantity"])
        rate = ln.get("tax_rate", DEFAULT_TAX_RATE)
        # 兼容旧入参 sale_unit_price（不含税单价）：当含税/金额都没给时按它算
        if (ln.get("amount_untaxed") is None and ln.get("tax_inclusive_price") is None
                and ln.get("sale_unit_price") is not None):
            ln = {**ln, "unit_price": ln.get("sale_unit_price")}
        untaxed, tax, taxed = _line_amounts(quantity, rate, ln)
        sale_price = round_money(untaxed / quantity) if quantity else ZERO_MONEY
        move = post_outbound(
            company, ln["product"], quantity, date=doc_date,
            source_type="SalesOutbound", source_id=doc.pk, source_no=doc.doc_no,
        )
        SalesOutboundLine.objects.create(
            outbound=doc, product=ln["product"], quantity=quantity,
            sale_unit_price=sale_price, tax_rate=rate,
            amount_untaxed=untaxed, tax_amount=tax, amount_taxed=taxed,
            unit_cost=move.unit_price, amount=move.amount, stock_move=move,
        )
        total_qty = round_qty(total_qty + quantity)
        total_cost = round_money(total_cost + move.amount)
        total_untaxed = round_money(total_untaxed + untaxed)
        total_tax = round_money(total_tax + tax)
        total_taxed = round_money(total_taxed + taxed)

    doc.total_quantity = total_qty
    doc.total_cost = total_cost
    doc.total_untaxed = total_untaxed
    doc.total_tax = total_tax
    doc.total_taxed = total_taxed
    doc.save(update_fields=["total_quantity", "total_cost",
                            "total_untaxed", "total_tax", "total_taxed"])

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
    return total_qty, total_cost


@transaction.atomic
def update_and_repost_outbound(doc, *, user, doc_date, lines, customer=None, remark="",
                               expenses=None, sales_type=SalesOutbound.SalesType.SALE,
                               borrow_counterparty=""):
    """修改销售出库单：冲正原过账 → 在同一张单上按新明细重过账（保留单号）。

    有关联镜像的出库单不可改（请作废重录以联动）。调用前应已校验可改性。
    """
    if doc.status == SalesOutbound.Status.VOID:
        raise InventoryError("已作废单据不可修改")
    if doc.mirror_inbound_id:
        raise InventoryError("本单已生成关联镜像入库，请作废重录以联动")

    for line in doc.lines.select_related("stock_move"):
        if line.stock_move_id:
            reverse_move(line.stock_move, date=doc.doc_date, source_type="SalesOutboundEdit",
                         source_id=doc.pk, source_no=f"改前{doc.doc_no}")
    doc.lines.all().delete()
    from apps.finance.models import BorrowTransaction, ExpenseEntry
    ExpenseEntry.objects.filter(company=doc.company, source_type="SalesOutbound",
                                source_id=str(doc.pk)).delete()
    BorrowTransaction.objects.filter(company=doc.company, source_type="SalesOutbound",
                                     source_id=str(doc.pk)).delete()

    doc.doc_date = doc_date
    doc.customer = customer
    doc.sales_type = sales_type
    doc.remark = remark
    doc.save(update_fields=["doc_date", "customer", "sales_type", "remark"])

    total_qty, total_cost = _apply_outbound_lines(
        doc, user, doc_date, lines, expenses, sales_type, customer, borrow_counterparty)
    AuditLog.record(
        actor=user, company=doc.company, action=AuditLog.Action.UPDATE, target=doc,
        summary=f"修改销售出库 {doc.doc_no}（冲正重过账，出 {total_qty} 件 成本 {total_cost}）",
    )
    return doc


def outbound_edit_block_reason(doc, user, today, is_manager=False):
    """返回不可修改原因；可改返回 None。本人+管理员、未被下游引用(发票/镜像)、本月内。"""
    from apps.finance.models import SalesInvoiceLine
    if doc.status == SalesOutbound.Status.VOID:
        return "单据已作废"
    if doc.mirror_inbound_id:
        return "本单已生成关联镜像入库，请作废重录"
    if not (is_manager or doc.created_by_id == getattr(user, "pk", None)):
        return "只有录入人本人或管理员可修改"
    if (doc.doc_date.year, doc.doc_date.month) != (today.year, today.month):
        return "跨月单据不可修改（请作废重录或在当月处理）"
    if SalesInvoiceLine.objects.filter(source_outbound_line__outbound=doc).exists():
        return "已被销售发票引用，不可修改（请先处理发票或作废重录）"
    return None


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


def _ensure_supplier_for_company(company_b, source_company):
    """在 B 账套找/建一个"代表源公司 A"的供应商（按 related_company=A 识别）。

    用于镜像采购入库自动带上供应商=源公司，便于 B 后续记采购发票/应付。
    已有则复用，避免重复建。
    """
    from apps.masterdata.models import Supplier
    sup = Supplier.objects.filter(company=company_b, related_company=source_company).first()
    if sup:
        return sup
    base = f"GL{source_company.code}"   # 关联企业供应商编码前缀
    code, n = base, 1
    while Supplier.objects.filter(company=company_b, code=code).exists():
        n += 1
        code = f"{base}-{n}"
    return Supplier.objects.create(
        company=company_b, code=code, name=source_company.name,
        related_company=source_company, remark="系统自动建立的关联企业供应商（可改名）",
    )


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

    # 销售→对方外购入库；借出/归还→对方借调入库（SPEC §5）
    borrow_kind = outbound.sales_type in (
        SalesOutbound.SalesType.LEND, SalesOutbound.SalesType.RETURN)
    # 计价口径（SPEC §5.1，2026-06-26）：
    #  · 销售：B 按 A 的「不含税售额」入库（B 按售价买进；含税/税额一并镜像），无售价则回退成本；
    #  · 借调：按 A 的移动加权结转成本平移（不涉税、无加价）。
    lines = []
    for ln in outbound.lines.select_related("product"):
        prod = _ensure_product_in(company_b, ln.product)
        if not borrow_kind and ln.amount_untaxed and ln.amount_untaxed > 0:
            lines.append({
                "product": prod, "quantity": ln.quantity, "tax_rate": ln.tax_rate,
                "amount_untaxed": ln.amount_untaxed, "tax_amount": ln.tax_amount,
                "amount_taxed": ln.amount_taxed,
            })
        else:
            lines.append({"product": prod, "quantity": ln.quantity, "unit_price": ln.unit_cost})
    # 外购镜像带上"代表源公司"的供应商；借调类走 borrow_counterparty，不设供应商
    supplier = None if borrow_kind else _ensure_supplier_for_company(company_b, outbound.company)
    from apps.purchasing.models import PurchaseInbound
    inbound = create_and_post_inbound(
        company=company_b, user=user, doc_date=outbound.doc_date, lines=lines,
        supplier=supplier,
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


def outbound_delete_block_reason(doc, user, today, is_manager=False):
    """返回不可硬删除的原因；可删返回 None。

    硬删=彻底移除单据与流水。为不破坏移动加权成本，仅当「该出库后相关商品再无任何
    出入库变动」时允许；另需：非作废、未生成关联镜像、未开票、当月、本人或管理员。
    其余情况请改用「作废」（反冲库存、留痕；有镜像则联动作废对方）。
    """
    from apps.finance.models import SalesInvoiceLine
    from apps.inventory.models import StockMove
    if doc.status == SalesOutbound.Status.VOID:
        return "已作废的出库单无需再删除"
    if doc.mirror_inbound_id:
        return "本单已生成关联镜像入库，请改用作废以联动对方"
    if not (is_manager or doc.created_by_id == getattr(user, "pk", None)):
        return "只有录入人本人或管理员可删除"
    if (doc.doc_date.year, doc.doc_date.month) != (today.year, today.month):
        return "跨月单据不可删除，请改用作废"
    if SalesInvoiceLine.objects.filter(source_outbound_line__outbound=doc).exists():
        return "已被销售发票引用，请先删除销售发票"
    for line in doc.lines.select_related("stock_move"):
        mv = line.stock_move
        if mv and StockMove.objects.filter(
                company=doc.company, product_id=mv.product_id, id__gt=mv.id).exists():
            return "该出库后相关商品已有其它出入库变动，硬删会影响成本核算，请改用作废"
    return None


@transaction.atomic
def delete_sales_outbound(doc, *, user, today, is_manager=False):
    """硬删除销售出库单（安全条件下）：精确反冲库存，彻底移除单据与其库存流水。"""
    reason = outbound_delete_block_reason(doc, user, today, is_manager)
    if reason:
        raise InventoryError(reason)
    for line in doc.lines.select_related("stock_move"):
        mv = line.stock_move
        if mv:
            # 借用 reverse_move 精确回退结存（该商品最后一笔，安全），再删掉两条流水不留痕
            rev = reverse_move(mv, date=doc.doc_date, source_type="SalesOutboundDelete",
                               source_id=doc.pk, source_no=f"删除{doc.doc_no}")
            line.stock_move = None
            line.save(update_fields=["stock_move"])
            rev.delete()
            mv.delete()
    from apps.finance.models import BorrowTransaction, ExpenseEntry
    ExpenseEntry.objects.filter(company=doc.company, source_type="SalesOutbound",
                                source_id=str(doc.pk)).delete()
    BorrowTransaction.objects.filter(
        company=doc.company, source_type="SalesOutbound", source_id=str(doc.pk)).delete()
    doc_no = doc.doc_no
    AuditLog.record(actor=user, company=doc.company, action=AuditLog.Action.DELETE, target=doc,
                    summary=f"删除销售出库 {doc_no}（彻底移除并反冲库存）")
    doc.delete()
