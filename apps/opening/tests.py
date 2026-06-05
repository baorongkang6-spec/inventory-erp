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
