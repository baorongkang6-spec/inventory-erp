"""采购订单视图（M18-3）。"""

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
from apps.finance.models import PurchaseInvoice, PurchaseInvoiceLine
from apps.inventory.services import InventoryError

from apps.masterdata.models import Supplier

from .forms import (
    PurchaseOrderHeaderForm, PurchaseOrderInvoiceForm, PurchaseOrderLineFormSet,
    PurchaseOrderReceiveForm,
)
from .models import PurchaseInboundLine, PurchaseOrder
from .order_services import (
    PurchaseOrderError,
    backfill_purchase_order,
    create_inbound_from_order,
    create_invoice_from_order,
    create_purchase_order,
    line_progress,
    purchase_backfill_candidates,
    purchase_order_progress_rows,
    qty_open_invoice,
    qty_open_receive,
    refresh_order_status,
    update_purchase_order,
    void_purchase_order,
)


class PurchaseOrderListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "supplier__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/供应商"
    export_filename = "采购订单"
    export_columns = [
        ("订单号", "doc_no"), ("日期", "doc_date"), ("供应商", "supplier__name"),
        ("数量", "total_quantity"), ("不含税", "total_untaxed"), ("含税", "total_taxed"),
        ("收货", "get_receive_status_display"), ("收票", "get_invoice_status_display"),
        ("付款", "get_payment_status_display"), ("状态", "get_status_display"),
    ]
    model = PurchaseOrder
    template_name = "purchasing/order_list.html"
    context_object_name = "orders"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


class PurchaseOrderDetailView(CompanyScopedMixin, DetailView):
    model = PurchaseOrder
    template_name = "purchasing/order_detail.html"
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
        ctx["can_receive"] = (order.status == PurchaseOrder.Status.OPEN
                              and any(r["qty_open_receive"] > 0 for r in rows))
        ctx["can_invoice"] = (order.status == PurchaseOrder.Status.OPEN
                              and any(r["qty_open_invoice"] > 0 for r in rows))
        has_exec = (order.inbounds.exclude(status="void").exists()
                    or any(r["qty_invoiced"] > 0 for r in rows))
        ctx["can_edit"] = order.status == PurchaseOrder.Status.OPEN and not has_exec
        ctx["can_void"] = order.status == PurchaseOrder.Status.OPEN and not has_exec
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
@permission_required("purchasing.add_purchaseorder", raise_exception=True)
def purchase_order_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "请先选择账套")
        return redirect("home")
    header = PurchaseOrderHeaderForm(
        request.POST or None, company=company, initial={"doc_date": timezone.localdate()})
    formset = PurchaseOrderLineFormSet(request.POST or None, company=company, prefix="lines")
    if request.method == "POST" and header.is_valid() and formset.is_valid():
        try:
            order = create_purchase_order(
                company=company, user=request.user,
                doc_date=header.cleaned_data["doc_date"],
                supplier=header.cleaned_data["supplier"],
                remark=header.cleaned_data.get("remark") or "",
                lines=_lines_from_formset(formset),
            )
            messages.success(request, f"已创建采购订单 {order.doc_no}")
            return redirect("purchase_order_detail", pk=order.pk)
        except PurchaseOrderError as e:
            messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "purchasing/order_form.html", {
        "title": "新建采购订单", "header": header, "formset": formset,
        "active_company": company,
    })


@login_required
@permission_required("purchasing.change_purchaseorder", raise_exception=True)
def purchase_order_edit(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(PurchaseOrder, pk=pk, company=company)
    initial_lines = [{
        "product": ln.product, "quantity": ln.quantity,
        "tax_rate": ln.tax_rate, "amount_untaxed": ln.amount_untaxed,
        "tax_amount": ln.tax_amount, "amount_taxed": ln.amount_taxed,
        "tax_inclusive_price": (
            (ln.amount_taxed / ln.quantity).quantize(Decimal("0.01"))
            if ln.quantity else None),
    } for ln in order.lines.all()]
    if request.method == "POST":
        header = PurchaseOrderHeaderForm(request.POST, company=company)
        formset = PurchaseOrderLineFormSet(request.POST, company=company, prefix="lines")
        if header.is_valid() and formset.is_valid():
            try:
                update_purchase_order(
                    order=order, user=request.user,
                    doc_date=header.cleaned_data["doc_date"],
                    supplier=header.cleaned_data["supplier"],
                    remark=header.cleaned_data.get("remark") or "",
                    lines=_lines_from_formset(formset),
                )
                messages.success(request, f"已保存采购订单 {order.doc_no}")
                return redirect("purchase_order_detail", pk=order.pk)
            except PurchaseOrderError as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    else:
        header = PurchaseOrderHeaderForm(
            company=company,
            initial={"doc_date": order.doc_date, "supplier": order.supplier,
                     "remark": order.remark})
        formset = PurchaseOrderLineFormSet(
            company=company, prefix="lines", initial=initial_lines)
        formset.extra = max(1, 3 - len(initial_lines))
    return render(request, "purchasing/order_form.html", {
        "title": f"修改采购订单 {order.doc_no}", "header": header, "formset": formset,
        "active_company": company, "order": order,
    })


@login_required
@permission_required("purchasing.add_purchaseinbound", raise_exception=True)
def purchase_order_receive(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("lines__product"), pk=pk, company=company)
    if order.status != PurchaseOrder.Status.OPEN:
        messages.error(request, "仅执行中订单可入库")
        return redirect("purchase_order_detail", pk=pk)
    recv_rows = []
    for ln in order.lines.all():
        remain = qty_open_receive(ln)
        if remain > 0:
            recv_rows.append({"line": ln, "remain": remain})
    if not recv_rows:
        messages.error(request, "没有待收货数量")
        return redirect("purchase_order_detail", pk=pk)
    form = PurchaseOrderReceiveForm(
        request.POST or None, initial={"doc_date": timezone.localdate()})
    if request.method == "POST" and form.is_valid():
        lines = []
        err = None
        for row in recv_rows:
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
            messages.error(request, "请至少填一行收货数量")
        else:
            try:
                doc = create_inbound_from_order(
                    order=order, user=request.user,
                    doc_date=form.cleaned_data["doc_date"],
                    remark=form.cleaned_data.get("remark") or "",
                    lines=lines,
                )
                messages.success(request, f"已生成入库单 {doc.doc_no}")
                return redirect("inbound_detail", pk=doc.pk)
            except (PurchaseOrderError, InventoryError) as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "purchasing/order_receive.html", {
        "order": order, "form": form, "recv_rows": recv_rows,
        "active_company": company,
    })


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def purchase_order_invoice(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(
        PurchaseOrder.objects.prefetch_related("lines__product"), pk=pk, company=company)
    if order.status != PurchaseOrder.Status.OPEN:
        messages.error(request, "仅执行中订单可收票")
        return redirect("purchase_order_detail", pk=pk)
    inv_rows = []
    for ln in order.lines.all():
        remain = qty_open_invoice(ln)
        if remain > 0:
            inv_rows.append({"line": ln, "remain": remain})
    if not inv_rows:
        messages.error(request, "没有待收票数量")
        return redirect("purchase_order_detail", pk=pk)
    form = PurchaseOrderInvoiceForm(
        request.POST or None, initial={"doc_date": timezone.localdate()})
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
            ib_line = None
            for il in (PurchaseInboundLine.objects
                       .filter(order_line=ln)
                       .exclude(inbound__status="void")
                       .order_by("id")):
                billed = (PurchaseInvoiceLine.objects
                          .filter(source_inbound_line=il)
                          .exclude(invoice__status=PurchaseInvoice.Status.VOID)
                          .aggregate(v=Sum("quantity"))["v"] or 0)
                if billed < il.quantity:
                    ib_line = il
                    break
            lines.append({
                "order_line": ln, "quantity": qty,
                "source_inbound_line": ib_line,
            })
        if err:
            messages.error(request, err)
        elif not lines:
            messages.error(request, "请至少填一行收票数量")
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
                messages.success(request, f"已生成采购发票 {inv.doc_no}")
                return redirect("purchase_invoice_detail", pk=inv.pk)
            except PurchaseOrderError as e:
                messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "purchasing/order_invoice.html", {
        "order": order, "form": form, "inv_rows": inv_rows,
        "active_company": company,
    })


@login_required
@require_POST
@permission_required("purchasing.change_purchaseorder", raise_exception=True)
def purchase_order_void(request, pk):
    company = resolve_company(request)
    order = get_object_or_404(PurchaseOrder, pk=pk, company=company)
    try:
        void_purchase_order(order=order, user=request.user)
        messages.success(request, f"已作废采购订单 {order.doc_no}")
    except PurchaseOrderError as e:
        messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return redirect("purchase_order_detail", pk=pk)


@login_required
@permission_required("purchasing.add_purchaseorder", raise_exception=True)
def purchase_order_backfill_list(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "请先选择账套")
        return redirect("home")
    rows = purchase_backfill_candidates(company)
    return render(request, "purchasing/order_backfill_list.html", {
        "active_company": company, "rows": rows,
    })


@login_required
@permission_required("purchasing.add_purchaseorder", raise_exception=True)
def purchase_order_backfill_supplier(request, supplier_id):
    company = resolve_company(request)
    supplier = get_object_or_404(Supplier, pk=supplier_id, company=company)
    candidates = {r["supplier"].pk: r for r in purchase_backfill_candidates(company)}
    row = candidates.get(supplier.pk)
    if not row:
        messages.info(request, "该供应商暂无待补单据")
        return redirect("purchase_order_backfill_list")
    if request.method == "POST":
        ib_ids = request.POST.getlist("inbound_ids")
        inv_ids = request.POST.getlist("invoice_ids")
        try:
            order = backfill_purchase_order(
                company=company, user=request.user, supplier=supplier,
                inbound_ids=ib_ids, invoice_ids=inv_ids,
                remark=request.POST.get("remark") or "",
            )
            messages.success(request, f"已补单 {order.doc_no}（仅回挂关联，未改入账金额）")
            return redirect("purchase_order_detail", pk=order.pk)
        except PurchaseOrderError as e:
            messages.error(request, "; ".join(e.messages) if hasattr(e, "messages") else str(e))
    return render(request, "purchasing/order_backfill_form.html", {
        "active_company": company, "supplier": supplier, "row": row,
    })


@login_required
@permission_required("purchasing.view_purchaseorder", raise_exception=True)
def purchase_order_progress(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "请先选择账套")
        return redirect("home")
    rows = purchase_order_progress_rows(company)
    return render(request, "purchasing/order_progress.html", {
        "active_company": company, "rows": rows,
    })
