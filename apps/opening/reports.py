"""总经理跨公司总览表聚合（M5-2，SPEC §9.1）。

每项含 期初 / 本期收入 / 本期发出 / 期末结存。
口径：期初 = 期初标记数据（is_opening / source=Opening）；本期 = 全部非期初活动
（系统自启用日起仅一个区间）；期末 = 当前余额。四列满足 期初+收入-发出=期末。
金额维度汇总（库存数量异构不跨商品相加，明细见库存报表）。
"""

from decimal import Decimal

from django.db.models import Sum

from apps.finance.models import (
    BankAccount,
    BankJournal,
    NoteReceivable,
    PurchaseInvoice,
    SalesInvoice,
)
from apps.inventory.models import StockBalance, StockMove

Z = Decimal("0.00")


def _s(qs, field="amount"):
    return qs.aggregate(v=Sum(field))["v"] or Z


def _row(opening, income, outgo, ending):
    return {"opening": opening, "income": income, "outgo": outgo, "ending": ending}


def company_overview(company):
    """返回 dict：5 类各自 {opening, income, outgo, ending}。"""
    # 银行存款
    bank_open = BankAccount.objects.filter(company=company).aggregate(v=Sum("opening_balance"))["v"] or Z
    bank_in = _s(BankJournal.objects.filter(company=company, direction=BankJournal.Direction.IN))
    bank_out = _s(BankJournal.objects.filter(company=company, direction=BankJournal.Direction.OUT))
    bank = _row(bank_open, bank_in, bank_out, bank_open + bank_in - bank_out)

    # 库存商品（金额）
    moves = StockMove.objects.filter(company=company)
    st_open = _s(moves.filter(direction=StockMove.Direction.IN, source_type="Opening"))
    st_in = _s(moves.filter(direction=StockMove.Direction.IN).exclude(source_type="Opening"))
    st_out = _s(moves.filter(direction=StockMove.Direction.OUT))
    st_end = StockBalance.objects.filter(company=company).aggregate(v=Sum("amount"))["v"] or Z
    stock = _row(st_open, st_in, st_out, st_end)

    # 供应商往来（应付）
    ap = PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED)
    ap_open = _s(ap.filter(is_opening=True), "amount_taxed")
    ap_add = _s(ap.filter(is_opening=False), "amount_taxed")
    ap_reduce = _s(ap, "settled_amount")
    payable = _row(ap_open, ap_add, ap_reduce, ap_open + ap_add - ap_reduce)

    # 客户往来（应收）
    ar = SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED)
    ar_open = _s(ar.filter(is_opening=True), "amount_taxed")
    ar_add = _s(ar.filter(is_opening=False), "amount_taxed")
    ar_reduce = _s(ar, "settled_amount")
    receivable = _row(ar_open, ar_add, ar_reduce, ar_open + ar_add - ar_reduce)

    # 应收票据
    nr = NoteReceivable.objects.filter(company=company).exclude(status=NoteReceivable.Status.VOID)
    nr_open = _s(nr.filter(is_opening=True))
    nr_add = _s(nr.filter(is_opening=False))
    nr_reduce = _s(nr, "settled_amount")
    note_recv = _row(nr_open, nr_add, nr_reduce, nr_open + nr_add - nr_reduce)

    return {
        "bank": bank, "stock": stock, "payable": payable,
        "receivable": receivable, "note_recv": note_recv,
    }


CATEGORIES = [
    ("bank", "银行存款"),
    ("stock", "库存商品（金额）"),
    ("payable", "供应商往来（应付）"),
    ("receivable", "客户往来（应收）"),
    ("note_recv", "应收票据"),
]


def overview_table(companies):
    """组织成模板友好结构：每类一张表，行=各公司+合计。"""
    per = {c.pk: company_overview(c) for c in companies}
    blocks = []
    for key, label in CATEGORIES:
        rows = []
        totals = _row(Z, Z, Z, Z)
        for c in companies:
            r = per[c.pk][key]
            rows.append({"company": c, **r})
            for k in ("opening", "income", "outgo", "ending"):
                totals[k] += r[k]
        blocks.append({"key": key, "label": label, "rows": rows, "totals": totals})
    return blocks
