"""资金往来服务：采购发票登记（→应付账款）等。"""

from django.db import transaction
from django.db.models import Sum

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import ZERO_MONEY, ZERO_QTY, round_money

from .models import (
    BankJournal,
    BankReconcileBatch,
    NotePayable,
    NoteReceivable,
    NoteSettlement,
    Payment,
    PaymentAllocation,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    Receipt,
    ReceiptAllocation,
    SalesInvoice,
    SalesInvoiceLine,
)


class SettlementError(Exception):
    """核销业务错误（超额等）。"""


def compute_tax(amount_untaxed, tax_rate):
    """按不含税金额与税率算 (税额, 含税金额)，均四舍五入到 2 位。"""
    untaxed = round_money(amount_untaxed)
    tax = round_money(untaxed * tax_rate)
    return tax, round_money(untaxed + tax)


def _resolve_tax(untaxed, rate, ln):
    """优先用录入的税额/含税金额(允许尾差手工微调)，否则按税率自动算。"""
    tax = ln.get("tax_amount")
    taxed = ln.get("amount_taxed")
    if tax is not None and taxed is not None:
        return round_money(tax), round_money(taxed)
    return compute_tax(untaxed, rate)


@transaction.atomic
def create_purchase_invoice(*, company, user, doc_date, supplier, lines,
                            invoice_no="", remark="", term_days=0) -> PurchaseInvoice:
    """登记采购发票并产生应付账款（发票即应付单据）。

    lines: [{"product": Product|None, "description": str,
             "amount_untaxed": Decimal, "tax_rate": Decimal,
             "source_inbound_line": PurchaseInboundLine|None}, ...]
    """
    inv = PurchaseInvoice.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(PurchaseInvoice, company, "CGF", doc_date),
        invoice_no=invoice_no, doc_date=doc_date, term_days=term_days or 0,
        supplier=supplier, remark=remark,
    )

    total_untaxed = ZERO_MONEY
    total_tax = ZERO_MONEY
    total_taxed = ZERO_MONEY
    for ln in lines:
        untaxed = round_money(ln["amount_untaxed"])
        rate = ln["tax_rate"]
        tax, taxed = _resolve_tax(untaxed, rate, ln)
        PurchaseInvoiceLine.objects.create(
            invoice=inv, product=ln.get("product"), description=ln.get("description", ""),
            quantity=ln.get("quantity") or ZERO_QTY,
            amount_untaxed=untaxed, tax_rate=rate, tax_amount=tax, amount_taxed=taxed,
            source_inbound_line=ln.get("source_inbound_line"),
        )
        total_untaxed += untaxed
        total_tax += tax
        total_taxed += taxed

    inv.amount_untaxed = total_untaxed
    inv.tax_amount = total_tax
    inv.amount_taxed = total_taxed
    inv.save(update_fields=["amount_untaxed", "tax_amount", "amount_taxed"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=inv,
        summary=f"采购发票 {inv.doc_no} 供应商 {supplier} 含税 {total_taxed}（应付）",
    )
    return inv


@transaction.atomic
def create_payment(*, company, user, doc_date, bank_account, supplier, amount, summary="") -> Payment:
    """付款登记：保存付款单并自动生成一条银行存款日记账（支出）。SPEC §7.1。"""
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("付款金额必须大于 0")

    pay = Payment.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(Payment, company, "FK", doc_date),
        doc_date=doc_date, bank_account=bank_account, supplier=supplier,
        amount=amount, summary=summary,
    )
    journal = BankJournal.objects.create(
        company=company, created_by=user, bank_account=bank_account, date=doc_date,
        direction=BankJournal.Direction.OUT, amount=amount,
        # 选了供应商 = 往来结算（可核销）；未选 = 其他付款
        entry_type=(BankJournal.EntryType.SETTLEMENT if supplier
                    else BankJournal.EntryType.OTHER),
        counterparty=str(supplier) if supplier else "",
        summary=summary or f"付款 {pay.doc_no}",
        source_type="Payment", source_id=str(pay.pk), source_no=pay.doc_no,
    )
    pay.bank_journal = journal
    pay.save(update_fields=["bank_journal"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=pay,
        summary=f"付款 {pay.doc_no} 付 {supplier or '其他'} {amount}（{bank_account}）",
    )
    return pay


@transaction.atomic
def allocate_payment(*, payment, allocations, user=None):
    """把付款核销到若干采购发票。SPEC §7.1。

    allocations: [{"invoice": PurchaseInvoice, "amount": Decimal}, ...]
    校验：金额>0、不超过付款未核销额、不超过各发票未核销额。任一违反整体回滚。
    """
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    total = ZERO_MONEY
    cleaned = []
    for a in allocations:
        amount = round_money(a["amount"])
        if amount <= 0:
            continue
        invoice = PurchaseInvoice.objects.select_for_update().get(pk=a["invoice"].pk)
        if invoice.company_id != payment.company_id or invoice.supplier_id != payment.supplier_id:
            raise SettlementError("发票与付款的公司/供应商不一致")
        # 允许核销超过发票未核销额（使发票余额/应付为负，对应预付/多付）
        total += amount
        cleaned.append((invoice, amount))

    if not cleaned:
        raise SettlementError("请填写有效的核销金额")
    if total > payment.unallocated:
        raise SettlementError(f"核销合计 {total} 超过付款未核销 {payment.unallocated}")

    for invoice, amount in cleaned:
        PaymentAllocation.objects.create(payment=payment, invoice=invoice, amount=amount)
        invoice.settled_amount += amount
        invoice.save(update_fields=["settled_amount"])

    payment.settled_amount += total
    payment.save(update_fields=["settled_amount"])

    AuditLog.record(
        actor=user, company=payment.company, action=AuditLog.Action.OFFSET, target=payment,
        summary=f"应付核销 {payment.doc_no} 核销 {total}（{len(cleaned)} 张发票）",
    )
    return payment


@transaction.atomic
def update_sales_invoice(inv, *, user, doc_date, customer, lines, invoice_no="", remark="", term_days=0):
    """修改销售发票（保留单号）：未核销才可改；替换明细并重算应收。"""
    if inv.is_opening:
        raise SettlementError("期初发票不可在此修改")
    if inv.settled_amount > 0:
        raise SettlementError("已核销（或部分核销）的发票不可修改，请先撤销核销")
    inv.lines.all().delete()
    inv.doc_date = doc_date
    inv.customer = customer
    inv.invoice_no = invoice_no
    inv.term_days = term_days or 0
    inv.remark = remark
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for ln in lines:
        untaxed = round_money(ln["amount_untaxed"])
        rate = ln["tax_rate"]
        tax, taxed = _resolve_tax(untaxed, rate, ln)
        SalesInvoiceLine.objects.create(
            invoice=inv, product=ln.get("product"), description=ln.get("description", ""),
            quantity=ln.get("quantity") or ZERO_QTY,
            amount_untaxed=untaxed, tax_rate=rate, tax_amount=tax, amount_taxed=taxed,
            source_outbound_line=ln.get("source_outbound_line"))
        total_untaxed += untaxed
        total_tax += tax
        total_taxed += taxed
    inv.amount_untaxed = total_untaxed
    inv.tax_amount = total_tax
    inv.amount_taxed = total_taxed
    inv.save(update_fields=["doc_date", "customer", "invoice_no", "term_days", "remark",
                            "amount_untaxed", "tax_amount", "amount_taxed"])
    AuditLog.record(actor=user, company=inv.company, action=AuditLog.Action.UPDATE, target=inv,
                    summary=f"修改销售发票 {inv.doc_no} 含税 {total_taxed}")
    return inv


@transaction.atomic
def void_sales_invoice(inv, user=None):
    """作废销售发票：未核销才可作废；作废后从应收剔除（报表按已开具过滤）。"""
    if inv.status == SalesInvoice.Status.VOID:
        raise SettlementError("该发票已作废")
    if inv.settled_amount > 0:
        raise SettlementError("已核销（或被票据抵冲）的发票不可作废，请先撤销核销")
    inv.status = SalesInvoice.Status.VOID
    inv.save(update_fields=["status"])
    AuditLog.record(actor=user, company=inv.company, action=AuditLog.Action.VOID, target=inv,
                    summary=f"作废销售发票 {inv.doc_no}（撤销应收 {inv.amount_taxed}）")
    return inv


@transaction.atomic
def void_purchase_invoice_doc(inv, user=None):
    """作废采购发票：未核销才可作废；作废后从应付剔除。"""
    if inv.status == PurchaseInvoice.Status.VOID:
        raise SettlementError("该发票已作废")
    if inv.settled_amount > 0:
        raise SettlementError("已核销（或被票据抵冲）的发票不可作废，请先撤销核销")
    inv.status = PurchaseInvoice.Status.VOID
    inv.save(update_fields=["status"])
    AuditLog.record(actor=user, company=inv.company, action=AuditLog.Action.VOID, target=inv,
                    summary=f"作废采购发票 {inv.doc_no}（撤销应付 {inv.amount_taxed}）")
    return inv


def invoice_delete_block_reason(inv):
    """发票可否删除（彻底移除记录）：未核销、非期初。可删返回 None。"""
    if inv.settled_amount > 0:
        return "已核销（或部分核销）不可删除，请先撤销核销"
    if inv.is_opening:
        return "期初发票请到期初导入解锁后处理，不能在此删除"
    return None


@transaction.atomic
def delete_purchase_invoice(inv, *, user):
    """删除采购发票（彻底移除）：未核销、非期初才可删；连同明细一并删除。"""
    reason = invoice_delete_block_reason(inv)
    if reason:
        raise SettlementError(reason)
    company, doc_no, taxed = inv.company, inv.doc_no, inv.amount_taxed
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.DELETE, target=inv,
                    summary=f"删除采购发票 {doc_no}（撤销应付 {taxed}）")
    inv.delete()


@transaction.atomic
def delete_sales_invoice(inv, *, user):
    """删除销售发票（彻底移除）：未核销、非期初才可删；连同明细一并删除。"""
    reason = invoice_delete_block_reason(inv)
    if reason:
        raise SettlementError(reason)
    company, doc_no, taxed = inv.company, inv.doc_no, inv.amount_taxed
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.DELETE, target=inv,
                    summary=f"删除销售发票 {doc_no}（撤销应收 {taxed}）")
    inv.delete()


def sales_invoice_edit_block_reason(inv, today):
    """返回不可修改原因；可改返回 None。规则：非期初、未核销、本月。"""
    if inv.is_opening:
        return "期初发票不可修改"
    if inv.settled_amount > 0:
        return "已核销（或部分核销）不可修改，请先撤销核销"
    if (inv.doc_date.year, inv.doc_date.month) != (today.year, today.month):
        return "跨月发票不可修改"
    return None


@transaction.atomic
def update_purchase_invoice(inv, *, user, doc_date, supplier, lines,
                            invoice_no="", remark="", term_days=0):
    """修改采购发票（保留单号）：未核销才可改；替换明细并重算应付。"""
    if inv.is_opening:
        raise SettlementError("期初发票不可在此修改")
    if inv.settled_amount > 0:
        raise SettlementError("已核销（或部分核销）的发票不可修改，请先撤销核销")
    inv.lines.all().delete()
    inv.doc_date = doc_date
    inv.supplier = supplier
    inv.invoice_no = invoice_no
    inv.term_days = term_days or 0
    inv.remark = remark
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for ln in lines:
        untaxed = round_money(ln["amount_untaxed"])
        rate = ln["tax_rate"]
        tax, taxed = _resolve_tax(untaxed, rate, ln)
        PurchaseInvoiceLine.objects.create(
            invoice=inv, product=ln.get("product"), description=ln.get("description", ""),
            quantity=ln.get("quantity") or ZERO_QTY,
            amount_untaxed=untaxed, tax_rate=rate, tax_amount=tax, amount_taxed=taxed,
            source_inbound_line=ln.get("source_inbound_line"))
        total_untaxed += untaxed
        total_tax += tax
        total_taxed += taxed
    inv.amount_untaxed = total_untaxed
    inv.tax_amount = total_tax
    inv.amount_taxed = total_taxed
    inv.save(update_fields=["doc_date", "supplier", "invoice_no", "term_days", "remark",
                            "amount_untaxed", "tax_amount", "amount_taxed"])
    AuditLog.record(actor=user, company=inv.company, action=AuditLog.Action.UPDATE, target=inv,
                    summary=f"修改采购发票 {inv.doc_no} 含税 {total_taxed}")
    return inv


def purchase_invoice_edit_block_reason(inv, today):
    """返回不可修改原因；可改返回 None。规则：非期初、未核销、本月。"""
    if inv.is_opening:
        return "期初发票不可修改"
    if inv.settled_amount > 0:
        return "已核销（或部分核销）不可修改，请先撤销核销"
    if (inv.doc_date.year, inv.doc_date.month) != (today.year, today.month):
        return "跨月发票不可修改"
    return None


# ============================= 销售侧（镜像采购侧）=============================
@transaction.atomic
def create_sales_invoice(*, company, user, doc_date, customer, lines,
                         invoice_no="", remark="", term_days=0) -> SalesInvoice:
    """开具销售发票并产生应收账款（发票即应收单据）。镜像 create_purchase_invoice。"""
    inv = SalesInvoice.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(SalesInvoice, company, "XSF", doc_date),
        invoice_no=invoice_no, doc_date=doc_date, term_days=term_days or 0,
        customer=customer, remark=remark,
    )
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for ln in lines:
        untaxed = round_money(ln["amount_untaxed"])
        rate = ln["tax_rate"]
        tax, taxed = _resolve_tax(untaxed, rate, ln)
        SalesInvoiceLine.objects.create(
            invoice=inv, product=ln.get("product"), description=ln.get("description", ""),
            quantity=ln.get("quantity") or ZERO_QTY,
            amount_untaxed=untaxed, tax_rate=rate, tax_amount=tax, amount_taxed=taxed,
            source_outbound_line=ln.get("source_outbound_line"),
        )
        total_untaxed += untaxed
        total_tax += tax
        total_taxed += taxed

    inv.amount_untaxed = total_untaxed
    inv.tax_amount = total_tax
    inv.amount_taxed = total_taxed
    inv.save(update_fields=["amount_untaxed", "tax_amount", "amount_taxed"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=inv,
        summary=f"销售发票 {inv.doc_no} 客户 {customer} 含税 {total_taxed}（应收）",
    )
    return inv


@transaction.atomic
def create_receipt(*, company, user, doc_date, bank_account, customer, amount, summary="") -> Receipt:
    """收款登记：保存收款单并自动生成一条银行存款日记账（收入）。镜像 create_payment。"""
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("收款金额必须大于 0")

    rec = Receipt.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(Receipt, company, "SK", doc_date),
        doc_date=doc_date, bank_account=bank_account, customer=customer,
        amount=amount, summary=summary,
    )
    journal = BankJournal.objects.create(
        company=company, created_by=user, bank_account=bank_account, date=doc_date,
        direction=BankJournal.Direction.IN, amount=amount,
        entry_type=(BankJournal.EntryType.SETTLEMENT if customer
                    else BankJournal.EntryType.OTHER),
        counterparty=str(customer) if customer else "",
        summary=summary or f"收款 {rec.doc_no}",
        source_type="Receipt", source_id=str(rec.pk), source_no=rec.doc_no,
    )
    rec.bank_journal = journal
    rec.save(update_fields=["bank_journal"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=rec,
        summary=f"收款 {rec.doc_no} 收 {customer or '其他'} {amount}（{bank_account}）",
    )
    return rec


@transaction.atomic
def allocate_receipt(*, receipt, allocations, user=None):
    """把收款核销到若干销售发票。镜像 allocate_payment。"""
    receipt = Receipt.objects.select_for_update().get(pk=receipt.pk)
    total = ZERO_MONEY
    cleaned = []
    for a in allocations:
        amount = round_money(a["amount"])
        if amount <= 0:
            continue
        invoice = SalesInvoice.objects.select_for_update().get(pk=a["invoice"].pk)
        if invoice.company_id != receipt.company_id or invoice.customer_id != receipt.customer_id:
            raise SettlementError("发票与收款的公司/客户不一致")
        # 允许核销超过发票未核销额（使发票余额/应收为负，对应预收/多收）
        total += amount
        cleaned.append((invoice, amount))

    if not cleaned:
        raise SettlementError("请填写有效的核销金额")
    if total > receipt.unallocated:
        raise SettlementError(f"核销合计 {total} 超过收款未核销 {receipt.unallocated}")

    for invoice, amount in cleaned:
        ReceiptAllocation.objects.create(receipt=receipt, invoice=invoice, amount=amount)
        invoice.settled_amount += amount
        invoice.save(update_fields=["settled_amount"])

    receipt.settled_amount += total
    receipt.save(update_fields=["settled_amount"])

    AuditLog.record(
        actor=user, company=receipt.company, action=AuditLog.Action.OFFSET, target=receipt,
        summary=f"应收核销 {receipt.doc_no} 核销 {total}（{len(cleaned)} 张发票）",
    )
    return receipt


def _cash_doc_block_reason(doc, today):
    """收/付款单可否修改/删除：非作废、未核销、未对账、当月。可改返回 None。"""
    if doc.status == doc.Status.VOID:
        return "已作废，不可修改/删除"
    if doc.settled_amount > 0:
        return "已核销，请先撤销核销后再操作"
    if doc.bank_journal_id and doc.bank_journal.reconciled:
        return "该笔已银行对账，不可修改/删除"
    if (doc.doc_date.year, doc.doc_date.month) != (today.year, today.month):
        return "仅当月单据可修改/删除"
    return None


def receipt_edit_block_reason(rec, today):
    return _cash_doc_block_reason(rec, today)


def payment_edit_block_reason(pay, today):
    return _cash_doc_block_reason(pay, today)


@transaction.atomic
def update_receipt(rec, *, user, doc_date, bank_account, customer, amount, summary=""):
    """修改收款（仅银行方式、未核销）：同步更新对应银行日记账。"""
    if rec.settled_amount > 0:
        raise SettlementError("已核销的收款不可修改，请先撤销核销")
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("收款金额必须大于 0")
    rec.doc_date = doc_date
    rec.bank_account = bank_account
    rec.customer = customer
    rec.amount = amount
    rec.summary = summary
    rec.save(update_fields=["doc_date", "bank_account", "customer", "amount", "summary"])
    j = rec.bank_journal
    if j is not None:
        j.date = doc_date
        j.bank_account = bank_account
        j.amount = amount
        j.entry_type = (BankJournal.EntryType.SETTLEMENT if customer
                        else BankJournal.EntryType.OTHER)
        j.counterparty = str(customer) if customer else ""
        j.summary = summary or f"收款 {rec.doc_no}"
        j.save(update_fields=["date", "bank_account", "amount", "entry_type",
                              "counterparty", "summary"])
    AuditLog.record(actor=user, company=rec.company, action=AuditLog.Action.UPDATE, target=rec,
                    summary=f"修改收款 {rec.doc_no} 收 {customer or '其他'} {amount}（{bank_account}）")
    return rec


@transaction.atomic
def delete_receipt(rec, *, user):
    """删除收款（未核销）：连同自动生成的银行日记账一并删除。"""
    if rec.settled_amount > 0:
        raise SettlementError("已核销的收款不可删除，请先撤销核销")
    company, doc_no = rec.company, rec.doc_no
    j = rec.bank_journal
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.DELETE, target=rec,
                    summary=f"删除收款 {doc_no} {rec.amount}")
    rec.bank_journal = None
    rec.save(update_fields=["bank_journal"])
    rec.delete()
    if j is not None:
        j.delete()


@transaction.atomic
def update_payment(pay, *, user, doc_date, bank_account, supplier, amount, summary=""):
    """修改付款（仅银行方式、未核销）：同步更新对应银行日记账。"""
    if pay.settled_amount > 0:
        raise SettlementError("已核销的付款不可修改，请先撤销核销")
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("付款金额必须大于 0")
    pay.doc_date = doc_date
    pay.bank_account = bank_account
    pay.supplier = supplier
    pay.amount = amount
    pay.summary = summary
    pay.save(update_fields=["doc_date", "bank_account", "supplier", "amount", "summary"])
    j = pay.bank_journal
    if j is not None:
        j.date = doc_date
        j.bank_account = bank_account
        j.amount = amount
        j.entry_type = (BankJournal.EntryType.SETTLEMENT if supplier
                        else BankJournal.EntryType.OTHER)
        j.counterparty = str(supplier) if supplier else ""
        j.summary = summary or f"付款 {pay.doc_no}"
        j.save(update_fields=["date", "bank_account", "amount", "entry_type",
                              "counterparty", "summary"])
    AuditLog.record(actor=user, company=pay.company, action=AuditLog.Action.UPDATE, target=pay,
                    summary=f"修改付款 {pay.doc_no} 付 {supplier or '其他'} {amount}（{bank_account}）")
    return pay


@transaction.atomic
def delete_payment(pay, *, user):
    """删除付款（未核销）：连同自动生成的银行日记账一并删除。"""
    if pay.settled_amount > 0:
        raise SettlementError("已核销的付款不可删除，请先撤销核销")
    company, doc_no = pay.company, pay.doc_no
    j = pay.bank_journal
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.DELETE, target=pay,
                    summary=f"删除付款 {doc_no} {pay.amount}")
    pay.bank_journal = None
    pay.save(update_fields=["bank_journal"])
    pay.delete()
    if j is not None:
        j.delete()


# ============================= 票据（M3）=====================================
@transaction.atomic
def create_note_receivable(*, company, user, draw_date, amount, customer=None,
                           note_no="", due_date=None, remark="", is_opening=False) -> NoteReceivable:
    """登记一张应收票据。"""
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("票面金额必须大于 0")
    note = NoteReceivable.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(NoteReceivable, company, "YSP", draw_date),
        note_no=note_no, draw_date=draw_date, due_date=due_date,
        customer=customer, amount=amount, remark=remark, is_opening=is_opening,
    )
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.CREATE, target=note,
                    summary=f"应收票据 {note.doc_no} 金额 {amount}")
    return note


def note_receivable_edit_block_reason(note) -> str | None:
    """应收票据可否修改：仅「已作废」整单不可改。其余可补录（票面金额是否可改在 update 里按已用额控制）。"""
    if note.status == NoteReceivable.Status.VOID:
        return "已作废的应收票据不可修改"
    return None


@transaction.atomic
def update_note_receivable(*, note, user, draw_date, amount, customer=None,
                           note_no="", due_date=None, remark="") -> NoteReceivable:
    """修改/补录应收票据信息（票号、出票日、到期日、来源客户、票面、备注）。

    护栏：已作废不可改；已使用（settled_amount>0）则票面金额锁定，不得改动
    （改票面会破坏已用/未用勾稽，需作废后重录）。其余字段任意补录。
    """
    note = NoteReceivable.objects.select_for_update().get(pk=note.pk)
    reason = note_receivable_edit_block_reason(note)
    if reason:
        raise SettlementError(reason)
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("票面金额必须大于 0")
    if amount != note.amount and note_has_usage(note):
        raise SettlementError("票据已使用（核销应收/背书），票面金额不可修改（如需更正请先撤销其冲销）")
    note.note_no = note_no
    note.draw_date = draw_date
    note.due_date = due_date
    note.customer = customer
    note.amount = amount
    note.remark = remark
    note.save(update_fields=["note_no", "draw_date", "due_date", "customer",
                             "amount", "remark", "updated_at"])
    AuditLog.record(actor=user, company=note.company, action=AuditLog.Action.UPDATE, target=note,
                    summary=f"修改应收票据 {note.doc_no}")
    return note


def note_receivable_delete_block_reason(note) -> str | None:
    """应收票据可否删除（彻底移除）：仅「未使用」即可删（含期初票据）。可删返回 None。

    已使用（核销过应收 / 背书抵过应付，存在 NoteSettlement）删除会留下孤儿冲销记录
    （NoteSettlement 用 note_id 泛指引用，无外键级联），故必须先到对应发票撤销冲销。
    未使用的票据（含期初导入的）不挂应收/应付、无日记账、无镜像，删除是干净的——
    期初票据正是导入时最易录错、最需删的，故不再额外拦期初。
    """
    if note_has_usage(note):
        return "票据已使用（核销应收/背书抵应付），不可删除；如需更正请到对应发票撤销冲销后再删"
    return None


@transaction.atomic
def delete_note_receivable(note, *, user):
    """删除应收票据（彻底移除）：仅「未使用」即可删（含期初票据）。

    未使用的票据不挂应收/应付、无银行日记账、无镜像，删除是干净的（仅撤销该票据登记）。
    """
    note = NoteReceivable.objects.select_for_update().get(pk=note.pk)
    reason = note_receivable_delete_block_reason(note)
    if reason:
        raise SettlementError(reason)
    company, doc_no, amount = note.company, note.doc_no, note.amount
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.DELETE, target=note,
                    summary=f"删除应收票据 {doc_no}（票面 {amount}）")
    note.delete()


@transaction.atomic
def create_note_payable(*, company, user, draw_date, supplier, amount,
                        note_no="", due_date=None, remark="", is_opening=False) -> NotePayable:
    """登记一张应付票据。"""
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("票面金额必须大于 0")
    note = NotePayable.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(NotePayable, company, "YFP", draw_date),
        note_no=note_no, draw_date=draw_date, due_date=due_date,
        supplier=supplier, amount=amount, remark=remark, is_opening=is_opening,
    )
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.CREATE, target=note,
                    summary=f"应付票据 {note.doc_no} 供应商 {supplier} 金额 {amount}")
    return note


def _note_applied_ar(note):
    """应收票据已抵应收账款的合计（核销应收=票进来抵应收，不消耗票面）。"""
    return (NoteSettlement.objects.filter(
        company=note.company, note_id=note.pk,
        note_kind=NoteSettlement.NoteKind.RECEIVABLE, is_endorsement=False
    ).aggregate(s=Sum("amount"))["s"] or ZERO_MONEY)


def note_has_usage(note) -> bool:
    """该票据是否有任何使用记录（核销应收 / 背书 / 抵应付）——用于禁改票面、禁删。"""
    kind = (NoteSettlement.NoteKind.RECEIVABLE if isinstance(note, NoteReceivable)
            else NoteSettlement.NoteKind.PAYABLE)
    return NoteSettlement.objects.filter(
        company=note.company, note_kind=kind, note_id=note.pk).exists()


def _apply_note(*, note, note_kind, invoice_model, invoice_kind, allocations,
                is_endorsement, user):
    """票据冲销通用核心：把票据冲抵若干发票未核销额。

    口径（关键）：
    - **应收票据核销应收账款**（is_endorsement=False，note_kind=RECEIVABLE）：票据是「收进来」
      抵客户应收账款（借应收票据/贷应收账款），**不消耗票面**——票仍持有可背书/托收。
      仅减发票未核销额；上限=票面−已抵应收额。
    - **背书抵应付 / 应付票据抵应付**（票据「出去」）：**消耗票面**，减未用额、到 0 定终态。
    校验：金额>0、不超相应额度、任一违反整体回滚。
    """
    NoteModel = type(note)
    note = NoteModel.objects.select_for_update().get(pk=note.pk)
    consumes = is_endorsement or note_kind == NoteSettlement.NoteKind.PAYABLE
    if note.status == NoteModel.Status.VOID:
        raise SettlementError(f"票据 {note.doc_no} 已作废，不可操作")
    if consumes and note.unused <= 0:
        raise SettlementError(f"票据 {note.doc_no} 已无未用额，不可再使用")

    total = ZERO_MONEY
    cleaned = []
    for a in allocations:
        amount = round_money(a["amount"])
        if amount <= 0:
            continue
        inv = invoice_model.objects.select_for_update().get(pk=a["invoice"].pk)
        if inv.company_id != note.company_id:
            raise SettlementError("票据与发票公司不一致")
        # 允许冲销超过发票未核销额（使发票余额为负）
        total += amount
        cleaned.append((inv, amount))

    if not cleaned:
        raise SettlementError("请填写有效的冲销金额")
    if consumes:
        if total > note.unused:
            raise SettlementError(f"冲销合计 {total} 超过票据未用额 {note.unused}")
    else:
        room = note.amount - _note_applied_ar(note)
        if total > room:
            raise SettlementError(f"核销应收合计 {total} 超过票据可抵应收额 {room}")

    for inv, amount in cleaned:
        NoteSettlement.objects.create(
            company=note.company, note_kind=note_kind, note_id=note.pk, note_no=note.doc_no,
            invoice_kind=invoice_kind, invoice_id=inv.pk, invoice_no=inv.doc_no,
            amount=amount, is_endorsement=is_endorsement,
        )
        inv.settled_amount += amount
        inv.save(update_fields=["settled_amount"])

    if consumes:
        note.settled_amount += total
        # 票面全部用出 → 定终态（应收=已背书，应付=已结算）；未用完保持在手/已开出
        if note.unused == 0:
            note.status = (NoteReceivable.Status.ENDORSED
                           if note_kind == NoteSettlement.NoteKind.RECEIVABLE
                           else NoteModel.Status.SETTLED)
        note.save(update_fields=["settled_amount", "status"])

    if is_endorsement:
        act = "背书抵应付"
    elif note_kind == NoteSettlement.NoteKind.PAYABLE:
        act = "应付票据抵应付"
    else:
        act = "核销应收账款"
    AuditLog.record(
        actor=user, company=note.company, action=AuditLog.Action.OFFSET, target=note,
        summary=f"票据{act} {note.doc_no} {total}（{len(cleaned)} 张发票）",
    )
    return note


@transaction.atomic
def settle_receivable_against_sales(*, note, allocations, user=None):
    """应收票据 → 核销应收账款（冲销售发票）。"""
    return _apply_note(
        note=note, note_kind=NoteSettlement.NoteKind.RECEIVABLE,
        invoice_model=SalesInvoice, invoice_kind=NoteSettlement.InvoiceKind.SALES,
        allocations=allocations, is_endorsement=False, user=user,
    )


@transaction.atomic
def endorse_receivable_against_purchase(*, note, allocations, user=None):
    """应收票据 → 背书转让给供应商抵付应付账款（冲采购发票）。"""
    return _apply_note(
        note=note, note_kind=NoteSettlement.NoteKind.RECEIVABLE,
        invoice_model=PurchaseInvoice, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE,
        allocations=allocations, is_endorsement=True, user=user,
    )


@transaction.atomic
def settle_payable_against_purchase(*, note, allocations, user=None):
    """应付票据 → 抵减应付账款（冲采购发票）。"""
    return _apply_note(
        note=note, note_kind=NoteSettlement.NoteKind.PAYABLE,
        invoice_model=PurchaseInvoice, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE,
        allocations=allocations, is_endorsement=False, user=user,
    )


def _note_of(settlement):
    """按 note_kind 取票据对象（应收/应付），取不到返回 None。"""
    Model = (NoteReceivable if settlement.note_kind == NoteSettlement.NoteKind.RECEIVABLE
             else NotePayable)
    return Model.objects.filter(pk=settlement.note_id).first()


def note_settlement_reverse_block_reason(settlement) -> str | None:
    """票据冲销可否撤销（恢复发票未核销额 + 票据未用额）。可撤返回 None。"""
    note = _note_of(settlement)
    if note is None:
        return "票据已不存在，无法撤销"
    if note.status == type(note).Status.VOID:
        return "票据已作废，不能撤销其冲销记录"
    return None


@transaction.atomic
def reverse_note_settlement(*, settlement, user):
    """撤销一笔票据冲销：发票未核销额退回、票据未用额与状态恢复，删除该冲销记录。

    用于更正「误用票据核销/背书」——撤销后票据回「在手/已开出」可重新处理。
    """
    s = NoteSettlement.objects.select_for_update().get(pk=settlement.pk)
    reason = note_settlement_reverse_block_reason(s)
    if reason:
        raise SettlementError(reason)
    NoteModel = (NoteReceivable if s.note_kind == NoteSettlement.NoteKind.RECEIVABLE
                 else NotePayable)
    note = NoteModel.objects.select_for_update().get(pk=s.note_id)
    invoice_model = (SalesInvoice if s.invoice_kind == NoteSettlement.InvoiceKind.SALES
                     else PurchaseInvoice)
    inv = invoice_model.objects.select_for_update().filter(pk=s.invoice_id).first()
    if inv is not None:
        inv.settled_amount -= s.amount
        inv.save(update_fields=["settled_amount"])
    # 只有「消耗票面」的冲销（背书/应付票抵应付）才退回票据未用额；
    # 核销应收（票进来抵应收账款）本就不消耗票面，撤销只退发票、不动票据。
    consumes = s.is_endorsement or s.note_kind == NoteSettlement.NoteKind.PAYABLE
    if consumes:
        note.settled_amount -= s.amount
        note.status = (NoteReceivable.Status.ON_HAND if isinstance(note, NoteReceivable)
                       else NotePayable.Status.ISSUED)
        note.save(update_fields=["settled_amount", "status"])
    AuditLog.record(
        actor=user, company=note.company, action=AuditLog.Action.OFFSET, target=note,
        summary=(f"撤销票据{'背书抵付' if s.is_endorsement else '核销应收'} "
                 f"{note.doc_no} 退回 {s.amount}（发票 {s.invoice_no}）"))
    s.delete()
    return note


# ============================= 期初往来（M5）==================================
@transaction.atomic
def create_opening_payable(*, company, user, supplier, amount, doc_date) -> PurchaseInvoice:
    """期初应付：建一张 is_opening 采购发票（含税额=期初应付，单行「期初」）。"""
    amount = round_money(amount)
    inv = PurchaseInvoice.objects.create(
        company=company, created_by=user, is_opening=True,
        doc_no=next_doc_no(PurchaseInvoice, company, "QCYF", doc_date),
        invoice_no="期初", doc_date=doc_date, supplier=supplier,
        amount_untaxed=amount, tax_amount=ZERO_MONEY, amount_taxed=amount,
        remark="期初应付",
    )
    PurchaseInvoiceLine.objects.create(
        invoice=inv, description="期初应付", amount_untaxed=amount,
        tax_rate=ZERO_MONEY, tax_amount=ZERO_MONEY, amount_taxed=amount,
    )
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.CREATE, target=inv,
                    summary=f"期初应付 {supplier} {amount}")
    return inv


@transaction.atomic
def create_opening_receivable(*, company, user, customer, amount, doc_date) -> SalesInvoice:
    """期初应收：建一张 is_opening 销售发票（含税额=期初应收，单行「期初」）。"""
    amount = round_money(amount)
    inv = SalesInvoice.objects.create(
        company=company, created_by=user, is_opening=True,
        doc_no=next_doc_no(SalesInvoice, company, "QCYS", doc_date),
        invoice_no="期初", doc_date=doc_date, customer=customer,
        amount_untaxed=amount, tax_amount=ZERO_MONEY, amount_taxed=amount,
        remark="期初应收",
    )
    SalesInvoiceLine.objects.create(
        invoice=inv, description="期初应收", amount_untaxed=amount,
        tax_rate=ZERO_MONEY, tax_amount=ZERO_MONEY, amount_taxed=amount,
    )
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.CREATE, target=inv,
                    summary=f"期初应收 {customer} {amount}")
    return inv


# ============================= 其他收支登记（M8-2）===========================
@transaction.atomic
def create_other_cashflow(*, company, user, doc_date, bank_account, direction, amount,
                          entry_type, counterparty="", summary="", txn_no=""):
    """手工登记非往来收支（费用/税费/工资/内部划转/其他），直接生成一条银行存款日记账。

    与付款/收款不同：不挂应收/应付，不参与核销。entry_type 不允许「往来结算」
    （那应走付款/收款登记）。
    """
    if amount is None or amount <= ZERO_MONEY:
        raise SettlementError("金额必须大于 0")
    if entry_type == BankJournal.EntryType.SETTLEMENT:
        raise SettlementError("往来结算请走付款/收款登记")

    journal = BankJournal.objects.create(
        company=company, created_by=user, bank_account=bank_account, date=doc_date,
        direction=direction, amount=round_money(amount), entry_type=entry_type,
        counterparty=counterparty, summary=summary, txn_no=txn_no,
        source_type="Other",
    )
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=journal,
        summary=f"其他收支登记 {journal.get_entry_type_display()} "
                f"{journal.get_direction_display()} {journal.amount}",
    )
    return journal


@transaction.atomic
def delete_other_cashflow(*, journal, user):
    """删除手工登记的其他收支日记账（仅限 source_type=Other，往来/系统生成的不可删）。"""
    if journal.source_type != "Other":
        raise SettlementError("仅可删除手工登记的其他收支；往来收付请到对应单据作废")
    company, summary = journal.company, str(journal)
    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.DELETE, target=journal,
        summary=f"删除其他收支 {summary}",
    )
    journal.delete()


def other_cashflow_block_reason(journal, today):
    """其他收支可否修改/删除：仅手工登记(Other)、未对账、当月。可改返回 None。"""
    if journal.source_type != "Other":
        return "仅手工登记的其他收支可修改/删除；往来收付请到对应单据操作"
    if journal.reconciled:
        return "该笔已银行对账，不可修改/删除"
    if (journal.date.year, journal.date.month) != (today.year, today.month):
        return "仅当月单据可修改/删除"
    return None


@transaction.atomic
def update_other_cashflow(*, journal, user, doc_date, bank_account, direction, amount,
                          entry_type, counterparty="", summary="", txn_no=""):
    """修改手工登记的其他收支日记账（仅 source_type=Other、未对账）。"""
    if journal.source_type != "Other":
        raise SettlementError("仅可修改手工登记的其他收支；往来收付请到对应单据修改")
    if journal.reconciled:
        raise SettlementError("该笔已银行对账，不可修改")
    if amount is None or amount <= ZERO_MONEY:
        raise SettlementError("金额必须大于 0")
    if entry_type == BankJournal.EntryType.SETTLEMENT:
        raise SettlementError("往来结算请走付款/收款登记")
    journal.date = doc_date
    journal.bank_account = bank_account
    journal.direction = direction
    journal.amount = round_money(amount)
    journal.entry_type = entry_type
    journal.counterparty = counterparty
    journal.summary = summary
    journal.txn_no = txn_no
    journal.save(update_fields=["date", "bank_account", "direction", "amount",
                                "entry_type", "counterparty", "summary", "txn_no"])
    AuditLog.record(
        actor=user, company=journal.company, action=AuditLog.Action.UPDATE, target=journal,
        summary=f"修改其他收支 {journal.get_entry_type_display()} "
                f"{journal.get_direction_display()} {journal.amount}",
    )
    return journal


# ============================= 银行对账（M8-3）===============================
@transaction.atomic
def reconcile_bank_journal(*, company, user, account, parsed, filename=""):
    """把导入的网银流水与系统已登记日记账勾对，并持久化「已对账」状态。

    parsed: parse_bank_journal_xlsx 的输出 [{date,summary,counterparty,direction,amount,txn_no}]。
    匹配优先级：① 账户+交易流水号；② 账户+日期+金额+方向（同一系统行只配一次）。
    返回 {batch, matched:[(line,journal)], system_only:[journal], bank_only:[line], period_from/to}。
    """
    dates = [p["date"] for p in parsed if p.get("date")]
    pfrom, pto = (min(dates), max(dates)) if dates else (None, None)

    jqs = BankJournal.objects.filter(company=company, bank_account=account)
    if pfrom:
        jqs = jqs.filter(date__gte=pfrom)
    if pto:
        jqs = jqs.filter(date__lte=pto)
    journals = list(jqs.order_by("date", "id"))

    by_txn, by_key = {}, {}
    for j in journals:
        if j.txn_no:
            by_txn.setdefault(j.txn_no, []).append(j)
        by_key.setdefault((j.date, j.amount, j.direction), []).append(j)

    used = set()
    matched, bank_only = [], []
    for p in parsed:
        hit = None
        if p.get("txn_no"):
            hit = next((c for c in by_txn.get(p["txn_no"], []) if c.pk not in used), None)
        if hit is None:
            key = (p["date"], round_money(p["amount"]), p["direction"])
            hit = next((c for c in by_key.get(key, []) if c.pk not in used), None)
        if hit is not None:
            used.add(hit.pk)
            matched.append((p, hit))
        else:
            bank_only.append(p)
    system_only = [j for j in journals if j.pk not in used]

    batch = BankReconcileBatch.objects.create(
        company=company, created_by=user, bank_account=account, filename=filename,
        period_from=pfrom, period_to=pto, matched_count=len(matched),
        system_only_count=len(system_only), bank_only_count=len(bank_only))
    BankJournal.objects.filter(pk__in=[j.pk for _, j in matched]).update(
        reconciled=True, reconcile_batch=batch)

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.OFFSET, target=batch,
        summary=f"银行对账 {account} 匹配 {len(matched)}／仅系统 {len(system_only)}／仅网银 {len(bank_only)}")
    return {"batch": batch, "matched": matched, "system_only": system_only,
            "bank_only": bank_only, "period_from": pfrom, "period_to": pto}
