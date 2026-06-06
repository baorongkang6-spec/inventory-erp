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


class TaxInclusivePriceTests(TestCase):
    """含税单价录入：自动反算不含税/税额（M15）。"""

    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")

    def test_inbound_from_tax_inclusive_price(self):
        from apps.purchasing.services import create_and_post_inbound
        doc = create_and_post_inbound(company=self.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[{"product": self.p, "quantity": Decimal("10"),
                    "tax_inclusive_price": Decimal("113"), "tax_rate": Decimal("0.13")}])
        ln = doc.lines.first()
        self.assertEqual(ln.amount_taxed, Decimal("1130.00"))
        self.assertEqual(ln.amount_untaxed, Decimal("1000.00"))   # 1130/1.13
        self.assertEqual(ln.tax_amount, Decimal("130.00"))
        self.assertEqual(ln.amount, Decimal("1000.00"))           # 入库成本=不含税

    def test_explicit_amounts_win(self):
        from apps.purchasing.services import create_and_post_inbound
        # 手工改了金额（含税单价仅作参考）→ 用显式金额
        doc = create_and_post_inbound(company=self.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[{"product": self.p, "quantity": Decimal("10"), "tax_rate": Decimal("0.13"),
                    "tax_inclusive_price": Decimal("113"),
                    "amount_untaxed": Decimal("990"), "tax_amount": Decimal("128.70"),
                    "amount_taxed": Decimal("1118.70")}])
        ln = doc.lines.first()
        self.assertEqual(ln.amount_untaxed, Decimal("990.00"))
        self.assertEqual(ln.amount_taxed, Decimal("1118.70"))


class InboundEditTests(TestCase):
    """采购入库修改（M14）：冲正重过账、保留单号、可改性守卫。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.u = get_user_model().objects.create_user(username="op", password="x")

    def _today(self):
        from django.utils import timezone
        return timezone.localdate()

    def test_update_reposts_and_keeps_docno(self):
        from apps.purchasing.services import create_and_post_inbound, update_and_repost_inbound
        doc = create_and_post_inbound(company=self.c1, user=self.u, doc_date=self._today(),
            lines=[{"product": self.p, "quantity": Decimal("10"), "unit_price": Decimal("5")}])
        no = doc.doc_no
        update_and_repost_inbound(doc, user=self.u, doc_date=self._today(),
            lines=[{"product": self.p, "quantity": Decimal("20"), "unit_price": Decimal("6"),
                    "tax_rate": Decimal("0.13")}])
        doc.refresh_from_db()
        self.assertEqual(doc.doc_no, no)                  # 单号不变
        self.assertEqual(doc.total_quantity, Decimal("20.000"))
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("20.000"))
        self.assertEqual(bal.amount, Decimal("120.00"))

    def test_block_reasons(self):
        from datetime import date
        from apps.purchasing.services import create_and_post_inbound, inbound_edit_block_reason
        doc = create_and_post_inbound(company=self.c1, user=self.u, doc_date=self._today(),
            lines=[{"product": self.p, "quantity": Decimal("1"), "unit_price": Decimal("1")}])
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(username="x2", password="x")
        self.assertIsNone(inbound_edit_block_reason(doc, self.u, self._today(), False))
        self.assertIsNotNone(inbound_edit_block_reason(doc, other, self._today(), False))  # 非本人
        self.assertIsNone(inbound_edit_block_reason(doc, other, self._today(), True))       # 管理员
        old = create_and_post_inbound(company=self.c1, user=self.u, doc_date=date(2026, 1, 1),
            lines=[{"product": self.p, "quantity": Decimal("1"), "unit_price": Decimal("1")}])
        self.assertIn("跨月", inbound_edit_block_reason(old, self.u, self._today(), True))


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

    def test_export_xlsx_respects_filter(self):
        from io import BytesIO
        from openpyxl import load_workbook
        resp = self.client.get("/purchasing/inbound/", {"q": "乙供应商", "export": "xlsx"},
                               SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        wb = load_workbook(BytesIO(resp.content))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        self.assertEqual(rows[0][0], "采购入库")           # 标题行
        # 表头行（带样式后位于标题+元信息之后）
        hdr_idx = next(i for i, r in enumerate(rows) if r[0] == "单据编号")
        data = rows[hdr_idx + 1:]
        self.assertEqual(len(data), 1)                     # 筛选后仅 1 行
        self.assertEqual(data[0][0], "RK-C1-20260620-001")
