"""采购入库：列表 / 详情 / 录入（录入即过账增加库存）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.core.mixins import CompanyScopedMixin, FilteredListMixin
from apps.core.scope import get_active_company, get_visible_companies, resolve_company
from apps.inventory.services import InventoryError

from apps.masterdata.forms import ExpenseFormSet

from .forms import InboundHeaderForm, InboundLineFormSet
from .models import PurchaseInbound
from .services import create_and_post_inbound, void_purchase_inbound


class InboundListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "supplier__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/供应商"
    export_filename = "采购入库"
    export_columns = [("单据编号","doc_no"),("日期","doc_date"),("供应商","supplier__name"),
                      ("方式","get_purchase_type_display"),("总数量","total_quantity"),
                      ("入库成本","total_amount"),("含税合计","total_taxed"),("状态","get_status_display")]
    model = PurchaseInbound
    template_name = "purchasing/inbound_list.html"
    context_object_name = "docs"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


@login_required
@permission_required("purchasing.view_purchaseinbound", raise_exception=True)
def inbound_print(request, pk):
    """采购入库单打印页（A4，含公司全称、制单人）。"""
    company = resolve_company(request)
    doc = get_object_or_404(
        PurchaseInbound.objects.select_related("company", "supplier", "created_by"),
        pk=pk, company=company)
    return render(request, "purchasing/inbound_print.html",
                  {"doc": doc, "now": timezone.now()})


class InboundDetailView(CompanyScopedMixin, DetailView):
    model = PurchaseInbound
    template_name = "purchasing/inbound_detail.html"
    context_object_name = "doc"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


@login_required
@permission_required("purchasing.add_purchaseinbound", raise_exception=True)
def inbound_create(request):
    company = _active_company(request)
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        header = InboundHeaderForm(request.POST, company=company)
        formset = InboundLineFormSet(request.POST, company=company)
        expenses_fs = ExpenseFormSet(request.POST, prefix="exp", company=company)
        if header.is_valid() and formset.is_valid() and expenses_fs.is_valid():
            lines = [
                {"product": cd["product"], "quantity": cd["quantity"],
                 "unit_price": cd["unit_price"], "tax_rate": cd["tax_rate"]}
                for cd in formset.valid_lines
            ]
            expenses = [{"category": e["category"], "amount": e["amount"]}
                        for e in expenses_fs.expense_lines]
            try:
                doc = create_and_post_inbound(
                    company=company, user=request.user,
                    doc_date=header.cleaned_data["doc_date"],
                    supplier=header.cleaned_data.get("supplier"),
                    remark=header.cleaned_data.get("remark", ""),
                    lines=lines, expenses=expenses,
                    purchase_type=header.cleaned_data["purchase_type"],
                )
            except InventoryError as e:
                messages.error(request, f"过账失败：{e}")
            else:
                messages.success(request, f"采购入库已过账：{doc.doc_no}")
                return redirect("inbound_detail", pk=doc.pk)
    else:
        header = InboundHeaderForm(company=company, initial={"doc_date": timezone.localdate()})
        formset = InboundLineFormSet(company=company)
        expenses_fs = ExpenseFormSet(prefix="exp", company=company)

    return render(request, "purchasing/inbound_form.html",
                  {"header": header, "formset": formset, "expenses_fs": expenses_fs, "title": "采购入库"})


def _active_company(request):
    return get_active_company(request, list(get_visible_companies(request.user)))


@require_POST
@login_required
@permission_required("purchasing.void_purchaseinbound", raise_exception=True)
def inbound_void(request, pk):
    company = _active_company(request)
    doc = get_object_or_404(PurchaseInbound, pk=pk, company=company)
    try:
        void_purchase_inbound(doc, request.user)
    except InventoryError as e:
        messages.error(request, f"作废失败：{e}")
    else:
        messages.success(request, f"已作废采购入库 {doc.doc_no}")
    return redirect("inbound_detail", pk=doc.pk)
