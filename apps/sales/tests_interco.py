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

    def test_mirror_inbound_uses_sale_untaxed_when_priced(self):
        """有售价的销售镜像：B 入库成本=不含税售额（非 A 结转成本），含税/税额一并镜像。"""
        # C1 备货成本 10/桶；售价不含税 300/桶（30 桶=不含税 9000，13% → 含税 10170）
        out = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.cust_c2,
            lines=[{"product": self.p1, "quantity": Decimal("30"),
                    "amount_untaxed": Decimal("9000"), "tax_rate": Decimal("0.13")}])
        inbound = out.mirror_inbound
        p_c2 = Product.objects.get(company=self.c2, code="P001")
        line = inbound.lines.get()
        # 入库成本=不含税售额 9000（不是 C1 成本 300），含税镜像 10170
        self.assertEqual(line.amount_untaxed, Decimal("9000.00"))
        self.assertEqual(inbound.total_amount, Decimal("9000.00"))   # 入库成本=不含税售额
        self.assertEqual(inbound.total_taxed, Decimal("10170.00"))
        # C2 库存按 9000 入账（移动加权 300/桶）
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p_c2).amount, Decimal("9000.00"))

    def test_non_related_customer_no_mirror(self):
        plain = Customer.objects.create(company=self.c1, code="EXT", name="外部客户")
        out = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=plain,
            lines=[{"product": self.p1, "quantity": Decimal("5")}],
        )
        self.assertIsNone(out.mirror_inbound)
        self.assertFalse(PurchaseInbound.objects.filter(company=self.c2).exists())


class IntercoVoidTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        cls.p1 = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")
        cls.cust_c2 = Customer.objects.create(
            company=cls.c1, code="REL-C2", name="恒本源", related_company=cls.c2)
        post_inbound(cls.c1, cls.p1, Decimal("100"), Decimal("10"))

    def _mirror_outbound(self, qty):
        return create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.cust_c2,
            lines=[{"product": self.p1, "quantity": Decimal(qty)}])

    def test_void_outbound_cascades_to_mirror_and_reverses_both(self):
        out = self._mirror_outbound("30")
        inbound = out.mirror_inbound
        p_c2 = Product.objects.get(company=self.c2, code="P001")
        from apps.sales.services import void_sales_outbound
        void_sales_outbound(out, None)
        out.refresh_from_db(); inbound.refresh_from_db()
        self.assertEqual(out.status, "void")
        self.assertEqual(inbound.status, "void")  # 镜像联动作废
        # C1 库存恢复 100，C2 库存归 0
        self.assertEqual(
            StockBalance.objects.get(company=self.c1, product=self.p1).quantity, Decimal("100.000"))
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p_c2).quantity, Decimal("0.000"))

    def test_void_succeeds_even_if_mirror_consumed(self):
        from apps.sales.services import void_sales_outbound
        from apps.inventory.services import post_outbound
        out = self._mirror_outbound("30")
        p_c2 = Product.objects.get(company=self.c2, code="P001")
        # C2 把镜像入库的货卖掉一部分；允许负库存 → 作废照常（C2 反冲后变负）
        post_outbound(self.c2, p_c2, Decimal("25"))   # C2 余 5
        void_sales_outbound(out, None)
        out.refresh_from_db()
        self.assertEqual(out.status, "void")
        # C1 库存恢复到出库前
        self.assertEqual(
            StockBalance.objects.get(company=self.c1, product=self.p1).quantity, Decimal("100.000"))
        # C2 镜像入库 30 被反冲：5 − 30 = −25
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p_c2).quantity, Decimal("-25.000"))

    def test_cannot_void_mirror_directly(self):
        from apps.purchasing.services import void_purchase_inbound
        from apps.inventory.services import InventoryError
        out = self._mirror_outbound("30")
        with self.assertRaises(InventoryError):
            void_purchase_inbound(out.mirror_inbound, None)  # 直接作废镜像应被拒绝


class IntercoBorrowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        cls.p1 = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.rel = Customer.objects.create(company=cls.c1, code="REL-C2", name="恒本源",
                                          related_company=cls.c2)
        post_inbound(cls.c1, cls.p1, Decimal("100"), Decimal("10"))

    def test_lend_mirrors_borrow_in(self):
        from apps.finance.models import BorrowTransaction
        out = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.rel,
            lines=[{"product": self.p1, "quantity": Decimal("30")}], sales_type="lend")
        # 对方 C2 自动生成借调入库
        inbound = out.mirror_inbound
        self.assertIsNotNone(inbound)
        self.assertEqual(inbound.company, self.c2)
        self.assertEqual(inbound.purchase_type, "borrow")
        p_c2 = Product.objects.get(company=self.c2, code="P001")
        self.assertEqual(
            StockBalance.objects.get(company=self.c2, product=p_c2).quantity, Decimal("30.000"))
        # 借调往来：C1 侧记 OUT(对方欠我 -300)，C2 侧记 IN(欠 C1 +300)
        c1_bal = sum((t.signed_amount for t in BorrowTransaction.objects.filter(company=self.c1)), Decimal("0"))
        c2_bal = sum((t.signed_amount for t in BorrowTransaction.objects.filter(company=self.c2)), Decimal("0"))
        self.assertEqual(c1_bal, Decimal("-300.00"))
        self.assertEqual(c2_bal, Decimal("300.00"))

    def test_void_lend_cascades_borrow(self):
        from apps.finance.models import BorrowTransaction
        from apps.sales.services import void_sales_outbound
        out = create_and_post_outbound(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.rel,
            lines=[{"product": self.p1, "quantity": Decimal("30")}], sales_type="lend")
        void_sales_outbound(out, None)
        out.refresh_from_db()
        self.assertEqual(out.status, "void")
        self.assertEqual(out.mirror_inbound.status, "void")
        # 两侧借调往来均撤销
        self.assertEqual(BorrowTransaction.objects.count(), 0)
