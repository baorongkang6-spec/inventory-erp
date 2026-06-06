"""库存报表：数量金额表（当前结存）+ 商品流水台账（数量金额式）。

金额相关列受 inventory.view_amount 权限控制：采购/销售只看数量（SPEC §9.2）。
"""

from decimal import Decimal

from django.views.generic import ListView, TemplateView

from apps.core.mixins import CompanyScopedMixin
from apps.core.scope import resolve_company
from apps.masterdata.models import Product

from .models import StockBalance, StockMove


class StockReportView(CompanyScopedMixin, ListView):
    """库存数量金额表：账套各商品结存（支持 ?company= 下钻）。"""

    model = StockBalance
    template_name = "inventory/stock_report.html"
    context_object_name = "balances"
    perm_action = "view"  # inventory.view_stockbalance

    def get_queryset(self):
        company = resolve_company(self.request)
        qs = self.model.objects.all() if company is None else self.model.objects.for_company(company)
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
        return xlsx_response("库存数量金额表", headers, rows)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_view_amount"] = self.request.user.has_perm("inventory.view_amount")
        ctx["total_amount"] = sum((b.amount for b in ctx["balances"]), start=Decimal("0.00"))
        ctx["active_company"] = resolve_company(self.request)
        return ctx


class StockLedgerView(CompanyScopedMixin, TemplateView):
    """商品流水台账：按商品看 收入/发出/结存 明细。"""

    template_name = "inventory/stock_ledger.html"

    def get_permission_required(self):
        # 台账与库存表共用同一权限：能看库存即可看其流水
        return ("inventory.view_stockbalance",)

    def _active_company(self):
        return resolve_company(self.request)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        company = self._active_company()
        ctx["active_company"] = company
        products = Product.objects.filter(company=company).order_by("code") if company else []

        product = None
        product_id = self.request.GET.get("product")
        if product_id:
            product = Product.objects.filter(company=company, pk=product_id).first()

        rows = []
        if product:
            moves = StockMove.objects.filter(company=company, product=product).order_by("date", "id")
            for m in moves:
                is_in = m.direction == StockMove.Direction.IN
                rows.append({
                    "move": m,
                    "in_qty": m.quantity if is_in else None,
                    "in_amount": m.amount if is_in else None,
                    "out_qty": None if is_in else m.quantity,
                    "out_amount": None if is_in else m.amount,
                })

        ctx.update({
            "products": products,
            "selected_product": product,
            "rows": rows,
            "can_view_amount": self.request.user.has_perm("inventory.view_amount"),
        })
        return ctx
