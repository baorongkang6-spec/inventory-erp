"""P4：背书挂采购订单；销售收款挂销售订单（预收）。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Company
from apps.finance.models import BankAccount, NoteSettlement, Receipt
from apps.finance.services import (
    create_note_receivable,
    create_receipt,
    create_sales_invoice,
    endorse_receivable_against_purchase,
)
from apps.masterdata.models import Customer, Product, Supplier
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.order_services import create_purchase_order, order_payment_summary
from apps.sales.models import SalesOrder
from apps.sales.order_services import create_sales_order, order_receipt_summary


class NoteEndorsePrepaidOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")
        cls.order = create_purchase_order(
            company=cls.c1, user=None, doc_date=date(2026, 8, 1), supplier=cls.sup,
            lines=[{"product": cls.p, "quantity": Decimal("10"), "unit_price": Decimal("100"),
                    "tax_rate": Decimal("0.13")}],
        )
        cls.note = create_note_receivable(
            company=cls.c1, user=None, draw_date=date(2026, 8, 1),
            amount=Decimal("500"), note_no="N001", due_date=date(2026, 12, 1),
        )

    def test_endorse_prepaid_without_invoice(self):
        endorse_receivable_against_purchase(
            note=self.note, allocations=[], user=None,
            purchase_order=self.order, prepaid_amount=Decimal("500"),
        )
        self.note.refresh_from_db()
        self.assertEqual(self.note.unused, Decimal("0.00"))
        ns = NoteSettlement.objects.get(note_id=self.note.pk, invoice_id__isnull=True)
        self.assertEqual(ns.purchase_order_id, self.order.pk)
        self.assertEqual(ns.amount, Decimal("500.00"))
        s = order_payment_summary(self.order)
        self.assertEqual(s["amount_prepaid"], Decimal("500.00"))
        self.assertEqual(len(s["note_prepaids"]), 1)


class ReceiptSalesOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")
        cls.order = create_sales_order(
            company=cls.c1, user=None, doc_date=date(2026, 8, 1), customer=cls.cust,
            lines=[{"product": cls.p, "quantity": Decimal("10"), "unit_price": Decimal("100"),
                    "tax_rate": Decimal("0.13")}],
        )
        cls.user = get_user_model().objects.create_user(
            username="recop", password="x", can_view_all_companies=True)
        for app, code in (
            ("sales", "view_salesorder"),
            ("finance", "add_receipt"),
            ("finance", "view_receipt"),
        ):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code))

    def test_create_receipt_with_order(self):
        rec = create_receipt(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, customer=self.cust, amount=Decimal("400"),
            sales_order=self.order,
        )
        self.assertEqual(rec.sales_order_id, self.order.pk)
        s = order_receipt_summary(self.order)
        self.assertEqual(s["amount_prepaid"], Decimal("400.00"))
        self.assertEqual(s["amount_received"], Decimal("400.00"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.receipt_status, SalesOrder.Progress.PARTIAL)

    def test_reject_customer_mismatch(self):
        other = Customer.objects.create(company=self.c1, code="K2", name="客户乙")
        with self.assertRaises(ValueError):
            create_receipt(
                company=self.c1, user=None, doc_date=date(2026, 8, 5),
                bank_account=self.acc, customer=other, amount=Decimal("100"),
                sales_order=self.order,
            )

    def test_list_filter_and_detail_link(self):
        create_receipt(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, customer=self.cust, amount=Decimal("200"),
            sales_order=self.order,
        )
        client = Client()
        client.force_login(self.user)
        session = client.session
        session["active_company_id"] = self.c1.pk
        session.save()
        r = client.get(reverse("order_list") + "?prepaid=1")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.order.doc_no)
        self.assertContains(r, "200.00")
        r2 = client.get(reverse("order_detail", args=[self.order.pk]))
        self.assertContains(r2, "登记预收/收款")
        self.assertContains(r2, f"order={self.order.pk}")
        r3 = client.get(reverse("receipt_create") + f"?order={self.order.pk}")
        self.assertEqual(r3.status_code, 200)
        self.assertContains(r3, self.order.doc_no)
