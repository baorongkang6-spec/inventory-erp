"""通用 Excel 导出（M7-7 / #10）。

- xlsx_response(filename, headers, rows)：把二维数据写成下载响应；
- resolve_cell(obj, accessor)：accessor 支持属性名 / "a__b" 跨级 / callable / get_x_display。
列表视图经 ExportMixin 一键导出（沿用当前筛选后的查询集），报表视图直接调 xlsx_response。
"""

from datetime import date, datetime
from decimal import Decimal

from django.http import HttpResponse
from openpyxl import Workbook
from openpyxl.styles import Font

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def resolve_cell(obj, accessor):
    """取值：callable(obj) / 字段路径 'supplier__name' / 'get_status_display' 自动调用。"""
    if callable(accessor):
        return accessor(obj)
    cur = obj
    for part in accessor.split("__"):
        if cur is None:
            return ""
        cur = getattr(cur, part, None)
        if callable(cur):  # 如 get_status_display
            cur = cur()
    return cur


def _norm(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float, str)):
        return v
    return str(v)  # 模型实例 / 其它对象统一转字符串，避免 openpyxl 写入报错


def xlsx_response(filename, headers, rows, sheet_title="数据"):
    """headers: [str]；rows: [[cell,...]]。返回 xlsx 下载 HttpResponse。"""
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or "数据")[:31]
    ws.append(list(headers))
    bold = Font(bold=True)
    for c in ws[1]:
        c.font = bold
    for row in rows:
        ws.append([_norm(v) for v in row])
    resp = HttpResponse(content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    wb.save(resp)
    return resp
