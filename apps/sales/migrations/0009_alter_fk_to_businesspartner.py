"""销售单据 customer FK → BusinessPartner。"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("masterdata", "0005_business_partner_m19"),
        ("sales", "0008_sale_purchase_return_types"),
    ]

    operations = [
        migrations.AlterField(
            model_name="salesoutbound",
            name="customer",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="客户",
            ),
        ),
        migrations.AlterField(
            model_name="salesorder",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="客户",
            ),
        ),
    ]
