"""期初数据 Excel 导入（M5-1，SPEC §8.1）。

6 类期初合并在**一个工作簿、每类一个 sheet**：下载一个模板→各 sheet 填好→上传一次全部导入。
导入到启用日 settings.OPENING_DATE；均做去重，重复导入跳过，便于纠错后重传。
库存为「数量金额式」（数量 + 金额，不录单价，均价由系统按金额/数量得出）。
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
from apps.core.money import ZERO_MONEY
from apps.inventory.models import StockMove
from apps.inventory.services import post_inbound
from apps.masterdata.models import Customer, Product, Supplier

OPENING = settings.OPENING_DATE

# 各类期初的表头（首行）
TEMPLATES = {
    "stock": ["商品编码", "数量", "金额"],
    "payable": ["供应商编码", "期初应付金额"],
    "receivable": ["客户编码", "期初应收金额"],
    "bank": ["银行账户名称", "期初余额"],
    "note_receivable": ["票据号", "金额", "来源客户编码(可空)", "到期日(可空)"],
    "note_payable": ["票据号", "金额", "收票供应商编码", "到期日(可空)"],
}

# 合并工作簿里各 sheet 的标题（也用于导入时按标题定位）
SHEET_TITLES = {
    "stock": "期初库存",
    "payable": "期初应付",
    "receivable": "期初应收",
    "bank": "期初银行存款",
    "note_receivable": "期初应收票据",
    "note_payable": "期初应付票据",
}

_ALL_HEADERS = sum(TEMPLATES.values(), [])


def build_template(kind) -> bytes:
    """单类模板（保留，供测试/旧链接用）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "期初"
    ws.append(TEMPLATES[kind])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_combined_template() -> bytes:
    """一个工作簿、每类一个 sheet 的合并模板。"""
    wb = Workbook()
    wb.remove(wb.active)  # 去掉默认空 sheet
    for kind, title in SHEET_TITLES.items():
        ws = wb.create_sheet(title=title)
        ws.append(TEMPLATES[kind])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _rows_ws(ws):
    out = []
    for idx, values in enumerate(ws.iter_rows(values_only=True), start=1):
        values = list(values)
        if not any(v not in (None, "") for v in values):
            continue
        first = str(values[0]).strip() if values and values[0] is not None else ""
        if idx == 1 or first in _ALL_HEADERS:  # 跳过表头
            continue
        out.append((idx, values))
    return out


def _rows(file):
    """读取上传文件的活动 sheet（单类导入用）。"""
    wb = load_workbook(file, read_only=True, data_only=True)
    return _rows_ws(wb.active)


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


def _apply_stock(company, user, rows):
    """期初库存（数量金额式）：数量 + 金额，均价由系统按 金额/数量 得出。"""
    created = skipped = 0
    errors = []
    for idx, vals in rows:
        code = str(vals[0] or "").strip()
        qty = _dec(vals[1] if len(vals) > 1 else None)
        amount = _dec(vals[2] if len(vals) > 2 else None)
        product = Product.objects.filter(company=company, code=code).first()
        if not product:
            errors.append(f"第{idx}行：商品编码「{code}」不存在"); continue
        if qty is None or amount is None or qty == 0:
            errors.append(f"第{idx}行：数量/金额无效（数量不能为 0、金额必填）"); continue
        if StockMove.objects.filter(company=company, product=product, source_type="Opening").exists():
            skipped += 1; continue
        # 数量金额式：金额直接入账，单价由 post_inbound 按 金额/数量 反算（期初允许负数）
        post_inbound(company, product, qty, ZERO_MONEY, amount=amount, date=OPENING,
                     source_type="Opening", source_no="期初", allow_nonpositive=True)
        created += 1
    return created, skipped, errors


def _apply_payable(company, user, rows):
    created = skipped = 0
    errors = []
    for idx, vals in rows:
        code = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        sup = Supplier.objects.filter(company=company, code=code).first()
        if not sup:
            errors.append(f"第{idx}行：供应商编码「{code}」不存在"); continue
        if amount is None:
            errors.append(f"第{idx}行：金额无效"); continue
        if amount == 0:
            skipped += 1; continue   # 0 视为空行跳过；负数允许（红字期初）
        if PurchaseInvoice.objects.filter(company=company, supplier=sup, is_opening=True).exists():
            skipped += 1; continue
        create_opening_payable(company=company, user=user, supplier=sup, amount=amount, doc_date=OPENING)
        created += 1
    return created, skipped, errors


def _apply_receivable(company, user, rows):
    created = skipped = 0
    errors = []
    for idx, vals in rows:
        code = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        cust = Customer.objects.filter(company=company, code=code).first()
        if not cust:
            errors.append(f"第{idx}行：客户编码「{code}」不存在"); continue
        if amount is None:
            errors.append(f"第{idx}行：金额无效"); continue
        if amount == 0:
            skipped += 1; continue   # 0 视为空行跳过；负数允许（红字期初）
        if SalesInvoice.objects.filter(company=company, customer=cust, is_opening=True).exists():
            skipped += 1; continue
        create_opening_receivable(company=company, user=user, customer=cust, amount=amount, doc_date=OPENING)
        created += 1
    return created, skipped, errors


def _apply_bank(company, user, rows):
    updated = skipped = 0
    errors = []
    for idx, vals in rows:
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


def _apply_note_receivable(company, user, rows):
    created = skipped = 0
    errors = []
    for idx, vals in rows:
        note_no = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        cust = Customer.objects.filter(company=company, code=str(vals[2] or "").strip()).first() if len(vals) > 2 and vals[2] else None
        due = _date(vals[3]) if len(vals) > 3 else None
        if amount is None:
            errors.append(f"第{idx}行：金额无效"); continue
        if amount == 0:
            skipped += 1; continue   # 0 视为空行跳过；负数允许
        if note_no and NoteReceivable.objects.filter(company=company, note_no=note_no).exists():
            skipped += 1; continue
        create_note_receivable(company=company, user=user, draw_date=OPENING, amount=amount,
                               customer=cust, note_no=note_no, due_date=due, is_opening=True)
        created += 1
    return created, skipped, errors


def _apply_note_payable(company, user, rows):
    created = skipped = 0
    errors = []
    for idx, vals in rows:
        note_no = str(vals[0] or "").strip()
        amount = _dec(vals[1] if len(vals) > 1 else None)
        sup = Supplier.objects.filter(company=company, code=str(vals[2] or "").strip()).first() if len(vals) > 2 and vals[2] else None
        due = _date(vals[3]) if len(vals) > 3 else None
        if amount is None:
            errors.append(f"第{idx}行：金额无效"); continue
        if amount == 0:
            skipped += 1; continue   # 0 视为空行跳过；负数允许
        if not sup:
            errors.append(f"第{idx}行：收票供应商编码无效"); continue
        if note_no and NotePayable.objects.filter(company=company, note_no=note_no).exists():
            skipped += 1; continue
        create_note_payable(company=company, user=user, draw_date=OPENING, amount=amount,
                            supplier=sup, note_no=note_no, due_date=due, is_opening=True)
        created += 1
    return created, skipped, errors


_APPLY = {
    "stock": ("期初库存", _apply_stock),
    "payable": ("期初应付", _apply_payable),
    "receivable": ("期初应收", _apply_receivable),
    "bank": ("期初银行存款", _apply_bank),
    "note_receivable": ("期初应收票据", _apply_note_receivable),
    "note_payable": ("期初应付票据", _apply_note_payable),
}


def import_combined(company, user, file):
    """读取合并工作簿，按 sheet 标题逐类导入；返回 [{kind,label,created,skipped,errors}]。"""
    wb = load_workbook(file, read_only=True, data_only=True)
    titles = set(wb.sheetnames)
    results = []
    for kind, title in SHEET_TITLES.items():
        if title not in titles:
            continue
        label, fn = _APPLY[kind]
        rows = _rows_ws(wb[title])
        created, skipped, errors = fn(company, user, rows)
        results.append({"kind": kind, "label": label, "created": created,
                        "skipped": skipped, "errors": errors})
    return results


# 单类导入（保留：旧链接/测试用）。file = 上传文件的活动 sheet。
def import_stock(company, user, file):
    return _apply_stock(company, user, _rows(file))


def import_payable(company, user, file):
    return _apply_payable(company, user, _rows(file))


def import_receivable(company, user, file):
    return _apply_receivable(company, user, _rows(file))


def import_bank(company, user, file):
    return _apply_bank(company, user, _rows(file))


def import_note_receivable(company, user, file):
    return _apply_note_receivable(company, user, _rows(file))


def import_note_payable(company, user, file):
    return _apply_note_payable(company, user, _rows(file))


IMPORTERS = {
    "stock": ("期初库存", import_stock),
    "payable": ("期初应付", import_payable),
    "receivable": ("期初应收", import_receivable),
    "bank": ("期初银行存款", import_bank),
    "note_receivable": ("期初应收票据", import_note_receivable),
    "note_payable": ("期初应付票据", import_note_payable),
}
