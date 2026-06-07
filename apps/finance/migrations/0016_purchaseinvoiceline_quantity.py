# Generated for 已入库未收到发票明细表（采购发票行加数量）

from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0015_salesinvoiceline_quantity'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseinvoiceline',
            name='quantity',
            field=models.DecimalField(decimal_places=3, default=Decimal('0.000'), max_digits=18, verbose_name='数量'),
        ),
    ]
