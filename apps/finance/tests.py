"""资金往来测试：采购发票含税换算与应付产生。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.finance.models import BankAccount, BankJournal, NotePayable, NoteReceivable  # noqa: F401
from apps.finance.services import (
    SettlementError,
    allocate_payment,
    allocate_receipt,
    compute_tax,
    create_payment,
    create_purchase_invoice,
    create_receipt,
    create_sales_invoice,
)
from apps.masterdata.models import Customer, Product, Supplier


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


class SalesSideTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def test_sales_invoice_produces_receivable(self):
        inv = create_sales_invoice(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.cust,
            lines=[{"product": self.p, "description": "", "amount_untaxed": Decimal("1000"),
                    "tax_rate": Decimal("0.13")}],
        )
        self.assertEqual(inv.doc_no, "XSF-C1-20260605-001")
        self.assertEqual(inv.amount_taxed, Decimal("1130.00"))
        self.assertEqual(inv.outstanding, Decimal("1130.00"))

    def test_receipt_auto_journal_and_allocate(self):
        inv = create_sales_invoice(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.cust,
            lines=[{"product": self.p, "description": "", "amount_untaxed": Decimal("1000"),
                    "tax_rate": Decimal("0.13")}],
        )
        rec = create_receipt(company=self.c1, user=None, doc_date=date(2026, 6, 5),
                             bank_account=self.acc, customer=self.cust, amount=Decimal("1130"))
        self.assertEqual(rec.doc_no, "SK-C1-20260605-001")
        # 自动生成收入日记账
        self.assertEqual(rec.bank_journal.direction, BankJournal.Direction.IN)
        self.assertEqual(rec.bank_journal.signed_amount, Decimal("1130.00"))
        # 核销
        allocate_receipt(receipt=rec, allocations=[{"invoice": inv, "amount": Decimal("1130")}])
        inv.refresh_from_db(); rec.refresh_from_db()
        self.assertEqual(inv.outstanding, Decimal("0.00"))
        self.assertEqual(rec.unallocated, Decimal("0.00"))

    def test_receipt_over_allocate_rejected(self):
        inv = create_sales_invoice(
            company=self.c1, user=None, doc_date=date(2026, 6, 5), customer=self.cust,
            lines=[{"product": self.p, "description": "", "amount_untaxed": Decimal("100"),
                    "tax_rate": Decimal("0.13")}],
        )  # 113
        rec = create_receipt(company=self.c1, user=None, doc_date=date(2026, 6, 5),
                             bank_account=self.acc, customer=self.cust, amount=Decimal("500"))
        with self.assertRaises(SettlementError):
            allocate_receipt(receipt=rec, allocations=[{"invoice": inv, "amount": Decimal("200")}])
        inv.refresh_from_db()
        self.assertEqual(inv.settled_amount, Decimal("0.00"))


class BankJournalExcelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户",
                                             opening_balance=Decimal("1000"))

    def test_export_then_parse_roundtrip(self):
        from apps.finance.excel import export_bank_journal, parse_bank_journal_xlsx
        from apps.finance.views import _journal_rows
        from io import BytesIO
        create_payment(company=self.c1, user=None, doc_date=date(2026, 6, 5),
                       bank_account=self.acc, supplier=self.sup, amount=Decimal("300"), summary="付款A")
        _, rows, closing = _journal_rows(self.c1, self.acc)
        self.assertEqual(closing, Decimal("700.00"))  # 1000 - 300
        content = export_bank_journal(self.acc, rows)
        parsed, errors = parse_bank_journal_xlsx(BytesIO(content))
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["direction"], "out")
        self.assertEqual(parsed[0]["amount"], Decimal("300"))

    def test_parse_skips_header_and_blank(self):
        from openpyxl import Workbook
        from io import BytesIO
        from apps.finance.excel import parse_bank_journal_xlsx
        wb = Workbook(); ws = wb.active
        ws.append(["账户：基本户"])
        ws.append(["日期", "摘要", "对方单位", "收入", "支出"])
        ws.append(["2026-06-05", "收货款", "客户甲", 500, None])
        ws.append([None, None, None, None, None])
        ws.append(["2026/06/06", "付款", "供应商甲", None, 200])
        buf = BytesIO(); wb.save(buf); buf.seek(0)
        parsed, errors = parse_bank_journal_xlsx(buf)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["direction"], "in")
        self.assertEqual(parsed[1]["direction"], "out")
        self.assertEqual(parsed[1]["date"], date(2026, 6, 6))


class BankJournalImportViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        U = get_user_model()
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.acc = BankAccount.objects.create(company=cls.c1, name="基本户")
        cls.user = U.objects.create_user(username="fin", password="x", can_view_all_companies=True)
        for code in ("add_bankjournal", "view_bankjournal"):
            cls.user.user_permissions.add(
                Permission.objects.get(content_type__app_label="finance", codename=code))

    def _xlsx(self):
        from openpyxl import Workbook
        from io import BytesIO
        wb = Workbook(); ws = wb.active
        ws.append(["日期", "摘要", "对方单位", "收入", "支出"])
        ws.append(["2026-06-05", "收货款", "客户甲", 500, None])
        ws.append(["2026-06-06", "付电费", "电力公司", None, 80])
        buf = BytesIO(); wb.save(buf); return buf.getvalue()

    def test_import_creates_then_dedups(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.finance.models import BankJournal
        self.client.force_login(self.user)
        data = self._xlsx()

        def upload():
            return self.client.post(
                "/finance/reports/bank-journal/import/",
                {"account": self.acc.pk,
                 "file": SimpleUploadedFile("流水.xlsx", data,
                     content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                SERVER_NAME="localhost", follow=True,
            )

        upload()
        self.assertEqual(BankJournal.objects.filter(company=self.c1, is_imported=True).count(), 2)
        upload()  # 第二次全部重复
        self.assertEqual(BankJournal.objects.filter(company=self.c1, is_imported=True).count(), 2)


class NoteRegistrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")

    def test_create_notes(self):
        from apps.finance.services import create_note_receivable, create_note_payable
        nr = create_note_receivable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                                    amount=Decimal("5000"), customer=self.cust, note_no="BA001")
        self.assertEqual(nr.doc_no, "YSP-C1-20260605-001")
        self.assertEqual(nr.unused, Decimal("5000.00"))
        self.assertEqual(nr.status, "on_hand")
        npay = create_note_payable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                                   supplier=self.sup, amount=Decimal("3000"))
        self.assertEqual(npay.doc_no, "YFP-C1-20260605-001")
        self.assertEqual(npay.unused, Decimal("3000.00"))


class NoteSettlementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")
        cls.sup = Supplier.objects.create(company=cls.c1, code="S1", name="供应商甲")
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")

    def _sales_inv(self, untaxed):
        return create_sales_invoice(company=self.c1, user=None, doc_date=date(2026, 6, 5),
            customer=self.cust, lines=[{"product": self.p, "description": "",
            "amount_untaxed": Decimal(untaxed), "tax_rate": Decimal("0.13")}])

    def _purchase_inv(self, untaxed):
        return create_purchase_invoice(company=self.c1, user=None, doc_date=date(2026, 6, 5),
            supplier=self.sup, lines=[{"product": self.p, "description": "",
            "amount_untaxed": Decimal(untaxed), "tax_rate": Decimal("0.13")}])

    def test_receivable_note_settles_sales(self):
        from apps.finance.services import create_note_receivable, settle_receivable_against_sales
        inv = self._sales_inv("1000")  # 含税 1130
        note = create_note_receivable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                                      amount=Decimal("1130"), customer=self.cust)
        settle_receivable_against_sales(note=note,
            allocations=[{"invoice": inv, "amount": Decimal("1130")}])
        inv.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(inv.outstanding, Decimal("0.00"))
        self.assertEqual(note.unused, Decimal("0.00"))
        self.assertEqual(note.status, "settled")

    def test_receivable_note_endorse_to_payable(self):
        from apps.finance.services import create_note_receivable, endorse_receivable_against_purchase
        pinv = self._purchase_inv("2000")  # 含税 2260
        note = create_note_receivable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                                      amount=Decimal("2260"), customer=self.cust)
        endorse_receivable_against_purchase(note=note,
            allocations=[{"invoice": pinv, "amount": Decimal("2260")}])
        pinv.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(pinv.outstanding, Decimal("0.00"))
        self.assertEqual(note.status, "endorsed")

    def test_payable_note_settles_purchase_partial(self):
        from apps.finance.services import create_note_payable, settle_payable_against_purchase
        pinv = self._purchase_inv("2000")  # 含税 2260
        note = create_note_payable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                                   supplier=self.sup, amount=Decimal("5000"))
        settle_payable_against_purchase(note=note,
            allocations=[{"invoice": pinv, "amount": Decimal("2260")}])
        pinv.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(pinv.outstanding, Decimal("0.00"))
        self.assertEqual(note.unused, Decimal("2740.00"))  # 5000-2260

    def test_note_over_use_rejected(self):
        from apps.finance.services import create_note_receivable, settle_receivable_against_sales, SettlementError
        inv = self._sales_inv("1000")  # 1130
        note = create_note_receivable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                                      amount=Decimal("500"), customer=self.cust)
        with self.assertRaises(SettlementError):
            settle_receivable_against_sales(note=note,
                allocations=[{"invoice": inv, "amount": Decimal("600")}])  # 超票据可用
        inv.refresh_from_db(); note.refresh_from_db()
        self.assertEqual(note.settled_amount, Decimal("0.00"))
        self.assertEqual(inv.settled_amount, Decimal("0.00"))


class NoteExcelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.cust = Customer.objects.create(company=cls.c1, code="K1", name="客户甲")

    def test_export_then_parse_notes(self):
        from io import BytesIO
        from apps.finance.services import create_note_receivable
        from apps.finance.excel import export_notes, parse_notes_xlsx
        create_note_receivable(company=self.c1, user=None, draw_date=date(2026, 6, 5),
                               amount=Decimal("2260"), customer=self.cust, note_no="BA1")
        notes = list(NoteReceivable.objects.filter(company=self.c1).select_related("customer"))
        content = export_notes(notes, "来源客户")
        parsed, errors = parse_notes_xlsx(BytesIO(content))
        self.assertEqual(errors, [])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["note_no"], "BA1")
        self.assertEqual(parsed[0]["amount"], Decimal("2260"))
        self.assertEqual(parsed[0]["party_name"], "K1 客户甲")
