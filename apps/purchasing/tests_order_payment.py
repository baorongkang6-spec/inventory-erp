"""P2：采购订单详情/进度显示已付与预付未核销。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from apps.core.models import Company
from apps.finance.models import BankAccount
from apps.finance.services import allocate_payment, create_payment, create_purchase_invoice
from apps.masterdata.models import Product, Supplier
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.order_services import (
    create_purchase_order,
    order_payment_summary,
    purchase_order_progress_rows,
    refresh_order_status,
)


class PurchaseOrderPaymentSummaryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")
        cls.order = create_purchase_order(
            company=cls.c1, user=None, doc_date=date(2026, 8, 1), supplier=cls.sup,
            lines=[{"product": cls.p, "quantity": Decimal("10"), "unit_price": Decimal("100"),
                    "tax_rate": Decimal("0.13")}],
        )
        # 合同含税 1130
        cls.user = get_user_model().objects.create_user(
            username="po2", password="x", can_view_all_companies=True)
        for app, code in (
            ("purchasing", "view_purchaseorder"),
            ("finance", "view_payment"),
            ("finance", "add_payment"),
        ):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=code))

    def test_prepaid_shows_in_summary(self):
        create_payment(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("500"),
            purchase_order=self.order,
        )
        s = order_payment_summary(self.order)
        self.assertEqual(s["amount_contract"], Decimal("1130.00"))
        self.assertEqual(s["amount_prepaid"], Decimal("500.00"))
        self.assertEqual(s["amount_settled"], Decimal("0.00"))
        self.assertEqual(s["amount_paid"], Decimal("500.00"))
        self.assertEqual(s["amount_unpaid"], Decimal("630.00"))
        self.assertEqual(len(s["payments"]), 1)
        refresh_order_status(self.order)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, PurchaseOrder.Progress.PARTIAL)

    def test_allocate_moves_prepaid_to_settled(self):
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
        s = order_payment_summary(self.order)
        self.assertEqual(s["amount_prepaid"], Decimal("0.00"))
        self.assertEqual(s["amount_settled"], Decimal("1130.00"))
        self.assertEqual(s["amount_paid"], Decimal("1130.00"))
        self.assertEqual(s["amount_unpaid"], Decimal("0.00"))

    def test_progress_rows_include_amounts(self):
        create_payment(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("200"),
            purchase_order=self.order,
        )
        rows = purchase_order_progress_rows(self.c1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_prepaid"], Decimal("200.00"))
        self.assertEqual(rows[0]["amount_paid"], Decimal("200.00"))

    def test_detail_page_shows_prepaid(self):
        create_payment(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("300"),
            summary="预付货款", purchase_order=self.order,
        )
        client = Client()
        client.force_login(self.user)
        session = client.session
        session["active_company_id"] = self.c1.pk
        session.save()
        r = client.get(reverse("purchase_order_detail", args=[self.order.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "预付未核销")
        self.assertContains(r, "300.00")
        self.assertContains(r, "关联付款")
        self.assertContains(r, "登记预付/付款")
        self.assertContains(r, f"order={self.order.pk}")
        r2 = client.get(reverse("purchase_order_progress"))
        self.assertEqual(r2.status_code, 200)
        self.assertContains(r2, "预付未核销")
        self.assertContains(r2, "300.00")
        self.assertContains(r2, f"order={self.order.pk}")

    def test_list_prepaid_filter_and_pay_link(self):
        other = create_purchase_order(
            company=self.c1, user=None, doc_date=date(2026, 8, 2), supplier=self.sup,
            lines=[{"product": self.p, "quantity": Decimal("1"), "unit_price": Decimal("50"),
                    "tax_rate": Decimal("0")}],
        )
        create_payment(
            company=self.c1, user=None, doc_date=date(2026, 8, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("300"),
            purchase_order=self.order,
        )
        client = Client()
        client.force_login(self.user)
        session = client.session
        session["active_company_id"] = self.c1.pk
        session.save()
        r_all = client.get(reverse("purchase_order_list"))
        self.assertEqual(r_all.status_code, 200)
        self.assertContains(r_all, self.order.doc_no)
        self.assertContains(r_all, other.doc_no)
        self.assertContains(r_all, "仅有预付未核销")
        r_pre = client.get(reverse("purchase_order_list") + "?prepaid=1")
        self.assertEqual(r_pre.status_code, 200)
        self.assertContains(r_pre, self.order.doc_no)
        self.assertNotContains(r_pre, other.doc_no)
        self.assertContains(r_pre, "300.00")
        self.assertContains(r_pre, f"{reverse('payment_create')}?order={self.order.pk}")
