"""销售出库过账集成测试：成本结转、库存不足整单回滚。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance
from apps.inventory.services import InsufficientStockError, post_inbound
from apps.masterdata.models import Product
from apps.sales.models import SalesOutbound
from apps.sales.services import create_and_post_outbound


class OutboundPostingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")

    def test_outbound_transfers_moving_average_cost(self):
        post_inbound(self.c1, self.p, Decimal("100"), Decimal("10"))
        post_inbound(self.c1, self.p, Decimal("50"), Decimal("13"))  # 均价 11.00
        doc = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[{"product": self.p, "quantity": Decimal("60")}],
        )
        self.assertEqual(doc.doc_no, "CK-C1-20260605-001")
        self.assertEqual(doc.total_quantity, Decimal("60.000"))
        self.assertEqual(doc.total_cost, Decimal("660.00"))
        line = doc.lines.get()
        self.assertEqual(line.unit_cost, Decimal("11.00"))
        self.assertEqual(line.amount, Decimal("660.00"))

        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("90.000"))
        self.assertEqual(bal.amount, Decimal("990.00"))

    def test_oversell_allowed_negative(self):
        # 允许负库存：出库超过结存照常过账，结存变负
        post_inbound(self.c1, self.p, Decimal("10"), Decimal("5"))
        doc = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[{"product": self.p, "quantity": Decimal("11")}],
        )
        self.assertEqual(SalesOutbound.objects.count(), 1)
        self.assertEqual(doc.total_cost, Decimal("55.00"))   # 11 * 5
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("-1.000"))
        self.assertEqual(bal.amount, Decimal("-5.00"))

    def test_multiline_oversell_allowed(self):
        # 多行：一行正常、一行超卖，两行都过账（超卖行变负）
        post_inbound(self.c1, self.p, Decimal("5"), Decimal("10"))
        p2 = Product.objects.create(company=self.c1, code="P002", name="固化剂")
        post_inbound(self.c1, p2, Decimal("1"), Decimal("10"))
        doc = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[
                {"product": self.p, "quantity": Decimal("5")},   # 出清
                {"product": p2, "quantity": Decimal("99")},      # 超卖→负
            ],
        )
        self.assertEqual(SalesOutbound.objects.count(), 1)
        self.assertEqual(
            StockBalance.objects.get(company=self.c1, product=self.p).quantity, Decimal("0.000")
        )
        self.assertEqual(
            StockBalance.objects.get(company=self.c1, product=p2).quantity, Decimal("-98.000")
        )
