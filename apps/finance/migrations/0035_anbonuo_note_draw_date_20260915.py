"""更正安博诺应收票据 YSP-C1-20260721-001 出票日。

来源：应收票据使用明细-YSP-C1-20260721-001_安博诺_20260701-20260915_20260915.xlsx
第二笔「出票」系统日期 2026-08-06，实际日期应为 2026-07-21（与单号日期一致）。
"""

from datetime import date

from django.db import migrations


NOTE_DOC_NO = "YSP-C1-20260721-001"
CORRECT_DRAW_DATE = date(2026, 7, 21)


def fix_anbonuo_note_draw_date(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    NoteReceivable = apps.get_model("finance", "NoteReceivable")

    company = Company.objects.filter(code="C1").first()
    if company is None:
        return
    note = NoteReceivable.objects.filter(company=company, doc_no=NOTE_DOC_NO).first()
    if note is None:
        return
    if note.draw_date != CORRECT_DRAW_DATE:
        note.draw_date = CORRECT_DRAW_DATE
        note.save(update_fields=["draw_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0034_anbonuo_endorsement_dates_20260908"),
    ]

    operations = [
        migrations.RunPython(fix_anbonuo_note_draw_date, migrations.RunPython.noop),
    ]
