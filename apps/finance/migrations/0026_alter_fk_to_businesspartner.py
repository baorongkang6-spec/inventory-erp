"""财务单据 customer/supplier FK → BusinessPartner。"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("masterdata", "0005_business_partner_m19"),
        ("finance", "0025_partneroffset_partner_m19"),
    ]

    operations = [
        migrations.AlterField(
            model_name="purchaseinvoice",
            name="supplier",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="供应商",
            ),
        ),
        migrations.AlterField(
            model_name="payment",
            name="supplier",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="供应商",
            ),
        ),
        migrations.AlterField(
            model_name="salesinvoice",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="客户",
            ),
        ),
        migrations.AlterField(
            model_name="receipt",
            name="customer",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="客户",
            ),
        ),
        migrations.AlterField(
            model_name="notereceivable",
            name="customer",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="出票/来源客户",
            ),
        ),
        migrations.AlterField(
            model_name="notepayable",
            name="supplier",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="收票供应商",
            ),
        ),
        migrations.AlterField(
            model_name="expenserecord",
            name="customer",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                to="masterdata.businesspartner", verbose_name="客户",
            ),
        ),
    ]
