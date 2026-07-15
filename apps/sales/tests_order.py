"""销售订单 M18-2 测试。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase

from apps.core.models import Company
from apps.inventory.services import post_inbound
from apps.masterdata.models import Customer, Product
from apps.sales.models import SalesOrder, SalesOutbound
from apps.sales.order_services import (
    SalesOrderError,
    create_invoice_from_order,
    create_outbound_from_order,
    create_sales_order,
    qty_open_invoice,
    qty_open_ship,
    refresh_order_status,
)


class SalesOrderFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        post_inbound(cls.c1, cls.p, Decimal("100"), Decimal("10"), date=date(2026, 6, 1))
        U = get_user_model()
        cls.user = U.objects.create_user(username="so", password="x", can_view_all_companies=True)
        for app, code in (
            ("sales", "view_salesorder"), ("sales", "add_salesorder"),
            ("sales", "change_salesorder"), ("sales", "view_salesoutbound"),
            ("sales", "add_salesoutbound"), ("finance", "add_salesinvoice"),
            ("finance", "view_salesinvoice"), ("inventory", "view_amount"),
        ):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code))

    def _order(self, qty=Decimal("20"), untaxed=Decimal("2000")):
        return create_sales_order(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 1),
            customer=self.cust,
            lines=[{"product": self.p, "quantity": qty,
                    "amount_untaxed": untaxed, "tax_rate": Decimal("0.13")}])

    def test_create_order_and_progress(self):
        order = self._order()
        self.assertTrue(order.doc_no.startswith("SO-"))
        self.assertEqual(order.total_quantity, Decimal("20.000"))
        self.assertEqual(order.ship_status, SalesOrder.Progress.NONE)
        ln = order.lines.get()
        self.assertEqual(qty_open_ship(ln), Decimal("20.000"))

    def test_ship_then_invoice(self):
        order = self._order()
        ob = create_outbound_from_order(
            order=order, user=self.user, doc_date=date(2026, 7, 2),
            lines=[{"order_line": order.lines.get(), "quantity": Decimal("8")}])
        self.assertEqual(ob.sales_order_id, order.pk)
        self.assertEqual(ob.lines.get().order_line_id, order.lines.get().pk)
        order.refresh_from_db()
        self.assertEqual(order.ship_status, SalesOrder.Progress.PARTIAL)
        self.assertEqual(qty_open_ship(order.lines.get()), Decimal("12.000"))

        inv = create_invoice_from_order(
            order=order, user=self.user, doc_date=date(2026, 7, 3),
            lines=[{"order_line": order.lines.get(), "quantity": Decimal("8"),
                    "source_outbound_line": ob.lines.get()}])
        self.assertEqual(inv.sales_order_id, order.pk)
        self.assertEqual(inv.lines.get().order_line_id, order.lines.get().pk)
        self.assertEqual(inv.lines.get().source_outbound_line_id, ob.lines.get().pk)
        refresh_order_status(order)
        order.refresh_from_db()
        self.assertEqual(order.invoice_status, SalesOrder.Progress.PARTIAL)
        self.assertEqual(qty_open_invoice(order.lines.get()), Decimal("12.000"))

    def test_invoice_before_ship(self):
        order = self._order()
        inv = create_invoice_from_order(order=order, user=self.user, doc_date=date(2026, 7, 2))
        self.assertEqual(inv.lines.get().quantity, Decimal("20.000"))
        order.refresh_from_db()
        self.assertEqual(order.invoice_status, SalesOrder.Progress.FULL)
        self.assertEqual(order.ship_status, SalesOrder.Progress.NONE)
        # 发货仍可全量
        ob = create_outbound_from_order(order=order, user=self.user, doc_date=date(2026, 7, 3))
        self.assertEqual(ob.total_quantity, Decimal("20.000"))
        order.refresh_from_db()
        self.assertEqual(order.ship_status, SalesOrder.Progress.FULL)

    def test_cannot_overship(self):
        order = self._order(qty=Decimal("5"))
        create_outbound_from_order(
            order=order, user=self.user, doc_date=date(2026, 7, 2),
            lines=[{"order_line": order.lines.get(), "quantity": Decimal("5")}])
        with self.assertRaises(SalesOrderError):
            create_outbound_from_order(
                order=order, user=self.user, doc_date=date(2026, 7, 3),
                lines=[{"order_line": order.lines.get(), "quantity": Decimal("1")}])

    def test_outbound_without_order_still_works(self):
        from apps.sales.services import create_and_post_outbound
        doc = create_and_post_outbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 2),
            customer=self.cust,
            lines=[{"product": self.p, "quantity": Decimal("3"),
                    "amount_untaxed": Decimal("300"), "tax_rate": Decimal("0")}])
        self.assertIsNone(doc.sales_order_id)

    def test_list_and_detail_pages(self):
        order = self._order()
        self.client.force_login(self.user)
        r = self.client.get("/sales/orders/", SERVER_NAME="localhost")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, order.doc_no)
        r2 = self.client.get(f"/sales/orders/{order.pk}/", SERVER_NAME="localhost")
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "生成出库")
        self.assertContains(r2, "生成发票")


class SalesOrderBackfillTests(TestCase):
    """M18-4：未完成出库补单回挂。"""

    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        post_inbound(cls.c1, cls.p, Decimal("100"), Decimal("10"), date=date(2026, 6, 1))
        U = get_user_model()
        cls.user = U.objects.create_user(username="sobf", password="x", can_view_all_companies=True)
        for app, code in (
            ("sales", "view_salesorder"), ("sales", "add_salesorder"),
            ("sales", "view_salesoutbound"), ("sales", "add_salesoutbound"),
            ("finance", "add_salesinvoice"), ("finance", "view_salesinvoice"),
        ):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code))

    def test_backfill_outbound_links_without_changing_amounts(self):
        from apps.inventory.models import StockBalance
        from apps.sales.order_services import backfill_sales_order, sales_backfill_candidates
        from apps.sales.services import create_and_post_outbound

        bal_before = StockBalance.objects.get(company=self.c1, product=self.p)
        qty_before, cost_before = bal_before.quantity, bal_before.avg_price

        ob = create_and_post_outbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 2),
            customer=self.cust,
            lines=[{"product": self.p, "quantity": Decimal("5"),
                    "amount_untaxed": Decimal("500"), "tax_rate": Decimal("0.13")}])
        self.assertIsNone(ob.sales_order_id)
        taxed_before = ob.total_taxed

        cand = sales_backfill_candidates(self.c1)
        self.assertEqual(len(cand), 1)
        self.assertEqual(cand[0]["ob_count"], 1)

        order = backfill_sales_order(
            company=self.c1, user=self.user, customer=self.cust,
            outbound_ids=[ob.pk], invoice_ids=[])
        ob.refresh_from_db()
        self.assertEqual(ob.sales_order_id, order.pk)
        self.assertEqual(ob.lines.get().order_line_id, order.lines.get().pk)
        self.assertEqual(ob.total_taxed, taxed_before)
        self.assertEqual(order.total_quantity, Decimal("5.000"))
        self.assertEqual(order.ship_status, SalesOrder.Progress.FULL)
        self.assertEqual(order.invoice_status, SalesOrder.Progress.NONE)

        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, qty_before - Decimal("5.000"))
        # 补单后库存数量仍是出库后状态，平均成本不变
        self.assertEqual(bal.avg_price, cost_before)

        # 再次补同一出库应失败
        with self.assertRaises(SalesOrderError):
            backfill_sales_order(
                company=self.c1, user=self.user, customer=self.cust,
                outbound_ids=[ob.pk], invoice_ids=[])

    def test_backfill_pages(self):
        from apps.sales.services import create_and_post_outbound
        create_and_post_outbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 2),
            customer=self.cust,
            lines=[{"product": self.p, "quantity": Decimal("2"),
                    "amount_untaxed": Decimal("200"), "tax_rate": Decimal("0")}])
        self.client.force_login(self.user)
        r = self.client.get("/sales/orders/backfill/", SERVER_NAME="localhost")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "客户甲")
        r2 = self.client.get(f"/sales/orders/backfill/{self.cust.pk}/", SERVER_NAME="localhost")
        self.assertEqual(r2.status_code, 200)
        r3 = self.client.get("/sales/orders/progress/", SERVER_NAME="localhost")
        self.assertEqual(r3.status_code, 200)
