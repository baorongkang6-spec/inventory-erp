"""无票背书预付：补全 invoice_kind=purchase（旧数据为空导致应付/付款一览漏显）。"""

from django.db import migrations


def fix_prepaid_endorsement_kind(apps, schema_editor):
    NoteSettlement = apps.get_model("finance", "NoteSettlement")
    NoteSettlement.objects.filter(
        is_endorsement=True, invoice_kind="", purchase_order_id__isnull=False,
    ).update(invoice_kind="purchase")


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0029_receipt_sales_order_note_settlement_po"),
    ]

    operations = [
        migrations.RunPython(fix_prepaid_endorsement_kind, migrations.RunPython.noop),
    ]
