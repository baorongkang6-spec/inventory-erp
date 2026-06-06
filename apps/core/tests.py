"""seed_init 冒烟测试：在干净库上跑通公司/角色/演示单据，并校验幂等。"""

from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance


class SeedInitTests(TestCase):
    def _run(self):
        call_command("seed_init", "--demo", stdout=StringIO())

    def test_seed_creates_companies_roles_and_demo_documents(self):
        self._run()
        self.assertEqual(Company.objects.count(), 3)
        self.assertEqual(Group.objects.count(), 5)
        self.assertTrue(get_user_model().objects.filter(username="purchaser").exists())

        # 演示单据后：C1 P001 结存 90@11.00（100@10 + 50@13 → 均价 11，出 60）
        bal = StockBalance.objects.get(company__code="C1", product__code="P001")
        self.assertEqual(bal.quantity, Decimal("90.000"))
        self.assertEqual(bal.amount, Decimal("990.00"))
        self.assertEqual(bal.avg_price, Decimal("11.00"))

        # M2/M3：采购发票含税 1864.50，付款核销 1000 + 应付票据抵 864.50 → 应付余额 0
        from apps.finance.models import NotePayable, PurchaseInvoice, SalesInvoice
        pinv = PurchaseInvoice.objects.get(company__code="C1")
        self.assertEqual(pinv.amount_taxed, Decimal("1864.50"))
        self.assertEqual(pinv.outstanding, Decimal("0.00"))
        npay = NotePayable.objects.get(company__code="C1")
        self.assertEqual(npay.unused, Decimal("0.00"))
        self.assertEqual(npay.status, "settled")
        # M6：C2 借调入库挂借调往来 50
        from apps.finance.models import BorrowTransaction
        bt = BorrowTransaction.objects.get(company__code="C2")
        self.assertEqual(bt.amount, Decimal("50.00"))
        self.assertEqual(bt.direction, "in")
        # 销售发票含税 4520，收款全额核销 → 应收余额 0
        sinv = SalesInvoice.objects.get(company__code="C1")
        self.assertEqual(sinv.amount_taxed, Decimal("4520.00"))
        self.assertEqual(sinv.outstanding, Decimal("0.00"))

    def test_seed_is_idempotent(self):
        self._run()
        self._run()  # 第二次不应报错，也不应重复建公司/单据
        self.assertEqual(Company.objects.count(), 3)
        self.assertEqual(
            StockBalance.objects.get(company__code="C1", product__code="P001").quantity,
            Decimal("90.000"),
        )


class DocRefsTests(TestCase):
    """单据来源跳转映射（M10）。"""

    def test_doc_url_maps_known_types(self):
        from apps.core.docrefs import doc_url, invoice_url
        self.assertEqual(doc_url("PurchaseInbound", 5), "/purchasing/inbound/5/")
        self.assertEqual(doc_url("SalesOutbound", 7), "/sales/outbound/7/")
        self.assertEqual(doc_url("Payment", 3), "/finance/payments/3/")
        self.assertEqual(doc_url("Receipt", 4), "/finance/receipts/4/")
        # 无对应详情/缺参 → 空串
        self.assertEqual(doc_url("Opening", 1), "")
        self.assertEqual(doc_url("Other", 1), "")
        self.assertEqual(doc_url("PurchaseInbound", ""), "")
        self.assertEqual(invoice_url("purchase", 2), "/finance/purchase-invoices/2/")
        self.assertEqual(invoice_url("sales", 9), "/finance/sales-invoices/9/")
        self.assertEqual(invoice_url("", 9), "")
