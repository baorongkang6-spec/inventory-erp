"""P1：付款可选挂采购订单（预付）+ 无票可保存。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Company
from apps.finance.models import BankAccount, BankJournal, Payment
from apps.finance.services import allocate_payment, create_payment, create_purchase_invoice
from apps.masterdata.models import Product, Supplier
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.order_services import create_purchase_order, refresh_order_status


class PaymentPrepaidOrderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.sup2 = Supplier.objects.create(company=cls.c1, code="S2", name="供应商乙")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")
        cls.order = create_purchase_order(
            company=cls.c1, user=None, doc_date=date(2026, 8, 1), supplier=cls.sup,
            lines=[{"product": cls.p, "quantity": Decimal("10"), "unit_price": Decimal("100"),
                    "tax_rate": Decimal("0.13")}],
        )
        cls.user = get_user_model().objects.create_user(
            username="payop", password="x", can_view_all_companies=True)
        from django.contrib.auth.models import Permission
        for codename in ("add_payment", "view_payment", "add_paymentallocation"):
            cls.user.user_permissions.add(
                Permission.objects.get(codename=codename, content_type__app_label="finance"))

    def test_create_payment_with_order_no_invoice(self):
        pay = create_payment(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("500"),
            summary="预付", purchase_order=self.order,
        )
        self.assertEqual(pay.purchase_order_id, self.order.pk)
        self.assertEqual(pay.unallocated, Decimal("500.00"))
        self.assertEqual(BankJournal.objects.filter(company=self.c1).count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PurchaseOrder.Progress.PARTIAL)

    def test_reject_order_supplier_mismatch(self):
        with self.assertRaises(ValueError):
            create_payment(
                company=self.c1, user=None, doc_date=date(2026, 8, 5),
                bank_account=self.acc, supplier=self.sup2, amount=Decimal("100"),
                purchase_order=self.order,
            )

    def test_reject_void_order(self):
        self.order.status = PurchaseOrder.Status.VOID
        self.order.save(update_fields=["status"])
        with self.assertRaises(ValueError):
            create_payment(
                company=self.c1, user=None, doc_date=date(2026, 8, 5),
                bank_account=self.acc, supplier=self.sup, amount=Decimal("100"),
                purchase_order=self.order,
            )

    def test_allocate_after_invoice_clears_prepaid(self):
        pay = create_payment(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("1130"),
            purchase_order=self.order,
        )
        inv = create_purchase_invoice(
            company=self.c1, user=None, doc_date=date(2026, 8, 10), supplier=self.sup,
            purchase_order=self.order,
            lines=[{"product": self.p, "description": "", "amount_untaxed": Decimal("1000"),
                    "tax_rate": Decimal("0.13"), "order_line": self.order.lines.first()}],
        )
        allocate_payment(payment=pay, allocations=[{"invoice": inv, "amount": Decimal("1130")}])
        pay.refresh_from_db()
        inv.refresh_from_db()
        self.assertEqual(pay.unallocated, Decimal("0.00"))
        self.assertEqual(inv.outstanding, Decimal("0.00"))
        refresh_order_status(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PurchaseOrder.Progress.FULL)

    def test_payment_create_view_with_order_query(self):
        client = Client()
        client.force_login(self.user)
        # 切到 C1 账套
        session = client.session
        session["active_company_id"] = self.c1.pk
        session.save()
        r = client.get(reverse("payment_create") + f"?order={self.order.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, self.order.doc_no)
        self.assertContains(r, 'value="2026-08-01"')  # 挂订单时付款日默认订单日
        r2 = client.post(reverse("payment_create"), {
            "doc_date": "2026-08-05",
            "method": f"bank:{self.acc.pk}",
            "supplier": self.sup.pk,
            "purchase_order": self.order.pk,
            "amount": "200.00",
            "summary": "预付挂单",
        })
        self.assertEqual(r2.status_code, 302)
        pay = Payment.objects.get(company=self.c1, amount=Decimal("200.00"))
        self.assertEqual(pay.purchase_order_id, self.order.pk)
