"""资金往来服务：采购发票登记（→应付账款）等。"""

from django.db import transaction

from apps.core.docnum import next_doc_no
from apps.core.models import AuditLog
from apps.core.money import ZERO_MONEY, round_money

from .models import BankJournal, Payment, PurchaseInvoice, PurchaseInvoiceLine


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
