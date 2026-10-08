"""更正鸿威达付款 FK-C3-20261015-001 的业务日为 2026-09-15。

该笔已全部核销，付款一览不提供「修改」。只改日期：付款单、对应银行日记账、
应付核销归期日。单号、金额、核销关系不变。
"""

from datetime import date

from django.db import migrations


DOC_NO = "FK-C3-20261015-001"
CORRECT_DATE = date(2026, 9, 15)


def fix_hongweida_payment_date(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    Payment = apps.get_model("finance", "Payment")
    BankJournal = apps.get_model("finance", "BankJournal")
    PaymentAllocation = apps.get_model("finance", "PaymentAllocation")

    company = Company.objects.filter(code="C3").first()
    if company is None:
        return
    pay = Payment.objects.filter(company=company, doc_no=DOC_NO).first()
    if pay is None:
        return
    if pay.doc_date != CORRECT_DATE:
        pay.doc_date = CORRECT_DATE
        pay.save(update_fields=["doc_date"])
    if pay.bank_journal_id:
        journal = BankJournal.objects.filter(pk=pay.bank_journal_id).first()
        if journal is not None and journal.date != CORRECT_DATE:
            journal.date = CORRECT_DATE
            journal.save(update_fields=["date"])
    PaymentAllocation.objects.filter(payment_id=pay.pk).exclude(date=CORRECT_DATE).update(
        date=CORRECT_DATE)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0035_anbonuo_note_draw_date_20260915"),
    ]

    operations = [
        migrations.RunPython(fix_hongweida_payment_date, migrations.RunPython.noop),
    ]
