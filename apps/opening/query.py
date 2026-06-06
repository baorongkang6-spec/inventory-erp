"""查询中心（M11）：跨公司、按日期/关键字等组合查询各类账务明细。

每个「查询事项」一段，统一返回 {columns, rows, totals}：
- columns：表头列表
- rows：二维数据（每行与 columns 对齐）
- totals：合计行（与 columns 对齐，首列为「合计」，非金额列留空）；无则 None
公司范围由调用方传入（已按用户可见性过滤）。
"""

from decimal import Decimal

from django.db.models import Q

from apps.finance.models import BankJournal, PurchaseInvoice, SalesInvoice
from apps.purchasing.models import PurchaseInboundLine
from apps.sales.models import SalesOutboundLine
from apps.inventory.models import StockMove

Z = Decimal("0.00")

# 事项注册：key → {label, placeholder(关键字提示), extra(额外筛选控件)}
SUBJECTS = {
    "stock_moves":    {"label": "库存流水明细", "placeholder": "商品编码/名称", "extra": ["direction"]},
    "sales_lines":    {"label": "销售出库明细", "placeholder": "客户/商品", "extra": []},
    "purchase_lines": {"label": "采购入库明细", "placeholder": "供应商/商品", "extra": []},
    "bank":           {"label": "银行流水", "placeholder": "账户/摘要/对方", "extra": ["entry_type", "direction"]},
    "receivable":     {"label": "应收明细", "placeholder": "客户", "extra": ["status"]},
    "payable":        {"label": "应付明细", "placeholder": "供应商", "extra": ["status"]},
}

DIRECTION_CHOICES = [("", "全部"), ("in", "收入/入"), ("out", "支出/出")]
STATUS_CHOICES = [("", "全部"), ("open", "仅未结清")]


def _co(company):
    return company.short_name or str(company)


def run_query(subject, companies, dfrom, dto, params):
    fn = _RUNNERS.get(subject)
    if fn is None or not companies:
        return {"columns": [], "rows": [], "totals": None}
    return fn(companies, dfrom, dto, params)


def _totals(columns, idxs, rows):
    """对 idxs 指定的列求和，生成合计行（首列「合计」）。"""
    row = ["" for _ in columns]
    row[0] = "合计"
    for i in idxs:
        row[i] = sum((r[i] for r in rows if isinstance(r[i], Decimal)), Z)
    return row


def _q_stock_moves(companies, dfrom, dto, p):
    qs = StockMove.objects.filter(company__in=companies, date__gte=dfrom, date__lte=dto)
    q = p.get("q")
    if q:
        qs = qs.filter(Q(product__code__icontains=q) | Q(product__name__icontains=q))
    if p.get("direction"):
        qs = qs.filter(direction=p["direction"])
    qs = qs.select_related("company", "product").order_by("company__code", "date", "id")
    cols = ["公司", "日期", "商品", "来源单据", "收入数量", "收入金额", "发出数量", "发出金额"]
    rows = []
    for m in qs:
        is_in = m.direction == StockMove.Direction.IN
        rows.append([_co(m.company), m.date, f"{m.product.code} {m.product.name}", m.source_no,
                     m.quantity if is_in else "", m.amount if is_in else "",
                     "" if is_in else m.quantity, "" if is_in else m.amount])
    return {"columns": cols, "rows": rows, "totals": _totals(cols, [5, 7], rows)}


def _q_sales_lines(companies, dfrom, dto, p):
    qs = SalesOutboundLine.objects.filter(
        outbound__company__in=companies,
        outbound__doc_date__gte=dfrom, outbound__doc_date__lte=dto)
    q = p.get("q")
    if q:
        qs = qs.filter(Q(outbound__customer__name__icontains=q)
                       | Q(product__code__icontains=q) | Q(product__name__icontains=q))
    qs = qs.select_related("outbound", "outbound__company", "outbound__customer", "product")
    qs = qs.order_by("outbound__company__code", "outbound__doc_date", "id")
    cols = ["公司", "日期", "出库单", "客户", "商品", "数量", "含税售额", "结转成本"]
    rows = []
    for ln in qs:
        o = ln.outbound
        rows.append([_co(o.company), o.doc_date, o.doc_no, str(o.customer or ""),
                     f"{ln.product.code} {ln.product.name}", ln.quantity,
                     ln.amount_taxed, ln.amount])
    return {"columns": cols, "rows": rows, "totals": _totals(cols, [6, 7], rows)}


def _q_purchase_lines(companies, dfrom, dto, p):
    qs = PurchaseInboundLine.objects.filter(
        inbound__company__in=companies,
        inbound__doc_date__gte=dfrom, inbound__doc_date__lte=dto)
    q = p.get("q")
    if q:
        qs = qs.filter(Q(inbound__supplier__name__icontains=q)
                       | Q(product__code__icontains=q) | Q(product__name__icontains=q))
    qs = qs.select_related("inbound", "inbound__company", "inbound__supplier", "product")
    qs = qs.order_by("inbound__company__code", "inbound__doc_date", "id")
    cols = ["公司", "日期", "入库单", "供应商", "商品", "数量", "入库成本", "含税金额"]
    rows = []
    for ln in qs:
        ib = ln.inbound
        rows.append([_co(ib.company), ib.doc_date, ib.doc_no, str(ib.supplier or ""),
                     f"{ln.product.code} {ln.product.name}", ln.quantity,
                     ln.amount, ln.amount_taxed])
    return {"columns": cols, "rows": rows, "totals": _totals(cols, [6, 7], rows)}


def _q_bank(companies, dfrom, dto, p):
    qs = BankJournal.objects.filter(company__in=companies, date__gte=dfrom, date__lte=dto)
    q = p.get("q")
    if q:
        qs = qs.filter(Q(bank_account__name__icontains=q) | Q(bank_account__account_no__icontains=q)
                       | Q(summary__icontains=q) | Q(counterparty__icontains=q))
    if p.get("entry_type"):
        qs = qs.filter(entry_type=p["entry_type"])
    if p.get("direction"):
        qs = qs.filter(direction=p["direction"])
    qs = qs.select_related("company", "bank_account").order_by("company__code", "date", "id")
    cols = ["公司", "日期", "业务类型", "摘要", "对方单位", "账户", "收入", "支出"]
    rows = []
    for j in qs:
        is_in = j.direction == BankJournal.Direction.IN
        rows.append([_co(j.company), j.date, j.get_entry_type_display(), j.summary,
                     j.counterparty, str(j.bank_account),
                     j.amount if is_in else "", "" if is_in else j.amount])
    return {"columns": cols, "rows": rows, "totals": _totals(cols, [6, 7], rows)}


def _q_receivable(companies, dfrom, dto, p):
    qs = SalesInvoice.objects.filter(company__in=companies, status=SalesInvoice.Status.REGISTERED,
                                     doc_date__gte=dfrom, doc_date__lte=dto)
    if p.get("q"):
        qs = qs.filter(customer__name__icontains=p["q"])
    qs = qs.select_related("company", "customer").order_by("company__code", "doc_date", "id")
    only_open = p.get("status") == "open"
    cols = ["公司", "开票日期", "单据编号", "客户", "含税(应收)", "已核销", "未核销"]
    rows = []
    for inv in qs:
        if only_open and inv.outstanding <= 0:
            continue
        rows.append([_co(inv.company), inv.doc_date, inv.doc_no, str(inv.customer),
                     inv.amount_taxed, inv.settled_amount, inv.outstanding])
    return {"columns": cols, "rows": rows, "totals": _totals(cols, [4, 5, 6], rows)}


def _q_payable(companies, dfrom, dto, p):
    qs = PurchaseInvoice.objects.filter(company__in=companies, status=PurchaseInvoice.Status.REGISTERED,
                                        doc_date__gte=dfrom, doc_date__lte=dto)
    if p.get("q"):
        qs = qs.filter(supplier__name__icontains=p["q"])
    qs = qs.select_related("company", "supplier").order_by("company__code", "doc_date", "id")
    only_open = p.get("status") == "open"
    cols = ["公司", "开票日期", "单据编号", "供应商", "含税(应付)", "已核销", "未核销"]
    rows = []
    for inv in qs:
        if only_open and inv.outstanding <= 0:
            continue
        rows.append([_co(inv.company), inv.doc_date, inv.doc_no, str(inv.supplier),
                     inv.amount_taxed, inv.settled_amount, inv.outstanding])
    return {"columns": cols, "rows": rows, "totals": _totals(cols, [4, 5, 6], rows)}


_RUNNERS = {
    "stock_moves": _q_stock_moves,
    "sales_lines": _q_sales_lines,
    "purchase_lines": _q_purchase_lines,
    "bank": _q_bank,
    "receivable": _q_receivable,
    "payable": _q_payable,
}
