"""按财务清单更正安博诺（C1）1 笔背书业务日（付款登记_安博诺_20260908.xlsx）。

匹配规则同迁移 0032/0033：票据号 + 金额 + 供应商代码 → 写入「实际日期」。
"""

from datetime import date
from decimal import Decimal

from django.db import migrations


# 来源：付款登记_安博诺_20260908.xlsx
ANBONUO_ENDORSEMENT_DATES = (
    ("YSP-C1-20260811-001", "W0016", Decimal("200000.00"), date(2026, 8, 11)),
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


def apply_anbonuo_endorsement_dates_20260908(apps, schema_editor):
    _apply_rows(apps, "C1", ANBONUO_ENDORSEMENT_DATES)


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0033_anbonuo_endorsement_dates_20260901"),
        ("purchasing", "0009_fk_point_proxy_customer_supplier"),
    ]

    operations = [
        migrations.RunPython(apply_anbonuo_endorsement_dates_20260908, migrations.RunPython.noop),
    ]
