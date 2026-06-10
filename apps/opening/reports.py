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

from apps.core.money import round_money, round_qty

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
    # SQLite 对 DecimalField 的 SUM 返回浮点→Decimal 会带长尾零，统一量化为 2 位
    return round_money(qs.aggregate(v=Sum(field))["v"] or Z)


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
    bank_open0 = round_money(
        BankAccount.objects.filter(company=company).aggregate(v=Sum("opening_balance"))["v"] or Z)
    bj = BankJournal.objects.filter(company=company)
    bj_in, bj_out = bj.filter(direction=BankJournal.Direction.IN), bj.filter(direction=BankJournal.Direction.OUT)
    b = _period(bj_in, bj_out, "date", "date", dfrom, dto)
    b["opening"] += bank_open0
    b["ending"] += bank_open0
    bank = b

    # 库存商品（金额）：期初流水(source_type=Opening)恒计期初，其余按 date
    moves = StockMove.objects.filter(company=company)
    biz = moves.exclude(source_type="Opening")
    stock = _period(biz.filter(direction=StockMove.Direction.IN),
                    biz.filter(direction=StockMove.Direction.OUT), "date", "date", dfrom, dto)
    open_moves = moves.filter(source_type="Opening")
    _add_opening(stock, _s(open_moves.filter(direction=StockMove.Direction.IN))
                 - _s(open_moves.filter(direction=StockMove.Direction.OUT)))

    # 供应商往来（应付）：期初发票(is_opening)恒计期初；增=本期采购发票；减=付款核销+票据抵付
    ap_all = PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED)
    ap_inv = ap_all.filter(is_opening=False)
    ap_pay = PaymentAllocation.objects.filter(payment__company=company)
    ap_note = NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE)
    payable = _merge_period(ap_inv, "doc_date", "amount_taxed",
                            [(ap_pay, "created_at__date"), (ap_note, "created_at__date")], dfrom, dto)
    _add_opening(payable, _s(ap_all.filter(is_opening=True), "amount_taxed"))

    # 客户往来（应收）：期初发票恒计期初；增=本期销售发票；减=收款核销+应收票据冲应收
    ar_all = SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED)
    ar_inv = ar_all.filter(is_opening=False)
    ar_rec = ReceiptAllocation.objects.filter(receipt__company=company)
    ar_note = NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES,
                                            is_endorsement=False)
    receivable = _merge_period(ar_inv, "doc_date", "amount_taxed",
                               [(ar_rec, "created_at__date"), (ar_note, "created_at__date")], dfrom, dto)
    _add_opening(receivable, _s(ar_all.filter(is_opening=True), "amount_taxed"))

    # 应收票据：期初票据(is_opening)恒计期初；增=本期出票；减=票据使用
    nr_all = NoteReceivable.objects.filter(company=company).exclude(status=NoteReceivable.Status.VOID)
    nr = nr_all.filter(is_opening=False)
    nr_use = NoteSettlement.objects.filter(company=company, note_kind=NoteSettlement.NoteKind.RECEIVABLE)
    note_recv = _merge_period(nr, "draw_date", "amount",
                              [(nr_use, "created_at__date")], dfrom, dto)
    _add_opening(note_recv, _s(nr_all.filter(is_opening=True), "amount"))

    return {"bank": bank, "stock": stock, "payable": payable,
            "receivable": receivable, "note_recv": note_recv}


def _add_opening(row, amt):
    """把期初标记数据的金额恒加到期初与期末（不计入本期收入/发出）。"""
    row["opening"] += amt
    row["ending"] += amt
    return row


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
    ("bank", "银行存款", "bank_accounts_report"),
    ("stock", "库存商品（金额）", "stock_products_report"),
    ("payable", "供应商往来（应付）", "payable_partners_report"),
    ("receivable", "客户往来（应收）", "receivable_partners_report"),
    ("note_recv", "应收票据", "receivable_notes_report"),
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


# ============================= 账户余额表（M7-6 / #8）=========================
def _pset():
    return {"opening": Z, "income": Z, "outgo": Z}


def bank_accounts_balance(company, dfrom, dto):
    """某公司各银行账户在 [dfrom, dto] 的 期初/本期收入/本期发出/期末（带 account 对象，供下钻）。

    期初 = 账户期初余额 + 区间前日记账净额；当期无发生时 期初=期末。
    """
    rows = []
    for acc in BankAccount.objects.filter(company=company).order_by("name"):
        bj = BankJournal.objects.filter(company=company, bank_account=acc)
        r = _period(bj.filter(direction=BankJournal.Direction.IN),
                    bj.filter(direction=BankJournal.Direction.OUT), "date", "date", dfrom, dto)
        r["opening"] += acc.opening_balance
        r["ending"] += acc.opening_balance
        rows.append({"account": acc, **r})
    return rows


def stock_products_balance(company, dfrom, dto):
    """某公司各库存商品在 [dfrom, dto] 的 期初/本期收入/本期发出/期末（金额+数量，带 product 对象）。

    金额维度同总览（移动加权金额），同时给出数量便于下钻。当期无发生 期初=期末。
    """
    ZQ = Decimal("0.000")
    moves = StockMove.objects.filter(company=company).select_related("product")
    data = {}
    for m in moves:
        d = data.setdefault(m.product, {"opening": Z, "income": Z, "outgo": Z,
                                        "open_qty": ZQ, "in_qty": ZQ, "out_qty": ZQ})
        is_in = m.direction == StockMove.Direction.IN
        if m.source_type == "Opening" or m.date < dfrom:
            d["opening"] += m.amount if is_in else -m.amount
            d["open_qty"] += m.quantity if is_in else -m.quantity
        elif m.date <= dto:
            if is_in:
                d["income"] += m.amount
                d["in_qty"] += m.quantity
            else:
                d["outgo"] += m.amount
                d["out_qty"] += m.quantity
    rows = []
    for product, d in sorted(data.items(), key=lambda kv: kv[0].code):
        rows.append({
            "product": product,
            "opening": d["opening"], "income": d["income"], "outgo": d["outgo"],
            "ending": d["opening"] + d["income"] - d["outgo"],
            "ending_qty": d["open_qty"] + d["in_qty"] - d["out_qty"],
        })
    return rows


def account_balance_table(companies, dfrom, dto):
    """三公司明细账户余额：银行分账户 / 库存按品种 / 应付按供应商 / 应收按客户。

    每行 期初(区间前净)/本期收入(增)/本期发出(减)/期末。低数据量，用 Python 归并。
    """
    bank_rows, stock_rows, ap_rows, ar_rows = [], [], [], []
    for company in companies:
        # 银行：每账户
        for r in bank_accounts_balance(company, dfrom, dto):
            bank_rows.append({"company": company, "name": str(r["account"]),
                              "opening": r["opening"], "income": r["income"],
                              "outgo": r["outgo"], "ending": r["ending"]})

        # 库存：每商品（金额）
        for r in stock_products_balance(company, dfrom, dto):
            stock_rows.append({"company": company,
                               "name": f"{r['product'].code} {r['product'].name}",
                               "opening": r["opening"], "income": r["income"],
                               "outgo": r["outgo"], "ending": r["ending"]})

        # 应付：每供应商
        ap_rows += _partner_rows(
            company,
            PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED),
            "supplier",
            PaymentAllocation.objects.filter(payment__company=company).select_related("invoice__supplier"),
            lambda a: a.invoice.supplier,
            NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE),
            PurchaseInvoice, dfrom, dto)
        # 应收：每客户
        ar_rows += _partner_rows(
            company,
            SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED),
            "customer",
            ReceiptAllocation.objects.filter(receipt__company=company).select_related("invoice__customer"),
            lambda a: a.invoice.customer,
            NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES,
                                          is_endorsement=False),
            SalesInvoice, dfrom, dto)

    return {"bank": bank_rows, "stock": stock_rows, "payable": ap_rows, "receivable": ar_rows}


def _partner_balance(company, invoices, partner_attr, allocations, alloc_partner, note_settlements,
                     invoice_model, dfrom, dto):
    """按往来对象归并 期初/增/减，保留 partner 对象。增=发票(doc_date)，减=核销+票据(created_at)。"""
    data = {}
    for inv in invoices.select_related(partner_attr):
        partner = getattr(inv, partner_attr)
        d = data.setdefault(partner, _pset())
        if inv.is_opening or inv.doc_date < dfrom:
            d["opening"] += inv.amount_taxed
        elif inv.doc_date <= dto:
            d["income"] += inv.amount_taxed
    for a in allocations:
        partner = alloc_partner(a)
        d = data.setdefault(partner, _pset())
        ad = a.created_at.date()
        if ad < dfrom:
            d["opening"] -= a.amount
        elif ad <= dto:
            d["outgo"] += a.amount
    inv_partner = {i.pk: getattr(i, partner_attr) for i in invoice_model.objects.filter(company=company)}
    for ns in note_settlements:
        partner = inv_partner.get(ns.invoice_id)
        if partner is None:
            continue
        d = data.setdefault(partner, _pset())
        nd = ns.created_at.date()
        if nd < dfrom:
            d["opening"] -= ns.amount
        elif nd <= dto:
            d["outgo"] += ns.amount
    rows = []
    for partner, d in sorted(data.items(), key=lambda kv: kv[0].code):
        ending = d["opening"] + d["income"] - d["outgo"]
        if d["opening"] or d["income"] or d["outgo"] or ending:
            rows.append({"partner": partner, "opening": d["opening"], "income": d["income"],
                         "outgo": d["outgo"], "ending": ending})
    return rows


def _partner_rows(company, invoices, partner_attr, allocations, alloc_partner, note_settlements,
                  invoice_model, dfrom, dto):
    """account_balance_table 用：在 _partner_balance 基础上加 company/name 字段。"""
    return [{"company": company, "name": str(r["partner"]), "opening": r["opening"],
             "income": r["income"], "outgo": r["outgo"], "ending": r["ending"]}
            for r in _partner_balance(company, invoices, partner_attr, allocations, alloc_partner,
                                      note_settlements, invoice_model, dfrom, dto)]


def payable_partners_balance(company, dfrom, dto):
    """某公司各供应商应付 期初/本期增/本期减/期末（带 partner 对象，供下钻）。"""
    return _partner_balance(
        company,
        PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED),
        "supplier",
        PaymentAllocation.objects.filter(payment__company=company).select_related("invoice__supplier"),
        lambda a: a.invoice.supplier,
        NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE),
        PurchaseInvoice, dfrom, dto)


def receivable_partners_balance(company, dfrom, dto):
    """某公司各客户应收 期初/本期增/本期减/期末（带 partner 对象，供下钻）。"""
    return _partner_balance(
        company,
        SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED),
        "customer",
        ReceiptAllocation.objects.filter(receipt__company=company).select_related("invoice__customer"),
        lambda a: a.invoice.customer,
        NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES,
                                      is_endorsement=False),
        SalesInvoice, dfrom, dto)


def invoice_aging(model, partner_attr, companies, asof):
    """按 (公司, 往来对象) 汇总逾期金额与账龄分桶。

    逾期天数 = (asof 期末日 − 开票日) − 账期(天)；>0 即逾期，金额取发票未核销额(>0)。
    桶：b1 ≤90天(3个月以内) / b2 91-180(3-6月) / b3 181-365(6月-1年) / b4 >365(1年以上)。
    返回 {(company_id, partner_id): {overdue,b1,b2,b3,b4}}。
    """
    companies = list(companies)
    res = {}
    if not companies:
        return res
    qs = (model.objects.filter(company__in=companies, status=model.Status.REGISTERED,
                               doc_date__lte=asof)
          .only("company_id", f"{partner_attr}_id", "doc_date", "term_days",
                "amount_taxed", "settled_amount"))
    for inv in qs:
        out = inv.amount_taxed - inv.settled_amount
        if out <= 0:
            continue
        past = (asof - inv.doc_date).days - (inv.term_days or 0)
        if past <= 0:
            continue
        key = (inv.company_id, getattr(inv, f"{partner_attr}_id"))
        b = res.setdefault(key, {"overdue": Z, "b1": Z, "b2": Z, "b3": Z, "b4": Z})
        b["overdue"] += out
        if past <= 90:
            b["b1"] += out
        elif past <= 180:
            b["b2"] += out
        elif past <= 365:
            b["b3"] += out
        else:
            b["b4"] += out
    return res


def invoice_quota_usage(companies, asof):
    """剩余发票额度查询：每家公司 本年/本月开票额(不含税) + 额度 + 剩余。

    开票额 = 已开具(非作废、非期初)销售发票的不含税金额，按 asof 的年/月统计。
    剩余 = 额度 − 本月开票额。
    """
    from apps.finance.models import SalesInvoice
    from apps.masterdata.models import InvoiceQuota
    companies = list(companies)
    rows = []
    for c in companies:
        base = SalesInvoice.objects.filter(company=c, status=SalesInvoice.Status.REGISTERED,
                                           is_opening=False)
        year_amt = round_money(base.filter(doc_date__year=asof.year)
                               .aggregate(v=Sum("amount_untaxed"))["v"] or Z)
        month_amt = round_money(base.filter(doc_date__year=asof.year, doc_date__month=asof.month)
                                .aggregate(v=Sum("amount_untaxed"))["v"] or Z)
        q = InvoiceQuota.objects.filter(company=c).first()
        quota = q.amount if q else Z
        rows.append({"company": c, "year_amt": year_amt, "month_amt": month_amt,
                     "quota": quota, "remain": round_money(quota - month_amt)})
    return rows


def aging_bucket(past_days):
    """逾期天数 → 账龄段中文标签。"""
    if past_days <= 90:
        return "3个月以内"
    if past_days <= 180:
        return "3-6个月"
    if past_days <= 365:
        return "6个月-1年"
    return "1年以上"


def overdue_invoice_list(model, partner_attr, companies, asof):
    """逾期发票明细：每张已登记、有未核销且已过账期的发票一行（按 asof 期末日判定）。"""
    companies = list(companies)
    rows = []
    if not companies:
        return rows
    qs = (model.objects.filter(company__in=companies, status=model.Status.REGISTERED,
                               doc_date__lte=asof)
          .select_related("company", partner_attr)
          .order_by("company__code", f"{partner_attr}__code", "doc_date"))
    for inv in qs:
        out = inv.amount_taxed - inv.settled_amount
        if out <= 0:
            continue
        past = (asof - inv.doc_date).days - (inv.term_days or 0)
        if past <= 0:
            continue
        rows.append({
            "company": inv.company, "partner": getattr(inv, partner_attr), "inv": inv,
            "doc_no": inv.doc_no, "invoice_no": inv.invoice_no, "doc_date": inv.doc_date,
            "term_days": inv.term_days or 0, "due_date": inv.due_date,
            "overdue_days": past, "outstanding": out, "bucket": aging_bucket(past),
        })
    return rows


def partner_ledger(company, partner, kind, dfrom, dto):
    """往来对象明细账：发票(增) + 核销/票据(减) 按时间滚动余额。kind ∈ {payable, receivable}。

    返回 {opening, rows:[{date,kind,doc_no,inc,dec,balance}], income, outgo, ending}。
    """
    from apps.core.docrefs import doc_url, invoice_url
    if kind == "payable":
        invoices = PurchaseInvoice.objects.filter(
            company=company, supplier=partner, status=PurchaseInvoice.Status.REGISTERED)
        allocs = PaymentAllocation.objects.filter(
            payment__company=company, invoice__supplier=partner).select_related("payment")
        alloc_label, alloc_doc = "付款核销", lambda a: a.payment.doc_no
        alloc_url = lambda a: doc_url("Payment", a.payment_id)
        inv_url = lambda inv: doc_url("PurchaseInvoice", inv.pk)
        notes = NoteSettlement.objects.filter(
            company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE)
        inv_ids = set(PurchaseInvoice.objects.filter(
            company=company, supplier=partner).values_list("pk", flat=True))
        inv_label = "采购发票"
    else:
        invoices = SalesInvoice.objects.filter(
            company=company, customer=partner, status=SalesInvoice.Status.REGISTERED)
        allocs = ReceiptAllocation.objects.filter(
            receipt__company=company, invoice__customer=partner).select_related("receipt")
        alloc_label, alloc_doc = "收款核销", lambda a: a.receipt.doc_no
        alloc_url = lambda a: doc_url("Receipt", a.receipt_id)
        inv_url = lambda inv: doc_url("SalesInvoice", inv.pk)
        notes = NoteSettlement.objects.filter(
            company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES, is_endorsement=False)
        inv_ids = set(SalesInvoice.objects.filter(
            company=company, customer=partner).values_list("pk", flat=True))
        inv_label = "销售发票"

    events = []
    for inv in invoices:
        events.append({"date": inv.doc_date, "kind": inv_label, "doc_no": inv.doc_no,
                       "inc": inv.amount_taxed, "dec": Z, "ref_url": inv_url(inv),
                       "is_opening": inv.is_opening})
    for a in allocs:
        events.append({"date": a.created_at.date(), "kind": alloc_label, "doc_no": alloc_doc(a),
                       "inc": Z, "dec": a.amount, "ref_url": alloc_url(a)})
    for ns in notes.filter(invoice_id__in=inv_ids):
        events.append({"date": ns.created_at.date(), "kind": "票据抵付", "doc_no": ns.note_no,
                       "inc": Z, "dec": ns.amount,
                       "ref_url": invoice_url(ns.invoice_kind, ns.invoice_id)})

    opening = Z
    period = []
    for e in events:
        if e.get("is_opening") or e["date"] < dfrom:
            opening += e["inc"] - e["dec"]
        elif e["date"] <= dto:
            period.append(e)
    period.sort(key=lambda e: (e["date"], 0 if e["inc"] else 1))
    bal, income, outgo, rows = opening, Z, Z, []
    for e in period:
        bal += e["inc"] - e["dec"]
        income += e["inc"]
        outgo += e["dec"]
        rows.append({**e, "balance": bal})
    return {"opening": opening, "rows": rows, "income": income, "outgo": outgo,
            "ending": opening + income - outgo}


def sales_revenue_cost_by_outbound(company, dfrom, dto):
    """销售收入成本计算表（按出库口径，按商品）。

    以销售出库行(出库日落在区间、非作废、方式=销售)直接汇总：数量、销售收入(不含税)、
    结转成本(移动加权)。出库行本身收入成本数量齐全，无缺口。借出/归还不计入。
    """
    from apps.sales.models import SalesOutbound, SalesOutboundLine
    ZQ = Decimal("0.000")
    lines = (SalesOutboundLine.objects
             .filter(outbound__company=company,
                     outbound__sales_type=SalesOutbound.SalesType.SALE,
                     outbound__doc_date__gte=dfrom, outbound__doc_date__lte=dto)
             .exclude(outbound__status=SalesOutbound.Status.VOID)
             .select_related("product"))
    data = {}
    for ln in lines:
        d = data.setdefault(ln.product_id, {"product": ln.product, "qty": ZQ,
                                            "revenue": Z, "cost": Z})
        d["qty"] += ln.quantity
        d["revenue"] += ln.amount_untaxed
        d["cost"] += ln.amount
    rows = []
    for d in sorted(data.values(), key=lambda x: x["product"].code if x["product"] else ""):
        profit = d["revenue"] - d["cost"]
        margin = (profit / d["revenue"] * 100).quantize(Decimal("0.1")) if d["revenue"] else Z
        rows.append({"product": d["product"], "qty": d["qty"], "revenue": d["revenue"],
                     "cost": d["cost"], "profit": profit, "margin": margin})
    return rows


def shipped_uninvoiced(companies, dfrom=None, dto=None):
    """已出库未开具发票明细：销售出库行中「出库数量 − 已开票数量 ≠ 0」的行。

    companies 为公司列表（支持多公司联合查询）。已开票数量 = 关联该出库行的销售发票行数量之和
    (不含作废)。金额取未开票部分(按出库行单价×未开票数量换算不含税/含税)。可按出库日期区间过滤。
    """
    from django.db.models import Sum

    from apps.finance.models import SalesInvoice, SalesInvoiceLine
    from apps.sales.models import SalesOutbound, SalesOutboundLine
    ZQ = Decimal("0.000")
    one = Decimal(1)
    companies = list(companies)
    if not companies:
        return []
    qs = (SalesOutboundLine.objects
          .filter(outbound__company__in=companies, outbound__sales_type=SalesOutbound.SalesType.SALE)
          .exclude(outbound__status=SalesOutbound.Status.VOID)
          .select_related("outbound", "outbound__company", "outbound__customer", "product"))
    if dfrom:
        qs = qs.filter(outbound__doc_date__gte=dfrom)
    if dto:
        qs = qs.filter(outbound__doc_date__lte=dto)

    invoiced = {r["source_outbound_line"]: round_qty(r["q"] or ZQ) for r in
                SalesInvoiceLine.objects.filter(source_outbound_line__in=qs)
                .exclude(invoice__status=SalesInvoice.Status.VOID)
                .values("source_outbound_line").annotate(q=Sum("quantity"))}

    rows = []
    for ln in qs.order_by("outbound__company__code", "outbound__customer__code",
                          "outbound__doc_no", "id"):
        billed = invoiced.get(ln.pk, ZQ)
        remain = ln.quantity - billed
        if remain == 0:
            continue
        unit_u = (ln.amount_untaxed / ln.quantity) if ln.quantity else Z
        ru = round_money(remain * unit_u)
        rt = round_money(ru * (one + ln.tax_rate))
        rows.append({
            "company": ln.outbound.company, "customer": ln.outbound.customer,
            "outbound": ln.outbound, "product": ln.product,
            "out_qty": ln.quantity, "billed_qty": billed, "remain_qty": remain,
            "untaxed": ru, "taxed": rt,
        })
    return rows


def received_uninvoiced(companies, dfrom=None, dto=None):
    """已入库未收到发票明细：采购入库行中「入库数量 − 已收票数量 ≠ 0」的行。

    作为库存商品「暂估」的依据。companies 为公司列表（支持多公司联合查询）。
    已收票数量 = 关联该入库行的采购发票行数量之和(不含作废)。金额取未收票部分
    (按入库行单价×未收票数量换算不含税/含税)。可按入库日期区间过滤。
    """
    from django.db.models import Sum

    from apps.finance.models import PurchaseInvoice, PurchaseInvoiceLine
    from apps.purchasing.models import PurchaseInbound, PurchaseInboundLine
    ZQ = Decimal("0.000")
    one = Decimal(1)
    companies = list(companies)
    if not companies:
        return []
    qs = (PurchaseInboundLine.objects
          .filter(inbound__company__in=companies,
                  inbound__purchase_type=PurchaseInbound.PurchaseType.EXTERNAL)
          .exclude(inbound__status=PurchaseInbound.Status.VOID)
          .select_related("inbound", "inbound__company", "inbound__supplier", "product"))
    if dfrom:
        qs = qs.filter(inbound__doc_date__gte=dfrom)
    if dto:
        qs = qs.filter(inbound__doc_date__lte=dto)

    invoiced = {r["source_inbound_line"]: round_qty(r["q"] or ZQ) for r in
                PurchaseInvoiceLine.objects.filter(source_inbound_line__in=qs)
                .exclude(invoice__status=PurchaseInvoice.Status.VOID)
                .values("source_inbound_line").annotate(q=Sum("quantity"))}

    rows = []
    for ln in qs.order_by("inbound__company__code", "inbound__supplier__code",
                          "inbound__doc_no", "id"):
        billed = invoiced.get(ln.pk, ZQ)
        remain = ln.quantity - billed
        if remain == 0:
            continue
        unit_u = (ln.amount_untaxed / ln.quantity) if ln.quantity else Z
        ru = round_money(remain * unit_u)
        rt = round_money(ru * (one + ln.tax_rate))
        rows.append({
            "company": ln.inbound.company, "supplier": ln.inbound.supplier,
            "inbound": ln.inbound, "product": ln.product,
            "in_qty": ln.quantity, "billed_qty": billed, "remain_qty": remain,
            "untaxed": ru, "taxed": rt,
        })
    return rows


def _avg_cost_asof(company, product, date):
    """商品在某业务日期的移动加权单价：取该日(含)前最后一笔流水的结存均价；
    无则回退当前结存均价，再无则 0。供"提前开票、未关联出库"时估算成本。"""
    if product is None:
        return Z
    m = (StockMove.objects.filter(company=company, product=product, date__lte=date)
         .order_by("-date", "-id").first())
    if m is not None:
        return m.balance_price
    bal = StockBalance.objects.filter(company=company, product=product).first()
    return bal.avg_price if bal else Z


def sales_revenue_cost(company, dfrom, dto):
    """销售收入成本计算表（按开票口径，按商品）。

    收入=期间内销售发票(开票日,不含期初)的不含税金额。成本：
    - 关联了出库行 → 取出库行实际结转成本(移动加权)；
    - 未关联(提前开票)但填了商品+数量 → 取该商品开票日的移动加权单价 × 数量 估算；
    - 未关联且无数量/商品 → 无法估算，另行提示。
    """
    ZQ = Decimal("0.000")
    invoices = (SalesInvoice.objects
                .filter(company=company, status=SalesInvoice.Status.REGISTERED,
                        is_opening=False, doc_date__gte=dfrom, doc_date__lte=dto)
                .prefetch_related("lines__source_outbound_line", "lines__product"))
    data = {}
    seen_ob = set()
    est_count = 0       # 按移动加权估算成本的行数
    gap_count = 0       # 仍无法算成本(无数量/商品)的行数
    gap_amount = Z
    for inv in invoices:
        for ln in inv.lines.all():
            prod = ln.product
            key = prod.pk if prod else 0
            d = data.setdefault(key, {"product": prod, "qty": ZQ, "revenue": Z, "cost": Z})
            d["revenue"] += ln.amount_untaxed
            ob = ln.source_outbound_line
            if ob is not None:
                if ob.pk not in seen_ob:           # 关联出库：实际结转成本
                    seen_ob.add(ob.pk)
                    d["cost"] += ob.amount
                    d["qty"] += ob.quantity
            elif prod is not None and ln.quantity and ln.quantity > 0:
                # 未关联但有商品+数量：按移动加权单价估算
                d["cost"] += round_money(ln.quantity * _avg_cost_asof(company, prod, inv.doc_date))
                d["qty"] += ln.quantity
                est_count += 1
            else:
                gap_count += 1
                gap_amount += ln.amount_untaxed
    rows = []
    for d in sorted(data.values(), key=lambda x: x["product"].code if x["product"] else ""):
        profit = d["revenue"] - d["cost"]
        margin = (profit / d["revenue"] * 100).quantize(Decimal("0.1")) if d["revenue"] else Z
        rows.append({"product": d["product"], "qty": d["qty"], "revenue": d["revenue"],
                     "cost": d["cost"], "profit": profit, "margin": margin})
    return {"rows": rows, "est_count": est_count,
            "gap_count": gap_count, "gap_amount": gap_amount}


def receivable_notes_balance(company, dfrom, dto):
    """某公司各应收票据 期初/本期增(出票)/本期减(使用)/期末（未用额，带 note 对象，供下钻）。"""
    notes = NoteReceivable.objects.filter(company=company).exclude(
        status=NoteReceivable.Status.VOID).order_by("doc_no")
    sett = {}
    for s in NoteSettlement.objects.filter(company=company,
                                           note_kind=NoteSettlement.NoteKind.RECEIVABLE):
        sett.setdefault(s.note_id, []).append(s)
    rows = []
    for n in notes:
        opening = income = outgo = Z
        if n.is_opening or n.draw_date < dfrom:
            opening += n.amount
        elif n.draw_date <= dto:
            income += n.amount
        for s in sett.get(n.pk, []):
            sd = s.created_at.date()
            if sd < dfrom:
                opening -= s.amount
            elif sd <= dto:
                outgo += s.amount
        ending = opening + income - outgo
        if opening or income or outgo or ending:
            rows.append({"note": n, "opening": opening, "income": income,
                         "outgo": outgo, "ending": ending})
    return rows


def note_ledger(company, note, dfrom, dto):
    """应收票据使用明细：出票(增) + 使用(冲应收/背书抵应付，减) 按时间滚动未用额。"""
    from apps.core.docrefs import invoice_url
    events = [{"date": note.draw_date, "kind": "出票", "doc_no": note.note_no or note.doc_no,
               "inc": note.amount, "dec": Z, "ref_url": "", "is_opening": note.is_opening}]
    for s in NoteSettlement.objects.filter(company=company, note_id=note.pk,
                                           note_kind=NoteSettlement.NoteKind.RECEIVABLE):
        events.append({"date": s.created_at.date(),
                       "kind": "背书抵应付" if s.is_endorsement else "冲应收",
                       "doc_no": s.invoice_no, "inc": Z, "dec": s.amount,
                       "ref_url": invoice_url(s.invoice_kind, s.invoice_id)})
    opening = Z
    period = []
    for e in events:
        if e.get("is_opening") or e["date"] < dfrom:
            opening += e["inc"] - e["dec"]
        elif e["date"] <= dto:
            period.append(e)
    period.sort(key=lambda e: (e["date"], 0 if e["inc"] else 1))
    bal, income, outgo, rows = opening, Z, Z, []
    for e in period:
        bal += e["inc"] - e["dec"]
        income += e["inc"]
        outgo += e["dec"]
        rows.append({**e, "balance": bal})
    return {"opening": opening, "rows": rows, "income": income, "outgo": outgo,
            "ending": opening + income - outgo}
