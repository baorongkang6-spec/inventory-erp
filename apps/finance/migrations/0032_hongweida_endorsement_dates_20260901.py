"""按财务清单更正鸿威达（C3）背书业务日（付款登记_鸿威达_20260901.xlsx）。

背景：迁移 0031 将误存出票日的记录改为 created_at，与登记时选的付款日不符。
本迁移按「票据号 + 金额 + 供应商」匹配 NoteSettlement，写入「实际日期」列。
"""

from datetime import date
from decimal import Decimal

from django.db import migrations


# 来源：付款登记_鸿威达_20260901.xlsx（列：单号/票据号、实际日期、供应商、金额）
HONGWEIDA_ENDORSEMENT_DATES = (
    ("YSP-C3-20260730-003", "W0007", Decimal("200000.00"), date(2026, 8, 11)),
    ("YSP-C3-20260730-001", "W0010", Decimal("500000.00"), date(2026, 8, 11)),
    ("YSP-C3-20260713-002", "W0007", Decimal("159675.40"), date(2026, 8, 6)),
    ("YSP-C3-20260701-021", "W0007", Decimal("200000.00"), date(2026, 8, 6)),
    ("YSP-C3-20260701-020", "W0007", Decimal("191864.42"), date(2026, 8, 6)),
    ("YSP-C3-20260701-020", "W0007", Decimal("288135.58"), date(2026, 8, 6)),
    ("YSP-C3-20260701-014", "W0007", Decimal("300000.00"), date(2026, 8, 11)),
    ("YSP-C3-20260701-013", "W0007", Decimal("300000.00"), date(2026, 8, 11)),
    ("YSP-C3-20260710-003", "W0013", Decimal("60000.00"), date(2026, 7, 22)),
    ("YSP-C3-20260710-003", "W0010", Decimal("340000.00"), date(2026, 7, 27)),
    ("YSP-C3-20260710-002", "W0010", Decimal("300000.00"), date(2026, 7, 20)),
    ("YSP-C3-20260701-022", "W0010", Decimal("500000.00"), date(2026, 7, 27)),
    ("YSP-C3-20260713-004", "W0007", Decimal("800000.00"), date(2026, 7, 21)),
    ("YSP-C3-20260701-002", "W0010", Decimal("500000.00"), date(2026, 7, 6)),
    ("YSP-C3-20260701-001", "W0010", Decimal("441000.00"), date(2026, 7, 20)),
    ("YSP-C3-20260701-001", "W0010", Decimal("59000.00"), date(2026, 7, 6)),
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


def apply_hongweida_endorsement_dates(apps, schema_editor):
    Company = apps.get_model("core", "Company")
    NoteSettlement = apps.get_model("finance", "NoteSettlement")
    PurchaseInvoice = apps.get_model("finance", "PurchaseInvoice")
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")

    company = Company.objects.filter(code="C3").first()
    if company is None:
        return

    for note_no, sup_code, amount, biz_date in HONGWEIDA_ENDORSEMENT_DATES:
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


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0031_fixup_endorsement_settlement_dates"),
        ("purchasing", "0009_fk_point_proxy_customer_supplier"),
    ]

    operations = [
        migrations.RunPython(apply_hongweida_endorsement_dates, migrations.RunPython.noop),
    ]
