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
                           note_no="", due_date=None, remark="") -> NoteReceivable:
    """登记一张应收票据。"""
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("票面金额必须大于 0")
    note = NoteReceivable.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(NoteReceivable, company, "YSP", draw_date),
        note_no=note_no, draw_date=draw_date, due_date=due_date,
        customer=customer, amount=amount, remark=remark,
    )
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.CREATE, target=note,
                    summary=f"应收票据 {note.doc_no} 金额 {amount}")
    return note


@transaction.atomic
def create_note_payable(*, company, user, draw_date, supplier, amount,
                        note_no="", due_date=None, remark="") -> NotePayable:
    """登记一张应付票据。"""
    amount = round_money(amount)
    if amount <= 0:
        raise ValueError("票面金额必须大于 0")
    note = NotePayable.objects.create(
        company=company, created_by=user,
        doc_no=next_doc_no(NotePayable, company, "YFP", draw_date),
        note_no=note_no, draw_date=draw_date, due_date=due_date,
        supplier=supplier, amount=amount, remark=remark,
    )
    AuditLog.record(actor=user, company=company, action=AuditLog.Action.CREATE, target=note,
                    summary=f"应付票据 {note.doc_no} 供应商 {supplier} 金额 {amount}")
    return note
