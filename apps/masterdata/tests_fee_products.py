"""D98运费 / D99服务费：非存货商品与费用类别种子。"""

from django.apps import apps as django_apps
from django.test import TestCase

from apps.core.models import Company
from apps.masterdata.models import ExpenseCategory, Product


class FeeProductSeedTests(TestCase):
    def test_migration_seeds_d98_freight_and_d99_service(self):
        import importlib

        c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        existing = Product.objects.create(
            company=c1, code="D99", name="服务费", count_in_inventory=True)
        mod = importlib.import_module(
            "apps.masterdata.migrations.0007_product_count_in_inventory_d98_d99")
        mod.seed_fee_codes(django_apps, None)

        d98 = Product.objects.get(company=c1, code="D98")
        self.assertEqual(d98.name, "运费")
        self.assertFalse(d98.count_in_inventory)
        existing.refresh_from_db()
        self.assertFalse(existing.count_in_inventory)
        self.assertTrue(
            ExpenseCategory.objects.filter(company=c1, name="D98运费", include_in_cost=False).exists())
        self.assertTrue(
            ExpenseCategory.objects.filter(company=c1, name="D99服务费", include_in_cost=False).exists())
