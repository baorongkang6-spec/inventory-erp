"""更正安博诺票据冲应收核销日：YSP-C1-20260928-003 → 2026-09-28。

收款登记显示的是出票日 9/28；冲销售发票 XSF-C1-20260807-001 的 314,641.72
核销日误记为表单「收款日期」2026-10-09。客户往来明细账按核销日归期，故改回 9/28。
"""

from datetime import date
from decimal import Decimal

from django.db import migrations


NOTE_NO = "YSP-C1-20260928-003"
INVOICE_NO = "XSF-C1-20260807-001"
AMOUNT = Decimal("314641.72")
CORRECT_DATE = date(2026, 9, 28)


def fix_anbonuo_note_settlement_date(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    NoteSettlement = apps.get_model("finance", "NoteSettlement")

    company = Company.objects.filter(code="C1").first()
    if company is None:
        return
    NoteSettlement.objects.filter(
        company=company,
        note_no=NOTE_NO,
        invoice_no=INVOICE_NO,
        amount=AMOUNT,
        is_endorsement=False,
        note_kind="ar_note",
    ).exclude(date=CORRECT_DATE).update(date=CORRECT_DATE)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0036_hongweida_payment_date_fk_c3_20261015"),
    ]

    operations = [
        migrations.RunPython(fix_anbonuo_note_settlement_date, migrations.RunPython.noop),
    ]
