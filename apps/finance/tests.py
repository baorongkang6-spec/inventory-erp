"""资金往来测试：采购发票含税换算与应付产生。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.finance.models import BankAccount, BankJournal
from apps.finance.services import (
    SettlementError,
    allocate_payment,
    compute_tax,
    create_payment,
    create_purchase_invoice,
)
from apps.masterdata.models import Product, Supplier


class PurchaseInvoiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def test_compute_tax(self):
        tax, taxed = compute_tax(Decimal("1000"), Decimal("0.13"))
        self.assertEqual(tax, Decimal("130.00"))
        self.assertEqual(taxed, Decimal("1130.00"))

    def test_create_invoice_produces_payable(self):
        inv = create_purchase_invoice(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), supplier=self.sup,
            invoice_no="FP001",
            lines=[
                {"product": self.p, "description": "货A", "amount_untaxed": Decimal("1000"),
                 "tax_rate": Decimal("0.13")},
                {"product": None, "description": "运费", "amount_untaxed": Decimal("100"),
                 "tax_rate": Decimal("0.09")},
            ],
        )
        self.assertEqual(inv.doc_no, "CGF-C1-20260605-001")
        self.assertEqual(inv.amount_untaxed, Decimal("1100.00"))
        self.assertEqual(inv.tax_amount, Decimal("139.00"))   # 130 + 9
        self.assertEqual(inv.amount_taxed, Decimal("1239.00"))
        self.assertEqual(inv.settled_amount, Decimal("0.00"))
        self.assertEqual(inv.outstanding, Decimal("1239.00"))  # 应付余额
        self.assertEqual(inv.lines.count(), 2)


class PaymentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")

    def test_payment_auto_generates_bank_journal(self):
        pay = create_payment(
            company=self.c1, user=None, doc_date=date(2026, 6, 5),
            bank_account=self.acc, supplier=self.sup, amount=Decimal("500"), summary="付货款",
        )
        self.assertEqual(pay.doc_no, "FK-C1-20260605-001")
        self.assertEqual(pay.amount, Decimal("500.00"))
        self.assertEqual(pay.unallocated, Decimal("500.00"))
        # 自动生成一条支出日记账
        self.assertIsNotNone(pay.bank_journal)
        j = pay.bank_journal
        self.assertEqual(j.direction, BankJournal.Direction.OUT)
        self.assertEqual(j.amount, Decimal("500.00"))
        self.assertEqual(j.signed_amount, Decimal("-500.00"))
        self.assertEqual(BankJournal.objects.filter(company=self.c1).count(), 1)


class AllocationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def _invoice(self, untaxed, rate="0.13"):
        return create_purchase_invoice(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), supplier=self.sup,
            lines=[{"product": self.p, "description": "", "amount_untaxed": Decimal(untaxed),
                    "tax_rate": Decimal(rate)}],
        )

    def _payment(self, amount):
        return create_payment(company=self.c1, user=None, doc_date=date(2026, 6, 5),
                              bank_account=self.acc, supplier=self.sup, amount=Decimal(amount))

    def test_partial_allocation(self):
        inv = self._invoice("1000")           # 含税 1130
        pay = self._payment("500")
        allocate_payment(payment=pay, allocations=[{"invoice": inv, "amount": Decimal("500")}])
        inv.refresh_from_db(); pay.refresh_from_db()
        self.assertEqual(inv.settled_amount, Decimal("500.00"))
        self.assertEqual(inv.outstanding, Decimal("630.00"))
        self.assertEqual(pay.settled_amount, Decimal("500.00"))
        self.assertEqual(pay.unallocated, Decimal("0.00"))

    def test_one_payment_multiple_invoices(self):
        a = self._invoice("100")   # 113
        b = self._invoice("200")   # 226
        pay = self._payment("339")
        allocate_payment(payment=pay, allocations=[
            {"invoice": a, "amount": Decimal("113")},
            {"invoice": b, "amount": Decimal("226")},
        ])
        a.refresh_from_db(); b.refresh_from_db(); pay.refresh_from_db()
        self.assertEqual(a.outstanding, Decimal("0.00"))
        self.assertEqual(b.outstanding, Decimal("0.00"))
        self.assertEqual(pay.unallocated, Decimal("0.00"))

    def test_over_invoice_outstanding_rejected(self):
        inv = self._invoice("100")  # 113
        pay = self._payment("500")
        with self.assertRaises(SettlementError):
            allocate_payment(payment=pay, allocations=[{"invoice": inv, "amount": Decimal("200")}])
        inv.refresh_from_db(); pay.refresh_from_db()
        self.assertEqual(inv.settled_amount, Decimal("0.00"))
        self.assertEqual(pay.settled_amount, Decimal("0.00"))

    def test_over_payment_balance_rejected(self):
        a = self._invoice("100")  # 113
        b = self._invoice("100")  # 113
        pay = self._payment("150")
        with self.assertRaises(SettlementError):
            allocate_payment(payment=pay, allocations=[
                {"invoice": a, "amount": Decimal("113")},
                {"invoice": b, "amount": Decimal("113")},  # 合计 226 > 付款 150
            ])
        a.refresh_from_db(); pay.refresh_from_db()
        self.assertEqual(a.settled_amount, Decimal("0.00"))  # 整体回滚
        self.assertEqual(pay.settled_amount, Decimal("0.00"))
