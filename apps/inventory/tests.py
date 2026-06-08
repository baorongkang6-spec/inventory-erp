"""移动加权平均算法测试（M1 核心）。手工演算对照。"""

from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance, StockMove
from apps.inventory.services import (
    InsufficientStockError,
    post_inbound,
    post_outbound,
)
from apps.masterdata.models import Product


def D(s):
    return Decimal(s)


class StockReportPermissionTests(TestCase):
    """库存报表的金额列受 view_amount 控制（SPEC §9.2）。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        U = get_user_model()
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")
        post_inbound(cls.c1, cls.p, Decimal("10"), Decimal("5"))  # 50.00

        def mk(name, perms, view_all=True):
            u = U.objects.create_user(username=name, password="x", can_view_all_companies=view_all)
            for dotted in perms:
                app, code = dotted.split(".")
                u.user_permissions.add(
                    Permission.objects.get(content_type__app_label=app, codename=code)
                )
            return u

        cls.qty_only = mk("qtyonly", ["inventory.view_stockbalance"])
        cls.with_amount = mk("withamt", ["inventory.view_stockbalance", "inventory.view_amount"])

    def test_quantity_only_user_sees_no_amount(self):
        self.client.force_login(self.qty_only)
        resp = self.client.get("/inventory/stock/", SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "10.000")
        self.assertNotContains(resp, "结存金额")

    def test_amount_user_sees_amount(self):
        self.client.force_login(self.with_amount)
        resp = self.client.get("/inventory/stock/", SERVER_NAME="localhost")
        self.assertContains(resp, "结存金额")
        self.assertContains(resp, "50.00")

    def test_keyword_filter_and_ledger_link(self):
        Product.objects.create(company=self.c1, code="P002", name="固化剂")
        post_inbound(self.c1, Product.objects.get(company=self.c1, code="P002"),
                     Decimal("5"), Decimal("2"))
        self.client.force_login(self.with_amount)
        resp = self.client.get("/inventory/stock/?q=固化", SERVER_NAME="localhost")
        self.assertContains(resp, "固化剂")
        self.assertNotContains(resp, "环氧树脂")
        self.assertContains(resp, "明细账")        # 下钻按钮存在


class MovingAverageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def bal(self):
        return StockBalance.objects.get(company=self.c1, product=self.p)

    def test_basic_weighted_average(self):
        # 入 100@10 → 100, 1000.00, 10.00
        post_inbound(self.c1, self.p, D("100"), D("10"))
        b = self.bal()
        self.assertEqual(b.quantity, D("100.000"))
        self.assertEqual(b.amount, D("1000.00"))
        self.assertEqual(b.avg_price, D("10.00"))

        # 入 50@13 → 150, 1650.00, 11.00
        post_inbound(self.c1, self.p, D("50"), D("13"))
        b = self.bal()
        self.assertEqual(b.quantity, D("150.000"))
        self.assertEqual(b.amount, D("1650.00"))
        self.assertEqual(b.avg_price, D("11.00"))

        # 出 60 → 成本 660.00；结存 90, 990.00, 11.00
        m = post_outbound(self.c1, self.p, D("60"))
        self.assertEqual(m.unit_price, D("11.00"))
        self.assertEqual(m.amount, D("660.00"))
        b = self.bal()
        self.assertEqual(b.quantity, D("90.000"))
        self.assertEqual(b.amount, D("990.00"))
        self.assertEqual(b.avg_price, D("11.00"))

        # 出 90（清零）→ 成本 990.00；结存 0/0/0
        m = post_outbound(self.c1, self.p, D("90"))
        self.assertEqual(m.amount, D("990.00"))
        b = self.bal()
        self.assertEqual(b.quantity, D("0.000"))
        self.assertEqual(b.amount, D("0.00"))
        self.assertEqual(b.avg_price, D("0.00"))

    def test_rounding_clears_to_zero(self):
        # 制造除不尽的均价，验证清零无残值
        post_inbound(self.c1, self.p, D("3"), D("10"))     # 30.00
        post_inbound(self.c1, self.p, D("1"), D("10.01"))  # 40.01, qty4, avg 10.0025→10.00
        self.assertEqual(self.bal().avg_price, D("10.00"))
        post_outbound(self.c1, self.p, D("1"))              # 成本 10.00 → 30.01, qty3
        b = self.bal()
        self.assertEqual(b.amount, D("30.01"))
        post_outbound(self.c1, self.p, D("3"))              # 清零，成本=30.01
        b = self.bal()
        self.assertEqual(b.quantity, D("0.000"))
        self.assertEqual(b.amount, D("0.00"))

    def test_allow_negative_inventory(self):
        # 允许负库存：出库可超过结存，数量/金额变负，按当前均价结转成本
        post_inbound(self.c1, self.p, D("10"), D("5"))   # 10@5 = 50
        post_outbound(self.c1, self.p, D("11"))          # 成本 11*5=55
        b = self.bal()
        self.assertEqual(b.quantity, D("-1.000"))
        self.assertEqual(b.amount, D("-5.00"))

    def test_outbound_from_empty_allowed(self):
        # 零库存出库 → 负库存，无成本基准成本按 0
        post_outbound(self.c1, self.p, D("1"))
        b = self.bal()
        self.assertEqual(b.quantity, D("-1.000"))
        self.assertEqual(b.amount, D("0.00"))

    def test_fractional_quantity(self):
        post_inbound(self.c1, self.p, D("1.5"), D("10"))
        b = self.bal()
        self.assertEqual(b.quantity, D("1.500"))
        self.assertEqual(b.amount, D("15.00"))

    def test_move_snapshots_recorded(self):
        post_inbound(self.c1, self.p, D("100"), D("10"))
        post_outbound(self.c1, self.p, D("40"))
        moves = list(StockMove.objects.filter(product=self.p))
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[0].direction, "in")
        self.assertEqual(moves[0].balance_quantity, D("100.000"))
        self.assertEqual(moves[1].direction, "out")
        self.assertEqual(moves[1].balance_quantity, D("60.000"))
        self.assertEqual(moves[1].balance_amount, D("600.00"))

    def test_company_isolation(self):
        # C2 自己的同名商品，独立结存
        p2 = Product.objects.create(company=self.c2, code="P1", name="货A-C2")
        post_inbound(self.c1, self.p, D("100"), D("10"))
        post_inbound(self.c2, p2, D("5"), D("99"))
        self.assertEqual(self.bal().quantity, D("100.000"))
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p2).amount, D("495.00")
        )


class StockDrilldownTests(TestCase):
    """库存两级下钻（M9-1）：商品余额表 + 商品明细账（带日期区间与期初/期末）。"""

    @classmethod
    def setUpTestData(cls):
        from datetime import date
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")
        post_inbound(cls.c1, cls.p, D("100"), D("10"), date=date(2026, 5, 20))  # 期初前
        post_inbound(cls.c1, cls.p, D("50"), D("10"), date=date(2026, 6, 10))   # 本期入 500
        post_outbound(cls.c1, cls.p, D("30"), date=date(2026, 6, 15))           # 本期出 300

    def test_products_balance(self):
        from datetime import date
        from apps.opening.reports import stock_products_balance
        rows = stock_products_balance(self.c1, date(2026, 6, 1), date(2026, 6, 30))
        r = rows[0]
        self.assertEqual(r["opening"], D("1000.00"))   # 100@10
        self.assertEqual(r["income"], D("500.00"))     # 50@10
        self.assertEqual(r["outgo"], D("300.00"))      # 30@10
        self.assertEqual(r["ending"], D("1200.00"))
        self.assertEqual(r["ending_qty"], D("120.000"))

    def test_ledger_opening_closing_and_empty_period(self):
        from datetime import date
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        U = get_user_model()
        u = U.objects.create_user(username="v", password="x", can_view_all_companies=True)
        for code in ("view_stockbalance", "view_amount"):
            u.user_permissions.add(Permission.objects.get(
                content_type__app_label="inventory", codename=code))
        self.client.force_login(u)
        # 本期：期初结存 100/1000，期末 120/1200
        r = self.client.get(f"/inventory/ledger/?product={self.p.pk}&from=2026-06-01&to=2026-06-30",
                            SERVER_NAME="localhost")
        self.assertEqual(r.context["open_qty"], D("100.000"))
        self.assertEqual(r.context["close_qty"], D("120.000"))
        self.assertEqual(r.context["close_amount"], D("1200.00"))
        # 未来空区间：无流水，期初=期末=120/1200
        r2 = self.client.get(f"/inventory/ledger/?product={self.p.pk}&from=2099-01-01&to=2099-12-31",
                            SERVER_NAME="localhost")
        self.assertEqual(len(r2.context["rows"]), 0)
        self.assertEqual(r2.context["open_qty"], r2.context["close_qty"])
        self.assertEqual(r2.context["close_qty"], D("120.000"))


class StockMoveDateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def test_move_uses_business_date(self):
        from datetime import date
        m = post_inbound(self.c1, self.p, D("10"), D("5"), date=date(2026, 6, 1))
        self.assertEqual(m.date, date(2026, 6, 1))
        out = post_outbound(self.c1, self.p, D("3"), date=date(2026, 6, 2))
        self.assertEqual(out.date, date(2026, 6, 2))
