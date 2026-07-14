"""库存报表：数量金额表（当前结存）+ 商品流水台账（数量金额式）。

金额相关列受 inventory.view_amount 权限控制：采购/销售只看数量（SPEC §9.2）。
"""

from datetime import datetime
from decimal import Decimal

from django.views.generic import ListView, TemplateView

from apps.core.mixins import CompanyScopedMixin
from apps.core.period import get_report_dates
from apps.core.scope import resolve_company
from apps.masterdata.models import Product

from .models import StockBalance, StockMove

ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


class StockReportView(CompanyScopedMixin, ListView):
    """库存数量金额表：账套各商品结存（支持 ?company= 下钻）。"""

    model = StockBalance
    template_name = "inventory/stock_report.html"
    context_object_name = "balances"
    perm_action = "view"  # inventory.view_stockbalance

    def get_queryset(self):
        company = resolve_company(self.request)
        qs = self.model.objects.all() if company is None else self.model.objects.for_company(company)
        q = (self.request.GET.get("q") or "").strip()
        if q:
            from django.db.models import Q
            qs = qs.filter(Q(product__code__icontains=q) | Q(product__name__icontains=q))
        return qs.select_related("product").order_by("product__code")

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx":
            return self._export_xlsx()
        return super().get(request, *args, **kwargs)

    def _export_xlsx(self):
        from apps.core.exports import xlsx_response
        can_amt = self.request.user.has_perm("inventory.view_amount")
        headers = ["商品编码", "商品名称", "规格", "单位", "数量"]
        if can_amt:
            headers += ["金额", "移动加权单价"]
        rows = []
        for b in self.get_queryset():
            row = [b.product.code, b.product.name, b.product.spec, b.product.unit, b.quantity]
            if can_amt:
                row += [b.amount, b.avg_price]
            rows.append(row)
        return xlsx_response("库存数量金额表", headers, rows, company=resolve_company(self.request))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_view_amount"] = self.request.user.has_perm("inventory.view_amount")
        ctx["total_amount"] = sum((b.amount for b in ctx["balances"]), start=Decimal("0.00"))
        ctx["active_company"] = resolve_company(self.request)
        ctx["q"] = self.request.GET.get("q", "")
        ctx["date_from"], ctx["date_to"] = get_report_dates(self.request, resolve_company(self.request))
        ctx["company_id"] = self.request.GET.get("company", "")
        return ctx


class StockProductsReportView(CompanyScopedMixin, TemplateView):
    """库存商品余额表（总览「库存商品」下钻第一层）：某公司各商品 期初/本期收入/本期发出/期末。

    每行可再点入该商品的流水台账（明细账）。
    """

    template_name = "inventory/stock_products_report.html"

    def get_permission_required(self):
        return ("inventory.view_stockbalance",)

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx":
            return self._export_xlsx()
        return super().get(request, *args, **kwargs)

    def _export_xlsx(self):
        from apps.core.exports import xlsx_response
        from apps.opening.reports import stock_products_balance
        company = resolve_company(self.request)
        dfrom, dto = get_report_dates(self.request, company)
        rows = stock_products_balance(company, dfrom, dto) if company else []
        headers = [
            "商品编码", "商品名称",
            "期初数量", "期初金额", "本期收入数量", "本期收入金额",
            "本期发出数量", "本期发出金额", "期末数量", "期末金额",
        ]
        data = [[
            r["product"].code, r["product"].name,
            r["opening_qty"], r["opening"], r["income_qty"], r["income"],
            r["outgo_qty"], r["outgo"], r["ending_qty"], r["ending"],
        ] for r in rows]
        return xlsx_response("库存商品余额表", headers, data, company=company, period=(dfrom, dto))

    def get_context_data(self, **kwargs):
        from apps.opening.reports import stock_products_balance
        ctx = super().get_context_data(**kwargs)
        company = resolve_company(self.request)
        dfrom, dto = get_report_dates(self.request, company)
        rows = stock_products_balance(company, dfrom, dto) if company else []
        keys = ("opening", "income", "outgo", "ending",
                "opening_qty", "income_qty", "outgo_qty", "ending_qty")
        totals = {k: sum((r[k] for r in rows), ZERO) for k in keys}
        ctx.update({
            "active_company": company, "rows": rows, "totals": totals,
            "date_from": dfrom, "date_to": dto,
            "can_view_amount": self.request.user.has_perm("inventory.view_amount"),
            "company_id": self.request.GET.get("company", ""),
        })
        return ctx


class StockLedgerView(CompanyScopedMixin, TemplateView):
    """商品流水台账（明细账）：按商品看 期初结存 / 收入 / 发出 / 逐笔结存 / 期末结存。

    支持日期区间；当期无发生也显示期初=期末结存。
    """

    template_name = "inventory/stock_ledger.html"

    def get_permission_required(self):
        # 台账与库存表共用同一权限：能看库存即可看其流水
        return ("inventory.view_stockbalance",)

    def _active_company(self):
        return resolve_company(self.request)

    def _ledger(self, company):
        """计算明细账数据，供页面与导出共用。"""
        from apps.core.docrefs import doc_url
        product = None
        product_id = self.request.GET.get("product")
        if product_id and company:
            product = Product.objects.filter(company=company, pk=product_id).first()
        date_from = _parse_date(self.request.GET.get("from"))
        date_to = _parse_date(self.request.GET.get("to"))
        if date_from is None and date_to is None:
            date_from, date_to = get_report_dates(self.request, company)

        rows = []
        open_qty = open_amount = close_qty = close_amount = None
        if product:
            base = StockMove.objects.filter(company=company, product=product)
            open_qty, open_amount = ZERO_QTY, ZERO
            if date_from:
                for m in base.filter(date__lt=date_from):
                    sign = 1 if m.direction == StockMove.Direction.IN else -1
                    open_qty += sign * m.quantity
                    open_amount += sign * m.amount
            period = base
            if date_from:
                period = period.filter(date__gte=date_from)
            if date_to:
                period = period.filter(date__lte=date_to)
            bal_qty, bal_amount = open_qty, open_amount
            for m in period.order_by("date", "id"):
                is_in = m.direction == StockMove.Direction.IN
                bal_qty += m.quantity if is_in else -m.quantity
                bal_amount += m.amount if is_in else -m.amount
                rows.append({
                    "move": m,
                    "in_qty": m.quantity if is_in else None,
                    "in_amount": m.amount if is_in else None,
                    "out_qty": None if is_in else m.quantity,
                    "out_amount": None if is_in else m.amount,
                    "bal_qty": bal_qty, "bal_amount": bal_amount,
                    "ref_url": doc_url(m.source_type, m.source_id),
                })
            close_qty, close_amount = bal_qty, bal_amount
        return {"product": product, "rows": rows, "date_from": date_from, "date_to": date_to,
                "open_qty": open_qty, "open_amount": open_amount,
                "close_qty": close_qty, "close_amount": close_amount}

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx" and request.GET.get("product"):
            return self._export_xlsx()
        return super().get(request, *args, **kwargs)

    def _export_xlsx(self):
        from apps.core.exports import xlsx_response
        company = self._active_company()
        d = self._ledger(company)
        product = d["product"]
        if not product:
            from django.shortcuts import redirect
            return redirect("stock_ledger")
        can_amt = self.request.user.has_perm("inventory.view_amount")
        if can_amt:
            headers = ["日期", "来源单据", "摘要", "收入数量", "收入金额", "发出数量",
                       "发出金额", "结存数量", "均价", "结存金额"]
            rows = [["期初结存", "", "", "", "", "", "", d["open_qty"], "", d["open_amount"]]]
            for r in d["rows"]:
                m = r["move"]
                rows.append([m.date, m.source_no, m.get_direction_display(),
                             r["in_qty"], r["in_amount"], r["out_qty"], r["out_amount"],
                             m.balance_quantity, m.balance_price, m.balance_amount])
            rows.append(["期末结存", "", "", "", "", "", "", d["close_qty"], "", d["close_amount"]])
        else:
            headers = ["日期", "来源单据", "摘要", "收入数量", "发出数量", "结存数量"]
            rows = [["期初结存", "", "", "", "", d["open_qty"]]]
            for r in d["rows"]:
                m = r["move"]
                rows.append([m.date, m.source_no, m.get_direction_display(),
                             r["in_qty"], r["out_qty"], m.balance_quantity])
            rows.append(["期末结存", "", "", "", "", d["close_qty"]])
        return xlsx_response(f"商品流水台账-{product.code} {product.name}", headers, rows,
                             company=company, period=(d["date_from"], d["date_to"]))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self._active_company()
        products = Product.objects.filter(company=company).order_by("code") if company else []
        d = self._ledger(company)
        ctx.update({
            "active_company": company,
            "products": products,
            "selected_product": d["product"],
            "rows": d["rows"],
            "open_qty": d["open_qty"], "open_amount": d["open_amount"],
            "close_qty": d["close_qty"], "close_amount": d["close_amount"],
            "date_from": d["date_from"], "date_to": d["date_to"],
            "company_id": self.request.GET.get("company", ""),
            "can_view_amount": self.request.user.has_perm("inventory.view_amount"),
        })
        return ctx
