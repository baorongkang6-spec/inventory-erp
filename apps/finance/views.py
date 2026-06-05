"""资金往来视图：银行账户（M2-1）、采购发票→应付（M2-2）、付款与核销（M2-3/4）。"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, ListView

from apps.core.crud import (
    ScopedCreateView,
    ScopedDeleteView,
    ScopedListView,
    ScopedUpdateView,
)
from apps.core.mixins import CompanyScopedMixin
from apps.core.scope import get_active_company, get_visible_companies
from apps.purchasing.models import PurchaseInbound
from apps.sales.models import SalesOutbound

from .forms import (
    BankAccountForm,
    PaymentForm,
    PurchaseInvoiceHeaderForm,
    PurchaseInvoiceLineFormSet,
    ReceiptForm,
    SalesInvoiceHeaderForm,
    SalesInvoiceLineFormSet,
)
from .models import BankAccount, Payment, PurchaseInvoice, Receipt, SalesInvoice
from .services import (
    SettlementError,
    allocate_payment,
    allocate_receipt,
    create_payment,
    create_purchase_invoice,
    create_receipt,
    create_sales_invoice,
)


class BankAccountListView(ScopedListView):
    model = BankAccount
    title = "银行账户"
    columns = [("账户名称", "name"), ("开户行", "bank_name"), ("银行账号", "account_no"),
               ("期初余额", "opening_balance"), ("启用", "is_active")]
    create_url_name = "bankaccount_create"
    update_url_name = "bankaccount_update"
    delete_url_name = "bankaccount_delete"


class BankAccountCreateView(ScopedCreateView):
    model = BankAccount
    form_class = BankAccountForm
    title = "银行账户"
    success_url = reverse_lazy("bankaccount_list")


class BankAccountUpdateView(ScopedUpdateView):
    model = BankAccount
    form_class = BankAccountForm
    title = "银行账户"
    success_url = reverse_lazy("bankaccount_list")


class BankAccountDeleteView(ScopedDeleteView):
    model = BankAccount
    title = "银行账户"
    success_url = reverse_lazy("bankaccount_list")


# --- 采购发票（→应付账款）----------------------------------------------------
class PurchaseInvoiceListView(CompanyScopedMixin, ListView):
    model = PurchaseInvoice
    template_name = "finance/purchase_invoice_list.html"
    context_object_name = "invoices"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


class PurchaseInvoiceDetailView(CompanyScopedMixin, DetailView):
    model = PurchaseInvoice
    template_name = "finance/purchase_invoice_detail.html"
    context_object_name = "inv"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


def _inbound_prefill(company, inbound_id):
    """从入库单带出发票明细初始值；返回 (header_initial, lines_initial, inbound)。"""
    inbound = PurchaseInbound.objects.filter(company=company, pk=inbound_id).first()
    if not inbound:
        return {}, [], None
    lines = [
        {
            "product": ln.product_id,
            "description": ln.product.name,
            "amount_untaxed": ln.amount,           # 入库金额为不含税成本
            "tax_rate": ln.product.default_tax_rate,
        }
        for ln in inbound.lines.select_related("product")
    ]
    header = {"supplier": inbound.supplier_id, "doc_date": inbound.doc_date}
    return header, lines, inbound


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def purchase_invoice_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        header = PurchaseInvoiceHeaderForm(request.POST, company=company)
        formset = PurchaseInvoiceLineFormSet(request.POST, company=company)
        if header.is_valid() and formset.is_valid():
            lines = [
                {
                    "product": cd.get("product"),
                    "description": cd.get("description", ""),
                    "amount_untaxed": cd["amount_untaxed"],
                    "tax_rate": cd["tax_rate"],
                }
                for cd in formset.valid_lines
            ]
            inv = create_purchase_invoice(
                company=company, user=request.user,
                doc_date=header.cleaned_data["doc_date"],
                supplier=header.cleaned_data["supplier"],
                invoice_no=header.cleaned_data.get("invoice_no", ""),
                remark=header.cleaned_data.get("remark", ""),
                lines=lines,
            )
            messages.success(request, f"采购发票已登记（应付）：{inv.doc_no}")
            return redirect("purchase_invoice_detail", pk=inv.pk)
    else:
        header_initial = {"doc_date": timezone.localdate()}
        lines_initial = []
        inbound_id = request.GET.get("inbound")
        if inbound_id:
            h, lines_initial, inbound = _inbound_prefill(company, inbound_id)
            if inbound:
                header_initial.update(h)
                messages.info(request, f"已从入库单 {inbound.doc_no} 带出明细，请核对税率后登记")
        header = PurchaseInvoiceHeaderForm(company=company, initial=header_initial)
        formset = PurchaseInvoiceLineFormSet(company=company, initial=lines_initial)

    # 可选择的入库单（供「从入库单带入」下拉）
    inbounds = PurchaseInbound.objects.filter(company=company).order_by("-doc_date", "-id")[:50]
    return render(request, "finance/purchase_invoice_form.html",
                  {"header": header, "formset": formset, "inbounds": inbounds, "title": "采购发票"})


# --- 付款登记（自动生成银行日记账）------------------------------------------
class PaymentListView(CompanyScopedMixin, ListView):
    model = Payment
    template_name = "finance/payment_list.html"
    context_object_name = "payments"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "bank_account")


class PaymentDetailView(CompanyScopedMixin, DetailView):
    model = Payment
    template_name = "finance/payment_detail.html"
    context_object_name = "pay"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "bank_account", "bank_journal")


@login_required
@permission_required("finance.add_payment", raise_exception=True)
def payment_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        form = PaymentForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            pay = create_payment(
                company=company, user=request.user, doc_date=cd["doc_date"],
                bank_account=cd["bank_account"], supplier=cd["supplier"],
                amount=cd["amount"], summary=cd.get("summary", ""),
            )
            messages.success(request, f"付款已登记，并生成银行日记账：{pay.doc_no}")
            return redirect("payment_detail", pk=pay.pk)
    else:
        form = PaymentForm(company=company, initial={"doc_date": timezone.localdate()})

    return render(request, "finance/payment_form.html", {"form": form, "title": "付款登记"})


@login_required
@permission_required("finance.add_paymentallocation", raise_exception=True)
def payment_allocate(request, pk):
    """应付核销：把一笔付款核销到该供应商的若干采购发票（支持部分核销）。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    payment = get_object_or_404(Payment, pk=pk, company=company)

    open_invoices = list(
        PurchaseInvoice.objects.filter(
            company=company, supplier=payment.supplier,
            status=PurchaseInvoice.Status.REGISTERED,
        ).order_by("doc_date", "id")
    )
    open_invoices = [inv for inv in open_invoices if inv.outstanding > 0]

    if request.method == "POST":
        allocations = []
        for inv in open_invoices:
            raw = (request.POST.get(f"alloc-{inv.pk}") or "").strip()
            if raw:
                try:
                    amt = Decimal(raw)
                except (InvalidOperation, ValueError):
                    messages.error(request, f"发票 {inv.doc_no} 的核销金额无效")
                    break
                allocations.append({"invoice": inv, "amount": amt})
        else:
            try:
                allocate_payment(payment=payment, allocations=allocations, user=request.user)
            except SettlementError as e:
                messages.error(request, f"核销失败：{e}")
            else:
                messages.success(request, "核销完成")
                return redirect("payment_detail", pk=payment.pk)

    return render(request, "finance/payment_allocate.html",
                  {"payment": payment, "open_invoices": open_invoices})


# ============================= 销售侧（镜像采购侧）=============================
class SalesInvoiceListView(CompanyScopedMixin, ListView):
    model = SalesInvoice
    template_name = "finance/sales_invoice_list.html"
    context_object_name = "invoices"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


class SalesInvoiceDetailView(CompanyScopedMixin, DetailView):
    model = SalesInvoice
    template_name = "finance/sales_invoice_detail.html"
    context_object_name = "inv"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


def _outbound_prefill(company, outbound_id):
    outbound = SalesOutbound.objects.filter(company=company, pk=outbound_id).first()
    if not outbound:
        return {}, [], None
    lines = [
        {
            "product": ln.product_id,
            "description": ln.product.name,
            "amount_untaxed": ln.amount,      # 出库结转成本作不含税额初值，用户可改为售价
            "tax_rate": ln.product.default_tax_rate,
        }
        for ln in outbound.lines.select_related("product")
    ]
    header = {"customer": outbound.customer_id, "doc_date": outbound.doc_date}
    return header, lines, outbound


@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
def sales_invoice_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        header = SalesInvoiceHeaderForm(request.POST, company=company)
        formset = SalesInvoiceLineFormSet(request.POST, company=company)
        if header.is_valid() and formset.is_valid():
            lines = [
                {"product": cd.get("product"), "description": cd.get("description", ""),
                 "amount_untaxed": cd["amount_untaxed"], "tax_rate": cd["tax_rate"]}
                for cd in formset.valid_lines
            ]
            inv = create_sales_invoice(
                company=company, user=request.user,
                doc_date=header.cleaned_data["doc_date"],
                customer=header.cleaned_data["customer"],
                invoice_no=header.cleaned_data.get("invoice_no", ""),
                remark=header.cleaned_data.get("remark", ""), lines=lines,
            )
            messages.success(request, f"销售发票已开具（应收）：{inv.doc_no}")
            return redirect("sales_invoice_detail", pk=inv.pk)
    else:
        header_initial = {"doc_date": timezone.localdate()}
        lines_initial = []
        outbound_id = request.GET.get("outbound")
        if outbound_id:
            h, lines_initial, outbound = _outbound_prefill(company, outbound_id)
            if outbound:
                header_initial.update(h)
                messages.info(request, f"已从出库单 {outbound.doc_no} 带出明细，请核对售价/税率后开具")
        header = SalesInvoiceHeaderForm(company=company, initial=header_initial)
        formset = SalesInvoiceLineFormSet(company=company, initial=lines_initial)

    outbounds = SalesOutbound.objects.filter(company=company).order_by("-doc_date", "-id")[:50]
    return render(request, "finance/sales_invoice_form.html",
                  {"header": header, "formset": formset, "outbounds": outbounds, "title": "销售发票"})


class ReceiptListView(CompanyScopedMixin, ListView):
    model = Receipt
    template_name = "finance/receipt_list.html"
    context_object_name = "receipts"

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "bank_account")


class ReceiptDetailView(CompanyScopedMixin, DetailView):
    model = Receipt
    template_name = "finance/receipt_detail.html"
    context_object_name = "rec"

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "bank_account", "bank_journal")


@login_required
@permission_required("finance.add_receipt", raise_exception=True)
def receipt_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        form = ReceiptForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            rec = create_receipt(
                company=company, user=request.user, doc_date=cd["doc_date"],
                bank_account=cd["bank_account"], customer=cd["customer"],
                amount=cd["amount"], summary=cd.get("summary", ""),
            )
            messages.success(request, f"收款已登记，并生成银行日记账：{rec.doc_no}")
            return redirect("receipt_detail", pk=rec.pk)
    else:
        form = ReceiptForm(company=company, initial={"doc_date": timezone.localdate()})

    return render(request, "finance/receipt_form.html", {"form": form, "title": "收款登记"})


@login_required
@permission_required("finance.add_receiptallocation", raise_exception=True)
def receipt_allocate(request, pk):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    receipt = get_object_or_404(Receipt, pk=pk, company=company)

    open_invoices = [
        inv for inv in SalesInvoice.objects.filter(
            company=company, customer=receipt.customer,
            status=SalesInvoice.Status.REGISTERED,
        ).order_by("doc_date", "id")
        if inv.outstanding > 0
    ]

    if request.method == "POST":
        allocations = []
        for inv in open_invoices:
            raw = (request.POST.get(f"alloc-{inv.pk}") or "").strip()
            if raw:
                try:
                    amt = Decimal(raw)
                except (InvalidOperation, ValueError):
                    messages.error(request, f"发票 {inv.doc_no} 的核销金额无效")
                    break
                allocations.append({"invoice": inv, "amount": amt})
        else:
            try:
                allocate_receipt(receipt=receipt, allocations=allocations, user=request.user)
            except SettlementError as e:
                messages.error(request, f"核销失败：{e}")
            else:
                messages.success(request, "核销完成")
                return redirect("receipt_detail", pk=receipt.pk)

    return render(request, "finance/receipt_allocate.html",
                  {"receipt": receipt, "open_invoices": open_invoices})
