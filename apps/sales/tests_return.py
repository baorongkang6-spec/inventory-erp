"""销售退回 / 采购退回（SPEC §4.2）。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance
from apps.inventory.services import post_inbound
from apps.masterdata.models import Customer, Product, Supplier
from apps.purchasing.models import PurchaseInbound
from apps.purchasing.services import create_and_post_inbound
from apps.sales.models import SalesOutbound
from apps.sales.services import create_and_post_outbound


class SalePurchaseReturnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        post_inbound(cls.c1, cls.p, Decimal("100"), Decimal("10"), date=date(2026, 7, 1))
        cls.user = get_user_model().objects.create_user(
            username="ret", password="x", can_view_all_companies=True)

    def test_sale_return_increases_stock(self):
        bal0 = StockBalance.objects.get(company=self.c1, product=self.p)
        doc = create_and_post_outbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 2),
            customer=self.cust, sales_type=SalesOutbound.SalesType.SALE_RETURN,
            lines=[{"product": self.p, "quantity": Decimal("5"),
                    "amount_untaxed": Decimal("55"), "tax_rate": Decimal("0.13")}])
        self.assertEqual(doc.sales_type, "sale_return")
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, bal0.quantity + Decimal("5.000"))
        self.assertEqual(doc.lines.get().stock_move.direction, "in")
        self.assertEqual(doc.total_cost, Decimal("55.00"))

    def test_purchase_return_decreases_stock(self):
        bal0 = StockBalance.objects.get(company=self.c1, product=self.p)
        doc = create_and_post_inbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 2),
            supplier=self.sup, purchase_type=PurchaseInbound.PurchaseType.PURCHASE_RETURN,
            lines=[{"product": self.p, "quantity": Decimal("4"),
                    "amount_untaxed": Decimal("40"), "tax_rate": Decimal("0.13")}])
        self.assertEqual(doc.purchase_type, "purchase_return")
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, bal0.quantity - Decimal("4.000"))
        self.assertEqual(doc.lines.get().stock_move.direction, "out")

    def test_sale_return_mirrors_purchase_return(self):
        cust = Customer.objects.create(
            company=self.c1, code="K2", name="恒本源客户", related_company=self.c2)
        # C2 need matching product + some stock so purchase return can ship
        p2 = Product.objects.create(company=self.c2, code="P001", name="货A")
        post_inbound(self.c2, p2, Decimal("50"), Decimal("11"), date=date(2026, 7, 1))
        bal2_0 = StockBalance.objects.get(company=self.c2, product=p2)

        doc = create_and_post_outbound(
            company=self.c1, user=self.user, doc_date=date(2026, 7, 3),
            customer=cust, sales_type=SalesOutbound.SalesType.SALE_RETURN,
            lines=[{"product": self.p, "quantity": Decimal("3"),
                    "amount_untaxed": Decimal("36"), "tax_rate": Decimal("0.13")}])
        self.assertIsNotNone(doc.mirror_inbound_id)
        mirror = doc.mirror_inbound
        self.assertEqual(mirror.purchase_type, PurchaseInbound.PurchaseType.PURCHASE_RETURN)
        self.assertEqual(mirror.company_id, self.c2.pk)
        bal2 = StockBalance.objects.get(company=self.c2, product=p2)
        self.assertEqual(bal2.quantity, bal2_0.quantity - Decimal("3.000"))
