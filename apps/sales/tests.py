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


class OutboundListTotalsTests(TestCase):
    """销售出库列表：不含税售额列 + 底部合计行。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")
        post_inbound(cls.c1, cls.p, Decimal("100"), Decimal("10"))
        # 两张出库单，各带不含税售额（13% 税）
        for _ in range(2):
            create_and_post_outbound(
                company=cls.c1, user=None, doc_date=date(2026, 6, 5),
                lines=[{"product": cls.p, "quantity": Decimal("10"),
                        "amount_untaxed": Decimal("2000"), "tax_rate": Decimal("0.13")}])
        U = get_user_model()
        cls.user = U.objects.create_user(username="sales", password="x", can_view_all_companies=True)
        for code in ("view_salesoutbound", "view_amount"):
            app = "inventory" if code == "view_amount" else "sales"
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code))

    def test_list_shows_untaxed_and_totals(self):
        self.client.force_login(self.user)
        resp = self.client.get("/sales/outbound/", SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "不含税售额")
        t = resp.context["totals"]
        self.assertEqual(t["untaxed"], Decimal("4000.00"))            # 2 × 2000
        self.assertEqual(t["taxed"], Decimal("4520.00"))             # 2 × 2260
        self.assertEqual(t["cost"], Decimal("200.00"))              # 2 × (10件@10)
        self.assertContains(resp, "合计")


class OutboundListVoidButtonTests(TestCase):
    """销售出库列表「作废」快捷按钮。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        post_inbound(cls.c1, cls.p, Decimal("100"), Decimal("10"))
        cls.doc = create_and_post_outbound(
            company=cls.c1, user=None, doc_date=date(2026, 6, 5),
            lines=[{"product": cls.p, "quantity": Decimal("10")}])
        U = get_user_model()
        cls.user = U.objects.create_user(username="s", password="x", can_view_all_companies=True)
        for code in ("view_salesoutbound", "void_salesoutbound"):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label="sales", codename=code))

    def test_list_shows_void_button_and_voids(self):
        from apps.sales.models import SalesOutbound
        self.client.force_login(self.user)
        lst = self.client.get("/sales/outbound/", SERVER_NAME="localhost")
        self.assertContains(lst, f"/sales/outbound/{self.doc.pk}/void/")
        resp = self.client.post(f"/sales/outbound/{self.doc.pk}/void/",
                                SERVER_NAME="localhost", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, SalesOutbound.Status.VOID)


class OutboundDeleteTests(TestCase):
    """销售出库硬删除（安全条件下）：反冲库存、彻底移除；非安全场景拦截。对称采购入库。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.p2 = Product.objects.create(company=cls.c1, code="P002", name="货B")
        cls.u = get_user_model().objects.create_user(
            username="op", password="x", can_view_all_companies=True)

    def _today(self):
        from django.utils import timezone
        return timezone.localdate()

    def _outbound(self, product=None, qty="10"):
        product = product or self.p
        post_inbound(self.c1, product, Decimal("100"), Decimal("10"))
        return create_and_post_outbound(
            company=self.c1, user=self.u, doc_date=self._today(),
            lines=[{"product": product, "quantity": Decimal(qty)}])

    def test_delete_latest_outbound_reverses_stock(self):
        from apps.inventory.models import StockMove
        from apps.sales.services import delete_sales_outbound
        doc = self._outbound(qty="10")          # 入 100 出 10 → 结存 90
        self.assertEqual(StockBalance.objects.get(company=self.c1, product=self.p).quantity,
                         Decimal("90.000"))
        out_mv_ids = list(doc.lines.values_list("stock_move_id", flat=True))
        delete_sales_outbound(doc, user=self.u, today=self._today(), is_manager=False)
        self.assertEqual(SalesOutbound.objects.filter(pk=doc.pk).count(), 0)   # 单据没了
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("100.000"))                     # 出库被反冲，库存还原
        self.assertEqual(bal.amount, Decimal("1000.00"))
        # 出库流水（原始+反冲）均无残留
        self.assertFalse(StockMove.objects.filter(pk__in=out_mv_ids).exists())

    def test_delete_blocked_when_later_movement_exists(self):
        from apps.sales.services import delete_sales_outbound, outbound_delete_block_reason
        doc = self._outbound(qty="10")
        post_inbound(self.c1, self.p, Decimal("5"), Decimal("6"))   # 同商品后续又入库
        self.assertIsNotNone(outbound_delete_block_reason(doc, self.u, self._today(), False))
        with self.assertRaises(Exception):
            delete_sales_outbound(doc, user=self.u, today=self._today(), is_manager=False)

    def test_other_product_later_movement_does_not_block(self):
        from apps.sales.services import outbound_delete_block_reason
        doc = self._outbound(product=self.p, qty="10")
        post_inbound(self.c1, self.p2, Decimal("3"), Decimal("2"))   # 别的商品，不影响
        self.assertIsNone(outbound_delete_block_reason(doc, self.u, self._today(), False))

    def test_delete_blocked_when_invoiced(self):
        from apps.finance.services import create_sales_invoice
        from apps.masterdata.models import Customer
        from apps.sales.services import outbound_delete_block_reason
        cust = Customer.objects.create(company=self.c1, code="K1", name="客户甲")
        post_inbound(self.c1, self.p, Decimal("100"), Decimal("10"))
        doc = create_and_post_outbound(
            company=self.c1, user=self.u, doc_date=self._today(), customer=cust,
            lines=[{"product": self.p, "quantity": Decimal("10")}])
        ln = doc.lines.first()
        create_sales_invoice(
            company=self.c1, user=self.u, doc_date=self._today(), customer=cust,
            lines=[{"product": self.p, "description": "", "amount_untaxed": Decimal("100"),
                    "tax_rate": Decimal("0"), "source_outbound_line": ln}])
        self.assertIn("发票", outbound_delete_block_reason(doc, self.u, self._today(), False))

    def test_delete_view(self):
        doc = self._outbound()
        self.client.force_login(self.u)
        from django.contrib.auth.models import Permission
        for code in ("add_salesoutbound", "view_salesoutbound"):
            self.u.user_permissions.add(
                Permission.objects.get(content_type__app_label="sales", codename=code))
        r = self.client.post(f"/sales/outbound/{doc.pk}/delete/",
                             SERVER_NAME="localhost", follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SalesOutbound.objects.filter(pk=doc.pk).count(), 0)
