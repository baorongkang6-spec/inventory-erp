"""核销/冲销记录补业务日期字段（统一会计口径）。

背景：应付/应收核销与票据冲销原先没有独立业务日期，报表用 `created_at`
（操作时钟）当归期依据 —— 跨月核销会记到操作月而非付款/收款/票据月，
且导致依赖当前日期的测试不稳定。本迁移加 `date` 字段并回填：

- PaymentAllocation.date  ← 付款单 doc_date
- ReceiptAllocation.date  ← 收款单 doc_date
- NoteSettlement.date     ← 对应票据 draw_date（取不到则回退 created_at.date()）

回填会改动历史数据：仅当某笔核销的操作月与付款/收款/票据月不同（跨月核销）时，
相关往来/票据余额表的「本期减少」所属月份会随之更正。升级前务必先 backup.bat。
"""

from django.db import migrations, models


def backfill_dates(apps, schema_editor):
    PaymentAllocation = apps.get_model("finance", "PaymentAllocation")
    ReceiptAllocation = apps.get_model("finance", "ReceiptAllocation")
    NoteSettlement = apps.get_model("finance", "NoteSettlement")
    NoteReceivable = apps.get_model("finance", "NoteReceivable")
    NotePayable = apps.get_model("finance", "NotePayable")

    for a in PaymentAllocation.objects.select_related("payment").all():
        a.date = a.payment.doc_date
        a.save(update_fields=["date"])

    for a in ReceiptAllocation.objects.select_related("receipt").all():
        a.date = a.receipt.doc_date
        a.save(update_fields=["date"])

    ar_dates = dict(NoteReceivable.objects.values_list("pk", "draw_date"))
    ap_dates = dict(NotePayable.objects.values_list("pk", "draw_date"))
    for s in NoteSettlement.objects.all():
        if s.note_kind == "ar_note":
            d = ar_dates.get(s.note_id)
        else:
            d = ap_dates.get(s.note_id)
        s.date = d or s.created_at.date()
        s.save(update_fields=["date"])


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0020_alter_bankjournal_entry_type_notedisposal"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentallocation",
            name="date",
            field=models.DateField(blank=True, null=True, verbose_name="核销日期"),
        ),
        migrations.AddField(
            model_name="receiptallocation",
            name="date",
            field=models.DateField(blank=True, null=True, verbose_name="核销日期"),
        ),
        migrations.AddField(
            model_name="notesettlement",
            name="date",
            field=models.DateField(blank=True, null=True, verbose_name="冲销日期"),
        ),
        migrations.RunPython(backfill_dates, migrations.RunPython.noop),
    ]
