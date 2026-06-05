"""M4 关联联动测试：销售出库自动镜像对方采购入库。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.inventory.models import StockBalance
from apps.inventory.services import post_inbound
from apps.masterdata.models import Customer, Product
from apps.purchasing.models import PurchaseInbound
from apps.sales.services import create_and_post_outbound


class IntercoMirrorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        cls.p1 = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂", unit="桶")
        # C1 的客户指向关联公司 C2
        cls.cust_c2 = Customer.objects.create(
            company=cls.c1, code="REL-C2", name="恒本源", related_company=cls.c2)
        # C1 备货：100@10
        post_inbound(cls.c1, cls.p1, Decimal("100"), Decimal("10"))

    def test_outbound_mirrors_inbound_in_related_company(self):
        out = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.cust_c2,
            lines=[{"product": self.p1, "quantity": Decimal("30")}],
        )
        # 镜像入库已生成且互链
        self.assertIsNotNone(out.mirror_inbound)
        inbound = out.mirror_inbound
        self.assertEqual(inbound.company, self.c2)
        self.assertEqual(inbound.source_outbound_id, out.pk)
        self.assertEqual(inbound.purchase_type, PurchaseInbound.PurchaseType.EXTERNAL)
        # C2 自动建了同编码商品
        p_c2 = Product.objects.get(company=self.c2, code="P001")
        self.assertNotEqual(p_c2.pk, self.p1.pk)
        # 数量照搬、单价取 C1 结转成本 10.00 → C2 入库 30@10=300
        line = inbound.lines.get()
        self.assertEqual(line.quantity, Decimal("30.000"))
        self.assertEqual(line.unit_price, Decimal("10.00"))
        # 双方库存：C1 减 30（剩 70），C2 增 30
        self.assertEqual(
            StockBalance.objects.get(company=self.c1, product=self.p1).quantity, Decimal("70.000"))
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p_c2).quantity, Decimal("30.000"))
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p_c2).amount, Decimal("300.00"))

    def test_non_related_customer_no_mirror(self):
        plain = Customer.objects.create(company=self.c1, code="EXT", name="外部客户")
        out = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=plain,
            lines=[{"product": self.p1, "quantity": Decimal("5")}],
        )
        self.assertIsNone(out.mirror_inbound)
        self.assertFalse(PurchaseInbound.objects.filter(company=self.c2).exists())
