"""销售出库：列表 / 详情 / 录入（录入即过账减少库存、结转成本）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.generic import DetailView, ListView

from apps.core.mixins import CompanyScopedMixin
from apps.core.scope import get_active_company, get_visible_companies
from apps.inventory.services import InsufficientStockError, InventoryError

from .forms import OutboundHeaderForm, OutboundLineFormSet
from .models import SalesOutbound
from .services import create_and_post_outbound


class OutboundListView(CompanyScopedMixin, ListView):
    model = SalesOutbound
    template_name = "sales/outbound_list.html"
    context_object_name = "docs"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


class OutboundDetailView(CompanyScopedMixin, DetailView):
    model = SalesOutbound
    template_name = "sales/outbound_detail.html"
    context_object_name = "doc"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


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
        if header.is_valid() and formset.is_valid():
            lines = [
                {"product": cd["product"], "quantity": cd["quantity"]}
                for cd in formset.valid_lines
            ]
            try:
                doc = create_and_post_outbound(
                    company=company, user=request.user,
                    doc_date=header.cleaned_data["doc_date"],
                    customer=header.cleaned_data.get("customer"),
                    remark=header.cleaned_data.get("remark", ""),
                    lines=lines,
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

    return render(request, "sales/outbound_form.html",
                  {"header": header, "formset": formset, "title": "销售出库"})
