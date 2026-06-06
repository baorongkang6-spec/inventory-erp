"""资金往来服务：采购发票登记（→应付账款）等。"""

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import ZERO_MONEY, round_money

from .models import (
    BankJournal,
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


@transaction.atomic
def create_purchase_invoice(*, company, user, doc_date, supplier, lines,
                            invoice_no="", remark="") -> PurchaseInvoice:
    """登记采购发票并产生应付账款（发票即应付单据）。

    lines: [{"product": Product|None, "description": str,
             "amount_untaxed": Decimal, "tax_rate": Decimal,
             "source_inbound_line": PurchaseInboundLine|None}, ...]
    """
    inv = PurchaseInvoice.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(PurchaseInvoice, company, "CGF", doc_date),
        invoice_no=invoice_no, doc_date=doc_date, supplier=supplier, remark=remark,
    )

    total_untaxed = ZERO_MONEY
    total_tax = ZERO_MONEY
    total_taxed = ZERO_MONEY
    for ln in lines:
        untaxed = round_money(ln["amount_untaxed"])
        rate = ln["tax_rate"]
        tax, taxed = compute_tax(untaxed, rate)
        PurchaseInvoiceLine.objects.create(
            invoice=inv, product=ln.get("product"), description=ln.get("description", ""),
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
        entry_type=BankJournal.EntryType.SETTLEMENT,
        counterparty=str(supplier), summary=summary or f"付款 {pay.doc_no}",
        source_type="Payment", source_id=str(pay.pk), source_no=pay.doc_no,
    )
    pay.bank_journal = journal
    pay.save(update_fields=["bank_journal"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=pay,
        summary=f"付款 {pay.doc_no} 付 {supplier} {amount}（{bank_account}）",
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
        if amount > invoice.outstanding:
            raise SettlementError(
                f"核销额 {amount} 超过发票 {invoice.doc_no} 未核销 {invoice.outstanding}"
            )
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


# ============================= 销售侧（镜像采购侧）=============================
@transaction.atomic
def create_sales_invoice(*, company, user, doc_date, customer, lines,
                         invoice_no="", remark="") -> SalesInvoice:
    """开具销售发票并产生应收账款（发票即应收单据）。镜像 create_purchase_invoice。"""
    inv = SalesInvoice.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(SalesInvoice, company, "XSF", doc_date),
        invoice_no=invoice_no, doc_date=doc_date, customer=customer, remark=remark,
    )
    total_untaxed = total_tax = total_taxed = ZERO_MONEY
    for ln in lines:
        untaxed = round_money(ln["amount_untaxed"])
        rate = ln["tax_rate"]
        tax, taxed = compute_tax(untaxed, rate)
        SalesInvoiceLine.objects.create(
            invoice=inv, product=ln.get("product"), description=ln.get("description", ""),
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
        entry_type=BankJournal.EntryType.SETTLEMENT,
        counterparty=str(customer), summary=summary or f"收款 {rec.doc_no}",
        source_type="Receipt", source_id=str(rec.pk), source_no=rec.doc_no,
    )
    rec.bank_journal = journal
    rec.save(update_fields=["bank_journal"])

    AuditLog.record(
        actor=user, company=company, action=AuditLog.Action.CREATE, target=rec,
        summary=f"收款 {rec.doc_no} 收 {customer} {amount}（{bank_account}）",
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
        if amount > invoice.outstanding:
            raise SettlementError(
                f"核销额 {amount} 超过发票 {invoice.doc_no} 未核销 {invoice.outstanding}"
            )
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


def _apply_note(*, note, note_kind, invoice_model, invoice_kind, allocations,
                is_endorsement, user):
    """票据冲销通用核心：把票据未用额冲抵若干发票未核销额。

    校验：金额>0、不超票据未用额、不超各发票未核销额；任一违反整体回滚。
    """
    NoteModel = type(note)
    note = NoteModel.objects.select_for_update().get(pk=note.pk)
    if note.status in ("void", "settled", "endorsed"):
        raise SettlementError(f"票据 {note.doc_no} 状态为「{note.get_status_display()}」，不可再冲销")

    total = ZERO_MONEY
    cleaned = []
    for a in allocations:
        amount = round_money(a["amount"])
        if amount <= 0:
            continue
        inv = invoice_model.objects.select_for_update().get(pk=a["invoice"].pk)
        if inv.company_id != note.company_id:
            raise SettlementError("票据与发票公司不一致")
        if amount > inv.outstanding:
            raise SettlementError(f"冲销额 {amount} 超过发票 {inv.doc_no} 未核销 {inv.outstanding}")
        total += amount
        cleaned.append((inv, amount))

    if not cleaned:
        raise SettlementError("请填写有效的冲销金额")
    if total > note.unused:
        raise SettlementError(f"冲销合计 {total} 超过票据未用额 {note.unused}")

    for inv, amount in cleaned:
        NoteSettlement.objects.create(
            company=note.company, note_kind=note_kind, note_id=note.pk, note_no=note.doc_no,
            invoice_kind=invoice_kind, invoice_id=inv.pk, invoice_no=inv.doc_no,
            amount=amount, is_endorsement=is_endorsement,
        )
        inv.settled_amount += amount
        inv.save(update_fields=["settled_amount"])

    note.settled_amount += total
    if is_endorsement:
        note.status = NoteReceivable.Status.ENDORSED
    elif note.unused == 0:
        note.status = type(note).Status.SETTLED
    note.save(update_fields=["settled_amount", "status"])

    AuditLog.record(
        actor=user, company=note.company, action=AuditLog.Action.OFFSET, target=note,
        summary=f"票据冲销 {note.doc_no} 冲 {total}（{len(cleaned)} 张发票{'，背书抵付' if is_endorsement else ''}）",
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
