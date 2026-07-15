"""M17：往来对冲 + 应收票据拆借。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.models import Company
from apps.finance.models import IntercoBalance, NoteLoan, NoteReceivable, PartnerOffset
from apps.finance.services import (
    SettlementError,
    create_partner_offset,
    create_purchase_invoice,
    create_sales_invoice,
    lend_note_receivable,
    return_note_loan,
    reverse_partner_offset,
)
from apps.masterdata.models import Customer, Product, Supplier


class PartnerOffsetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.masterdata.models import BusinessPartner
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        # 同一往来单位兼客户+供应商
        cls.partner = BusinessPartner.objects.create(
            company=cls.c1, code="W1", name="甲公司",
            is_customer=True, is_supplier=True)
        cls.p = Product.objects.create(company=cls.c1, code="P1", name="货A")
        cls.sinv = create_sales_invoice(
            company=cls.c1, user=None, doc_date=date(2026, 6, 10), customer=cls.partner,
            lines=[{"product": cls.p, "description": "", "amount_untaxed": Decimal("1000"),
                    "tax_rate": Decimal("0")}])
        cls.pinv = create_purchase_invoice(
            company=cls.c1, user=None, doc_date=date(2026, 6, 11), supplier=cls.partner,
            lines=[{"product": cls.p, "description": "", "amount_untaxed": Decimal("800"),
                    "tax_rate": Decimal("0")}])

    def test_offset_and_reverse(self):
        doc = create_partner_offset(
            company=self.c1, user=None, doc_date=date(2026, 6, 20),
            partner=self.partner,
            ar_lines=[{"invoice": self.sinv, "amount": Decimal("500")}],
            ap_lines=[{"invoice": self.pinv, "amount": Decimal("500")}])
        self.sinv.refresh_from_db()
        self.pinv.refresh_from_db()
        self.assertEqual(self.sinv.outstanding, Decimal("500.00"))
        self.assertEqual(self.pinv.outstanding, Decimal("300.00"))
        self.assertEqual(doc.amount, Decimal("500.00"))

        reverse_partner_offset(doc, user=None)
        self.sinv.refresh_from_db()
        self.pinv.refresh_from_db()
        self.assertEqual(self.sinv.outstanding, Decimal("1000.00"))
        self.assertEqual(self.pinv.outstanding, Decimal("800.00"))
        doc.refresh_from_db()
        self.assertEqual(doc.status, PartnerOffset.Status.VOID)

    def test_sides_must_match(self):
        with self.assertRaises(SettlementError):
            create_partner_offset(
                company=self.c1, user=None, doc_date=date(2026, 6, 20),
                partner=self.partner,
                ar_lines=[{"invoice": self.sinv, "amount": Decimal("500")}],
                ap_lines=[{"invoice": self.pinv, "amount": Decimal("400")}])


class NoteLoanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        cls.note = NoteReceivable.objects.create(
            company=cls.c1, doc_no="YSP-C1-20260601-001", note_no="N001",
            draw_date=date(2026, 6, 1), due_date=date(2026, 12, 1),
            amount=Decimal("10000"), status=NoteReceivable.Status.ON_HAND)

    def test_lend_and_return(self):
        lend = lend_note_receivable(
            company=self.c1, user=None, doc_date=date(2026, 6, 15),
            note=self.note, borrower_company=self.c2, amount=Decimal("3000"))
        self.note.refresh_from_db()
        self.assertEqual(self.note.unused, Decimal("7000.00"))
        self.assertEqual(lend.role, NoteLoan.Role.LEND)
        self.assertIsNotNone(lend.mirror)
        borrow = lend.mirror
        self.assertEqual(borrow.company_id, self.c2.id)
        self.assertEqual(borrow.note_receivable.amount, Decimal("3000.00"))
        self.assertEqual(
            IntercoBalance.objects.filter(
                company=self.c1, kind=IntercoBalance.Kind.OTHER_AR).count(), 1)
        self.assertEqual(
            IntercoBalance.objects.filter(
                company=self.c2, kind=IntercoBalance.Kind.OTHER_AP).count(), 1)

        return_note_loan(lend, user=None, amount=Decimal("3000"), return_date=date(2026, 7, 1))
        lend.refresh_from_db()
        self.note.refresh_from_db()
        self.assertEqual(lend.status, NoteLoan.Status.CLOSED)
        self.assertEqual(self.note.unused, Decimal("10000.00"))
