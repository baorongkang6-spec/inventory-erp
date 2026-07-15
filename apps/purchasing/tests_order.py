"""采购订单 M18-3 测试。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase

from apps.core.models import Company
from apps.masterdata.models import Product, Supplier
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.order_services import (
    PurchaseOrderError,
    create_inbound_from_order,
    create_invoice_from_order,
    create_purchase_order,
    qty_open_invoice,
    qty_open_receive,
    refresh_order_status,
)


class PurchaseOrderFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        U = get_user_model()
        cls.user = U.objects.create_user(username="po", password="x", can_view_all_companies=True)
        for app, code in (
            ("purchasing", "view_purchaseorder"), ("purchasing", "add_purchaseorder"),
            ("purchasing", "change_purchaseorder"), ("purchasing", "view_purchaseinbound"),
            ("purchasing", "add_purchaseinbound"), ("finance", "add_purchaseinvoice"),
            ("finance", "view_purchaseinvoice"), ("inventory", "view_amount"),
        ):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code))

    def _order(self, qty=Decimal("20"), untaxed=Decimal("2000")):
        return create_purchase_order(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 1),
            supplier=self.sup,
            lines=[{"product": self.p, "quantity": qty,
                    "amount_untaxed": untaxed, "tax_rate": Decimal("0.13")}])

    def test_create_order_and_progress(self):
        order = self._order()
        self.assertTrue(order.doc_no.startswith("PO-"))
        self.assertEqual(order.total_quantity, Decimal("20.000"))
        self.assertEqual(order.receive_status, PurchaseOrder.Progress.NONE)
        ln = order.lines.get()
        self.assertEqual(qty_open_receive(ln), Decimal("20.000"))

    def test_receive_then_invoice(self):
        order = self._order()
        ib = create_inbound_from_order(
            order=order, user=self.user, doc_date=date(2026, 7, 2),
            lines=[{"order_line": order.lines.get(), "quantity": Decimal("8")}])
        self.assertEqual(ib.purchase_order_id, order.pk)
        self.assertEqual(ib.lines.get().order_line_id, order.lines.get().pk)
        order.refresh_from_db()
        self.assertEqual(order.receive_status, PurchaseOrder.Progress.PARTIAL)
        self.assertEqual(qty_open_receive(order.lines.get()), Decimal("12.000"))

        inv = create_invoice_from_order(
            order=order, user=self.user, doc_date=date(2026, 7, 3),
            lines=[{"order_line": order.lines.get(), "quantity": Decimal("8"),
                    "source_inbound_line": ib.lines.get()}])
        self.assertEqual(inv.purchase_order_id, order.pk)
        self.assertEqual(inv.lines.get().order_line_id, order.lines.get().pk)
        self.assertEqual(inv.lines.get().source_inbound_line_id, ib.lines.get().pk)
        refresh_order_status(order)
        order.refresh_from_db()
        self.assertEqual(order.invoice_status, PurchaseOrder.Progress.PARTIAL)
        self.assertEqual(qty_open_invoice(order.lines.get()), Decimal("12.000"))

    def test_invoice_before_receive(self):
        order = self._order()
        inv = create_invoice_from_order(order=order, user=self.user, doc_date=date(2026, 7, 2))
        self.assertEqual(inv.lines.get().quantity, Decimal("20.000"))
        order.refresh_from_db()
        self.assertEqual(order.invoice_status, PurchaseOrder.Progress.FULL)
        self.assertEqual(order.receive_status, PurchaseOrder.Progress.NONE)
        ib = create_inbound_from_order(order=order, user=self.user, doc_date=date(2026, 7, 3))
        self.assertEqual(ib.total_quantity, Decimal("20.000"))
        order.refresh_from_db()
        self.assertEqual(order.receive_status, PurchaseOrder.Progress.FULL)

    def test_cannot_over_receive(self):
        order = self._order(qty=Decimal("5"))
        create_inbound_from_order(
            order=order, user=self.user, doc_date=date(2026, 7, 2),
            lines=[{"order_line": order.lines.get(), "quantity": Decimal("5")}])
        with self.assertRaises(PurchaseOrderError):
            create_inbound_from_order(
                order=order, user=self.user, doc_date=date(2026, 7, 3),
                lines=[{"order_line": order.lines.get(), "quantity": Decimal("1")}])

    def test_inbound_without_order_still_works(self):
        from apps.purchasing.services import create_and_post_inbound
        doc = create_and_post_inbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 2),
            supplier=self.sup,
            lines=[{"product": self.p, "quantity": Decimal("3"),
                    "amount_untaxed": Decimal("300"), "tax_rate": Decimal("0")}])
        self.assertIsNone(doc.purchase_order_id)

    def test_list_and_detail_pages(self):
        order = self._order()
        self.client.force_login(self.user)
        r = self.client.get("/purchasing/orders/", SERVER_NAME="localhost")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, order.doc_no)
        r2 = self.client.get(f"/purchasing/orders/{order.pk}/", SERVER_NAME="localhost")
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "生成入库")
        self.assertContains(r2, "生成发票")
