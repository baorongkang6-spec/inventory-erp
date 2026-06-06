"""采购入库过账集成测试。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance
from apps.masterdata.models import Product
from apps.purchasing.models import PurchaseInbound
from apps.purchasing.services import create_and_post_inbound


class InboundPostingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")

    def test_create_and_post_weighted_average(self):
        doc = create_and_post_inbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[
                {"product": self.p, "quantity": Decimal("100"), "unit_price": Decimal("10")},
                {"product": self.p, "quantity": Decimal("50"), "unit_price": Decimal("13")},
            ],
        )
        self.assertEqual(doc.doc_no, "RK-C1-20260605-001")
        self.assertEqual(doc.total_quantity, Decimal("150.000"))
        self.assertEqual(doc.total_amount, Decimal("1650.00"))
        self.assertEqual(doc.lines.count(), 2)

        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("150.000"))
        self.assertEqual(bal.amount, Decimal("1650.00"))
        self.assertEqual(bal.avg_price, Decimal("11.00"))

    def test_doc_no_increments_per_day(self):
        for _ in range(2):
            create_and_post_inbound(
                company=self.c1, user=None, doc_date=date(2026, 6, 5),
                lines=[{"product": self.p, "quantity": Decimal("1"), "unit_price": Decimal("1")}],
            )
        nos = list(PurchaseInbound.objects.order_by("doc_no").values_list("doc_no", flat=True))
        self.assertEqual(nos, ["RK-C1-20260605-001", "RK-C1-20260605-002"])


class InboundListFilterTests(TestCase):
    """FilteredListMixin（#9）：入库单列表按日期区间 + 关键字筛选。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from apps.masterdata.models import Supplier
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")
        cls.s1 = Supplier.objects.create(company=cls.c1, code="SUP1", name="甲供应商")
        cls.s2 = Supplier.objects.create(company=cls.c1, code="SUP2", name="乙供应商")
        create_and_post_inbound(company=cls.c1, user=None, doc_date=date(2026, 5, 1), supplier=cls.s1,
            lines=[{"product": cls.p, "quantity": Decimal("1"), "unit_price": Decimal("1")}])
        create_and_post_inbound(company=cls.c1, user=None, doc_date=date(2026, 6, 20), supplier=cls.s2,
            lines=[{"product": cls.p, "quantity": Decimal("1"), "unit_price": Decimal("1")}])
        U = get_user_model()
        cls.user = U.objects.create_superuser(username="root", password="x")

    def setUp(self):
        self.client.force_login(self.user)

    def _docnos(self, **params):
        resp = self.client.get("/purchasing/inbound/", params, SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 200)
        return {d.doc_no for d in resp.context["docs"]}

    def test_date_range_filters(self):
        got = self._docnos(**{"from": "2026-06-01", "to": "2026-06-30"})
        self.assertEqual(got, {"RK-C1-20260620-001"})

    def test_keyword_filters_by_supplier(self):
        got = self._docnos(q="乙供应商")
        self.assertEqual(got, {"RK-C1-20260620-001"})

    def test_no_filter_returns_all(self):
        self.assertEqual(len(self._docnos()), 2)
