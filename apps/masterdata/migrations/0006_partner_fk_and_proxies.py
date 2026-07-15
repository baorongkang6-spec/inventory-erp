"""业务 FK 切到 BusinessPartner 之后：删除旧 Customer/Supplier 表，改为代理模型。"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("masterdata", "0005_business_partner_m19"),
        ("finance", "0026_alter_fk_to_businesspartner"),
        ("sales", "0009_alter_fk_to_businesspartner"),
        ("purchasing", "0008_alter_fk_to_businesspartner"),
    ]

    operations = [
        migrations.DeleteModel(name="Customer"),
        migrations.DeleteModel(name="Supplier"),
        migrations.CreateModel(
            name="Customer",
            fields=[],
            options={
                "verbose_name": "客户",
                "verbose_name_plural": "客户",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("masterdata.businesspartner",),
        ),
        migrations.CreateModel(
            name="Supplier",
            fields=[],
            options={
                "verbose_name": "供应商",
                "verbose_name_plural": "供应商",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("masterdata.businesspartner",),
        ),
    ]
