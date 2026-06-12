"""采购入库的创建与过账（一个事务内完成）。"""

from decimal import Decimal

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY, round_money, round_qty
from apps.inventory.services import InventoryError, post_inbound, reverse_move

from .models import PurchaseInbound, PurchaseInboundLine


@transaction.atomic
def create_and_post_inbound(*, company, user, doc_date, lines,
                            supplier=None, remark="", expenses=None,
                            purchase_type=PurchaseInbound.PurchaseType.EXTERNAL,
                            borrow_counterparty="") -> PurchaseInbound:
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
        purchase_type=purchase_type,
        remark=remark,
    )
    total_qty, total_amount = _apply_inbound_lines(
        doc, user, doc_date, lines, expenses, purchase_type, supplier, borrow_counterparty)
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=doc,
        summary=f"采购入库 {doc.doc_no} 入 {total_qty} 件 金额 {total_amount}",
    )
    return doc


def _line_amounts(qty, rate, ln):
    """求一行的 (不含税金额, 税额, 含税金额)。优先级：
    显式金额(amount_untaxed) > 含税单价(tax_inclusive_price) > 不含税单价(unit_price，镜像用)。
    任何缺失的项按税率换算补齐；含税单价路径下 不含税=含税/(1+税率)。"""
    one = Decimal(1)
    if ln.get("amount_untaxed") is not None:
        untaxed = round_money(ln["amount_untaxed"])
        tax = round_money(ln["tax_amount"]) if ln.get("tax_amount") is not None \
            else round_money(untaxed * rate)
        taxed = round_money(ln["amount_taxed"]) if ln.get("amount_taxed") is not None \
            else round_money(untaxed + tax)
    elif ln.get("tax_inclusive_price") is not None:
        taxed = round_money(qty * round_money(ln["tax_inclusive_price"]))
        untaxed = round_money(taxed / (one + rate)) if (one + rate) else taxed
        tax = round_money(taxed - untaxed)
    else:
        up = round_money(ln.get("unit_price") or ZERO_MONEY)
        untaxed = round_money(qty * up)
        tax = round_money(untaxed * rate)
        taxed = round_money(untaxed + tax)
    return untaxed, tax, taxed


def _apply_inbound_lines(doc, user, doc_date, lines, expenses, purchase_type,
                         supplier, borrow_counterparty):
    """把明细行过账到已存在的入库单 doc 上（创建/修改共用）。返回 (总数量, 入库成本合计)。"""
    company = doc.company
    norm = []
    for ln in lines:
        qty = round_qty(ln["quantity"])
        rate = ln.get("tax_rate", DEFAULT_TAX_RATE)
        untaxed, tax, taxed = _line_amounts(qty, rate, ln)
        norm.append({"product": ln["product"], "quantity": qty, "tax_rate": rate,
                     "untaxed": untaxed, "tax": tax, "taxed": taxed})
    base = [x["untaxed"] for x in norm]
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
    total_amount = total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for x, b, fee in zip(norm, base, alloc):
        line_amount = round_money(b + fee)  # 入库成本（不含税 + 计入成本的费用分摊）
        move = post_inbound(
            company, x["product"], x["quantity"], ZERO_MONEY, amount=line_amount,
            date=doc_date,
            source_type="PurchaseInbound", source_id=doc.pk, source_no=doc.doc_no,
        )
        PurchaseInboundLine.objects.create(
            inbound=doc, product=x["product"], quantity=x["quantity"],
            unit_price=move.unit_price, tax_rate=x["tax_rate"],
            amount_untaxed=b, tax_amount=x["tax"], amount_taxed=x["taxed"],
            amount=move.amount, stock_move=move,
        )
        total_qty = round_qty(total_qty + x["quantity"])
        total_amount = round_money(total_amount + move.amount)
        total_untaxed = round_money(total_untaxed + b)
        total_tax = round_money(total_tax + x["tax"])
        total_taxed = round_money(total_taxed + x["taxed"])

    # 记录其他费用（含计入成本与期间费用）
    _record_expenses(company, user, doc, doc_date, expenses, kind="purchase")

    # 借调入库：挂借调往来（类其他应付，不涉税，SPEC §4.1）
    if purchase_type == PurchaseInbound.PurchaseType.BORROW:
        from apps.finance.models import BorrowTransaction
        BorrowTransaction.objects.create(
            company=company, created_by=user,
            counterparty=borrow_counterparty or (str(supplier) if supplier else ""),
            direction=BorrowTransaction.Direction.IN, amount=total_amount, date=doc_date,
            source_type="PurchaseInbound", source_id=str(doc.pk), source_no=doc.doc_no,
        )

    doc.total_quantity = total_qty
    doc.total_amount = total_amount
    doc.total_untaxed = total_untaxed
    doc.total_tax = total_tax
    doc.total_taxed = total_taxed
    doc.save(update_fields=["total_quantity", "total_amount",
                            "total_untaxed", "total_tax", "total_taxed"])
    return total_qty, total_amount


@transaction.atomic
def update_and_repost_inbound(doc, *, user, doc_date, lines, supplier=None, remark="",
                              expenses=None, purchase_type=PurchaseInbound.PurchaseType.EXTERNAL,
                              borrow_counterparty=""):
    """修改采购入库单：冲正原过账 → 按新明细在同一张单上重新过账（保留单号）。

    调用前应已校验可改性（见 inbound_edit_block_reason）。镜像生成单不可改。
    """
    if doc.status == PurchaseInbound.Status.VOID:
        raise InventoryError("已作废单据不可修改")
    if doc.source_outbound_id:
        raise InventoryError("关联镜像生成的入库单不可直接修改，请改源销售出库单")

    # 冲正原过账
    for line in doc.lines.select_related("stock_move"):
        if line.stock_move_id:
            reverse_move(line.stock_move, date=doc.doc_date, source_type="PurchaseInboundEdit",
                         source_id=doc.pk, source_no=f"改前{doc.doc_no}")
    doc.lines.all().delete()
    from apps.finance.models import BorrowTransaction, ExpenseEntry
    ExpenseEntry.objects.filter(company=doc.company, source_type="PurchaseInbound",
                                source_id=str(doc.pk)).delete()
    BorrowTransaction.objects.filter(company=doc.company, source_type="PurchaseInbound",
                                     source_id=str(doc.pk)).delete()

    doc.doc_date = doc_date
    doc.supplier = supplier
    doc.purchase_type = purchase_type
    doc.remark = remark
    doc.save(update_fields=["doc_date", "supplier", "purchase_type", "remark"])

    total_qty, total_amount = _apply_inbound_lines(
        doc, user, doc_date, lines, expenses, purchase_type, supplier, borrow_counterparty)
    AuditLog.record(
        actor=user, company=doc.company, action=AuditLog.Action.UPDATE, target=doc,
        summary=f"修改采购入库 {doc.doc_no}（冲正重过账，入 {total_qty} 件 金额 {total_amount}）",
    )
    return doc


def inbound_edit_block_reason(doc, user, today, is_manager=False):
    """返回不可修改的原因字符串；可改则返回 None。规则：本人+管理员、未被下游引用、本月内。"""
    from apps.finance.models import PurchaseInvoiceLine
    if doc.status == PurchaseInbound.Status.VOID:
        return "单据已作废"
    if doc.source_outbound_id:
        return "本单由关联销售出库自动生成，请修改源出库单"
    if not (is_manager or doc.created_by_id == getattr(user, "pk", None)):
        return "只有录入人本人或管理员可修改"
    if (doc.doc_date.year, doc.doc_date.month) != (today.year, today.month):
        return "跨月单据不可修改（请作废重录或在当月处理）"
    if PurchaseInvoiceLine.objects.filter(source_inbound_line__inbound=doc).exists():
        return "已被采购发票引用，不可修改（请先处理发票或作废重录）"
    # 允许负库存：不再因"已被后续出库消耗"阻止修改（反冲可令结存为负）
    return None


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
    # 借调入库作废：撤销借调往来
    from apps.finance.models import BorrowTransaction
    BorrowTransaction.objects.filter(
        company=doc.company, source_type="PurchaseInbound", source_id=str(doc.pk)).delete()
    doc.status = PurchaseInbound.Status.VOID
    doc.save(update_fields=["status"])
    AuditLog.record(actor=user, company=doc.company, action=AuditLog.Action.VOID, target=doc,
                    summary=f"作废采购入库 {doc.doc_no}（反冲库存）")
    return doc


def inbound_delete_block_reason(doc, user, today, is_manager=False):
    """返回不可硬删除的原因；可删返回 None。

    硬删=彻底移除单据与流水。为不破坏移动加权成本，仅当「该入库后相关商品再无任何
    出入库变动」时允许；另需：非作废、非镜像、未开票、当月、本人或管理员。
    其余情况请改用「作废」（反冲库存、留痕）。
    """
    from apps.finance.models import PurchaseInvoiceLine
    from apps.inventory.models import StockMove
    if doc.status == PurchaseInbound.Status.VOID:
        return "已作废的入库单无需再删除"
    if doc.source_outbound_id:
        return "本单由关联销售出库自动生成，请作废源销售出库单"
    if not (is_manager or doc.created_by_id == getattr(user, "pk", None)):
        return "只有录入人本人或管理员可删除"
    if (doc.doc_date.year, doc.doc_date.month) != (today.year, today.month):
        return "跨月单据不可删除，请改用作废"
    if PurchaseInvoiceLine.objects.filter(source_inbound_line__inbound=doc).exists():
        return "已被采购发票引用，请先删除采购发票"
    for line in doc.lines.select_related("stock_move"):
        mv = line.stock_move
        if mv and StockMove.objects.filter(
                company=doc.company, product_id=mv.product_id, id__gt=mv.id).exists():
            return "该入库后相关商品已有其它出入库变动，硬删会影响成本核算，请改用作废"
    return None


@transaction.atomic
def delete_purchase_inbound(doc, *, user, today, is_manager=False):
    """硬删除采购入库单（安全条件下）：精确反冲库存，彻底移除单据与其库存流水。"""
    reason = inbound_delete_block_reason(doc, user, today, is_manager)
    if reason:
        raise InventoryError(reason)
    for line in doc.lines.select_related("stock_move"):
        mv = line.stock_move
        if mv:
            # 借用 reverse_move 精确回退结存（该商品最后一笔，安全），再删掉两条流水不留痕
            rev = reverse_move(mv, date=doc.doc_date, source_type="PurchaseInboundDelete",
                               source_id=doc.pk, source_no=f"删除{doc.doc_no}")
            line.stock_move = None
            line.save(update_fields=["stock_move"])
            rev.delete()
            mv.delete()
    from apps.finance.models import BorrowTransaction
    BorrowTransaction.objects.filter(
        company=doc.company, source_type="PurchaseInbound", source_id=str(doc.pk)).delete()
    doc_no = doc.doc_no
    AuditLog.record(actor=user, company=doc.company, action=AuditLog.Action.DELETE, target=doc,
                    summary=f"删除采购入库 {doc_no}（彻底移除并反冲库存）")
    doc.delete()
