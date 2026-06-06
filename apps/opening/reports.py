"""总经理跨公司总览表聚合（M5-2 / M7-4，SPEC §9.1）。

每项含 期初 / 本期收入 / 本期发出 / 期末结存，按业务日期区间 [dfrom, dto] 统计：
- 期初 = 区间起始日之前的累计净额（含期初导入数据，它们日期=启用日）；
- 本期收入/发出 = 区间内的增减；
- 期末 = 期初 + 本期收入 − 本期发出。
全按业务日期口径（银行日记账 date、库存流水 date、发票 doc_date、票据 draw_date、
核销按 created_at 日期）。金额维度汇总（库存数量异构不跨商品相加）。
"""

from decimal import Decimal

from django.db.models import Sum

from apps.finance.models import (
    BankAccount,
    BankJournal,
    NotePayable,
    NoteReceivable,
    NoteSettlement,
    PaymentAllocation,
    PurchaseInvoice,
    ReceiptAllocation,
    SalesInvoice,
)
from apps.inventory.models import StockBalance, StockMove

Z = Decimal("0.00")


def _s(qs, field="amount"):
    return qs.aggregate(v=Sum(field))["v"] or Z


def _before(qs, dfield, dfrom, field="amount"):
    return _s(qs.filter(**{f"{dfield}__lt": dfrom}), field)


def _in(qs, dfield, dfrom, dto, field="amount"):
    return _s(qs.filter(**{f"{dfield}__gte": dfrom, f"{dfield}__lte": dto}), field)


def _row(opening, income, outgo, ending):
    return {"opening": opening, "income": income, "outgo": outgo, "ending": ending}


def _period(inc_qs, dec_qs, inc_dfield, dec_dfield, dfrom, dto, inc_field="amount", dec_field="amount"):
    """通用：按日期把增/减拆成 期初(区间前净)/本期增/本期减/期末。"""
    opening = _before(inc_qs, inc_dfield, dfrom, inc_field) - _before(dec_qs, dec_dfield, dfrom, dec_field)
    income = _in(inc_qs, inc_dfield, dfrom, dto, inc_field)
    outgo = _in(dec_qs, dec_dfield, dfrom, dto, dec_field)
    return _row(opening, income, outgo, opening + income - outgo)


def company_overview(company, dfrom, dto):
    """返回 dict：5 类各自 {opening, income, outgo, ending}，按 [dfrom,dto] 统计。"""
    # 银行存款（期初余额 + 区间前日记账净额 = 期初）
    bank_open0 = BankAccount.objects.filter(company=company).aggregate(v=Sum("opening_balance"))["v"] or Z
    bj = BankJournal.objects.filter(company=company)
    bj_in, bj_out = bj.filter(direction=BankJournal.Direction.IN), bj.filter(direction=BankJournal.Direction.OUT)
    b = _period(bj_in, bj_out, "date", "date", dfrom, dto)
    b["opening"] += bank_open0
    b["ending"] += bank_open0
    bank = b

    # 库存商品（金额）：IN/OUT 流水按 date
    moves = StockMove.objects.filter(company=company)
    stock = _period(moves.filter(direction=StockMove.Direction.IN),
                    moves.filter(direction=StockMove.Direction.OUT), "date", "date", dfrom, dto)

    # 供应商往来（应付）：增=采购发票(doc_date,含税)；减=付款核销+票据抵付(created_at)
    ap_inv = PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED)
    ap_pay = PaymentAllocation.objects.filter(payment__company=company)
    ap_note = NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE)
    payable = _merge_period(ap_inv, "doc_date", "amount_taxed",
                            [(ap_pay, "created_at__date"), (ap_note, "created_at__date")], dfrom, dto)

    # 客户往来（应收）：增=销售发票；减=收款核销+应收票据冲应收
    ar_inv = SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED)
    ar_rec = ReceiptAllocation.objects.filter(receipt__company=company)
    ar_note = NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES,
                                            is_endorsement=False)
    receivable = _merge_period(ar_inv, "doc_date", "amount_taxed",
                               [(ar_rec, "created_at__date"), (ar_note, "created_at__date")], dfrom, dto)

    # 应收票据：增=票据(draw_date)；减=票据使用(冲应收/背书，note_kind=ar_note)
    nr = NoteReceivable.objects.filter(company=company).exclude(status=NoteReceivable.Status.VOID)
    nr_use = NoteSettlement.objects.filter(company=company, note_kind=NoteSettlement.NoteKind.RECEIVABLE)
    note_recv = _merge_period(nr, "draw_date", "amount",
                              [(nr_use, "created_at__date")], dfrom, dto)

    return {"bank": bank, "stock": stock, "payable": payable,
            "receivable": receivable, "note_recv": note_recv}


def _merge_period(inc_qs, inc_dfield, inc_field, dec_specs, dfrom, dto):
    """增项一个 qs，减项可多个 (qs, dfield) 求和。"""
    opening = _before(inc_qs, inc_dfield, dfrom, inc_field)
    income = _in(inc_qs, inc_dfield, dfrom, dto, inc_field)
    outgo = Z
    for qs, dfield in dec_specs:
        opening -= _before(qs, dfield, dfrom)
        outgo += _in(qs, dfield, dfrom, dto)
    return _row(opening, income, outgo, opening + income - outgo)


# (key, 标签, 下钻报表 url 名)
CATEGORIES = [
    ("bank", "银行存款", "bank_journal_report"),
    ("stock", "库存商品（金额）", "stock_report"),
    ("payable", "供应商往来（应付）", "payables_report"),
    ("receivable", "客户往来（应收）", "receivables_report"),
    ("note_recv", "应收票据", "notes_balance_report"),
]


def overview_table(companies, dfrom, dto):
    """组织成模板友好结构：每类一张表，行=各公司+合计。"""
    per = {c.pk: company_overview(c, dfrom, dto) for c in companies}
    blocks = []
    for key, label, url in CATEGORIES:
        rows = []
        totals = _row(Z, Z, Z, Z)
        for c in companies:
            r = per[c.pk][key]
            rows.append({"company": c, **r})
            for k in ("opening", "income", "outgo", "ending"):
                totals[k] += r[k]
        blocks.append({"key": key, "label": label, "url": url,
                       "rows": rows, "totals": totals})
    return blocks


# ============================= 月底对账（M5-3）================================
def recon_lines(company, category):
    """返回某类别的系统侧对账行：[{label, system_amount}]。"""
    from apps.finance.models import NoteReceivable, PurchaseInvoice, SalesInvoice
    out = []
    if category == "bank":
        for acc in BankAccount.objects.filter(company=company).order_by("name"):
            jin = _s(BankJournal.objects.filter(company=company, bank_account=acc,
                                                direction=BankJournal.Direction.IN))
            jout = _s(BankJournal.objects.filter(company=company, bank_account=acc,
                                                 direction=BankJournal.Direction.OUT))
            out.append({"label": str(acc), "system_amount": acc.opening_balance + jin - jout})
    elif category == "note_recv":
        for n in NoteReceivable.objects.filter(company=company).exclude(
                status=NoteReceivable.Status.VOID).order_by("doc_no"):
            if n.unused > 0:
                out.append({"label": f"{n.doc_no} {n.note_no}", "system_amount": n.unused})
    elif category == "stock":
        for b in StockBalance.objects.filter(company=company).select_related("product").order_by("product__code"):
            if b.amount or b.quantity:
                out.append({"label": f"{b.product.code} {b.product.name}（{b.quantity}）",
                            "system_amount": b.amount})
    elif category == "receivable":
        agg = {}
        for inv in SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED).select_related("customer"):
            if inv.outstanding:
                agg[inv.customer] = agg.get(inv.customer, Z) + inv.outstanding
        for cust, amt in sorted(agg.items(), key=lambda kv: kv[0].code):
            out.append({"label": str(cust), "system_amount": amt})
    elif category == "payable":
        agg = {}
        for inv in PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED).select_related("supplier"):
            if inv.outstanding:
                agg[inv.supplier] = agg.get(inv.supplier, Z) + inv.outstanding
        for sup, amt in sorted(agg.items(), key=lambda kv: kv[0].code):
            out.append({"label": str(sup), "system_amount": amt})
    return out
