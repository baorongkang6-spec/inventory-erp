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
