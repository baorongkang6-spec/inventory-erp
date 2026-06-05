"""银行存款日记账的 Excel 导出 / 导入（openpyxl）。SPEC §7.1。

导入格式（首行表头，列名可按需扩展）：
    日期 | 摘要 | 对方单位 | 收入 | 支出
- 日期接受 Excel 日期或 YYYY-MM-DD / YYYY/MM/DD 文本。
- 收入>0 记为收入方向；否则按支出>0 记为支出方向。
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO

from openpyxl import Workbook, load_workbook

HEADERS = ["日期", "摘要", "对方单位", "收入", "支出", "余额"]


def export_bank_journal(account, rows) -> bytes:
    """rows: [{"j": BankJournal, "balance": Decimal}]，返回 xlsx 字节。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "银行存款日记账"
    ws.append([f"账户：{account.name} {account.account_no}"])
    ws.append(HEADERS)
    for r in rows:
        j = r["j"]
        ws.append([
            j.date.strftime("%Y-%m-%d"),
            j.summary,
            j.counterparty,
            float(j.amount) if j.direction == "in" else None,
            float(j.amount) if j.direction == "out" else None,
            float(r["balance"]),
        ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _to_date(value):
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    if value is None:
        return None
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_decimal(value):
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def parse_bank_journal_xlsx(file):
    """解析上传的 xlsx，返回 (有效行列表, 错误信息列表)。

    每行 dict：{date, summary, counterparty, direction, amount}。
    跳过空行与金额为 0 的行；表头行（含「日期」）自动识别跳过。
    """
    wb = load_workbook(file, read_only=True, data_only=True)
    ws = wb.active
    parsed, errors = [], []
    for idx, values in enumerate(ws.iter_rows(values_only=True), start=1):
        values = list(values)
        if not any(v not in (None, "") for v in values):
            continue
        # 跳过标题/表头行
        first = str(values[0]).strip() if values and values[0] is not None else ""
        if first.startswith("账户") or first == "日期":
            continue
        # 期望至少 5 列：日期 摘要 对方 收入 支出
        cells = (values + [None] * 5)[:5]
        d = _to_date(cells[0])
        if d is None:
            errors.append(f"第 {idx} 行：日期无法识别（{cells[0]!r}）")
            continue
        income = _to_decimal(cells[3])
        outcome = _to_decimal(cells[4])
        if income > 0:
            direction, amount = "in", income
        elif outcome > 0:
            direction, amount = "out", outcome
        else:
            errors.append(f"第 {idx} 行：收入/支出均为 0，已跳过")
            continue
        parsed.append({
            "date": d,
            "summary": str(cells[1] or "").strip(),
            "counterparty": str(cells[2] or "").strip(),
            "direction": direction,
            "amount": amount,
        })
    return parsed, errors
