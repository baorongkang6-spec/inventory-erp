"""M6-2 借调往来：借调入库挂往来、归还出库冲减、作废撤销。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.finance.models import BorrowTransaction
from apps.inventory.models import StockBalance
from apps.masterdata.models import Customer, Product, Supplier
from apps.purchasing.models import PurchaseInbound
from apps.purchasing.services import create_and_post_inbound, void_purchase_inbound
from apps.sales.services import create_and_post_outbound


class BorrowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="出借方甲")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="出借方甲")

    def _balance(self, party="出借方甲"):
        return sum((t.signed_amount for t in BorrowTransaction.objects.filter(
            company=self.c1, counterparty=party)), Decimal("0.00"))

    def test_borrow_in_creates_due(self):
        doc = create_and_post_inbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 1),
            lines=[{"product": self.p, "quantity": Decimal("20"), "unit_price": Decimal("10")}],
            purchase_type=PurchaseInbound.PurchaseType.BORROW, borrow_counterparty="出借方甲")
        self.assertEqual(doc.purchase_type, "borrow")
        b = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(b.quantity, Decimal("20.000"))
        self.assertEqual(self._balance(), Decimal("200.00"))  # 借调往来 +200

    def test_return_reduces_due(self):
        create_and_post_inbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 1),
            lines=[{"product": self.p, "quantity": Decimal("20"), "unit_price": Decimal("10")}],
            purchase_type=PurchaseInbound.PurchaseType.BORROW, borrow_counterparty="出借方甲")
        create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 2),
            lines=[{"product": self.p, "quantity": Decimal("5")}],
            sales_type="return", borrow_counterparty="出借方甲")
        b = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(b.quantity, Decimal("15.000"))  # 归还 5
        self.assertEqual(self._balance(), Decimal("150.00"))  # 200 - 50

    def test_void_borrow_in_removes_due(self):
        doc = create_and_post_inbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 1),
            lines=[{"product": self.p, "quantity": Decimal("20"), "unit_price": Decimal("10")}],
            purchase_type=PurchaseInbound.PurchaseType.BORROW, borrow_counterparty="出借方甲")
        void_purchase_inbound(doc, None)
        self.assertEqual(self._balance(), Decimal("0.00"))
        self.assertEqual(BorrowTransaction.objects.filter(company=self.c1).count(), 0)
