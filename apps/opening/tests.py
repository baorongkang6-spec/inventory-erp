"""期初导入测试（M5-1）。"""

from decimal import Decimal
from io import BytesIO

from django.test import TestCase
from openpyxl import Workbook

from apps.core.models import Company
from apps.finance.models import BankAccount, PurchaseInvoice
from apps.inventory.models import StockBalance
from apps.masterdata.models import Product, Supplier
from apps.opening import imports


def _xlsx(rows):
    wb = Workbook(); ws = wb.active
    for r in rows:
        ws.append(r)
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return buf


class OpeningImportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="环氧树脂")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")

    def test_import_stock(self):
        f = _xlsx([["商品编码", "数量", "单价"], ["P001", 100, 10]])
        created, skipped, errors = imports.import_stock(self.c1, None, f)
        self.assertEqual((created, errors), (1, []))
        bal = StockBalance.objects.get(company=self.c1, product=self.p)
        self.assertEqual(bal.quantity, Decimal("100.000"))
        self.assertEqual(bal.amount, Decimal("1000.00"))
        # 再次导入跳过（去重）
        f2 = _xlsx([["商品编码", "数量", "单价"], ["P001", 100, 10]])
        c2, s2, _ = imports.import_stock(self.c1, None, f2)
        self.assertEqual((c2, s2), (0, 1))

    def test_import_payable_creates_opening_invoice(self):
        f = _xlsx([["供应商编码", "期初应付金额"], ["S1", "5000"]])
        created, skipped, errors = imports.import_payable(self.c1, None, f)
        self.assertEqual((created, errors), (1, []))
        inv = PurchaseInvoice.objects.get(company=self.c1, supplier=self.sup, is_opening=True)
        self.assertEqual(inv.outstanding, Decimal("5000.00"))

    def test_import_bank_sets_opening_balance(self):
        f = _xlsx([["银行账户名称", "期初余额"], ["基本户", "12345.67"]])
        updated, skipped, errors = imports.import_bank(self.c1, None, f)
        self.assertEqual((updated, errors), (1, []))
        self.acc.refresh_from_db()
        self.assertEqual(self.acc.opening_balance, Decimal("12345.67"))

    def test_unknown_code_reports_error(self):
        f = _xlsx([["商品编码", "数量", "单价"], ["NOPE", 1, 1]])
        created, skipped, errors = imports.import_stock(self.c1, None, f)
        self.assertEqual(created, 0)
        self.assertEqual(len(errors), 1)


class OverviewReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import date
        from apps.inventory.services import post_inbound
        from apps.purchasing.services import create_and_post_inbound
        from apps.sales.services import create_and_post_outbound
        from apps.finance.services import (
            create_opening_payable, create_payment, create_purchase_invoice,
        )
        from apps.masterdata.models import Customer
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户",
                                             opening_balance=Decimal("1000"))
        d = date(2026, 6, 1)
        # 期初库存 50@10=500
        post_inbound(cls.c1, cls.p, Decimal("50"), Decimal("10"),
                     source_type="Opening", source_no="期初")
        # 本期入库 100@10=1000，出库 30（成本 10 → 300）
        create_and_post_inbound(company=cls.c1, user=None, doc_date=d,
            lines=[{"product": cls.p, "quantity": Decimal("100"), "unit_price": Decimal("10")}])
        create_and_post_outbound(company=cls.c1, user=None, doc_date=d,
            lines=[{"product": cls.p, "quantity": Decimal("30")}])
        # 期初应付 2000；本期采购发票 1130；付款 500 核销
        create_opening_payable(company=cls.c1, user=None, supplier=cls.sup,
                               amount=Decimal("2000"), doc_date=d)
        inv = create_purchase_invoice(company=cls.c1, user=None, doc_date=d, supplier=cls.sup,
            lines=[{"product": cls.p, "description": "", "amount_untaxed": Decimal("1000"),
                    "tax_rate": Decimal("0.13")}])
        pay = create_payment(company=cls.c1, user=None, doc_date=d, bank_account=cls.acc,
                             supplier=cls.sup, amount=Decimal("500"))
        from apps.finance.services import allocate_payment
        allocate_payment(payment=pay, allocations=[{"invoice": inv, "amount": Decimal("500")}])

    def test_overview_reconciles(self):
        from apps.opening.reports import company_overview
        ov = company_overview(self.c1)
        for key in ("bank", "stock", "payable", "receivable", "note_recv"):
            r = ov[key]
            self.assertEqual(r["opening"] + r["income"] - r["outgo"], r["ending"],
                             f"{key} 四列不勾稽")
        # 库存：期初500 + 入1000 - 出300 = 期末1200
        self.assertEqual(ov["stock"]["opening"], Decimal("500.00"))
        self.assertEqual(ov["stock"]["ending"], Decimal("1200.00"))
        # 银行：期初1000 - 付款500 = 期末500
        self.assertEqual(ov["bank"]["ending"], Decimal("500.00"))
        # 应付：期初2000 + 1130 - 500核销 = 期末2630
        self.assertEqual(ov["payable"]["opening"], Decimal("2000.00"))
        self.assertEqual(ov["payable"]["ending"], Decimal("2630.00"))


class ReconciliationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import date
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        from apps.finance.services import create_purchase_invoice
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.p = Product.objects.create(company=cls.c1, code="P001", name="货A")
        create_purchase_invoice(company=cls.c1, user=None, doc_date=date(2026, 6, 1), supplier=cls.sup,
            lines=[{"product": cls.p, "description": "", "amount_untaxed": Decimal("1000"),
                    "tax_rate": Decimal("0.13")}])  # 应付 1130
        U = get_user_model()
        cls.user = U.objects.create_user(username="fin", password="x", can_view_all_companies=True)
        cls.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="finance", codename="view_purchaseinvoice"))

    def test_payable_recon_lines(self):
        from apps.opening.reports import recon_lines
        lines = recon_lines(self.c1, "payable")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["system_amount"], Decimal("1130.00"))

    def test_reconciliation_post_persists_diff(self):
        from apps.opening.models import ReconciliationRun
        self.client.force_login(self.user)
        # 外部值填 1100 → 差异 -30
        resp = self.client.post("/reconciliation/", {
            "category": "payable", "as_of": "2026-06-30", "ext-0": "1100",
        }, SERVER_NAME="localhost")
        self.assertEqual(resp.status_code, 200)
        run = ReconciliationRun.objects.get(company=self.c1, category="payable")
        line = run.lines.get()
        self.assertEqual(line.system_amount, Decimal("1130.00"))
        self.assertEqual(line.external_amount, Decimal("1100.00"))
        self.assertEqual(line.diff, Decimal("-30.00"))
