"""销售订单视图（M18-2）。"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.core.mixins import CompanyScopedMixin, FilteredListMixin
from apps.core.scope import get_active_company, get_visible_companies, resolve_company
from apps.finance.models import SalesInvoice, SalesInvoiceLine
from apps.inventory.services import InventoryError

from .forms import OrderHeaderForm, OrderInvoiceForm, OrderLineFormSet, OrderShipForm
from .models import SalesOrder, SalesOutboundLine
from apps.masterdata.models import Customer

from .order_services import (
    SalesOrderError,
    backfill_sales_order,
    create_invoice_from_order,
    create_outbound_from_order,
    create_sales_order,
    line_progress,
    qty_open_invoice,
    qty_open_ship,
    refresh_order_status,
    sales_backfill_candidates,
    sales_order_progress_rows,
    update_sales_order,
    void_sales_order,
)


class OrderListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "customer__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/客户"
    export_filename = "销售订单"
    export_columns = [
        ("订单号", "doc_no"), ("日期", "doc_date"), ("客户", "customer__name"),
        ("数量", "total_quantity"), ("不含税", "total_untaxed"), ("含税", "total_taxed"),
        ("发货", "get_ship_status_display"), ("开票", "get_invoice_status_display"),
        ("收款", "get_receipt_status_display"), ("状态", "get_status_display"),
    ]
    model = SalesOrder
    template_name = "sales/order_list.html"
    context_object_name = "orders"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


class OrderDetailView(CompanyScopedMixin, DetailView):
    model = SalesOrder
    template_name = "sales/order_detail.html"
    context_object_name = "order"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order = ctx["order"]
        refresh_order_status(order)
        order.refresh_from_db()
        rows = []
        for ln in order.lines.select_related("product"):
            rows.append({"line": ln, **line_progress(ln)})
        ctx["line_rows"] = rows
        ctx["can_ship"] = (order.status == SalesOrder.Status.OPEN
                           and any(r["qty_open_ship"] > 0 for r in rows))
        ctx["can_invoice"] = (order.status == SalesOrder.Status.OPEN
                              and any(r["qty_open_invoice"] > 0 for r in rows))
        has_exec = (order.outbounds.exclude(status="void").exists()
                    or any(r["qty_invoiced"] > 0 for r in rows))
        ctx["can_edit"] = order.status == SalesOrder.Status.OPEN and not has_exec
        ctx["can_void"] = order.status == SalesOrder.Status.OPEN and not has_exec
        return ctx


def _lines_from_formset(formset):
    return [{
        "product": cd["product"], "quantity": cd["quantity"],
        "tax_rate": cd["tax_rate"],
        "tax_inclusive_price": cd.get("tax_inclusive_price"),
        "amount_untaxed": cd.get("amount_untaxed"),
        "tax_amount": cd.get("tax_amount"),
        "amount_taxed": cd.get("amount_taxed"),
    } for cd in formset.valid_lines]


@login_required
@permission_required("sales.add_salesorder", raise_exception=True)
def order_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "请先选择账套")
        return redirect("home")
    header = OrderHeaderForm(request.POST or None, company=company,
                             initial={"doc_date": timezone.localdate()})
    formset = OrderLineFormSet(request.POST or None, company=company, prefix="lines")
    if request.method == "POST" and header.is_valid() and formset.is_valid():
        try:
            order = create_sales_order(
                company=company, user=request.user,
                doc_date=header.cleaned_data["doc_date"],
                customer=header.cleaned_data["customer"],
                remark=header.cleaned_data.get("remark") or "",
                lines=_lines_from_formset(formset),
            )
            messages.success(request, f"已创建销售订单 {order.doc_no}")
            return redirect("order_detail", pk=order.pk)
        except SalesOrderError as e:
            messages.error(request, str(e.message if hasattr(e, "message") else e))
    return render(request, "sales/order_form.html", {
        "title": "新建销售订单", "header": header, "formset": formset,
        "active_company": company,
    })


@login_required
@permission_required("sales.change_salesorder", raise_exception=True)
def order_edit(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(SalesOrder, pk=pk, company=company)
    initial_lines = [{
        "product": ln.product, "quantity": ln.quantity,
        "tax_rate": ln.tax_rate, "amount_untaxed": ln.amount_untaxed,
        "tax_amount": ln.tax_amount, "amount_taxed": ln.amount_taxed,
        "tax_inclusive_price": (
            (ln.amount_taxed / ln.quantity).quantize(Decimal("0.01"))
            if ln.quantity else None),
    } for ln in order.lines.all()]
    if request.method == "POST":
        header = OrderHeaderForm(request.POST, company=company)
        formset = OrderLineFormSet(request.POST, company=company, prefix="lines")
        if header.is_valid() and formset.is_valid():
            try:
                update_sales_order(
                    order=order, user=request.user,
                    doc_date=header.cleaned_data["doc_date"],
                    customer=header.cleaned_data["customer"],
                    remark=header.cleaned_data.get("remark") or "",
                    lines=_lines_from_formset(formset),
                )
                messages.success(request, f"已保存销售订单 {order.doc_no}")
                return redirect("order_detail", pk=order.pk)
            except SalesOrderError as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    else:
        header = OrderHeaderForm(
            company=company,
            initial={"doc_date": order.doc_date, "customer": order.customer,
                     "remark": order.remark})
        formset = OrderLineFormSet(company=company, prefix="lines", initial=initial_lines)
        formset.extra = max(1, 3 - len(initial_lines))
    return render(request, "sales/order_form.html", {
        "title": f"修改销售订单 {order.doc_no}", "header": header, "formset": formset,
        "active_company": company, "order": order,
    })


@login_required
@permission_required("sales.add_salesoutbound", raise_exception=True)
def order_ship(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(
        SalesOrder.objects.prefetch_related("lines__product"), pk=pk, company=company)
    if order.status != SalesOrder.Status.OPEN:
        messages.error(request, "仅执行中订单可出库")
        return redirect("order_detail", pk=pk)
    ship_rows = []
    for ln in order.lines.all():
        remain = qty_open_ship(ln)
        if remain > 0:
            ship_rows.append({"line": ln, "remain": remain})
    if not ship_rows:
        messages.error(request, "没有待发货数量")
        return redirect("order_detail", pk=pk)
    form = OrderShipForm(request.POST or None, initial={"doc_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        lines = []
        err = None
        for row in ship_rows:
            ln = row["line"]
            raw = request.POST.get(f"qty_{ln.pk}", "")
            try:
                qty = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                err = f"行 {ln.line_no} 数量无效"
                break
            if qty <= 0:
                continue
            lines.append({"order_line": ln, "quantity": qty})
        if err:
            messages.error(request, err)
        elif not lines:
            messages.error(request, "请至少填一行发货数量")
        else:
            try:
                doc = create_outbound_from_order(
                    order=order, user=request.user,
                    doc_date=form.cleaned_data["doc_date"],
                    remark=form.cleaned_data.get("remark") or "",
                    lines=lines,
                )
                messages.success(request, f"已生成出库单 {doc.doc_no}")
                return redirect("outbound_detail", pk=doc.pk)
            except (SalesOrderError, InventoryError) as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "sales/order_ship.html", {
        "order": order, "form": form, "ship_rows": ship_rows,
        "active_company": company,
    })


@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
def order_invoice(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(
        SalesOrder.objects.prefetch_related("lines__product"), pk=pk, company=company)
    if order.status != SalesOrder.Status.OPEN:
        messages.error(request, "仅执行中订单可开票")
        return redirect("order_detail", pk=pk)
    inv_rows = []
    for ln in order.lines.all():
        remain = qty_open_invoice(ln)
        if remain > 0:
            inv_rows.append({"line": ln, "remain": remain})
    if not inv_rows:
        messages.error(request, "没有待开票数量")
        return redirect("order_detail", pk=pk)
    form = OrderInvoiceForm(request.POST or None, initial={"doc_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        lines = []
        err = None
        for row in inv_rows:
            ln = row["line"]
            raw = request.POST.get(f"qty_{ln.pk}", "")
            try:
                qty = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                err = f"行 {ln.line_no} 数量无效"
                break
            if qty <= 0:
                continue
            ob_line = None
            for ol in (SalesOutboundLine.objects
                       .filter(order_line=ln)
                       .exclude(outbound__status="void")
                       .order_by("id")):
                billed = (SalesInvoiceLine.objects
                          .filter(source_outbound_line=ol)
                          .exclude(invoice__status=SalesInvoice.Status.VOID)
                          .aggregate(v=Sum("quantity"))["v"] or 0)
                if billed < ol.quantity:
                    ob_line = ol
                    break
            lines.append({
                "order_line": ln, "quantity": qty,
                "source_outbound_line": ob_line,
            })
        if err:
            messages.error(request, err)
        elif not lines:
            messages.error(request, "请至少填一行开票数量")
        else:
            try:
                inv = create_invoice_from_order(
                    order=order, user=request.user,
                    doc_date=form.cleaned_data["doc_date"],
                    invoice_no=form.cleaned_data.get("invoice_no") or "",
                    term_days=form.cleaned_data.get("term_days") or 0,
                    remark=form.cleaned_data.get("remark") or "",
                    lines=lines,
                )
                messages.success(request, f"已生成销售发票 {inv.doc_no}")
                return redirect("sales_invoice_detail", pk=inv.pk)
            except SalesOrderError as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "sales/order_invoice.html", {
        "order": order, "form": form, "inv_rows": inv_rows,
        "active_company": company,
    })


@login_required
@require_POST
@permission_required("sales.change_salesorder", raise_exception=True)
def order_void(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(SalesOrder, pk=pk, company=company)
    try:
        void_sales_order(order=order, user=request.user)
        messages.success(request, f"已作废销售订单 {order.doc_no}")
    except SalesOrderError as e:
        messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return redirect("order_detail", pk=pk)


@login_required
@permission_required("sales.add_salesorder", raise_exception=True)
def order_backfill_list(request):
    """未完成业务补单：按客户列出可回挂候选。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "请先选择账套")
        return redirect("home")
    rows = sales_backfill_candidates(company)
    return render(request, "sales/order_backfill_list.html", {
        "active_company": company, "rows": rows,
    })


@login_required
@permission_required("sales.add_salesorder", raise_exception=True)
def order_backfill_customer(request, customer_id):
    """选定客户后勾选出库/发票并补建订单回挂。"""
    company = resolve_company(request)
    customer = get_object_or_404(Customer, pk=customer_id, company=company)
    candidates = {r["customer"].pk: r for r in sales_backfill_candidates(company)}
    row = candidates.get(customer.pk)
    if not row:
        messages.info(request, "该客户暂无待补单据")
        return redirect("order_backfill_list")
    if request.method == "POST":
        ob_ids = request.POST.getlist("outbound_ids")
        inv_ids = request.POST.getlist("invoice_ids")
        try:
            order = backfill_sales_order(
                company=company, user=request.user, customer=customer,
                outbound_ids=ob_ids, invoice_ids=inv_ids,
                remark=request.POST.get("remark") or "",
            )
            messages.success(request, f"已补单 {order.doc_no}（仅回挂关联，未改入账金额）")
            return redirect("order_detail", pk=order.pk)
        except SalesOrderError as e:
            messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "sales/order_backfill_form.html", {
        "active_company": company, "customer": customer, "row": row,
    })


@login_required
@permission_required("sales.view_salesorder", raise_exception=True)
def order_progress(request):
    """执行中销售订单进度简表。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "请先选择账套")
        return redirect("home")
    rows = sales_order_progress_rows(company)
    return render(request, "sales/order_progress.html", {
        "active_company": company, "rows": rows,
    })
