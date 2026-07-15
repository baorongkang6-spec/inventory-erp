"""PartnerOffset：客户+供应商双 FK → 单一往来单位 partner（SPEC §21）。"""

import django.db.models.deletion
from django.db import migrations, models


def _fill_partner(apps, schema_editor):
    PartnerOffset = apps.get_model("finance", "PartnerOffset")
    db = schema_editor.connection.alias
    for row in PartnerOffset.objects.using(db).all():
        # 优先客户 id（与 BusinessPartner 保留主键一致）；否则用已映射的供应商 id
        pid = getattr(row, "customer_id", None) or getattr(row, "supplier_id", None)
        if pid:
            row.partner_id = pid
            row.save(update_fields=["partner_id"])


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0024_purchase_order_m18"),
        ("masterdata", "0005_business_partner_m19"),
    ]

    operations = [
        migrations.AddField(
            model_name="partneroffset",
            name="partner",
            field=models.ForeignKey(
                null=True, blank=True,
                help_text="须同时具备客户与供应商角色（或该单位下既有未结应收又有未结应付）。",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="partner_offsets",
                to="masterdata.businesspartner",
                verbose_name="往来单位",
            ),
        ),
        migrations.RunPython(_fill_partner, _noop),
        migrations.AlterField(
            model_name="partneroffset",
            name="partner",
            field=models.ForeignKey(
                help_text="须同时具备客户与供应商角色（或该单位下既有未结应收又有未结应付）。",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="partner_offsets",
                to="masterdata.businesspartner",
                verbose_name="往来单位",
            ),
        ),
        migrations.RemoveField(model_name="partneroffset", name="customer"),
        migrations.RemoveField(model_name="partneroffset", name="supplier"),
    ]
