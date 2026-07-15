"""会计期间结账与报表默认日期。"""

from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings

from apps.core.models import Company
from apps.core.period import (
    close_period,
    last_month_range,
    period_edit_block_reason,
    report_date_range,
    report_date_range_overview,
    suggested_close_through,
    unclose_period,
)
from apps.finance.services import create_purchase_invoice
from apps.masterdata.models import Product, Supplier


class PeriodCloseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.sup = Supplier.objects.create(company=cls.company, code="S1", name="供应商甲")
        cls.product = Product.objects.create(company=cls.company, code="P1", name="货A")

    def test_last_month_range(self):
        first, last = last_month_range(date(2026, 7, 14))
        self.assertEqual(first, date(2026, 6, 1))
        self.assertEqual(last, date(2026, 6, 30))

    @override_settings(OPENING_DATE=date(2026, 6, 1))
    def test_report_date_defaults_to_last_month_when_open(self):
        today = date(2026, 7, 14)
        dfrom, dto = report_date_range(self.company, today, None, None)
        self.assertEqual(dfrom, date(2026, 6, 1))
        self.assertEqual(dto, date(2026, 6, 30))

    @override_settings(OPENING_DATE=date(2026, 7, 1))
    def test_report_date_clamps_to_opening_when_last_month_before_opening(self):
        """启用日 7/1、今天 7 月：上月 6 月早于启用日 → 默认启用日~今天。"""
        today = date(2026, 7, 14)
        dfrom, dto = report_date_range(self.company, today, None, None)
        self.assertEqual(dfrom, date(2026, 7, 1))
        self.assertEqual(dto, today)
        od_from, od_to = report_date_range_overview(today, None, None)
        self.assertEqual(od_from, date(2026, 7, 1))
        self.assertEqual(od_to, today)

    @override_settings(OPENING_DATE=date(2026, 6, 1))
    def test_report_date_defaults_to_current_month_after_close(self):
        today = date(2026, 7, 14)
        close_period(self.company, date(2026, 6, 30), today=today)
        dfrom, dto = report_date_range(self.company, today, None, None)
        self.assertEqual(dfrom, date(2026, 7, 1))
        self.assertEqual(dto, today)

    def test_close_and_unclose_sequential(self):
        today = date(2026, 7, 14)
        self.assertEqual(suggested_close_through(self.company, today), date(2026, 6, 30))
        close_period(self.company, date(2026, 6, 30), today=today)
        self.company.refresh_from_db()
        self.assertEqual(self.company.period_closed_through, date(2026, 6, 30))
        self.assertIsNone(suggested_close_through(self.company, today))

        unclose_period(self.company)
        self.company.refresh_from_db()
        self.assertIsNone(self.company.period_closed_through)

    def test_period_edit_block_reason(self):
        self.company.period_closed_through = date(2026, 6, 30)
        self.assertIsNotNone(period_edit_block_reason(self.company, date(2026, 6, 15)))
        self.assertIsNone(period_edit_block_reason(self.company, date(2026, 7, 1)))

    def test_purchase_invoice_blocked_after_close(self):
        from apps.finance.services import purchase_invoice_edit_block_reason

        inv = create_purchase_invoice(
            company=self.company, user=None, doc_date=date(2026, 6, 15), supplier=self.sup,
            lines=[{"product": self.product, "description": "货A",
                    "amount_untaxed": Decimal("100"), "tax_rate": Decimal("0")}],
        )
        self.company.period_closed_through = inv.doc_date
        self.company.save(update_fields=["period_closed_through"])
        reason = purchase_invoice_edit_block_reason(inv, date(2026, 7, 14))
        self.assertIn("已结账", reason)
