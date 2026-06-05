"""M6-1 其他费用：计入成本分摊 / 期间费用。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.finance.models import ExpenseEntry
from apps.inventory.models import StockBalance
from apps.masterdata.models import ExpenseCategory, Product
from apps.purchasing.services import create_and_post_inbound


class ExpenseCostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p1 = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.p2 = Product.objects.create(company=cls.c1, code="P002", name="货B")
        cls.freight = ExpenseCategory.objects.create(company=cls.c1, name="运费", include_in_cost=True)
        cls.travel = ExpenseCategory.objects.create(company=cls.c1, name="差旅费", include_in_cost=False)

    def test_cost_fee_raises_unit_cost(self):
        # 入 100@10=1000，运费 200 计入成本 → 金额1200、均价12
        create_and_post_inbound(company=self.c1, user=None, doc_date=date(2026, 6, 1),
            lines=[{"product": self.p1, "quantity": Decimal("100"), "unit_price": Decimal("10")}],
            expenses=[{"category": self.freight, "amount": Decimal("200")}])
        b = StockBalance.objects.get(company=self.c1, product=self.p1)
        self.assertEqual(b.amount, Decimal("1200.00"))
        self.assertEqual(b.avg_price, Decimal("12.00"))
        e = ExpenseEntry.objects.get(company=self.c1, category=self.freight)
        self.assertTrue(e.included_in_cost)

    def test_period_fee_not_in_cost(self):
        create_and_post_inbound(company=self.c1, user=None, doc_date=date(2026, 6, 1),
            lines=[{"product": self.p1, "quantity": Decimal("100"), "unit_price": Decimal("10")}],
            expenses=[{"category": self.travel, "amount": Decimal("50")}])
        b = StockBalance.objects.get(company=self.c1, product=self.p1)
        self.assertEqual(b.amount, Decimal("1000.00"))  # 不抬高
        e = ExpenseEntry.objects.get(company=self.c1, category=self.travel)
        self.assertFalse(e.included_in_cost)

    def test_cost_fee_allocated_proportionally(self):
        # 两行 base 1000 与 3000，运费 400 → 分摊 100 / 300
        create_and_post_inbound(company=self.c1, user=None, doc_date=date(2026, 6, 1),
            lines=[
                {"product": self.p1, "quantity": Decimal("100"), "unit_price": Decimal("10")},
                {"product": self.p2, "quantity": Decimal("100"), "unit_price": Decimal("30")},
            ],
            expenses=[{"category": self.freight, "amount": Decimal("400")}])
        b1 = StockBalance.objects.get(company=self.c1, product=self.p1)
        b2 = StockBalance.objects.get(company=self.c1, product=self.p2)
        self.assertEqual(b1.amount, Decimal("1100.00"))  # 1000 + 100
        self.assertEqual(b2.amount, Decimal("3300.00"))  # 3000 + 300
