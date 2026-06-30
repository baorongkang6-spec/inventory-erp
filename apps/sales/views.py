"""销售出库：列表 / 详情 / 录入（录入即过账减少库存、结转成本）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.core.mixins import CompanyScopedMixin, FilteredListMixin
from apps.core.scope import get_active_company, get_visible_companies, resolve_company
from apps.inventory.services import InsufficientStockError, InventoryError

from apps.masterdata.forms import ExpenseFormSet

from .forms import OutboundHeaderForm, OutboundLineFormSet
from .models import SalesOutbound
from .services import (create_and_post_outbound, delete_sales_outbound,
                       outbound_delete_block_reason, void_sales_outbound)


class OutboundListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "customer__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/客户"
    export_filename = "销售出库"
    export_columns = [("单据编号","doc_no"),("日期","doc_date"),("客户","customer__name"),
                      ("方式","get_sales_type_display"),("总数量","total_quantity"),
                      ("不含税售额","total_untaxed"),("含税售额","total_taxed"),
                      ("结转成本","total_cost"),("状态","get_status_display")]
    model = SalesOutbound
    template_name = "sales/outbound_list.html"
    context_object_name = "docs"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")

    def get_context_data(self, **kwargs):
        from decimal import Decimal
        ctx = super().get_context_data(**kwargs)
        docs = ctx["docs"]
        ctx["fully_invoiced_ids"] = _fully_invoiced_outbound_ids(docs)
        today = timezone.localdate()
        mgr = _outbound_is_manager(self.request.user)
        for d in docs:
            d.can_delete = outbound_delete_block_reason(d, self.request.user, today, mgr) is None
        z = Decimal("0.00")
        # 金额列求合计；总数量异构(不同商品)不跨单相加
        ctx["totals"] = {"untaxed": sum((d.total_untaxed for d in docs), z),
                         "taxed": sum((d.total_taxed for d in docs), z),
                         "cost": sum((d.total_cost for d in docs), z)}
        return ctx


def _fully_invoiced_outbound_ids(outbounds):
    """返回「各行已开票数量 ≥ 出库数量」的出库单 id 集合（用于列表显示发票已开具）。"""
    from django.db.models import Sum
    from apps.finance.models import SalesInvoice, SalesInvoiceLine
    from .models import SalesOutboundLine
    ids = [o.pk for o in outbounds]
    if not ids:
        return set()
    lines = list(SalesOutboundLine.objects.filter(outbound_id__in=ids)
                 .values("pk", "outbound_id", "quantity"))
    invoiced = {r["source_outbound_line"]: (r["q"] or 0) for r in
                SalesInvoiceLine.objects.filter(source_outbound_line__outbound_id__in=ids)
                .exclude(invoice__status=SalesInvoice.Status.VOID)
                .values("source_outbound_line").annotate(q=Sum("quantity"))}
    under = set()  # 有任一行未开满
    for ln in lines:
        if invoiced.get(ln["pk"], 0) < ln["quantity"]:
            under.add(ln["outbound_id"])
    # 全开 = 有明细行 且 无欠开行
    with_lines = {ln["outbound_id"] for ln in lines}
    return with_lines - under


@login_required
@permission_required("sales.view_salesoutbound", raise_exception=True)
def outbound_print(request, pk):
    """销售出库单打印页（A4，含公司全称、制单人）。"""
    company = resolve_company(request)
    doc = get_object_or_404(
        SalesOutbound.objects.select_related("company", "customer", "created_by"),
        pk=pk, company=company)
    return render(request, "sales/outbound_print.html",
                  {"doc": doc, "now": timezone.now()})


class OutboundDetailView(CompanyScopedMixin, DetailView):
    model = SalesOutbound
    template_name = "sales/outbound_detail.html"
    context_object_name = "doc"

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "mirror_inbound")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_delete"] = outbound_delete_block_reason(
            self.object, self.request.user, timezone.localdate(),
            _outbound_is_manager(self.request.user)) is None
        return ctx


@require_POST
@login_required
@permission_required("sales.void_salesoutbound", raise_exception=True)
def outbound_void(request, pk):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(SalesOutbound, pk=pk, company=company)
    try:
        void_sales_outbound(doc, request.user)
    except InventoryError as e:
        messages.error(request, f"作废失败：{e}")
    else:
        messages.success(request, f"已作废销售出库 {doc.doc_no}")
    return redirect("outbound_detail", pk=doc.pk)


@login_required
@permission_required("sales.add_salesoutbound", raise_exception=True)
def outbound_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        header = OutboundHeaderForm(request.POST, company=company)
        formset = OutboundLineFormSet(request.POST, company=company)
        expenses_fs = ExpenseFormSet(request.POST, prefix="exp", company=company)
        if header.is_valid() and formset.is_valid() and expenses_fs.is_valid():
            lines = [
                {"product": cd["product"], "quantity": cd["quantity"],
                 "tax_rate": cd["tax_rate"], "tax_inclusive_price": cd.get("tax_inclusive_price"), "amount_untaxed": cd.get("amount_untaxed"), "tax_amount": cd.get("tax_amount"), "amount_taxed": cd.get("amount_taxed")}
                for cd in formset.valid_lines
            ]
            expenses = [{"category": e["category"], "amount": e["amount"]}
                        for e in expenses_fs.expense_lines]
            try:
                doc = create_and_post_outbound(
                    company=company, user=request.user,
                    doc_date=header.cleaned_data["doc_date"],
                    customer=header.cleaned_data.get("customer"),
                    remark=header.cleaned_data.get("remark", ""),
                    lines=lines, expenses=expenses,
                    sales_type=header.cleaned_data["sales_type"],
                )
            except InsufficientStockError as e:
                messages.error(request, f"库存不足，整单未保存：{e}")
            except InventoryError as e:
                messages.error(request, f"过账失败，整单未保存：{e}")
            else:
                messages.success(request, f"销售出库已过账：{doc.doc_no}")
                return redirect("outbound_detail", pk=doc.pk)
    else:
        header = OutboundHeaderForm(company=company, initial={"doc_date": timezone.localdate()})
        formset = OutboundLineFormSet(company=company)
        expenses_fs = ExpenseFormSet(prefix="exp", company=company)

    return render(request, "sales/outbound_form.html",
                  {"header": header, "formset": formset, "expenses_fs": expenses_fs, "title": "销售出库"})


def _outbound_is_manager(user):
    return user.is_superuser or user.has_perm("sales.void_salesoutbound")


@login_required
@permission_required("sales.add_salesoutbound", raise_exception=True)
def outbound_edit(request, pk):
    """修改销售出库单（冲正重过账，保留单号）。本人+管理员、本月、未被下游引用方可改。"""
    from .services import outbound_edit_block_reason, update_and_repost_outbound
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(SalesOutbound.objects.select_related("customer", "mirror_inbound"),
                            pk=pk, company=company)
    reason = outbound_edit_block_reason(doc, request.user, timezone.localdate(),
                                        _outbound_is_manager(request.user))
    if reason:
        messages.error(request, f"不可修改：{reason}")
        return redirect("outbound_detail", pk=doc.pk)

    if request.method == "POST":
        header = OutboundHeaderForm(request.POST, company=company)
        formset = OutboundLineFormSet(request.POST, company=company)
        expenses_fs = ExpenseFormSet(request.POST, prefix="exp", company=company)
        if header.is_valid() and formset.is_valid() and expenses_fs.is_valid():
            lines = [{"product": cd["product"], "quantity": cd["quantity"],
                      "tax_rate": cd["tax_rate"], "tax_inclusive_price": cd.get("tax_inclusive_price"), "amount_untaxed": cd.get("amount_untaxed"), "tax_amount": cd.get("tax_amount"), "amount_taxed": cd.get("amount_taxed")}
                     for cd in formset.valid_lines]
            expenses = [{"category": e["category"], "amount": e["amount"]}
                        for e in expenses_fs.expense_lines]
            try:
                update_and_repost_outbound(
                    doc, user=request.user, doc_date=header.cleaned_data["doc_date"],
                    customer=header.cleaned_data.get("customer"),
                    remark=header.cleaned_data.get("remark", ""),
                    lines=lines, expenses=expenses,
                    sales_type=header.cleaned_data["sales_type"])
            except InsufficientStockError as e:
                messages.error(request, f"库存不足，未修改：{e}")
            except InventoryError as e:
                messages.error(request, f"修改失败：{e}")
            else:
                messages.success(request, f"销售出库已修改：{doc.doc_no}")
                return redirect("outbound_detail", pk=doc.pk)
    else:
        header = OutboundHeaderForm(company=company, initial={
            "doc_date": doc.doc_date, "sales_type": doc.sales_type,
            "customer": doc.customer_id, "remark": doc.remark})
        from decimal import Decimal
        line_init = [{
            "product": ln.product_id, "quantity": ln.quantity, "tax_rate": ln.tax_rate,
            "tax_inclusive_price": (ln.amount_taxed / ln.quantity).quantize(Decimal("0.01"))
                                   if ln.quantity else Decimal("0.00"),
            "amount_untaxed": ln.amount_untaxed, "tax_amount": ln.tax_amount,
            "amount_taxed": ln.amount_taxed,
        } for ln in doc.lines.all()]
        formset = OutboundLineFormSet(company=company, initial=line_init)
        from apps.finance.models import ExpenseEntry
        exp_init = [{"category": e.category_id, "amount": e.amount}
                    for e in ExpenseEntry.objects.filter(
                        company=company, source_type="SalesOutbound", source_id=str(doc.pk))]
        expenses_fs = ExpenseFormSet(prefix="exp", company=company, initial=exp_init)

    return render(request, "sales/outbound_form.html",
                  {"header": header, "formset": formset, "expenses_fs": expenses_fs,
                   "title": f"修改销售出库 {doc.doc_no}", "editing": True})


@require_POST
@login_required
@permission_required("sales.add_salesoutbound", raise_exception=True)
def outbound_delete(request, pk):
    """硬删除销售出库单（安全条件下）：彻底移除单据并反冲库存。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(SalesOutbound, pk=pk, company=company)
    doc_no = doc.doc_no
    try:
        delete_sales_outbound(doc, user=request.user, today=timezone.localdate(),
                              is_manager=_outbound_is_manager(request.user))
    except InventoryError as e:
        messages.error(request, f"删除失败：{e}")
        return redirect("outbound_detail", pk=pk)
    messages.success(request, f"销售出库已删除：{doc_no}")
    return redirect("outbound_list")
