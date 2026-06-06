"""期初数据 Excel 导入（M5-1，SPEC §8.1）。

5 类期初，每类一张表，首行表头。导入到启用日 settings.OPENING_DATE。
均做去重，重复导入跳过，便于纠错后重传。
"""

from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.conf import settings
from openpyxl import Workbook, load_workbook

from apps.finance.models import BankAccount, NotePayable, NoteReceivable, PurchaseInvoice, SalesInvoice
from apps.finance.services import (
    create_note_payable,
    create_note_receivable,
    create_opening_payable,
    create_opening_receivable,
)
from apps.inventory.models import StockMove
from apps.inventory.services import post_inbound
from apps.masterdata.models import Customer, Product, Supplier

OPENING = settings.OPENING_DATE

# 各模板的表头（首行）
TEMPLATES = {
    "stock": ["商品编码", "数量", "单价"],
    "payable": ["供应商编码", "期初应付金额"],
    "receivable": ["客户编码", "期初应收金额"],
    "bank": ["银行账户名称", "期初余额"],
    "note_receivable": ["票据号", "金额", "来源客户编码(可空)", "到期日(可空)"],
    "note_payable": ["票据号", "金额", "收票供应商编码", "到期日(可空)"],
}


def build_template(kind) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "期初"
    ws.append(TEMPLATES[kind])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _rows(file):
    wb = load_workbook(file, read_only=True, data_only=True)
    ws = wb.active
    out = []
    for idx, values in enumerate(ws.iter_rows(values_only=True), start=1):
        values = list(values)
        if not any(v not in (None, "") for v in values):
            continue
        first = str(values[0]).strip() if values and values[0] is not None else ""
        if idx == 1 or first in sum(TEMPLATES.values(), []):  # 跳过表头
            continue
        out.append((idx, values))
    return out


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(v):
    from datetime import date, datetime
    if isinstance(v, (datetime, date)):
        return v.date() if isinstance(v, datetime) else v
    if v in (None, ""):
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            continue
    return None


def import_stock(company, user, file):
    created = skipped = 0
    errors = []
    for idx, vals in _rows(file):
        code = str(vals[0] or "").strip()
        qty, price = _dec(vals[1] if len(vals) > 1 else None), _dec(vals[2] if len(vals) > 2 else None)
        product = Product.objects.filter(company=company, code=code).first()
        if not product:
            errors.append(f"第{idx}行：商品编码「{code}」不存在"); continue
        if qty is None or qty <= 0 or price is None or price < 0:
            errors.append(f"第{idx}行：数量/单价无效"); continue
        if StockMove.objects.filter(company=company, product=product, source_type="Opening").exists():
            skipped += 1; continue
        post_inbound(company, product, qty, price, date=OPENING,
                     source_type="Opening", source_no="期初")
        created += 1
    return created, skipped, errors


def import_payable(company, user, file):
    created = skipped = 0
    errors = []
    for idx, vals in _rows(file):
        code = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        sup = Supplier.objects.filter(company=company, code=code).first()
        if not sup:
            errors.append(f"第{idx}行：供应商编码「{code}」不存在"); continue
        if amount is None or amount <= 0:
            errors.append(f"第{idx}行：金额无效"); continue
        if PurchaseInvoice.objects.filter(company=company, supplier=sup, is_opening=True).exists():
            skipped += 1; continue
        create_opening_payable(company=company, user=user, supplier=sup, amount=amount, doc_date=OPENING)
        created += 1
    return created, skipped, errors


def import_receivable(company, user, file):
    created = skipped = 0
    errors = []
    for idx, vals in _rows(file):
        code = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        cust = Customer.objects.filter(company=company, code=code).first()
        if not cust:
            errors.append(f"第{idx}行：客户编码「{code}」不存在"); continue
        if amount is None or amount <= 0:
            errors.append(f"第{idx}行：金额无效"); continue
        if SalesInvoice.objects.filter(company=company, customer=cust, is_opening=True).exists():
            skipped += 1; continue
        create_opening_receivable(company=company, user=user, customer=cust, amount=amount, doc_date=OPENING)
        created += 1
    return created, skipped, errors


def import_bank(company, user, file):
    updated = skipped = 0
    errors = []
    for idx, vals in _rows(file):
        name = str(vals[0] or "").strip()
        bal = _dec(vals[1] if len(vals) > 1 else None)
        acc = BankAccount.objects.filter(company=company, name=name).first()
        if not acc:
            errors.append(f"第{idx}行：银行账户「{name}」不存在"); continue
        if bal is None:
            errors.append(f"第{idx}行：余额无效"); continue
        acc.opening_balance = bal
        acc.save(update_fields=["opening_balance"])
        updated += 1
    return updated, skipped, errors


def import_note_receivable(company, user, file):
    created = skipped = 0
    errors = []
    for idx, vals in _rows(file):
        note_no = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        cust = Customer.objects.filter(company=company, code=str(vals[2] or "").strip()).first() if len(vals) > 2 and vals[2] else None
        due = _date(vals[3]) if len(vals) > 3 else None
        if amount is None or amount <= 0:
            errors.append(f"第{idx}行：金额无效"); continue
        if note_no and NoteReceivable.objects.filter(company=company, note_no=note_no).exists():
            skipped += 1; continue
        create_note_receivable(company=company, user=user, draw_date=OPENING, amount=amount,
                               customer=cust, note_no=note_no, due_date=due, is_opening=True)
        created += 1
    return created, skipped, errors


def import_note_payable(company, user, file):
    created = skipped = 0
    errors = []
    for idx, vals in _rows(file):
        note_no = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        sup = Supplier.objects.filter(company=company, code=str(vals[2] or "").strip()).first() if len(vals) > 2 and vals[2] else None
        due = _date(vals[3]) if len(vals) > 3 else None
        if amount is None or amount <= 0:
            errors.append(f"第{idx}行：金额无效"); continue
        if not sup:
            errors.append(f"第{idx}行：收票供应商编码无效"); continue
        if note_no and NotePayable.objects.filter(company=company, note_no=note_no).exists():
            skipped += 1; continue
        create_note_payable(company=company, user=user, draw_date=OPENING, amount=amount,
                            supplier=sup, note_no=note_no, due_date=due, is_opening=True)
        created += 1
    return created, skipped, errors


IMPORTERS = {
    "stock": ("期初库存", import_stock),
    "payable": ("期初应付", import_payable),
    "receivable": ("期初应收", import_receivable),
    "bank": ("期初银行存款", import_bank),
    "note_receivable": ("期初应收票据", import_note_receivable),
    "note_payable": ("期初应付票据", import_note_payable),
}
