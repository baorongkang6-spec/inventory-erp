"""按财务清单更正安博诺（C1）背书业务日（付款登记_安博诺_20260901.xlsx）。

匹配规则同迁移 0032：票据号 + 金额 + 供应商代码 → 写入「实际日期」。
"""

from datetime import date
from decimal import Decimal

from django.db import migrations


# 来源：付款登记_安博诺_20260901.xlsx
ANBONUO_ENDORSEMENT_DATES = (
    ("YSP-C1-20260821-004", "W0008", Decimal("147732.54"), date(2026, 8, 21)),
    ("YSP-C1-20260821-003", "W0008", Decimal("152456.62"), date(2026, 8, 21)),
    ("YSP-C1-20260821-002", "W0008", Decimal("150000.00"), date(2026, 8, 21)),
    ("YSP-C1-20260821-001", "W0008", Decimal("100000.00"), date(2026, 8, 21)),
    ("YSP-C1-20260820-005", "W0008", Decimal("5600.00"), date(2026, 8, 28)),
    ("YSP-C1-20260820-001", "W0008", Decimal("38462.52"), date(2026, 8, 21)),
    ("YSP-C1-20260819-004", "W0008", Decimal("3021.09"), date(2026, 8, 28)),
    ("YSP-C1-20260819-004", "W0008", Decimal("9148.32"), date(2026, 8, 21)),
    ("YSP-C1-20260819-003", "W0008", Decimal("11508.91"), date(2026, 8, 28)),
    ("YSP-C1-20260811-003", "W0008", Decimal("300000.00"), date(2026, 8, 11)),
    ("YSP-C1-20260811-002", "W0008", Decimal("300000.00"), date(2026, 8, 11)),
    ("YSP-C1-20260806-002", "W0008", Decimal("13675.40"), date(2026, 8, 11)),
    ("YSP-C1-20260701-005", "W0008", Decimal("33124.60"), date(2026, 8, 11)),
    ("YSP-C1-20260806-004", "W0013", Decimal("480000.00"), date(2026, 8, 6)),
    ("YSP-C1-20260806-003", "W0013", Decimal("200000.00"), date(2026, 8, 6)),
    ("YSP-C1-20260806-002", "W0013", Decimal("146000.00"), date(2026, 8, 6)),
    ("YSP-C1-20260721-001", "W0013", Decimal("153200.00"), date(2026, 8, 6)),
    ("YSP-C1-20260721-001", "W0008", Decimal("646800.00"), date(2026, 7, 21)),
)


def _supplier_code(ns, PurchaseInvoice, PurchaseOrder):
    if ns.invoice_id:
        inv = PurchaseInvoice.objects.filter(pk=ns.invoice_id).select_related("supplier").first()
        if inv and inv.supplier_id:
            return inv.supplier.code
    if ns.purchase_order_id:
        po = PurchaseOrder.objects.filter(pk=ns.purchase_order_id).select_related("supplier").first()
        if po and po.supplier_id:
            return po.supplier.code
    return None


def _apply_rows(apps, company_code, rows):
    Company = apps.get_model("core", "Company")
    NoteSettlement = apps.get_model("finance", "NoteSettlement")
    PurchaseInvoice = apps.get_model("finance", "PurchaseInvoice")
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")

    company = Company.objects.filter(code=company_code).first()
    if company is None:
        return

    for note_no, sup_code, amount, biz_date in rows:
        candidates = list(NoteSettlement.objects.filter(
            company=company, note_no=note_no, amount=amount,
            is_endorsement=True, note_kind="ar_note",
        ))
        if not candidates:
            continue
        matched = [ns for ns in candidates if _supplier_code(ns, PurchaseInvoice, PurchaseOrder) == sup_code]
        if len(matched) == 1:
            ns = matched[0]
        elif len(candidates) == 1:
            ns = candidates[0]
        else:
            continue
        if ns.date != biz_date:
            ns.date = biz_date
            ns.save(update_fields=["date"])


def apply_anbonuo_endorsement_dates(apps, schema_editor):
    _apply_rows(apps, "C1", ANBONUO_ENDORSEMENT_DATES)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0032_hongweida_endorsement_dates_20260901"),
        ("purchasing", "0009_fk_point_proxy_customer_supplier"),
    ]

    operations = [
        migrations.RunPython(apply_anbonuo_endorsement_dates, migrations.RunPython.noop),
    ]
