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
    NoteDisposal,
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
    """返回 dict：各类 {opening, income, outgo, ending}，按 [dfrom,dto] 统计。"""
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

    # 发出商品（成本）：增=本期销售出库结转成本；减=本期开票对应出库成本
    goods_shipped = goods_shipped_period(company, dfrom, dto)

    # 供应商往来（应付）：期初发票(is_opening)恒计期初；增=本期采购发票；减=付款核销+票据抵付+往来对冲
    from apps.finance.models import PartnerOffset, PartnerOffsetAPLine, PartnerOffsetARLine
    ap_all = PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED)
    ap_inv = ap_all.filter(is_opening=False)
    ap_pay = PaymentAllocation.objects.filter(payment__company=company)
    ap_note = NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE)
    ap_off = PartnerOffsetAPLine.objects.filter(
        offset__company=company, offset__status=PartnerOffset.Status.REGISTERED)
    payable = _merge_period(ap_inv, "doc_date", "amount_taxed",
                            [(ap_pay, "date"), (ap_note, "date"), (ap_off, "offset__doc_date")],
                            dfrom, dto)
    _add_opening(payable, _s(ap_all.filter(is_opening=True), "amount_taxed"))

    # 应付账款-暂估（不含税）：增=本期外部采购入库；减=本期收票对应入库不含税
    ap_accrual = ap_accrual_period(company, dfrom, dto)

    # 客户往来（应收）：期初发票恒计期初；增=本期销售发票；减=收款核销+应收票据冲应收+往来对冲
    ar_all = SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED)
    ar_inv = ar_all.filter(is_opening=False)
    ar_rec = ReceiptAllocation.objects.filter(receipt__company=company)
    ar_note = NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES,
                                            is_endorsement=False)
    ar_off = PartnerOffsetARLine.objects.filter(
        offset__company=company, offset__status=PartnerOffset.Status.REGISTERED)
    receivable = _merge_period(ar_inv, "doc_date", "amount_taxed",
                               [(ar_rec, "date"), (ar_note, "date"), (ar_off, "offset__doc_date")],
                               dfrom, dto)
    _add_opening(receivable, _s(ar_all.filter(is_opening=True), "amount_taxed"))

    # 应收票据：期初票据(is_opening)恒计期初；增=本期出票；减=票据「出去」(背书/托收)。
    # 核销应收(is_endorsement=False)是票收进来抵应收账款、不消耗票面，不在此减。
    nr_all = NoteReceivable.objects.filter(company=company).exclude(status=NoteReceivable.Status.VOID)
    nr = nr_all.filter(is_opening=False)
    nr_use = NoteSettlement.objects.filter(company=company, note_kind=NoteSettlement.NoteKind.RECEIVABLE,
                                           is_endorsement=True)
    nr_disp = NoteDisposal.objects.filter(company=company)   # 兑付/贴现也是票出去
    note_recv = _merge_period(nr, "draw_date", "amount",
                              [(nr_use, "created_at__date"), (nr_disp, "date")], dfrom, dto)
    _add_opening(note_recv, _s(nr_all.filter(is_opening=True), "amount"))

    return {"bank": bank, "stock": stock, "goods_shipped": goods_shipped,
            "payable": payable, "ap_accrual": ap_accrual,
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
    ("goods_shipped", "发出商品", "goods_shipped_detail_report"),
    ("payable", "供应商往来（应付）", "payable_partners_report"),
    ("ap_accrual", "应付账款-暂估", "received_uninvoiced_report"),
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
            "opening_qty": d["open_qty"],
            "income_qty": d["in_qty"],
            "outgo_qty": d["out_qty"],
            "ending_qty": d["open_qty"] + d["in_qty"] - d["out_qty"],
        })
    return rows


def _attr_cost(src_line, inv_qty, amount_field="amount"):
    """发票数量对应源出入库行金额（按数量比例分摊）。"""
    if src_line is None or not src_line.quantity:
        return Z
    qty = inv_qty or Decimal("0.000")
    return round_money(getattr(src_line, amount_field) * (qty / src_line.quantity))


def goods_shipped_products_balance(company, dfrom, dto):
    """发出商品按商品：增=本期销售出库结转成本；减=本期开票对应出库成本。

    不含借出/归还。期末=期初+增−减，与「已出库未开票」截止日余额同口径。
    """
    from apps.finance.models import SalesInvoice, SalesInvoiceLine
    from apps.sales.models import SalesOutbound, SalesOutboundLine

    data = {}
    def bucket(product):
        return data.setdefault(product, {"opening": Z, "income": Z, "outgo": Z})

    lines = (SalesOutboundLine.objects
             .filter(outbound__company=company,
                     outbound__sales_type=SalesOutbound.SalesType.SALE)
             .exclude(outbound__status=SalesOutbound.Status.VOID)
             .select_related("product", "outbound"))
    for ln in lines:
        d = bucket(ln.product)
        if ln.outbound.doc_date < dfrom:
            d["opening"] += ln.amount
        elif ln.outbound.doc_date <= dto:
            d["income"] += ln.amount

    inv_lines = (SalesInvoiceLine.objects
                 .filter(invoice__company=company,
                         invoice__status=SalesInvoice.Status.REGISTERED,
                         invoice__is_opening=False,
                         source_outbound_line__isnull=False)
                 .select_related("source_outbound_line", "source_outbound_line__product", "invoice"))
    for il in inv_lines:
        ob = il.source_outbound_line
        cost = _attr_cost(ob, il.quantity, "amount")
        if not cost:
            continue
        d = bucket(ob.product)
        if il.invoice.doc_date < dfrom:
            d["opening"] -= cost
        elif il.invoice.doc_date <= dto:
            d["outgo"] += cost

    rows = []
    for product, d in sorted(data.items(), key=lambda kv: (kv[0].code if kv[0] else "")):
        ending = d["opening"] + d["income"] - d["outgo"]
        if d["opening"] or d["income"] or d["outgo"] or ending:
            rows.append({"product": product, "opening": d["opening"], "income": d["income"],
                         "outgo": d["outgo"], "ending": ending})
    return rows


def goods_shipped_period(company, dfrom, dto):
    """发出商品公司合计行。"""
    opening = income = outgo = Z
    for r in goods_shipped_products_balance(company, dfrom, dto):
        opening += r["opening"]
        income += r["income"]
        outgo += r["outgo"]
    return _row(opening, income, outgo, opening + income - outgo)


def goods_shipped_detail(companies, dfrom, dto):
    """发出商品明细（按出库行）：期初 / 本期收入(出库成本) / 本期发出(开票冲减) / 期末。

    与总览「发出商品」四列同一口径；含本期已全部开票的出库行（未开票余额为 0 也列出，
    只要四列任一非零）。不含借出/归还。companies 支持多公司。
    """
    from apps.finance.models import SalesInvoice, SalesInvoiceLine
    from apps.sales.models import SalesOutbound, SalesOutboundLine
    ZQ = Decimal("0.000")
    companies = list(companies)
    if not companies or dfrom is None or dto is None:
        return []

    lines = (SalesOutboundLine.objects
             .filter(outbound__company__in=companies,
                     outbound__sales_type=SalesOutbound.SalesType.SALE)
             .exclude(outbound__status=SalesOutbound.Status.VOID)
             .select_related("outbound", "outbound__company", "outbound__customer", "product"))

    inv_lines = (SalesInvoiceLine.objects
                 .filter(invoice__company__in=companies,
                         invoice__status=SalesInvoice.Status.REGISTERED,
                         invoice__is_opening=False,
                         source_outbound_line__isnull=False,
                         source_outbound_line__outbound__sales_type=SalesOutbound.SalesType.SALE)
                 .exclude(source_outbound_line__outbound__status=SalesOutbound.Status.VOID)
                 .select_related("invoice", "source_outbound_line"))

    billed_before = {}   # line_id -> qty before dfrom
    billed_period = {}   # line_id -> qty in [dfrom, dto]
    for il in inv_lines:
        lid = il.source_outbound_line_id
        qty = il.quantity or ZQ
        idate = il.invoice.doc_date
        if idate < dfrom:
            billed_before[lid] = billed_before.get(lid, ZQ) + qty
        elif idate <= dto:
            billed_period[lid] = billed_period.get(lid, ZQ) + qty

    rows = []
    for ln in lines.order_by("outbound__company__code", "outbound__doc_date",
                             "outbound__doc_no", "id"):
        ob = ln.outbound
        cost = ln.amount or Z
        if not ln.quantity:
            opening = income = outgo = ending = Z
        else:
            before_qty = billed_before.get(ln.pk, ZQ)
            period_qty = billed_period.get(ln.pk, ZQ)
            if ob.doc_date < dfrom:
                opening = round_money(cost - _attr_cost(ln, before_qty, "amount"))
                income = Z
            elif ob.doc_date <= dto:
                opening = Z
                income = cost
            else:
                # 区间后出库，不进入本表
                continue
            outgo = _attr_cost(ln, period_qty, "amount")
            ending = opening + income - outgo
        if not (opening or income or outgo or ending):
            continue
        rows.append({
            "company": ob.company,
            "customer": ob.customer,
            "outbound": ob,
            "product": ln.product,
            "out_qty": ln.quantity,
            "out_cost": cost,
            "opening": opening,
            "income": income,
            "outgo": outgo,
            "ending": ending,
        })
    return rows


def ap_accrual_partners_balance(company, dfrom, dto):
    """应付账款-暂估按供应商（不含税）：增=本期外部采购入库；减=本期收票对应入库不含税。

    借调入库不纳入。期末与「已入库未收票」截止日余额同口径。
    """
    from apps.finance.models import PurchaseInvoice, PurchaseInvoiceLine
    from apps.purchasing.models import PurchaseInbound, PurchaseInboundLine

    data = {}
    def bucket(supplier):
        return data.setdefault(supplier, {"opening": Z, "income": Z, "outgo": Z})

    lines = (PurchaseInboundLine.objects
             .filter(inbound__company=company,
                     inbound__purchase_type=PurchaseInbound.PurchaseType.EXTERNAL)
             .exclude(inbound__status=PurchaseInbound.Status.VOID)
             .select_related("inbound", "inbound__supplier"))
    for ln in lines:
        d = bucket(ln.inbound.supplier)
        if ln.inbound.doc_date < dfrom:
            d["opening"] += ln.amount_untaxed
        elif ln.inbound.doc_date <= dto:
            d["income"] += ln.amount_untaxed

    inv_lines = (PurchaseInvoiceLine.objects
                 .filter(invoice__company=company,
                         invoice__status=PurchaseInvoice.Status.REGISTERED,
                         invoice__is_opening=False,
                         source_inbound_line__isnull=False)
                 .select_related("source_inbound_line", "source_inbound_line__inbound",
                                 "source_inbound_line__inbound__supplier", "invoice"))
    for il in inv_lines:
        ib = il.source_inbound_line
        amt = _attr_cost(ib, il.quantity, "amount_untaxed")
        if not amt:
            continue
        d = bucket(ib.inbound.supplier)
        if il.invoice.doc_date < dfrom:
            d["opening"] -= amt
        elif il.invoice.doc_date <= dto:
            d["outgo"] += amt

    rows = []
    for supplier, d in sorted(data.items(), key=lambda kv: (kv[0].code if kv[0] else "")):
        ending = d["opening"] + d["income"] - d["outgo"]
        if d["opening"] or d["income"] or d["outgo"] or ending:
            rows.append({"partner": supplier, "opening": d["opening"], "income": d["income"],
                         "outgo": d["outgo"], "ending": ending})
    return rows


def ap_accrual_period(company, dfrom, dto):
    """应付账款-暂估公司合计行。"""
    opening = income = outgo = Z
    for r in ap_accrual_partners_balance(company, dfrom, dto):
        opening += r["opening"]
        income += r["income"]
        outgo += r["outgo"]
    return _row(opening, income, outgo, opening + income - outgo)


def _ledger_url(name, **params):
    """拼明细账链接（跳过空参数）。"""
    from urllib.parse import urlencode

    from django.urls import reverse
    q = {}
    for k, v in params.items():
        if v is None or v == "":
            continue
        q[k] = v.isoformat() if hasattr(v, "isoformat") else str(v)
    return f"{reverse(name)}?{urlencode(q)}" if q else reverse(name)


def account_balance_table(companies, dfrom, dto):
    """明细账户余额：银行 / 库存 / 发出商品 / 应付 / 应付暂估 / 应收。

    每行 期初(区间前净)/本期收入(增)/本期发出(减)/期末；带 detail_url 供账户余额表下钻。
    """
    bank_rows, stock_rows, shipped_rows, ap_rows, accrual_rows, ar_rows = [], [], [], [], [], []
    for company in companies:
        # 银行：每账户 → 银行存款日记账
        for r in bank_accounts_balance(company, dfrom, dto):
            acc = r["account"]
            bank_rows.append({
                "company": company, "name": str(acc),
                "opening": r["opening"], "income": r["income"],
                "outgo": r["outgo"], "ending": r["ending"],
                "detail_url": _ledger_url(
                    "bank_journal_report", company=company.pk, account=acc.pk,
                    **{"from": dfrom, "to": dto}),
            })

        # 库存：每商品 → 商品流水台账
        for r in stock_products_balance(company, dfrom, dto):
            prod = r["product"]
            stock_rows.append({
                "company": company,
                "name": f"{prod.code} {prod.name}",
                "opening": r["opening"], "income": r["income"],
                "outgo": r["outgo"], "ending": r["ending"],
                "detail_url": _ledger_url(
                    "stock_ledger", company=company.pk, product=prod.pk,
                    **{"from": dfrom, "to": dto}),
            })

        # 发出商品：每商品 → 发出商品明细表（本期发生全量）
        for r in goods_shipped_products_balance(company, dfrom, dto):
            prod = r["product"]
            name = f"{prod.code} {prod.name}" if prod else "（未指定商品）"
            url = _ledger_url(
                "goods_shipped_detail_report", company=company.pk,
                product=prod.pk if prod else None,
                **{"from": dfrom, "to": dto}) if prod else ""
            shipped_rows.append({
                "company": company, "name": name,
                "opening": r["opening"], "income": r["income"],
                "outgo": r["outgo"], "ending": r["ending"],
                "detail_url": url,
            })

        # 应付：每供应商 → 供应商往来明细账
        ap_rows += _partner_rows(
            company,
            PurchaseInvoice.objects.filter(company=company, status=PurchaseInvoice.Status.REGISTERED),
            "supplier",
            PaymentAllocation.objects.filter(payment__company=company).select_related("invoice__supplier"),
            lambda a: a.invoice.supplier,
            NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.PURCHASE),
            PurchaseInvoice, dfrom, dto,
            ledger_name="payable_partner_ledger")

        # 应付账款-暂估：每供应商 → 已入库未收票明细（按供应商）
        for r in ap_accrual_partners_balance(company, dfrom, dto):
            partner = r["partner"]
            accrual_rows.append({
                "company": company,
                "name": str(partner) if partner else "（未指定供应商）",
                "opening": r["opening"], "income": r["income"],
                "outgo": r["outgo"], "ending": r["ending"],
                "detail_url": _ledger_url(
                    "received_uninvoiced_report", company=company.pk,
                    supplier=partner.pk if partner else None,
                    **{"from": dfrom, "to": dto}) if partner else "",
            })

        # 应收：每客户 → 客户往来明细账
        ar_rows += _partner_rows(
            company,
            SalesInvoice.objects.filter(company=company, status=SalesInvoice.Status.REGISTERED),
            "customer",
            ReceiptAllocation.objects.filter(receipt__company=company).select_related("invoice__customer"),
            lambda a: a.invoice.customer,
            NoteSettlement.objects.filter(company=company, invoice_kind=NoteSettlement.InvoiceKind.SALES,
                                          is_endorsement=False),
            SalesInvoice, dfrom, dto,
            ledger_name="receivable_partner_ledger")

    return {"bank": bank_rows, "stock": stock_rows, "goods_shipped": shipped_rows,
            "payable": ap_rows, "ap_accrual": accrual_rows, "receivable": ar_rows}


def _partner_balance(company, invoices, partner_attr, allocations, alloc_partner, note_settlements,
                     invoice_model, dfrom, dto):
    """按往来对象归并 期初/增/减，保留 partner 对象。增=发票(doc_date)，减=核销+票据+往来对冲。"""
    from apps.finance.models import PartnerOffset, PartnerOffsetAPLine, PartnerOffsetARLine

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
        ad = a.date or a.created_at.date()
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
        nd = ns.date or ns.created_at.date()
        if nd < dfrom:
            d["opening"] -= ns.amount
        elif nd <= dto:
            d["outgo"] += ns.amount
    # 往来对冲
    if partner_attr == "supplier":
        off_lines = (PartnerOffsetAPLine.objects
                     .filter(offset__company=company, offset__status=PartnerOffset.Status.REGISTERED)
                     .select_related("offset", "invoice__supplier"))
        for ln in off_lines:
            partner = ln.invoice.supplier
            d = data.setdefault(partner, _pset())
            od = ln.offset.doc_date
            if od < dfrom:
                d["opening"] -= ln.amount
            elif od <= dto:
                d["outgo"] += ln.amount
    else:
        off_lines = (PartnerOffsetARLine.objects
                     .filter(offset__company=company, offset__status=PartnerOffset.Status.REGISTERED)
                     .select_related("offset", "invoice__customer"))
        for ln in off_lines:
            partner = ln.invoice.customer
            d = data.setdefault(partner, _pset())
            od = ln.offset.doc_date
            if od < dfrom:
                d["opening"] -= ln.amount
            elif od <= dto:
                d["outgo"] += ln.amount
    rows = []
    for partner, d in sorted(data.items(), key=lambda kv: kv[0].code):
        ending = d["opening"] + d["income"] - d["outgo"]
        if d["opening"] or d["income"] or d["outgo"] or ending:
            rows.append({"partner": partner, "opening": d["opening"], "income": d["income"],
                         "outgo": d["outgo"], "ending": ending})
    return rows


def _partner_rows(company, invoices, partner_attr, allocations, alloc_partner, note_settlements,
                  invoice_model, dfrom, dto, ledger_name=None):
    """account_balance_table 用：在 _partner_balance 基础上加 company/name/detail_url。"""
    out = []
    for r in _partner_balance(company, invoices, partner_attr, allocations, alloc_partner,
                              note_settlements, invoice_model, dfrom, dto):
        row = {"company": company, "name": str(r["partner"]), "opening": r["opening"],
               "income": r["income"], "outgo": r["outgo"], "ending": r["ending"],
               "detail_url": ""}
        if ledger_name and r["partner"]:
            row["detail_url"] = _ledger_url(
                ledger_name, company=company.pk, partner=r["partner"].pk,
                **{"from": dfrom, "to": dto})
        out.append(row)
    return out


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
        events.append({"date": a.date or a.created_at.date(), "kind": alloc_label,
                       "doc_no": alloc_doc(a),
                       "inc": Z, "dec": a.amount, "ref_url": alloc_url(a)})
    for ns in notes.filter(invoice_id__in=inv_ids):
        events.append({"date": ns.date or ns.created_at.date(), "kind": "票据抵付",
                       "doc_no": ns.note_no,
                       "inc": Z, "dec": ns.amount,
                       "ref_url": invoice_url(ns.invoice_kind, ns.invoice_id)})
    from apps.finance.models import PartnerOffset, PartnerOffsetAPLine, PartnerOffsetARLine
    from django.urls import reverse
    if kind == "payable":
        for ln in (PartnerOffsetAPLine.objects
                   .filter(offset__company=company, offset__status=PartnerOffset.Status.REGISTERED,
                           invoice__supplier=partner)
                   .select_related("offset")):
            events.append({
                "date": ln.offset.doc_date, "kind": "往来对冲",
                "doc_no": ln.offset.doc_no, "inc": Z, "dec": ln.amount,
                "ref_url": reverse("partner_offset_detail", args=[ln.offset_id]),
            })
    else:
        for ln in (PartnerOffsetARLine.objects
                   .filter(offset__company=company, offset__status=PartnerOffset.Status.REGISTERED,
                           invoice__customer=partner)
                   .select_related("offset")):
            events.append({
                "date": ln.offset.doc_date, "kind": "往来对冲",
                "doc_no": ln.offset.doc_no, "inc": Z, "dec": ln.amount,
                "ref_url": reverse("partner_offset_detail", args=[ln.offset_id]),
            })

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


def management_profit(companies, dfrom, dto, eliminate):
    """管理利润表（按出库）：每公司 本期/本年 列，可选内部交易抵销。

    内部销售收入=销售给关联公司(客户 related_company 已设)的不含税收入；
    内部销售成本=从关联公司采购(供应商 related_company 已设)的入库不含税额(简化口径)。
    返回 (cols, total)；cols=[{company, cur, ytd}]；多公司时 total 为合计列，否则 None。
    """
    from apps.finance.models import ExpenseRecord
    from apps.purchasing.models import PurchaseInbound, PurchaseInboundLine
    from apps.sales.models import SalesOutbound, SalesOutboundLine
    companies = list(companies)
    yfrom = dto.replace(month=1, day=1)

    def base(company, d1, d2):
        sl = (SalesOutboundLine.objects
              .filter(outbound__company=company,
                      outbound__sales_type=SalesOutbound.SalesType.SALE,
                      outbound__doc_date__gte=d1, outbound__doc_date__lte=d2)
              .exclude(outbound__status=SalesOutbound.Status.VOID))
        rev = round_money(sl.aggregate(v=Sum("amount_untaxed"))["v"] or Z)
        cost = round_money(sl.aggregate(v=Sum("amount"))["v"] or Z)
        irev = round_money(sl.filter(outbound__customer__related_company__isnull=False)
                           .aggregate(v=Sum("amount_untaxed"))["v"] or Z)
        il = (PurchaseInboundLine.objects
              .filter(inbound__company=company, inbound__doc_date__gte=d1, inbound__doc_date__lte=d2,
                      inbound__supplier__related_company__isnull=False)
              .exclude(inbound__status=PurchaseInbound.Status.VOID))
        icost = round_money(il.aggregate(v=Sum("amount_untaxed"))["v"] or Z)
        comm = round_money(ExpenseRecord.objects.filter(
            company=company, category="commission", date__gte=d1, date__lte=d2)
            .aggregate(v=Sum("amount"))["v"] or Z)
        return {"rev": rev, "cost": cost, "irev": irev, "icost": icost, "comm": comm}

    def derive(b):
        net_rev = b["rev"] - b["irev"]
        net_cost = b["cost"] - b["icost"]
        if eliminate:
            profit = net_rev - net_cost - b["comm"]
            denom = net_rev
        else:
            profit = b["rev"] - b["cost"] - b["comm"]
            denom = b["rev"]
        margin = (profit / denom * 100).quantize(Decimal("0.1")) if denom else Z
        return {**b, "net_rev": net_rev, "net_cost": net_cost, "profit": profit, "margin": margin}

    cols = []
    a_cur = {"rev": Z, "cost": Z, "irev": Z, "icost": Z, "comm": Z}
    a_ytd = {"rev": Z, "cost": Z, "irev": Z, "icost": Z, "comm": Z}
    for c in companies:
        bc, by = base(c, dfrom, dto), base(c, yfrom, dto)
        for k in a_cur:
            a_cur[k] += bc[k]
            a_ytd[k] += by[k]
        cols.append({"company": c, "cur": derive(bc), "ytd": derive(by)})
    total = {"company": None, "cur": derive(a_cur), "ytd": derive(a_ytd)} if len(companies) > 1 else None
    return cols, total


def customer_sales_analysis(companies, dfrom, dto, by_product, show_commission):
    """客户销售分析表（按出库）：公司→客户[→产品] 汇总 数量/收入(不含税)/成本/佣金/毛利率。

    by_product=True 列到产品并给每客户小计；每公司给合计。佣金仅 show_commission 时计入毛利率。
    返回 [{company, customers:[{customer, prods:[...], sub}], tot}]。
    """
    from apps.finance.models import ExpenseRecord
    from apps.sales.models import SalesOutbound, SalesOutboundLine
    companies = list(companies)
    if not companies:
        return []
    ZQ = Decimal("0.000")

    def blank():
        return {"qty": ZQ, "revenue": Z, "cost": Z, "commission": Z}

    def margin(a):
        profit = a["revenue"] - a["cost"] - (a["commission"] if show_commission else Z)
        return (profit / a["revenue"] * 100).quantize(Decimal("0.1")) if a["revenue"] else Z

    cells = {}   # (comp, cust, prod) -> 聚合
    lines = (SalesOutboundLine.objects
             .filter(outbound__company__in=companies,
                     outbound__sales_type=SalesOutbound.SalesType.SALE,
                     outbound__doc_date__gte=dfrom, outbound__doc_date__lte=dto)
             .exclude(outbound__status=SalesOutbound.Status.VOID)
             .select_related("outbound__company", "outbound__customer", "product"))
    for ln in lines:
        comp = ln.outbound.company
        cust = ln.outbound.customer
        key = (comp.pk, cust.pk if cust else 0, ln.product_id if by_product else 0)
        d = cells.setdefault(key, {"company": comp, "customer": cust,
                                   "product": ln.product if by_product else None, **blank()})
        d["qty"] += ln.quantity
        d["revenue"] += ln.amount_untaxed
        d["cost"] += ln.amount
    if show_commission:
        for e in (ExpenseRecord.objects.filter(company__in=companies, category="commission",
                                               date__gte=dfrom, date__lte=dto)
                  .select_related("company", "customer", "product")):
            key = (e.company_id, e.customer_id or 0, e.product_id if by_product else 0)
            d = cells.get(key)
            if d is None:
                d = cells.setdefault(key, {"company": e.company, "customer": e.customer,
                                           "product": e.product if by_product else None, **blank()})
            d["commission"] += e.amount

    comps = {}
    for d in cells.values():
        comp, cust = d["company"], d["customer"]
        cp = comps.setdefault(comp.pk, {"company": comp, "custs": {}, "tot": blank()})
        cc = cp["custs"].setdefault(cust.pk if cust else 0,
                                    {"customer": cust, "prods": [], "sub": blank()})
        if by_product:
            cc["prods"].append({"product": d["product"], "qty": d["qty"], "revenue": d["revenue"],
                                "cost": d["cost"], "commission": d["commission"], "margin": margin(d)})
        for agg in (cc["sub"], cp["tot"]):
            for k in ("qty", "revenue", "cost", "commission"):
                agg[k] += d[k]

    out = []
    for cp in sorted(comps.values(), key=lambda x: x["company"].code):
        custs = []
        for cc in sorted(cp["custs"].values(),
                         key=lambda x: x["customer"].code if x["customer"] else ""):
            cc["prods"].sort(key=lambda p: p["product"].code if p["product"] else "")
            cc["sub"]["margin"] = margin(cc["sub"])
            custs.append(cc)
        cp["tot"]["margin"] = margin(cp["tot"])
        out.append({"company": cp["company"], "customers": custs, "tot": cp["tot"]})
    return out


def shipped_uninvoiced(companies, dfrom=None, dto=None):
    """已出库未开具发票明细：销售出库行中「出库数量 − 已开票数量 ≠ 0」的行。

    companies 为公司列表（支持多公司联合查询）。
    已开票数量 = 关联该出库行的销售发票行数量之和(不含作废)；
    若给定 dto，仅统计开票日 ≤ dto 的发票（便于「截止某日」余额，如 6 月末发出商品）。
    金额：未开票售价(不含税/含税)供参考；cost=未开票数量对应出库结转成本（发出商品入账用）。
    可按出库日期区间过滤；核对月末发出商品余额时建议 from 留空、to=月末。
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

    inv_qs = (SalesInvoiceLine.objects.filter(source_outbound_line__in=qs)
              .exclude(invoice__status=SalesInvoice.Status.VOID))
    if dto:
        inv_qs = inv_qs.filter(invoice__doc_date__lte=dto)
    invoiced = {r["source_outbound_line"]: round_qty(r["q"] or ZQ) for r in
                inv_qs.values("source_outbound_line").annotate(q=Sum("quantity"))}

    rows = []
    for ln in qs.order_by("outbound__company__code", "outbound__customer__code",
                          "outbound__doc_no", "id"):
        billed = invoiced.get(ln.pk, ZQ)
        remain = ln.quantity - billed
        if remain == 0:
            continue
        unit_u = (ln.amount_untaxed / ln.quantity) if ln.quantity else Z
        unit_c = (ln.amount / ln.quantity) if ln.quantity else Z
        ru = round_money(remain * unit_u)
        rt = round_money(ru * (one + ln.tax_rate))
        rows.append({
            "company": ln.outbound.company, "customer": ln.outbound.customer,
            "outbound": ln.outbound, "product": ln.product,
            "out_qty": ln.quantity, "billed_qty": billed, "remain_qty": remain,
            "untaxed": ru, "taxed": rt,
            "cost": round_money(remain * unit_c),
        })
    return rows


def received_uninvoiced(companies, dfrom=None, dto=None):
    """已入库未收到发票明细：采购入库行中「入库数量 − 已收票数量 ≠ 0」的行。

    作为「应付账款-暂估」(不含税) 的依据。仅外部采购入库；借调不纳入。
    已收票数量 = 关联该入库行的采购发票行数量之和(不含作废)；
    若给定 dto，仅统计收票日 ≤ dto 的发票（便于「截止某日」暂估余额）。
    金额取未收票部分(按入库行不含税单价×未收票数量)。核对月末暂估时建议 from 留空、to=月末。
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

    inv_qs = (PurchaseInvoiceLine.objects.filter(source_inbound_line__in=qs)
              .exclude(invoice__status=PurchaseInvoice.Status.VOID))
    if dto:
        inv_qs = inv_qs.filter(invoice__doc_date__lte=dto)
    invoiced = {r["source_inbound_line"]: round_qty(r["q"] or ZQ) for r in
                inv_qs.values("source_inbound_line").annotate(q=Sum("quantity"))}

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
    # 票「出去」才减持有：背书 + 兑付 + 贴现；核销应收不消耗票面，不计入减项
    for s in NoteSettlement.objects.filter(company=company,
                                           note_kind=NoteSettlement.NoteKind.RECEIVABLE,
                                           is_endorsement=True):
        sett.setdefault(s.note_id, []).append(
            {"date": s.date or s.created_at.date(), "amount": s.amount})
    for d in NoteDisposal.objects.filter(company=company):
        sett.setdefault(d.note_id, []).append({"date": d.date, "amount": d.amount})
    rows = []
    for n in notes:
        opening = income = outgo = Z
        if n.is_opening or n.draw_date < dfrom:
            opening += n.amount
        elif n.draw_date <= dto:
            income += n.amount
        for s in sett.get(n.pk, []):
            if s["date"] < dfrom:
                opening -= s["amount"]
            elif s["date"] <= dto:
                outgo += s["amount"]
        ending = opening + income - outgo
        if opening or income or outgo or ending:
            rows.append({"note": n, "opening": opening, "income": income,
                         "outgo": outgo, "ending": ending})
    return rows


def note_ledger(company, note, dfrom, dto):
    """应收票据使用明细：出票(增) + 背书/托收(减) 按时间滚动未用额。

    核销应收(冲应收账款)是票收进来抵应收、不消耗票面，不影响未用余额，故不在本表减项；
    其与发票的勾稽见对应发票「核销明细」。
    """
    from apps.core.docrefs import invoice_url
    events = [{"date": note.draw_date, "kind": "出票", "doc_no": note.note_no or note.doc_no,
               "inc": note.amount, "dec": Z, "ref_url": "", "is_opening": note.is_opening}]
    for s in NoteSettlement.objects.filter(company=company, note_id=note.pk,
                                           note_kind=NoteSettlement.NoteKind.RECEIVABLE,
                                           is_endorsement=True):
        events.append({"date": s.date or s.created_at.date(),
                       "kind": "背书抵应付",
                       "doc_no": s.invoice_no, "inc": Z, "dec": s.amount,
                       "ref_url": invoice_url(s.invoice_kind, s.invoice_id),
                       "settlement_id": s.pk})
    for d in NoteDisposal.objects.filter(note=note).select_related("bank_account"):
        events.append({"date": d.date, "kind": d.get_kind_display(),
                       "doc_no": str(d.bank_account), "inc": Z, "dec": d.amount,
                       "ref_url": "", "disposal_id": d.pk})
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
