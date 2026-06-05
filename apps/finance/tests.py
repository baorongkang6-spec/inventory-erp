"""资金往来测试：采购发票含税换算与应付产生。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.finance.services import compute_tax, create_purchase_invoice
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
