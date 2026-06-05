"""seed_init 冒烟测试：在干净库上跑通公司/角色/演示单据，并校验幂等。"""

from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance


class SeedInitTests(TestCase):
    def _run(self):
        call_command("seed_init", "--demo", stdout=StringIO())

    def test_seed_creates_companies_roles_and_demo_documents(self):
        self._run()
        self.assertEqual(Company.objects.count(), 3)
        self.assertEqual(Group.objects.count(), 5)
        self.assertTrue(get_user_model().objects.filter(username="purchaser").exists())

        # 演示单据后：C1 P001 结存 90@11.00（100@10 + 50@13 → 均价 11，出 60）
        bal = StockBalance.objects.get(company__code="C1", product__code="P001")
        self.assertEqual(bal.quantity, Decimal("90.000"))
        self.assertEqual(bal.amount, Decimal("990.00"))
        self.assertEqual(bal.avg_price, Decimal("11.00"))

    def test_seed_is_idempotent(self):
        self._run()
        self._run()  # 第二次不应报错，也不应重复建公司/单据
        self.assertEqual(Company.objects.count(), 3)
        self.assertEqual(
            StockBalance.objects.get(company__code="C1", product__code="P001").quantity,
            Decimal("90.000"),
        )
