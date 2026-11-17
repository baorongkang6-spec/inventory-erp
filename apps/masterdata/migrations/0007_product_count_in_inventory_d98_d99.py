"""商品增加「计入库存商品」；各账套落地 D98运费/D99服务费（非存货）与费用类别。"""

from django.db import migrations, models


# (编码, 名称, 分类)
FEE_PRODUCTS = (
    ("D98", "运费", "运费"),
    ("D99", "服务费", "服务费"),
)
FEE_EXPENSE_CATEGORIES = (
    ("D98运费", "运费类，不计入存货成本"),
    ("D99服务费", "服务费类，不计入存货成本"),
)


def seed_fee_codes(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    Product = apps.get_model("masterdata", "Product")
    ExpenseCategory = apps.get_model("masterdata", "ExpenseCategory")

    for company in Company.objects.all():
        for code, name, category in FEE_PRODUCTS:
            product, created = Product.objects.get_or_create(
                company=company, code=code,
                defaults={
                    "name": name,
                    "category": category,
                    "count_in_inventory": False,
                    "is_active": True,
                    "remark": "非存货：不计入库存商品统计",
                },
            )
            update_fields = []
            if product.count_in_inventory:
                product.count_in_inventory = False
                update_fields.append("count_in_inventory")
            if created:
                pass
            else:
                # 已有 D98 时纠正名称为运费；D99 保留既有名称（多为服务费）
                if code == "D98" and product.name != name:
                    product.name = name
                    update_fields.append("name")
                if not product.category:
                    product.category = category
                    update_fields.append("category")
                if not product.remark:
                    product.remark = "非存货：不计入库存商品统计"
                    update_fields.append("remark")
            if update_fields:
                product.save(update_fields=update_fields)

        for name, remark in FEE_EXPENSE_CATEGORIES:
            ExpenseCategory.objects.get_or_create(
                company=company, name=name,
                defaults={
                    "include_in_cost": False,
                    "is_active": True,
                    "remark": remark,
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        ("masterdata", "0006_partner_fk_and_proxies"),
        ("core", "0006_company_zijinglan"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="count_in_inventory",
            field=models.BooleanField(
                default=True,
                help_text="取消勾选后不进入库存余额表、总览库存、发出商品等存货统计（运费/服务费等非存货编码用）。",
                verbose_name="计入库存商品",
            ),
        ),
        migrations.RunPython(seed_fee_codes, migrations.RunPython.noop),
    ]
