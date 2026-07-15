"""采购单据 supplier FK → BusinessPartner。"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("masterdata", "0005_business_partner_m19"),
        ("purchasing", "0007_sale_purchase_return_types"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseinbound",
            name="supplier",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="供应商",
            ),
        ),
        migrations.AlterField(
            model_name="purchaseorder",
            name="supplier",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="供应商",
            ),
        ),
    ]
