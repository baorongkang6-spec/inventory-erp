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


# ============================= 报表（M2-6）====================================
def _parse_date(s):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _journal_rows(company, account, date_from=None, date_to=None):
    """返回 (期初余额, 明细行[带逐笔余额], 期末余额)。"""
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

    rows = []
    balance = period_opening
    for j in period:
        balance += j.signed_amount
        rows.append({"j": j, "balance": balance})
    return period_opening, rows, balance


@login_required
@permission_required("finance.view_bankjournal", raise_exception=True)
def bank_journal_report(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    accounts = list(BankAccount.objects.filter(company=company).order_by("name"))
    account = None
    acc_id = request.GET.get("account")
    if acc_id:
        account = next((a for a in accounts if str(a.pk) == acc_id), None)
    elif accounts:
        account = accounts[0]

    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))

    opening = closing = Decimal("0.00")
    rows = []
    if account:
        opening, rows, closing = _journal_rows(company, account, date_from, date_to)

    return render(request, "finance/bank_journal_report.html", {
        "accounts": accounts, "account": account, "rows": rows,
        "opening": opening, "closing": closing,
        "date_from": request.GET.get("from", ""), "date_to": request.GET.get("to", ""),
    })


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def payables_report(request):
    """应付余额表：按供应商汇总未核销的采购发票。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
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
    return render(request, "finance/balance_report.html", {
        "title": "应付账款余额表", "kind": "应付", "groups": groups, "grand": grand,
    })


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def receivables_report(request):
    """应收余额表：按客户汇总未核销的销售发票。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
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
    return render(request, "finance/balance_report.html", {
        "title": "应收账款余额表", "kind": "应收", "groups": groups, "grand": grand,
    })


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
    _, rows, _ = _journal_rows(company, account, date_from, date_to)

    content = export_bank_journal(account, rows)
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
                for r in parsed:
                    exists = BankJournal.objects.filter(
                        company=company, bank_account=account, date=r["date"],
                        direction=r["direction"], amount=r["amount"],
                        summary=r["summary"], counterparty=r["counterparty"],
                    ).exists()
                    if exists:
                        skipped += 1
                        continue
                    BankJournal.objects.create(
                        company=company, created_by=request.user, bank_account=account,
                        date=r["date"], direction=r["direction"], amount=r["amount"],
                        summary=r["summary"], counterparty=r["counterparty"], is_imported=True,
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


# ============================= 票据登记（M3-1）================================
class NoteReceivableListView(CompanyScopedMixin, ListView):
    model = NoteReceivable
    template_name = "finance/note_receivable_list.html"
    context_object_name = "notes"

    def get_queryset(self):
        return super().get_queryset().select_related("customer")


class NotePayableListView(CompanyScopedMixin, ListView):
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
    company = get_active_company(request, list(get_visible_companies(request.user)))
    ar = [n for n in NoteReceivable.objects.filter(company=company).select_related("customer")
          if n.unused > 0 and n.status != NoteReceivable.Status.VOID]
    ap = [n for n in NotePayable.objects.filter(company=company).select_related("supplier")
          if n.unused > 0 and n.status != NotePayable.Status.VOID]
    ar_total = sum((n.unused for n in ar), start=Decimal("0.00"))
    ap_total = sum((n.unused for n in ap), start=Decimal("0.00"))
    return render(request, "finance/notes_balance_report.html", {
        "ar": ar, "ap": ap, "ar_total": ar_total, "ap_total": ap_total,
    })
