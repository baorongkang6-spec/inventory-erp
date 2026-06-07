"""资金往来视图：银行账户（M2-1）、采购发票→应付（M2-2）、付款与核销（M2-3/4）。"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView

from apps.core.crud import (
    ScopedCreateView,
    ScopedDeleteView,
    ScopedListView,
    ScopedUpdateView,
)
from apps.core.mixins import CompanyScopedMixin, FilteredListMixin
from apps.core.scope import get_active_company, get_visible_companies, resolve_company
from apps.purchasing.models import PurchaseInbound
from apps.sales.models import SalesOutbound

from .forms import (
    BankAccountForm,
    NotePayableForm,
    NoteReceivableForm,
    PaymentForm,
    PurchaseInvoiceHeaderForm,
    PurchaseInvoiceLineFormSet,
    ReceiptForm,
    SalesInvoiceHeaderForm,
    SalesInvoiceLineFormSet,
)
from .models import (
    BankAccount,
    BankJournal,
    NotePayable,
    NoteReceivable,
    Payment,
    PurchaseInvoice,
    Receipt,
    SalesInvoice,
)
from .services import (
    SettlementError,
    allocate_payment,
    allocate_receipt,
    create_note_payable,
    create_note_receivable,
    create_payment,
    create_purchase_invoice,
    create_receipt,
    create_sales_invoice,
    endorse_receivable_against_purchase,
    settle_payable_against_purchase,
    settle_receivable_against_sales,
)


class BankAccountListView(ScopedListView):
    search_fields = ["name", "account_no", "bank_name"]
    q_placeholder = "户名/账号"
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
class PurchaseInvoiceListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "invoice_no", "supplier__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/发票号/供应商"
    export_filename = "采购发票"
    export_columns = [("单据编号","doc_no"),("开票日期","doc_date"),("发票号码","invoice_no"),
                      ("供应商","supplier__name"),("不含税","amount_untaxed"),("税额","tax_amount"),
                      ("含税(应付)","amount_taxed"),("已核销","settled_amount"),("未核销","outstanding"),
                      ("状态","get_status_display")]
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
            "quantity": ln.quantity,
            "amount_untaxed": ln.amount_untaxed,   # 直接引入库单行的不含税金额
            "tax_rate": ln.tax_rate,
            "source_inbound_line": ln.pk,          # 关联入库行，供暂估匹配
        }
        for ln in inbound.lines.select_related("product")
    ]
    header = {"supplier": inbound.supplier_id, "doc_date": inbound.doc_date}
    return header, lines, inbound


def _resolve_inbound_line(company, line_id):
    """把表单里的入库行 id 解析成 PurchaseInboundLine（限本账套），无效返回 None。"""
    if not line_id:
        return None
    from apps.purchasing.models import PurchaseInboundLine
    return PurchaseInboundLine.objects.filter(pk=line_id, inbound__company=company).first()


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
                    "quantity": cd.get("quantity"),
                    "amount_untaxed": cd["amount_untaxed"],
                    "tax_rate": cd["tax_rate"],
                    "source_inbound_line": _resolve_inbound_line(company, cd.get("source_inbound_line")),
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
                  {"header": header, "formset": formset, "inbounds": inbounds, "title": "采购发票",
                   "selected_inbound_id": request.GET.get("inbound", "")})


# --- 付款登记（自动生成银行日记账）------------------------------------------
class PaymentListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "supplier__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/供应商"
    export_filename = "付款登记"
    export_columns = [("单据编号","doc_no"),("日期","doc_date"),("银行账户","bank_account__name"),
                      ("供应商","supplier__name"),("付款金额","amount"),("已核销","settled_amount"),
                      ("未核销","unallocated"),("状态","get_status_display")]
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
class SalesInvoiceListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "invoice_no", "customer__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/发票号/客户"
    export_filename = "销售发票"
    export_columns = [("单据编号","doc_no"),("开票日期","doc_date"),("发票号码","invoice_no"),
                      ("客户","customer__name"),("不含税","amount_untaxed"),("税额","tax_amount"),
                      ("含税(应收)","amount_taxed"),("已核销","settled_amount"),("未核销","outstanding"),
                      ("状态","get_status_display")]
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


def _resolve_outbound_line(company, line_id):
    """把表单里的出库行 id 解析成 SalesOutboundLine（限本账套），无效返回 None。"""
    if not line_id:
        return None
    from apps.sales.models import SalesOutboundLine
    return SalesOutboundLine.objects.filter(pk=line_id, outbound__company=company).first()


def _outbound_prefill(company, outbound_id):
    outbound = SalesOutbound.objects.filter(company=company, pk=outbound_id).first()
    if not outbound:
        return {}, [], None
    lines = [
        {
            "product": ln.product_id,
            "description": ln.product.name,
            "quantity": ln.quantity,
            "amount_untaxed": ln.amount_untaxed,   # 直接引出库单行的售价不含税金额
            "tax_rate": ln.tax_rate,
            "source_outbound_line": ln.pk,         # 关联出库行，供成本匹配
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
                 "quantity": cd.get("quantity"), "amount_untaxed": cd["amount_untaxed"],
                 "tax_rate": cd["tax_rate"],
                 "source_outbound_line": _resolve_outbound_line(company, cd.get("source_outbound_line"))}
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
                  {"header": header, "formset": formset, "outbounds": outbounds, "title": "销售发票",
                   "selected_outbound_id": request.GET.get("outbound", "")})


@require_POST
@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
def sales_invoice_void(request, pk):
    from .services import void_sales_invoice
    company = get_active_company(request, list(get_visible_companies(request.user)))
    inv = get_object_or_404(SalesInvoice, pk=pk, company=company)
    try:
        void_sales_invoice(inv, request.user)
    except SettlementError as e:
        messages.error(request, f"作废失败：{e}")
    else:
        messages.success(request, f"已作废销售发票 {inv.doc_no}")
    return redirect("sales_invoice_detail", pk=inv.pk)


@require_POST
@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def purchase_invoice_void(request, pk):
    from .services import void_purchase_invoice_doc
    company = get_active_company(request, list(get_visible_companies(request.user)))
    inv = get_object_or_404(PurchaseInvoice, pk=pk, company=company)
    try:
        void_purchase_invoice_doc(inv, request.user)
    except SettlementError as e:
        messages.error(request, f"作废失败：{e}")
    else:
        messages.success(request, f"已作废采购发票 {inv.doc_no}")
    return redirect("purchase_invoice_detail", pk=inv.pk)


@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
def sales_invoice_edit(request, pk):
    """修改销售发票（保留单号、重设关联出库单）。未核销、本月、非期初方可改。"""
    from .services import sales_invoice_edit_block_reason, update_sales_invoice
    company = get_active_company(request, list(get_visible_companies(request.user)))
    inv = get_object_or_404(SalesInvoice, pk=pk, company=company)
    reason = sales_invoice_edit_block_reason(inv, timezone.localdate())
    if reason:
        messages.error(request, f"不可修改：{reason}")
        return redirect("sales_invoice_detail", pk=inv.pk)

    if request.method == "POST":
        header = SalesInvoiceHeaderForm(request.POST, company=company)
        formset = SalesInvoiceLineFormSet(request.POST, company=company)
        if header.is_valid() and formset.is_valid():
            lines = [
                {"product": cd.get("product"), "description": cd.get("description", ""),
                 "quantity": cd.get("quantity"), "amount_untaxed": cd["amount_untaxed"],
                 "tax_rate": cd["tax_rate"],
                 "source_outbound_line": _resolve_outbound_line(company, cd.get("source_outbound_line"))}
                for cd in formset.valid_lines
            ]
            try:
                update_sales_invoice(
                    inv, user=request.user, doc_date=header.cleaned_data["doc_date"],
                    customer=header.cleaned_data["customer"],
                    invoice_no=header.cleaned_data.get("invoice_no", ""),
                    remark=header.cleaned_data.get("remark", ""), lines=lines)
            except SettlementError as e:
                messages.error(request, f"修改失败：{e}")
            else:
                messages.success(request, f"销售发票已修改：{inv.doc_no}")
                return redirect("sales_invoice_detail", pk=inv.pk)
    else:
        outbound_id = request.GET.get("outbound")
        if outbound_id:
            # 修改时也支持「从出库单带入」：用所选出库单覆盖明细并建立关联
            h, line_init, outbound = _outbound_prefill(company, outbound_id)
            header_initial = {"doc_date": inv.doc_date, "customer": inv.customer_id,
                              "invoice_no": inv.invoice_no, "remark": inv.remark}
            if outbound:
                header_initial.update(h)
                messages.info(request, f"已从出库单 {outbound.doc_no} 带出明细，请核对后保存")
            header = SalesInvoiceHeaderForm(company=company, initial=header_initial)
            formset = SalesInvoiceLineFormSet(company=company, initial=line_init)
        else:
            header = SalesInvoiceHeaderForm(company=company, initial={
                "doc_date": inv.doc_date, "customer": inv.customer_id,
                "invoice_no": inv.invoice_no, "remark": inv.remark})
            line_init = [{
                "product": ln.product_id, "description": ln.description, "quantity": ln.quantity,
                "amount_untaxed": ln.amount_untaxed, "tax_rate": ln.tax_rate,
                "source_outbound_line": ln.source_outbound_line_id,
            } for ln in inv.lines.all()]
            formset = SalesInvoiceLineFormSet(company=company, initial=line_init)

    outbounds = SalesOutbound.objects.filter(company=company).order_by("-doc_date", "-id")[:50]
    if request.GET.get("outbound"):
        sel_ob = request.GET.get("outbound")
    else:
        linked = inv.lines.exclude(source_outbound_line=None).first()
        sel_ob = linked.source_outbound_line.outbound_id if linked else ""
    return render(request, "finance/sales_invoice_form.html",
                  {"header": header, "formset": formset, "outbounds": outbounds,
                   "title": f"修改销售发票 {inv.doc_no}", "selected_outbound_id": sel_ob})


class ReceiptListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "customer__name"]
    date_filter_field = "doc_date"
    q_placeholder = "单号/客户"
    export_filename = "收款登记"
    export_columns = [("单据编号","doc_no"),("日期","doc_date"),("银行账户","bank_account__name"),
                      ("客户","customer__name"),("收款金额","amount"),("已核销","settled_amount"),
                      ("未核销","unallocated"),("状态","get_status_display")]
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


# ============================= 报表（M2-6）====================================
def _parse_date(s):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _journal_rows(company, account, date_from=None, date_to=None, entry_type=None):
    """返回 (期初余额, 明细行[带逐笔余额], 期末余额)。

    entry_type 仅作显示过滤：逐笔余额按**全部**流水累计（保持账户真实余额），
    再隐去非该类型的行；期初/期末仍为账户真实余额。
    """
    qs = BankJournal.objects.filter(company=company, bank_account=account)
    period_opening = account.opening_balance
    if date_from:
        before = qs.filter(date__lt=date_from)
        period_opening += sum((j.signed_amount for j in before), start=Decimal("0.00"))
    period = qs
    if date_from:
        period = period.filter(date__gte=date_from)
    if date_to:
        period = period.filter(date__lte=date_to)
    period = period.order_by("date", "id")

    from apps.core.docrefs import doc_url
    rows = []
    balance = period_opening
    for j in period:
        balance += j.signed_amount
        if entry_type and j.entry_type != entry_type:
            continue  # 余额已累计，仅不显示该行
        rows.append({"j": j, "balance": balance, "ref_url": doc_url(j.source_type, j.source_id)})
    return period_opening, rows, balance


@login_required
@permission_required("finance.view_bankjournal", raise_exception=True)
def bank_accounts_report(request):
    """银行存款分户余额表（总览「银行存款」下钻第一层）：
    某公司各银行账户的 期初/本期收入/本期发出/期末，行可再点入该账户流水。"""
    from apps.opening.reports import bank_accounts_balance
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    rows = bank_accounts_balance(company, dfrom, dto) if company else []
    totals = {k: sum((r[k] for r in rows), Decimal("0.00"))
              for k in ("opening", "income", "outgo", "ending")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["银行账户", "期初余额", "本期收入", "本期发出", "期末余额"]
        data = [[str(r["account"]), r["opening"], r["income"], r["outgo"], r["ending"]] for r in rows]
        return xlsx_response("银行存款分户余额表", headers, data, company=company, period=(dfrom, dto))
    return render(request, "finance/bank_accounts_report.html", {
        "rows": rows, "totals": totals, "active_company": company,
        "date_from": dfrom, "date_to": dto,
    })


@login_required
@permission_required("finance.view_bankjournal", raise_exception=True)
def bank_journal_report(request):
    company = resolve_company(request)
    accounts = list(BankAccount.objects.filter(company=company).order_by("name"))
    account = None
    acc_id = request.GET.get("account")
    if acc_id:
        account = next((a for a in accounts if str(a.pk) == acc_id), None)
    elif accounts:
        account = accounts[0]

    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    entry_type = request.GET.get("entry_type") or ""

    opening = closing = Decimal("0.00")
    rows = []
    if account:
        opening, rows, closing = _journal_rows(company, account, date_from, date_to,
                                               entry_type or None)
    income_total = sum((r["j"].amount for r in rows if r["j"].direction == "in"), Decimal("0.00"))
    outgo_total = sum((r["j"].amount for r in rows if r["j"].direction == "out"), Decimal("0.00"))

    return render(request, "finance/bank_journal_report.html", {
        "accounts": accounts, "account": account, "rows": rows,
        "opening": opening, "closing": closing,
        "income_total": income_total, "outgo_total": outgo_total,
        "entry_type": entry_type, "entry_types": BankJournal.EntryType.choices,
        "date_from": request.GET.get("from", ""), "date_to": request.GET.get("to", ""),
        "active_company": company,
    })


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def payables_report(request):
    """应付余额表：按供应商汇总未核销的采购发票。"""
    company = resolve_company(request)
    invoices = (PurchaseInvoice.objects
                .filter(company=company, status=PurchaseInvoice.Status.REGISTERED)
                .select_related("supplier").order_by("supplier__code", "doc_date"))
    groups = {}
    for inv in invoices:
        if inv.outstanding <= 0:
            continue
        g = groups.setdefault(inv.supplier, {"partner": inv.supplier, "items": [], "total": Decimal("0.00")})
        g["items"].append(inv)
        g["total"] += inv.outstanding
    groups = sorted(groups.values(), key=lambda g: g["partner"].code)
    grand = sum((g["total"] for g in groups), start=Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        return _export_balance_report("应付账款余额表", "供应商", "应付", groups, company=company)
    return render(request, "finance/balance_report.html", {
        "title": "应付账款余额表", "kind": "应付", "groups": groups, "grand": grand,
        "active_company": company,
    })


def _export_balance_report(title, partner_label, kind, groups, company=None):
    from apps.core.exports import xlsx_response
    headers = [partner_label, "单据编号", "开票日期", "发票号码", f"未核销({kind})"]
    rows = []
    for g in groups:
        for inv in g["items"]:
            rows.append([str(g["partner"]), inv.doc_no, inv.doc_date,
                         inv.invoice_no, inv.outstanding])
        rows.append([f"{g['partner']} 小计", "", "", "", g["total"]])
    return xlsx_response(title, headers, rows, company=company)


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def receivables_report(request):
    """应收余额表：按客户汇总未核销的销售发票。"""
    company = resolve_company(request)
    invoices = (SalesInvoice.objects
                .filter(company=company, status=SalesInvoice.Status.REGISTERED)
                .select_related("customer").order_by("customer__code", "doc_date"))
    groups = {}
    for inv in invoices:
        if inv.outstanding <= 0:
            continue
        g = groups.setdefault(inv.customer, {"partner": inv.customer, "items": [], "total": Decimal("0.00")})
        g["items"].append(inv)
        g["total"] += inv.outstanding
    groups = sorted(groups.values(), key=lambda g: g["partner"].code)
    grand = sum((g["total"] for g in groups), start=Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        return _export_balance_report("应收账款余额表", "客户", "应收", groups, company=company)
    return render(request, "finance/balance_report.html", {
        "title": "应收账款余额表", "kind": "应收", "groups": groups, "grand": grand,
        "active_company": company,
    })


# ===== 往来余额表 + 往来明细账（M9-2/M9-3，总览应付/应收两级下钻）=============
def _partner_report(request, kind):
    """往来余额表（公司→各往来对象 期初/本期增/本期减/期末），可下钻明细账。"""
    from apps.opening.reports import payable_partners_balance, receivable_partners_balance
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    if kind == "payable":
        rows = payable_partners_balance(company, dfrom, dto) if company else []
        cfg = {"title": "应付账款余额表（按供应商）", "partner_label": "供应商",
               "ledger_url": "payable_partner_ledger", "inc_label": "本期新增", "dec_label": "本期核销"}
    else:
        rows = receivable_partners_balance(company, dfrom, dto) if company else []
        cfg = {"title": "应收账款余额表（按客户）", "partner_label": "客户",
               "ledger_url": "receivable_partner_ledger", "inc_label": "本期新增", "dec_label": "本期收回"}
    totals = {k: sum((r[k] for r in rows), Decimal("0.00"))
              for k in ("opening", "income", "outgo", "ending")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = [cfg["partner_label"], "期初", cfg["inc_label"], cfg["dec_label"], "期末"]
        data = [[str(r["partner"]), r["opening"], r["income"], r["outgo"], r["ending"]] for r in rows]
        return xlsx_response(cfg["title"], headers, data, company=company, period=(dfrom, dto))
    return render(request, "finance/partner_balance_report.html", {
        "rows": rows, "totals": totals, "active_company": company,
        "date_from": dfrom, "date_to": dto, "company_id": request.GET.get("company", ""),
        **cfg})


def _partner_ledger_page(request, kind):
    """往来明细账（发票增 / 核销·票据减，滚动余额）。"""
    from apps.masterdata.models import Customer, Supplier
    from apps.opening.reports import partner_ledger
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    model = Supplier if kind == "payable" else Customer
    partner = get_object_or_404(model, pk=request.GET.get("partner"), company=company)
    data = partner_ledger(company, partner, kind, dfrom, dto)
    cfg = ({"title": "供应商往来明细账", "back_url": "payable_partners_report"} if kind == "payable"
           else {"title": "客户往来明细账", "back_url": "receivable_partners_report"})
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["日期", "类型", "单号", "增加", "减少", "余额"]
        rows = [["期初余额", "", "", "", "", data["opening"]]]
        rows += [[e["date"], e["kind"], e["doc_no"], e["inc"] or "", e["dec"] or "", e["balance"]]
                 for e in data["rows"]]
        rows.append(["期末余额", "", "", data["income"], data["outgo"], data["ending"]])
        return xlsx_response(f"{cfg['title']}-{partner}", headers, rows, company=company, period=(dfrom, dto))
    return render(request, "finance/partner_ledger.html", {
        "partner": partner, "data": data, "active_company": company,
        "date_from": dfrom, "date_to": dto, "company_id": request.GET.get("company", ""),
        **cfg})


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def payable_partners_report(request):
    return _partner_report(request, "payable")


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def payable_partner_ledger(request):
    return _partner_ledger_page(request, "payable")


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def receivable_partners_report(request):
    return _partner_report(request, "receivable")


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def receivable_partner_ledger(request):
    return _partner_ledger_page(request, "receivable")


# ===== 应收票据余额表 + 票据使用明细（M9-4，总览应收票据两级下钻）==============
@login_required
@permission_required("finance.view_notereceivable", raise_exception=True)
def receivable_notes_report(request):
    """应收票据余额表：公司各票据 期初/本期出票/本期使用/期末（未用额），可下钻使用明细。"""
    from apps.opening.reports import receivable_notes_balance
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    rows = receivable_notes_balance(company, dfrom, dto) if company else []
    totals = {k: sum((r[k] for r in rows), Decimal("0.00"))
              for k in ("opening", "income", "outgo", "ending")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["票据单号", "票据号", "出票日", "期初未用", "本期出票", "本期使用", "期末未用"]
        data = [[r["note"].doc_no, r["note"].note_no, r["note"].draw_date,
                 r["opening"], r["income"], r["outgo"], r["ending"]] for r in rows]
        return xlsx_response("应收票据余额表", headers, data, company=company, period=(dfrom, dto))
    return render(request, "finance/receivable_notes_report.html", {
        "rows": rows, "totals": totals, "active_company": company,
        "date_from": dfrom, "date_to": dto, "company_id": request.GET.get("company", "")})


@login_required
@permission_required("finance.view_notereceivable", raise_exception=True)
def receivable_note_ledger(request):
    """应收票据使用明细：出票(增) / 冲应收·背书抵应付(减) 滚动未用额。"""
    from apps.opening.reports import note_ledger
    from .models import NoteReceivable
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    note = get_object_or_404(NoteReceivable, pk=request.GET.get("note"), company=company)
    data = note_ledger(company, note, dfrom, dto)
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["日期", "类型", "对应发票", "增加", "减少", "未用余额"]
        rows = [["期初未用", "", "", "", "", data["opening"]]]
        rows += [[e["date"], e["kind"], e["doc_no"], e["inc"] or "", e["dec"] or "", e["balance"]]
                 for e in data["rows"]]
        rows.append(["期末未用", "", "", data["income"], data["outgo"], data["ending"]])
        return xlsx_response(f"应收票据使用明细-{note.doc_no}", headers, rows, company=company, period=(dfrom, dto))
    return render(request, "finance/receivable_note_ledger.html", {
        "note": note, "data": data, "active_company": company,
        "date_from": dfrom, "date_to": dto, "company_id": request.GET.get("company", "")})


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def sales_revenue_cost_report(request):
    """销售收入成本计算表（按开票口径、按商品；期间可选，默认本月）。"""
    from apps.opening.reports import sales_revenue_cost
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    data = (sales_revenue_cost(company, dfrom, dto) if company
            else {"rows": [], "est_count": 0, "gap_count": 0, "gap_amount": Decimal("0.00")})
    rows = data["rows"]
    totals = {k: sum((r[k] for r in rows), Decimal("0.00")) for k in ("revenue", "cost", "profit")}
    totals["margin"] = (totals["profit"] / totals["revenue"] * 100).quantize(Decimal("0.1")) \
        if totals["revenue"] else Decimal("0.0")
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["商品编码", "商品名称", "销售数量", "销售收入(不含税)", "销售成本", "销售毛利", "毛利率%"]
        out = [[(r["product"].code if r["product"] else ""),
                (r["product"].name if r["product"] else "（未指定商品）"),
                r["qty"], r["revenue"], r["cost"], r["profit"], r["margin"]] for r in rows]
        out.append(["合计", "", "", totals["revenue"], totals["cost"], totals["profit"], totals["margin"]])
        return xlsx_response("销售收入成本计算表", headers, out, company=company, period=(dfrom, dto))
    return render(request, "finance/sales_revenue_cost.html", {
        "rows": rows, "totals": totals, "active_company": company,
        "date_from": dfrom, "date_to": dto,
        "est_count": data["est_count"],
        "gap_count": data["gap_count"], "gap_amount": data["gap_amount"]})


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def shipped_uninvoiced_report(request):
    """已出库未开具发票明细表（出库数量 − 已开票数量 ≠ 0），支持多公司联合查询。"""
    from apps.opening.reports import shipped_uninvoiced
    visible = list(get_visible_companies(request.user))
    dfrom = _parse_date(request.GET.get("from"))
    dto = _parse_date(request.GET.get("to"))
    sel_ids = request.GET.getlist("company")
    if sel_ids:
        chosen = [c for c in visible if str(c.pk) in sel_ids]
    else:
        chosen = list(visible)   # 未选则默认全部可见公司
    rows = shipped_uninvoiced(chosen, dfrom, dto)
    totals = {k: sum((r[k] for r in rows), Decimal("0.00")) for k in ("untaxed", "taxed")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["公司", "客户", "出库单号", "出库日期", "商品", "出库数量", "已开票数量",
                   "未开票数量", "未开票不含税", "未开票含税"]
        out = [[r["company"].short_name or str(r["company"]), str(r["customer"] or ""),
                r["outbound"].doc_no, r["outbound"].doc_date, str(r["product"] or ""),
                r["out_qty"], r["billed_qty"], r["remain_qty"], r["untaxed"], r["taxed"]]
               for r in rows]
        out.append(["合计", "", "", "", "", "", "", "", totals["untaxed"], totals["taxed"]])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("已出库未开具发票明细表", headers, out,
                             company=company_arg, period=(dfrom, dto) if (dfrom or dto) else None)
    return render(request, "finance/shipped_uninvoiced.html", {
        "rows": rows, "totals": totals, "visible_companies": visible,
        "chosen_ids": {c.pk for c in chosen}, "date_from": dfrom, "date_to": dto})


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def received_uninvoiced_report(request):
    """已入库未收到发票明细表（入库数量 − 已收票数量 ≠ 0），支持多公司联合查询。

    作为库存商品暂估的依据。
    """
    from apps.opening.reports import received_uninvoiced
    visible = list(get_visible_companies(request.user))
    dfrom = _parse_date(request.GET.get("from"))
    dto = _parse_date(request.GET.get("to"))
    sel_ids = request.GET.getlist("company")
    if sel_ids:
        chosen = [c for c in visible if str(c.pk) in sel_ids]
    else:
        chosen = list(visible)   # 未选则默认全部可见公司
    rows = received_uninvoiced(chosen, dfrom, dto)
    totals = {k: sum((r[k] for r in rows), Decimal("0.00")) for k in ("untaxed", "taxed")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["公司", "供应商", "入库单号", "入库日期", "商品", "入库数量", "已收票数量",
                   "未收票数量", "未收票不含税", "未收票含税"]
        out = [[r["company"].short_name or str(r["company"]), str(r["supplier"] or ""),
                r["inbound"].doc_no, r["inbound"].doc_date, str(r["product"] or ""),
                r["in_qty"], r["billed_qty"], r["remain_qty"], r["untaxed"], r["taxed"]]
               for r in rows]
        out.append(["合计", "", "", "", "", "", "", "", totals["untaxed"], totals["taxed"]])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("已入库未收到发票明细表", headers, out,
                             company=company_arg, period=(dfrom, dto) if (dfrom or dto) else None)
    return render(request, "finance/received_uninvoiced.html", {
        "rows": rows, "totals": totals, "visible_companies": visible,
        "chosen_ids": {c.pk for c in chosen}, "date_from": dfrom, "date_to": dto})


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def sales_cost_by_outbound_report(request):
    """销售收入成本计算表（按出库口径、按商品；期间可选，默认本月）。"""
    from apps.opening.reports import sales_revenue_cost_by_outbound
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    rows = sales_revenue_cost_by_outbound(company, dfrom, dto) if company else []
    totals = {k: sum((r[k] for r in rows), Decimal("0.00")) for k in ("revenue", "cost", "profit")}
    totals["margin"] = (totals["profit"] / totals["revenue"] * 100).quantize(Decimal("0.1")) \
        if totals["revenue"] else Decimal("0.0")
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["商品编码", "商品名称", "销售数量", "销售收入(不含税)", "销售成本", "销售毛利", "毛利率%"]
        out = [[(r["product"].code if r["product"] else ""),
                (r["product"].name if r["product"] else ""),
                r["qty"], r["revenue"], r["cost"], r["profit"], r["margin"]] for r in rows]
        out.append(["合计", "", "", totals["revenue"], totals["cost"], totals["profit"], totals["margin"]])
        return xlsx_response("销售收入成本计算表(按出库)", headers, out, company=company, period=(dfrom, dto))
    return render(request, "finance/sales_cost_by_outbound.html", {
        "rows": rows, "totals": totals, "active_company": company,
        "date_from": dfrom, "date_to": dto})


@login_required
@permission_required("finance.view_bankjournal", raise_exception=True)
def bank_journal_export(request):
    """导出当前账户/期间的银行存款日记账为 Excel。"""
    from django.http import HttpResponse

    from .excel import export_bank_journal

    company = get_active_company(request, list(get_visible_companies(request.user)))
    account = get_object_or_404(BankAccount, pk=request.GET.get("account"), company=company)
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    entry_type = request.GET.get("entry_type") or None
    opening, rows, closing = _journal_rows(company, account, date_from, date_to, entry_type)

    content = export_bank_journal(account, rows, opening=opening, closing=closing,
                                  date_from=date_from, date_to=date_to)
    resp = HttpResponse(
        content,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="bank_journal_{account.pk}.xlsx"'
    return resp


@login_required
@permission_required("finance.add_bankjournal", raise_exception=True)
def bank_journal_import(request):
    """从 Excel 增量导入银行流水（按 账户+日期+方向+金额+摘要 去重）。"""
    from .excel import parse_bank_journal_xlsx
    from .models import BankJournal

    company = get_active_company(request, list(get_visible_companies(request.user)))
    accounts = list(BankAccount.objects.filter(company=company).order_by("name"))

    if request.method == "POST":
        account = next((a for a in accounts if str(a.pk) == request.POST.get("account")), None)
        upload = request.FILES.get("file")
        if not account or not upload:
            messages.error(request, "请选择银行账户并上传 Excel 文件")
        else:
            try:
                parsed, errors = parse_bank_journal_xlsx(upload)
            except Exception as e:  # 解析失败（非 xlsx 等）
                messages.error(request, f"文件解析失败：{e}")
            else:
                created = skipped = 0
                # 同一批次内也去重（文件里重复的流水号/同内容只入一次）
                seen_txn, seen_content = set(), set()
                for r in parsed:
                    txn = r.get("txn_no", "")
                    if txn:
                        # 有流水号：按「账户+流水号」判重（最稳）
                        dup = (txn in seen_txn or BankJournal.objects.filter(
                            company=company, bank_account=account, txn_no=txn).exists())
                        seen_txn.add(txn)
                    else:
                        # 无流水号：退回「账户+日期+方向+金额+摘要+对方」判重
                        key = (r["date"], r["direction"], r["amount"], r["summary"], r["counterparty"])
                        dup = (key in seen_content or BankJournal.objects.filter(
                            company=company, bank_account=account, date=r["date"],
                            direction=r["direction"], amount=r["amount"],
                            summary=r["summary"], counterparty=r["counterparty"]).exists())
                        seen_content.add(key)
                    if dup:
                        skipped += 1
                        continue
                    BankJournal.objects.create(
                        company=company, created_by=request.user, bank_account=account,
                        date=r["date"], direction=r["direction"], amount=r["amount"],
                        summary=r["summary"], counterparty=r["counterparty"],
                        txn_no=txn, is_imported=True,
                    )
                    created += 1
                msg = f"导入完成：新增 {created} 条，跳过重复 {skipped} 条"
                if errors:
                    msg += f"；{len(errors)} 行有问题"
                messages.success(request, msg)
                for e in errors[:10]:
                    messages.warning(request, e)
                return redirect("bank_journal_report")

    return render(request, "finance/bank_journal_import.html", {"accounts": accounts})


@login_required
@permission_required("finance.add_bankjournal", raise_exception=True)
def bank_reconcile(request):
    """银行对账：上传网银流水与系统日记账勾对，列出 已匹配/仅系统/仅网银。"""
    from .excel import parse_bank_journal_xlsx
    from .services import reconcile_bank_journal

    company = get_active_company(request, list(get_visible_companies(request.user)))
    accounts = list(BankAccount.objects.filter(company=company).order_by("name")) if company else []
    result = None
    if request.method == "POST":
        account = next((a for a in accounts if str(a.pk) == request.POST.get("account")), None)
        upload = request.FILES.get("file")
        if not account or not upload:
            messages.error(request, "请选择银行账户并上传 Excel 文件")
        else:
            try:
                parsed, errors = parse_bank_journal_xlsx(upload)
            except Exception as e:  # noqa: BLE001
                messages.error(request, f"文件解析失败：{e}")
            else:
                result = reconcile_bank_journal(company=company, user=request.user,
                                                account=account, parsed=parsed,
                                                filename=getattr(upload, "name", ""))
                result["account"] = account
                b = result["batch"]
                messages.success(request, f"对账完成：匹配 {b.matched_count}，仅系统 "
                                          f"{b.system_only_count}，仅网银 {b.bank_only_count}")
                for e in errors[:8]:
                    messages.warning(request, e)
    return render(request, "finance/bank_reconcile.html", {
        "accounts": accounts, "result": result,
        "entry_types": [c for c in BankJournal.EntryType.choices
                        if c[0] != BankJournal.EntryType.SETTLEMENT],
    })


@login_required
@permission_required("finance.add_bankjournal", raise_exception=True)
def other_cashflow_create(request):
    """其他收支登记：手工录非往来银行收/支，生成日记账。"""
    from .forms import OtherCashflowForm
    from .services import SettlementError, create_other_cashflow

    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        form = OtherCashflowForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                j = create_other_cashflow(
                    company=company, user=request.user, doc_date=cd["doc_date"],
                    bank_account=cd["bank_account"], direction=cd["direction"],
                    amount=cd["amount"], entry_type=cd["entry_type"],
                    counterparty=cd.get("counterparty", ""), summary=cd.get("summary", ""),
                    txn_no=cd.get("txn_no", ""))
            except SettlementError as e:
                messages.error(request, str(e))
            else:
                messages.success(request, f"其他收支已登记：{j.get_entry_type_display()} {j.amount}")
                return redirect(f"{reverse_lazy('bank_journal_report')}?account={j.bank_account_id}")
    else:
        form = OtherCashflowForm(company=company, initial={"doc_date": timezone.localdate()})
    return render(request, "finance/other_cashflow_form.html", {"form": form, "title": "其他收支登记"})


@login_required
@permission_required("finance.delete_bankjournal", raise_exception=True)
def other_cashflow_delete(request, pk):
    """删除手工登记的其他收支（仅 source_type=Other）。"""
    from .services import SettlementError, delete_other_cashflow

    company = get_active_company(request, list(get_visible_companies(request.user)))
    journal = get_object_or_404(BankJournal, pk=pk, company=company)
    acc_id = journal.bank_account_id
    if request.method == "POST":
        try:
            delete_other_cashflow(journal=journal, user=request.user)
        except SettlementError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, "已删除该其他收支记录")
        return redirect(f"{reverse_lazy('bank_journal_report')}?account={acc_id}")
    return render(request, "finance/other_cashflow_confirm_delete.html",
                  {"journal": journal})


@login_required
@permission_required("finance.add_bankjournal", raise_exception=True)
def bank_journal_template(request):
    """下载银行流水导入模板（含表头与示例行）。"""
    from django.http import HttpResponse

    from .excel import build_bank_template
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp = HttpResponse(build_bank_template(), content_type=XLSX)
    resp["Content-Disposition"] = 'attachment; filename="bank_journal_template.xlsx"'
    return resp


# ============================= 票据登记（M3-1）================================
class NoteReceivableListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "note_no", "customer__name"]
    date_filter_field = "draw_date"
    q_placeholder = "单号/票号/客户"
    export_filename = "应收票据"
    export_columns = [("单据编号","doc_no"),("票据号","note_no"),("出票日","draw_date"),("到期日","due_date"),
                      ("来源客户","customer__name"),("票面","amount"),("已用","settled_amount"),
                      ("未用","unused"),("状态","get_status_display")]
    model = NoteReceivable
    template_name = "finance/note_receivable_list.html"
    context_object_name = "notes"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


class NotePayableListView(FilteredListMixin, CompanyScopedMixin, ListView):
    search_fields = ["doc_no", "note_no", "supplier__name"]
    date_filter_field = "draw_date"
    q_placeholder = "单号/票号/供应商"
    export_filename = "应付票据"
    export_columns = [("单据编号","doc_no"),("票据号","note_no"),("开票日","draw_date"),("到期日","due_date"),
                      ("收票供应商","supplier__name"),("票面","amount"),("已用","settled_amount"),
                      ("未用","unused"),("状态","get_status_display")]
    model = NotePayable
    template_name = "finance/note_payable_list.html"
    context_object_name = "notes"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")


@login_required
@permission_required("finance.add_notereceivable", raise_exception=True)
def note_receivable_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")
    if request.method == "POST":
        form = NoteReceivableForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            create_note_receivable(
                company=company, user=request.user, draw_date=cd["draw_date"],
                amount=cd["amount"], customer=cd.get("customer"), note_no=cd.get("note_no", ""),
                due_date=cd.get("due_date"), remark=cd.get("remark", ""),
            )
            messages.success(request, "应收票据已登记")
            return redirect("note_receivable_list")
    else:
        form = NoteReceivableForm(company=company, initial={"draw_date": timezone.localdate()})
    return render(request, "finance/note_form.html", {"form": form, "title": "应收票据登记"})


@login_required
@permission_required("finance.add_notepayable", raise_exception=True)
def note_payable_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")
    if request.method == "POST":
        form = NotePayableForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            create_note_payable(
                company=company, user=request.user, draw_date=cd["draw_date"],
                supplier=cd["supplier"], amount=cd["amount"], note_no=cd.get("note_no", ""),
                due_date=cd.get("due_date"), remark=cd.get("remark", ""),
            )
            messages.success(request, "应付票据已登记")
            return redirect("note_payable_list")
    else:
        form = NotePayableForm(company=company, initial={"draw_date": timezone.localdate()})
    return render(request, "finance/note_form.html", {"form": form, "title": "应付票据登记"})


# ============================= 票据冲销（M3-2）================================
def _note_settle(request, *, note, candidates, service, title, hint, redirect_to):
    """票据冲销通用视图：列出候选发票，逐张填冲销额，调用对应服务。"""
    if request.method == "POST":
        allocations = []
        for inv in candidates:
            raw = (request.POST.get(f"alloc-{inv.pk}") or "").strip()
            if raw:
                try:
                    allocations.append({"invoice": inv, "amount": Decimal(raw)})
                except (InvalidOperation, ValueError):
                    messages.error(request, f"发票 {inv.doc_no} 的冲销金额无效")
                    break
        else:
            try:
                service(note=note, allocations=allocations, user=request.user)
            except SettlementError as e:
                messages.error(request, f"冲销失败：{e}")
            else:
                messages.success(request, "票据冲销完成")
                return redirect(redirect_to)
    return render(request, "finance/note_settle.html",
                  {"note": note, "candidates": candidates, "title": title, "hint": hint})


@login_required
@permission_required("finance.add_notesettlement", raise_exception=True)
def note_receivable_settle(request, pk):
    """应收票据 → 冲应收账款（销售发票）。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    note = get_object_or_404(NoteReceivable, pk=pk, company=company)
    candidates = [i for i in SalesInvoice.objects.filter(
        company=company, status=SalesInvoice.Status.REGISTERED).order_by("doc_date", "id")
        if i.outstanding > 0]
    return _note_settle(request, note=note, candidates=candidates,
                        service=settle_receivable_against_sales,
                        title=f"应收票据冲应收 · {note.doc_no}",
                        hint="用该票据冲销下列销售发票（应收账款）。",
                        redirect_to="note_receivable_list")


@login_required
@permission_required("finance.add_notesettlement", raise_exception=True)
def note_receivable_endorse(request, pk):
    """应收票据 → 背书抵应付（采购发票）。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    note = get_object_or_404(NoteReceivable, pk=pk, company=company)
    candidates = [i for i in PurchaseInvoice.objects.filter(
        company=company, status=PurchaseInvoice.Status.REGISTERED).order_by("doc_date", "id")
        if i.outstanding > 0]
    return _note_settle(request, note=note, candidates=candidates,
                        service=endorse_receivable_against_purchase,
                        title=f"应收票据背书抵应付 · {note.doc_no}",
                        hint="把该票据背书转让给供应商，抵付下列采购发票（应付账款）。",
                        redirect_to="note_receivable_list")


@login_required
@permission_required("finance.add_notesettlement", raise_exception=True)
def note_payable_settle(request, pk):
    """应付票据 → 抵应付（采购发票）。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    note = get_object_or_404(NotePayable, pk=pk, company=company)
    candidates = [i for i in PurchaseInvoice.objects.filter(
        company=company, status=PurchaseInvoice.Status.REGISTERED).order_by("doc_date", "id")
        if i.outstanding > 0]
    return _note_settle(request, note=note, candidates=candidates,
                        service=settle_payable_against_purchase,
                        title=f"应付票据抵应付 · {note.doc_no}",
                        hint="用开出的应付票据抵减下列采购发票（应付账款）。",
                        redirect_to="note_payable_list")


# ============================= 票据 Excel / 报表（M3-3）=======================
@login_required
@permission_required("finance.view_notereceivable", raise_exception=True)
def note_receivable_export(request):
    from django.http import HttpResponse
    from .excel import export_notes
    company = get_active_company(request, list(get_visible_companies(request.user)))
    notes = list(NoteReceivable.objects.filter(company=company).select_related("customer"))
    resp = HttpResponse(export_notes(notes, "来源客户"),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = 'attachment; filename="notes_receivable.xlsx"'
    return resp


@login_required
@permission_required("finance.view_notepayable", raise_exception=True)
def note_payable_export(request):
    from django.http import HttpResponse
    from .excel import export_notes
    company = get_active_company(request, list(get_visible_companies(request.user)))
    notes = list(NotePayable.objects.filter(company=company).select_related("supplier"))
    resp = HttpResponse(export_notes(notes, "收票供应商"),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = 'attachment; filename="notes_payable.xlsx"'
    return resp


def _import_notes(request, *, kind, model, create_fn, list_url, party_label):
    """票据 Excel 导入通用：按票据号去重；对方按名称在账套内匹配。"""
    from apps.masterdata.models import Customer, Supplier
    from .excel import parse_notes_xlsx
    company = get_active_company(request, list(get_visible_companies(request.user)))

    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "请上传 Excel 文件")
        else:
            try:
                parsed, errors = parse_notes_xlsx(upload)
            except Exception as e:
                messages.error(request, f"文件解析失败：{e}")
                return render(request, "finance/note_import.html",
                              {"kind": kind, "party_label": party_label})
            created = skipped = 0
            for r in parsed:
                if r["note_no"] and model.objects.filter(
                        company=company, note_no=r["note_no"]).exists():
                    skipped += 1
                    continue
                party = None
                if r["party_name"]:
                    pm = Customer if kind == "receivable" else Supplier
                    party = pm.objects.filter(company=company, name=r["party_name"]).first()
                if kind == "payable" and party is None:
                    errors.append(f"票据 {r['note_no'] or r['draw_date']}：找不到供应商「{r['party_name']}」，已跳过")
                    continue
                kwargs = dict(company=company, user=request.user, draw_date=r["draw_date"],
                              amount=r["amount"], note_no=r["note_no"], due_date=r["due_date"])
                if kind == "receivable":
                    kwargs["customer"] = party
                else:
                    kwargs["supplier"] = party
                note = create_fn(**kwargs)
                note.is_imported = True
                note.save(update_fields=["is_imported"])
                created += 1
            messages.success(request, f"导入完成：新增 {created} 张，跳过重复 {skipped} 张")
            for e in errors[:10]:
                messages.warning(request, e)
            return redirect(list_url)

    return render(request, "finance/note_import.html", {"kind": kind, "party_label": party_label})


@login_required
@permission_required("finance.add_notereceivable", raise_exception=True)
def note_receivable_import(request):
    return _import_notes(request, kind="receivable", model=NoteReceivable,
                         create_fn=create_note_receivable, list_url="note_receivable_list",
                         party_label="来源客户")


@login_required
@permission_required("finance.add_notepayable", raise_exception=True)
def note_payable_import(request):
    return _import_notes(request, kind="payable", model=NotePayable,
                         create_fn=create_note_payable, list_url="note_payable_list",
                         party_label="收票供应商")


@login_required
@permission_required("finance.view_notereceivable", raise_exception=True)
def notes_balance_report(request):
    """票据余额表：在手/已开未用的应收、应付票据。"""
    company = resolve_company(request)
    ar = [n for n in NoteReceivable.objects.filter(company=company).select_related("customer")
          if n.unused > 0 and n.status != NoteReceivable.Status.VOID]
    ap = [n for n in NotePayable.objects.filter(company=company).select_related("supplier")
          if n.unused > 0 and n.status != NotePayable.Status.VOID]
    ar_total = sum((n.unused for n in ar), start=Decimal("0.00"))
    ap_total = sum((n.unused for n in ap), start=Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["类别", "单号", "票据号", "出票/开票日", "到期日", "往来对象", "票面", "已用", "未用"]
        rows = ([["应收票据", n.doc_no, n.note_no, n.draw_date, n.due_date,
                  str(n.customer or ""), n.amount, n.settled_amount, n.unused] for n in ar]
                + [["应付票据", n.doc_no, n.note_no, n.draw_date, n.due_date,
                    str(n.supplier or ""), n.amount, n.settled_amount, n.unused] for n in ap])
        return xlsx_response("票据余额表", headers, rows, company=company)
    return render(request, "finance/notes_balance_report.html", {
        "ar": ar, "ap": ap, "ar_total": ar_total, "ap_total": ap_total,
        "active_company": company,
    })


# ============================= 借调往来余额表（M6-2）==========================
@login_required
@permission_required("finance.view_borrowtransaction", raise_exception=True)
def borrow_report(request):
    from .models import BorrowTransaction
    company = resolve_company(request)
    agg = {}
    for t in BorrowTransaction.objects.filter(company=company):
        agg[t.counterparty] = agg.get(t.counterparty, Decimal("0.00")) + t.signed_amount
    rows = [{"counterparty": k or "（未指定）", "balance": v} for k, v in sorted(agg.items())]
    total = sum((r["balance"] for r in rows), start=Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        return xlsx_response("借调往来余额表", ["对手单位", "往来余额"],
                             [[r["counterparty"], r["balance"]] for r in rows], company=company)
    return render(request, "finance/borrow_report.html",
                  {"rows": rows, "total": total, "active_company": company})
