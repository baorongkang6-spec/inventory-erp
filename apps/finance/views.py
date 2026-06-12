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
    delete_payment,
    delete_receipt,
    endorse_receivable_against_purchase,
    payment_edit_block_reason,
    receipt_edit_block_reason,
    settle_payable_against_purchase,
    settle_receivable_against_sales,
    update_payment,
    update_receipt,
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


def _invoice_settlements(inv, invoice_kind):
    """汇总一张发票的核销来源（付款/收款核销 + 票据抵冲），返回 [{kind, doc_no, amount}]。"""
    from .models import NoteSettlement
    rows = []
    if invoice_kind == NoteSettlement.InvoiceKind.PURCHASE:
        for a in inv.allocations.select_related("payment").all():
            rows.append({"kind": "付款核销", "doc_no": a.payment.doc_no, "amount": a.amount})
    else:
        for a in inv.allocations.select_related("receipt").all():
            rows.append({"kind": "收款核销", "doc_no": a.receipt.doc_no, "amount": a.amount})
    for s in NoteSettlement.objects.filter(
            company=inv.company, invoice_kind=invoice_kind, invoice_id=inv.pk):
        if s.note_kind == NoteSettlement.NoteKind.RECEIVABLE:
            kind = "应收票据背书抵付" if s.is_endorsement else "应收票据冲销"
        else:
            kind = "应付票据抵付"
        rows.append({"kind": kind, "doc_no": s.note_no, "amount": s.amount})
    return rows


class PurchaseInvoiceDetailView(CompanyScopedMixin, DetailView):
    model = PurchaseInvoice
    template_name = "finance/purchase_invoice_detail.html"
    context_object_name = "inv"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier")

    def get_context_data(self, **kwargs):
        from .models import NoteSettlement
        ctx = super().get_context_data(**kwargs)
        ctx["settlements"] = _invoice_settlements(
            self.object, NoteSettlement.InvoiceKind.PURCHASE)
        return ctx


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
                    "tax_amount": cd.get("tax_amount"),
                    "amount_taxed": cd.get("amount_taxed"),
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
                term_days=header.cleaned_data.get("term_days") or 0,
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
                  {"header": header, "formset": formset, "inbounds": inbounds, "title": "登记采购发票",
                   "selected_inbound_id": request.GET.get("inbound", "")})


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def purchase_invoice_edit(request, pk):
    """修改采购发票（保留单号、可重设关联入库单）。未核销、本月、非期初方可改。"""
    from .services import purchase_invoice_edit_block_reason, update_purchase_invoice
    company = get_active_company(request, list(get_visible_companies(request.user)))
    inv = get_object_or_404(PurchaseInvoice, pk=pk, company=company)
    reason = purchase_invoice_edit_block_reason(inv, timezone.localdate())
    if reason:
        messages.error(request, f"不可修改：{reason}")
        return redirect("purchase_invoice_detail", pk=inv.pk)

    if request.method == "POST":
        header = PurchaseInvoiceHeaderForm(request.POST, company=company)
        formset = PurchaseInvoiceLineFormSet(request.POST, company=company)
        if header.is_valid() and formset.is_valid():
            lines = [
                {"product": cd.get("product"), "description": cd.get("description", ""),
                 "quantity": cd.get("quantity"), "amount_untaxed": cd["amount_untaxed"],
                 "tax_rate": cd["tax_rate"],
                 "tax_amount": cd.get("tax_amount"), "amount_taxed": cd.get("amount_taxed"),
                 "source_inbound_line": _resolve_inbound_line(company, cd.get("source_inbound_line"))}
                for cd in formset.valid_lines
            ]
            try:
                update_purchase_invoice(
                    inv, user=request.user, doc_date=header.cleaned_data["doc_date"],
                    supplier=header.cleaned_data["supplier"],
                    invoice_no=header.cleaned_data.get("invoice_no", ""),
                    remark=header.cleaned_data.get("remark", ""),
                    term_days=header.cleaned_data.get("term_days") or 0, lines=lines)
            except SettlementError as e:
                messages.error(request, f"修改失败：{e}")
            else:
                messages.success(request, f"采购发票已修改：{inv.doc_no}")
                return redirect("purchase_invoice_detail", pk=inv.pk)
    else:
        inbound_id = request.GET.get("inbound")
        if inbound_id:
            # 修改时也支持「从入库单带入」：用所选入库单覆盖明细并建立关联
            h, line_init, inbound = _inbound_prefill(company, inbound_id)
            header_initial = {"doc_date": inv.doc_date, "supplier": inv.supplier_id,
                              "invoice_no": inv.invoice_no, "remark": inv.remark,
                              "term_days": inv.term_days}
            if inbound:
                header_initial.update(h)
                messages.info(request, f"已从入库单 {inbound.doc_no} 带出明细，请核对后保存")
            header = PurchaseInvoiceHeaderForm(company=company, initial=header_initial)
            formset = PurchaseInvoiceLineFormSet(company=company, initial=line_init)
        else:
            header = PurchaseInvoiceHeaderForm(company=company, initial={
                "doc_date": inv.doc_date, "supplier": inv.supplier_id,
                "invoice_no": inv.invoice_no, "remark": inv.remark,
                "term_days": inv.term_days})
            line_init = [{
                "product": ln.product_id, "description": ln.description, "quantity": ln.quantity,
                "amount_untaxed": ln.amount_untaxed, "tax_rate": ln.tax_rate,
                "source_inbound_line": ln.source_inbound_line_id,
            } for ln in inv.lines.all()]
            formset = PurchaseInvoiceLineFormSet(company=company, initial=line_init)

    inbounds = PurchaseInbound.objects.filter(company=company).order_by("-doc_date", "-id")[:50]
    if request.GET.get("inbound"):
        sel_ib = request.GET.get("inbound")
    else:
        linked = inv.lines.exclude(source_inbound_line=None).first()
        sel_ib = linked.source_inbound_line.inbound_id if linked else ""
    return render(request, "finance/purchase_invoice_form.html",
                  {"header": header, "formset": formset, "inbounds": inbounds,
                   "title": f"修改采购发票 {inv.doc_no}", "selected_inbound_id": sel_ib})


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
        return super().get_queryset().select_related("supplier", "bank_account", "bank_journal")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        for p in ctx.get(self.context_object_name, []):
            p.can_edit = payment_edit_block_reason(p, today) is None
        return ctx


class PaymentDetailView(CompanyScopedMixin, DetailView):
    model = Payment
    template_name = "finance/payment_detail.html"
    context_object_name = "pay"

    def get_queryset(self):
        return super().get_queryset().select_related("supplier", "bank_account", "bank_journal")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_edit"] = payment_edit_block_reason(self.object, timezone.localdate()) is None
        return ctx


def _collect_allocations(request, candidates):
    """从 POST 收集逐张发票的冲销额：alloc-<pk>。返回 (allocations, error_msg)。"""
    allocations = []
    for inv in candidates:
        raw = (request.POST.get(f"alloc-{inv.pk}") or "").strip()
        if not raw:
            continue
        try:
            amt = Decimal(raw)
        except (InvalidOperation, ValueError):
            return None, f"发票 {inv.doc_no} 的冲销金额无效"
        if amt:
            allocations.append({"invoice": inv, "amount": amt})
    return allocations, None


@login_required
def note_receivable_lookup(request):
    """付款背书：按票据号码查在手应收票据，带出出票/到期/可用余额。返回 JSON。"""
    from django.http import JsonResponse
    company = get_active_company(request, list(get_visible_companies(request.user)))
    note_no = (request.GET.get("note_no") or "").strip()
    if company is None or not note_no:
        return JsonResponse({"found": False})
    note = (NoteReceivable.objects.filter(
        company=company, note_no=note_no, status=NoteReceivable.Status.ON_HAND)
        .order_by("-draw_date", "-id").first())
    if note is None or note.unused <= 0:
        return JsonResponse({"found": False})
    return JsonResponse({
        "found": True, "doc_no": note.doc_no,
        "draw_date": note.draw_date.strftime("%Y-%m-%d") if note.draw_date else "",
        "due_date": note.due_date.strftime("%Y-%m-%d") if note.due_date else "",
        "amount": str(note.amount), "unused": str(note.unused),
        "customer": str(note.customer) if note.customer_id else "",
    })


@login_required
@permission_required("finance.add_payment", raise_exception=True)
def payment_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    # 背书可冲抵的采购发票（应付未结）
    inv_candidates = [i for i in PurchaseInvoice.objects.filter(
        company=company, status=PurchaseInvoice.Status.REGISTERED)
        .select_related("supplier").order_by("doc_date", "id") if i.outstanding > 0]

    if request.method == "POST":
        form = PaymentForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            if cd["method"] == PaymentForm.METHOD_NOTE:
                ok = _handle_payment_by_note(request, company, cd, inv_candidates)
                if ok:
                    return ok
            else:
                pay = create_payment(
                    company=company, user=request.user, doc_date=cd["doc_date"],
                    bank_account=cd["bank_account"], supplier=cd["supplier"],
                    amount=cd["amount"], summary=cd.get("summary", ""),
                )
                messages.success(request, f"付款已登记，并生成银行日记账：{pay.doc_no}")
                return redirect("payment_detail", pk=pay.pk)
    else:
        form = PaymentForm(company=company, initial={"doc_date": timezone.localdate()})

    return render(request, "finance/payment_form.html",
                  {"form": form, "title": "付款登记", "inv_candidates": inv_candidates})


def _resolve_endorsement(request, company, cd, inv_candidates):
    """校验背书：找在手票据、金额≤可用余额、勾选采购发票且合计=金额。

    成功返回 (note, allocations, amount)；失败已 messages.error 并返回 (None, None, None)。
    """
    from apps.core.money import round_money
    note = (NoteReceivable.objects.filter(
        company=company, note_no=cd["note_no"], status=NoteReceivable.Status.ON_HAND)
        .order_by("-draw_date", "-id").first())
    if note is None or note.unused <= 0:
        messages.error(request, f"未找到可用的在手应收票据：{cd['note_no']}")
        return None, None, None
    amount = round_money(cd["amount"])
    if amount > note.unused:
        messages.error(request, f"付款金额 {amount} 超过票据可用余额 {note.unused}")
        return None, None, None
    allocations, err = _collect_allocations(request, inv_candidates)
    if err:
        messages.error(request, err)
        return None, None, None
    if not allocations:
        messages.error(request, "背书付款需勾选至少一张要冲抵的采购发票")
        return None, None, None
    total = round_money(sum(a["amount"] for a in allocations))
    if total != amount:
        messages.error(request, f"勾选发票冲销合计 {total} 应等于付款金额 {amount}")
        return None, None, None
    return note, allocations, amount


def _handle_payment_by_note(request, company, cd, inv_candidates):
    """付款方式=应收票据(背书)：找在手票据→按勾选采购发票背书抵应付。成功返回 redirect。"""
    note, allocations, amount = _resolve_endorsement(request, company, cd, inv_candidates)
    if note is None:
        return None
    try:
        endorse_receivable_against_purchase(note=note, allocations=allocations, user=request.user)
    except SettlementError as e:
        messages.error(request, f"背书失败：{e}")
        return None
    messages.success(request, f"已用应收票据 {note.doc_no} 背书付款 {amount}，抵付 {len(allocations)} 张采购发票")
    return redirect("note_receivable_list")


@login_required
@permission_required("finance.add_payment", raise_exception=True)
def payment_edit(request, pk):
    """修改付款（仅当月、未核销/未对账）。

    可改银行付款字段并同步银行日记账；也可把付款方式切换为「应收票据(背书)」——
    此时自动删除原银行付款及其日记账，改记为票据背书抵应付。
    """
    from django.db import transaction
    company = get_active_company(request, list(get_visible_companies(request.user)))
    pay = get_object_or_404(Payment, pk=pk, company=company)
    reason = payment_edit_block_reason(pay, timezone.localdate())
    if reason:
        messages.error(request, f"不可修改：{reason}")
        return redirect("payment_detail", pk=pay.pk)

    inv_candidates = [i for i in PurchaseInvoice.objects.filter(
        company=company, status=PurchaseInvoice.Status.REGISTERED)
        .select_related("supplier").order_by("doc_date", "id") if i.outstanding > 0]

    if request.method == "POST":
        form = PaymentForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            if cd["method"] == PaymentForm.METHOD_NOTE:
                # 切换为票据背书：先校验，再原子地「删旧银行付款 + 建背书」
                note, allocations, amount = _resolve_endorsement(
                    request, company, cd, inv_candidates)
                if note is not None:
                    try:
                        with transaction.atomic():
                            old_no = pay.doc_no
                            delete_payment(pay, user=request.user)
                            endorse_receivable_against_purchase(
                                note=note, allocations=allocations, user=request.user)
                    except (SettlementError, ValueError) as e:
                        messages.error(request, f"转为票据背书失败：{e}")
                    else:
                        messages.success(
                            request,
                            f"原银行付款 {old_no} 已删除，改为应收票据 {note.doc_no} "
                            f"背书付款 {amount}，抵付 {len(allocations)} 张采购发票")
                        return redirect("note_receivable_list")
            else:
                try:
                    update_payment(pay, user=request.user, doc_date=cd["doc_date"],
                                   bank_account=cd["bank_account"], supplier=cd["supplier"],
                                   amount=cd["amount"], summary=cd.get("summary", ""))
                except (SettlementError, ValueError) as e:
                    messages.error(request, f"修改失败：{e}")
                else:
                    messages.success(request, f"付款已修改：{pay.doc_no}")
                    return redirect("payment_detail", pk=pay.pk)
    else:
        form = PaymentForm(company=company, initial={
            "doc_date": pay.doc_date, "method": f"bank:{pay.bank_account_id}",
            "supplier": pay.supplier_id, "amount": pay.amount, "summary": pay.summary})

    return render(request, "finance/payment_form.html",
                  {"form": form, "title": f"修改付款 {pay.doc_no}",
                   "inv_candidates": inv_candidates})


@login_required
@permission_required("finance.add_payment", raise_exception=True)
@require_POST
def payment_delete(request, pk):
    """删除付款（仅银行方式、当月、未核销/未对账）。连同银行日记账删除。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    pay = get_object_or_404(Payment, pk=pk, company=company)
    reason = payment_edit_block_reason(pay, timezone.localdate())
    if reason:
        messages.error(request, f"不可删除：{reason}")
        return redirect("payment_detail", pk=pay.pk)
    doc_no = pay.doc_no
    try:
        delete_payment(pay, user=request.user)
    except SettlementError as e:
        messages.error(request, f"删除失败：{e}")
        return redirect("payment_detail", pk=pk)
    messages.success(request, f"付款已删除：{doc_no}")
    return redirect("payment_list")


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

    def get_context_data(self, **kwargs):
        from .models import NoteSettlement
        ctx = super().get_context_data(**kwargs)
        ctx["settlements"] = _invoice_settlements(
            self.object, NoteSettlement.InvoiceKind.SALES)
        return ctx


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
                 "tax_amount": cd.get("tax_amount"), "amount_taxed": cd.get("amount_taxed"),
                 "source_outbound_line": _resolve_outbound_line(company, cd.get("source_outbound_line"))}
                for cd in formset.valid_lines
            ]
            inv = create_sales_invoice(
                company=company, user=request.user,
                doc_date=header.cleaned_data["doc_date"],
                customer=header.cleaned_data["customer"],
                invoice_no=header.cleaned_data.get("invoice_no", ""),
                remark=header.cleaned_data.get("remark", ""),
                term_days=header.cleaned_data.get("term_days") or 0, lines=lines,
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


@require_POST
@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
def sales_invoice_delete(request, pk):
    """删除销售发票（彻底移除）：未核销、非期初才可删。"""
    from .services import delete_sales_invoice
    company = get_active_company(request, list(get_visible_companies(request.user)))
    inv = get_object_or_404(SalesInvoice, pk=pk, company=company)
    doc_no = inv.doc_no
    try:
        delete_sales_invoice(inv, user=request.user)
    except SettlementError as e:
        messages.error(request, f"删除失败：{e}")
        return redirect("sales_invoice_detail", pk=pk)
    messages.success(request, f"销售发票已删除：{doc_no}")
    return redirect("sales_invoice_list")


@require_POST
@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def purchase_invoice_delete(request, pk):
    """删除采购发票（彻底移除）：未核销、非期初才可删。"""
    from .services import delete_purchase_invoice
    company = get_active_company(request, list(get_visible_companies(request.user)))
    inv = get_object_or_404(PurchaseInvoice, pk=pk, company=company)
    doc_no = inv.doc_no
    try:
        delete_purchase_invoice(inv, user=request.user)
    except SettlementError as e:
        messages.error(request, f"删除失败：{e}")
        return redirect("purchase_invoice_detail", pk=pk)
    messages.success(request, f"采购发票已删除：{doc_no}")
    return redirect("purchase_invoice_list")


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
                 "tax_amount": cd.get("tax_amount"), "amount_taxed": cd.get("amount_taxed"),
                 "source_outbound_line": _resolve_outbound_line(company, cd.get("source_outbound_line"))}
                for cd in formset.valid_lines
            ]
            try:
                update_sales_invoice(
                    inv, user=request.user, doc_date=header.cleaned_data["doc_date"],
                    customer=header.cleaned_data["customer"],
                    invoice_no=header.cleaned_data.get("invoice_no", ""),
                    remark=header.cleaned_data.get("remark", ""),
                    term_days=header.cleaned_data.get("term_days") or 0, lines=lines)
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
                              "invoice_no": inv.invoice_no, "remark": inv.remark,
                              "term_days": inv.term_days}
            if outbound:
                header_initial.update(h)
                messages.info(request, f"已从出库单 {outbound.doc_no} 带出明细，请核对后保存")
            header = SalesInvoiceHeaderForm(company=company, initial=header_initial)
            formset = SalesInvoiceLineFormSet(company=company, initial=line_init)
        else:
            header = SalesInvoiceHeaderForm(company=company, initial={
                "doc_date": inv.doc_date, "customer": inv.customer_id,
                "invoice_no": inv.invoice_no, "remark": inv.remark,
                "term_days": inv.term_days})
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
        return super().get_queryset().select_related("customer", "bank_account", "bank_journal")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        for r in ctx.get(self.context_object_name, []):
            r.can_edit = receipt_edit_block_reason(r, today) is None
        return ctx


class ReceiptDetailView(CompanyScopedMixin, DetailView):
    model = Receipt
    template_name = "finance/receipt_detail.html"
    context_object_name = "rec"

    def get_queryset(self):
        return super().get_queryset().select_related("customer", "bank_account", "bank_journal")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_edit"] = receipt_edit_block_reason(self.object, timezone.localdate()) is None
        return ctx


@login_required
@permission_required("finance.add_receipt", raise_exception=True)
def receipt_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    # 收票可冲抵的销售发票（应收未结）
    inv_candidates = [i for i in SalesInvoice.objects.filter(
        company=company, status=SalesInvoice.Status.REGISTERED)
        .select_related("customer").order_by("doc_date", "id") if i.outstanding > 0]

    if request.method == "POST":
        form = ReceiptForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            if cd["method"] == ReceiptForm.METHOD_NOTE:
                ok = _handle_receipt_by_note(request, company, cd, inv_candidates)
                if ok:
                    return ok
            else:
                rec = create_receipt(
                    company=company, user=request.user, doc_date=cd["doc_date"],
                    bank_account=cd["bank_account"], customer=cd["customer"],
                    amount=cd["amount"], summary=cd.get("summary", ""),
                )
                messages.success(request, f"收款已登记，并生成银行日记账：{rec.doc_no}")
                return redirect("receipt_detail", pk=rec.pk)
    else:
        form = ReceiptForm(company=company, initial={"doc_date": timezone.localdate()})

    return render(request, "finance/receipt_form.html",
                  {"form": form, "title": "收款登记", "inv_candidates": inv_candidates})


def _resolve_note_receipt(request, company, cd, inv_candidates):
    """收票校验：勾选销售发票合计≤票面。返回 (amount, allocations) 或 (None, None)。"""
    from apps.core.money import round_money
    amount = round_money(cd["amount"])
    allocations, err = _collect_allocations(request, inv_candidates)
    if err:
        messages.error(request, err)
        return None, None
    total = round_money(sum(a["amount"] for a in allocations)) if allocations else round_money(0)
    if total > amount:
        messages.error(request, f"勾选发票冲销合计 {total} 不能超过票面金额 {amount}")
        return None, None
    return amount, allocations


def _create_note_receipt(request, company, cd, amount, allocations):
    """建在手应收票据，并按勾选销售发票冲应收。返回 note；异常由调用方处理。"""
    note = create_note_receivable(
        company=company, user=request.user, draw_date=cd["draw_date"],
        amount=amount, customer=cd["customer"], note_no=cd["note_no"],
        due_date=cd["due_date"], remark=cd.get("summary", ""))
    if allocations:
        settle_receivable_against_sales(note=note, allocations=allocations, user=request.user)
    return note


def _handle_receipt_by_note(request, company, cd, inv_candidates):
    """收款方式=应收票据：建一张在手应收票据，并按勾选销售发票冲应收。成功返回 redirect。"""
    amount, allocations = _resolve_note_receipt(request, company, cd, inv_candidates)
    if amount is None:
        return None
    try:
        note = _create_note_receipt(request, company, cd, amount, allocations)
    except (SettlementError, ValueError) as e:
        messages.error(request, f"收票失败：{e}")
        return None
    tail = f"，并冲抵 {len(allocations)} 张销售发票" if allocations else "（在手，未冲销）"
    messages.success(request, f"已收到应收票据 {note.doc_no} 票面 {amount}{tail}")
    return redirect("note_receivable_list")


@login_required
@permission_required("finance.add_receipt", raise_exception=True)
def receipt_edit(request, pk):
    """修改收款（仅当月、未核销/未对账）。

    可改银行收款字段并同步银行日记账；也可把收款方式切换为「应收票据」——
    此时自动删除原银行收款及其日记账，改记为收到一张应收票据（可顺带冲销售发票）。
    """
    from django.db import transaction
    company = get_active_company(request, list(get_visible_companies(request.user)))
    rec = get_object_or_404(Receipt, pk=pk, company=company)
    reason = receipt_edit_block_reason(rec, timezone.localdate())
    if reason:
        messages.error(request, f"不可修改：{reason}")
        return redirect("receipt_detail", pk=rec.pk)

    inv_candidates = [i for i in SalesInvoice.objects.filter(
        company=company, status=SalesInvoice.Status.REGISTERED)
        .select_related("customer").order_by("doc_date", "id") if i.outstanding > 0]

    if request.method == "POST":
        form = ReceiptForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            if cd["method"] == ReceiptForm.METHOD_NOTE:
                # 切换为应收票据：先校验，再原子地「删旧银行收款 + 建票据」
                amount, allocations = _resolve_note_receipt(
                    request, company, cd, inv_candidates)
                if amount is not None:
                    try:
                        with transaction.atomic():
                            old_no = rec.doc_no
                            delete_receipt(rec, user=request.user)
                            note = _create_note_receipt(
                                request, company, cd, amount, allocations)
                    except (SettlementError, ValueError) as e:
                        messages.error(request, f"转为应收票据失败：{e}")
                    else:
                        tail = (f"，冲抵 {len(allocations)} 张销售发票"
                                if allocations else "（在手）")
                        messages.success(
                            request,
                            f"原银行收款 {old_no} 已删除，改记为应收票据 {note.doc_no} "
                            f"票面 {amount}{tail}")
                        return redirect("note_receivable_list")
            else:
                try:
                    update_receipt(rec, user=request.user, doc_date=cd["doc_date"],
                                   bank_account=cd["bank_account"], customer=cd["customer"],
                                   amount=cd["amount"], summary=cd.get("summary", ""))
                except (SettlementError, ValueError) as e:
                    messages.error(request, f"修改失败：{e}")
                else:
                    messages.success(request, f"收款已修改：{rec.doc_no}")
                    return redirect("receipt_detail", pk=rec.pk)
    else:
        form = ReceiptForm(company=company, initial={
            "doc_date": rec.doc_date, "method": f"bank:{rec.bank_account_id}",
            "customer": rec.customer_id, "amount": rec.amount, "summary": rec.summary})

    return render(request, "finance/receipt_form.html",
                  {"form": form, "title": f"修改收款 {rec.doc_no}",
                   "inv_candidates": inv_candidates})


@login_required
@permission_required("finance.add_receipt", raise_exception=True)
@require_POST
def receipt_delete(request, pk):
    """删除收款（仅银行方式、当月、未核销/未对账）。连同银行日记账删除。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    rec = get_object_or_404(Receipt, pk=pk, company=company)
    reason = receipt_edit_block_reason(rec, timezone.localdate())
    if reason:
        messages.error(request, f"不可删除：{reason}")
        return redirect("receipt_detail", pk=rec.pk)
    doc_no = rec.doc_no
    try:
        delete_receipt(rec, user=request.user)
    except SettlementError as e:
        messages.error(request, f"删除失败：{e}")
        return redirect("receipt_detail", pk=pk)
    messages.success(request, f"收款已删除：{doc_no}")
    return redirect("receipt_list")


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


def _company_scope(request):
    """多公司报表统一作用域：返回 (visible, chosen)。

    chosen = getlist('company') 命中的可见公司；未选=全部可见公司。
    """
    visible = list(get_visible_companies(request.user))
    sel = request.GET.getlist("company")
    chosen = [c for c in visible if str(c.pk) in sel] if sel else list(visible)
    return visible, chosen


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


def _journal_rows_multi(accounts, date_from=None, date_to=None, entry_type=None):
    """多账户合并日记账：每个账户内部按时间累计逐笔余额，再合并。

    返回 (期初合计, 行[每行含 account 与该账户逐笔余额], 期末合计)。
    行按 公司/账户/日期 排序——逐笔「余额」是各账户自身的滚动余额（跨账户无统一余额）。
    """
    total_opening = Decimal("0.00")
    total_closing = Decimal("0.00")
    all_rows = []
    for acc in accounts:
        op, rows, cl = _journal_rows(acc.company, acc, date_from, date_to, entry_type)
        total_opening += op
        total_closing += cl
        for r in rows:
            r["account"] = acc
            all_rows.append(r)
    all_rows.sort(key=lambda r: (r["account"].company.code, r["account"].name,
                                 r["j"].date, r["j"].id))
    return total_opening, all_rows, total_closing


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
    """银行存款日记账：支持多公司联合查询；银行账户不选=全部账户。

    选定单一账户时为经典存折式（逐笔滚动余额）；多账户时各账户内部滚动、
    合并按 公司/账户/日期 排序，期初/期末为各账户合计。
    """
    visible = list(get_visible_companies(request.user))
    sel_ids = request.GET.getlist("company")
    if sel_ids:
        chosen = [c for c in visible if str(c.pk) in sel_ids]
    else:
        chosen = list(visible)   # 未选=全部可见公司

    accounts = list(BankAccount.objects.filter(company__in=chosen)
                    .select_related("company").order_by("company__code", "name"))
    account = None
    acc_id = request.GET.get("account")
    if acc_id:
        account = next((a for a in accounts if str(a.pk) == acc_id), None)

    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    entry_type = request.GET.get("entry_type") or ""

    target_accounts = [account] if account else accounts
    opening, rows, closing = _journal_rows_multi(
        target_accounts, date_from, date_to, entry_type or None)
    income_total = sum((r["j"].amount for r in rows if r["j"].direction == "in"), Decimal("0.00"))
    outgo_total = sum((r["j"].amount for r in rows if r["j"].direction == "out"), Decimal("0.00"))

    return render(request, "finance/bank_journal_report.html", {
        "accounts": accounts, "account": account, "rows": rows,
        "opening": opening, "closing": closing,
        "income_total": income_total, "outgo_total": outgo_total,
        "entry_type": entry_type, "entry_types": BankJournal.EntryType.choices,
        "date_from": request.GET.get("from", ""), "date_to": request.GET.get("to", ""),
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "multi": account is None, "today": timezone.localdate(),
    })


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def payables_report(request):
    """应付账款余额表：按公司·供应商列 期初/本期增加/本期减少/期末，点供应商进明细账。"""
    return _outstanding_balance_report(
        request, kind="payable", title="应付账款余额表", partner_label="供应商",
        ledger_url="payable_partner_ledger", inc_label="本期增加", dec_label="本期减少")


def _outstanding_balance_report(request, *, kind, title, partner_label, ledger_url,
                                inc_label, dec_label):
    """应付/应收余额表（多公司联合）：按 公司·往来对象 列 期初/本期增/本期减/期末。

    公司不选=全部可见公司；日期区间默认月初~今天；点往来对象进该公司该对象明细账。
    """
    from apps.opening.reports import (invoice_aging, payable_partners_balance,
                                      receivable_partners_balance)
    visible, chosen = _company_scope(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    balance_fn = payable_partners_balance if kind == "payable" else receivable_partners_balance
    rows = []
    for company in chosen:
        for r in balance_fn(company, dfrom, dto):
            rows.append({**r, "company": company})
    rows.sort(key=lambda r: (r["company"].code, getattr(r["partner"], "code", "")))
    # 账龄/逾期（按查询期末日 dto）
    model = PurchaseInvoice if kind == "payable" else SalesInvoice
    pattr = "supplier" if kind == "payable" else "customer"
    aging = invoice_aging(model, pattr, chosen, dto)
    Z = Decimal("0.00")
    for r in rows:
        a = aging.get((r["company"].pk, r["partner"].pk), {})
        for k in ("overdue", "b1", "b2", "b3", "b4"):
            r[k] = a.get(k, Z)
    keys = ("opening", "income", "outgo", "ending", "overdue", "b1", "b2", "b3", "b4")
    totals = {k: sum((r[k] for r in rows), Z) for k in keys}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["公司", partner_label, "期初余额", inc_label, dec_label, "期末余额",
                   "逾期金额", "3个月以内", "3-6个月", "6个月-1年", "1年以上"]
        data = [[r["company"].short_name or str(r["company"]), str(r["partner"]),
                 r["opening"], r["income"], r["outgo"], r["ending"],
                 r["overdue"], r["b1"], r["b2"], r["b3"], r["b4"]] for r in rows]
        data.append(["合计", "", totals["opening"], totals["income"], totals["outgo"], totals["ending"],
                     totals["overdue"], totals["b1"], totals["b2"], totals["b3"], totals["b4"]])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response(title, headers, data, company=company_arg, period=(dfrom, dto))
    return render(request, "finance/partner_balance_multi.html", {
        "title": title, "partner_label": partner_label, "ledger_url": ledger_url,
        "inc_label": inc_label, "dec_label": dec_label, "rows": rows, "totals": totals,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "date_from": dfrom, "date_to": dto,
    })


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def receivables_report(request):
    """应收账款余额表：按公司·客户列 期初/本期增加/本期减少/期末，点客户进明细账。"""
    return _outstanding_balance_report(
        request, kind="receivable", title="应收账款余额表", partner_label="客户",
        ledger_url="receivable_partner_ledger", inc_label="本期增加", dec_label="本期减少")


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def invoice_quota_report(request):
    """剩余发票额度查询：每家公司 本年/本月开票(不含税) + 额度 + 剩余可开。"""
    from apps.opening.reports import invoice_quota_usage
    visible, chosen = _company_scope(request)
    asof = _parse_date(request.GET.get("to")) or timezone.localdate()
    rows = invoice_quota_usage(chosen, asof)
    totals = {k: sum((r[k] for r in rows), Decimal("0.00"))
              for k in ("year_amt", "month_amt", "quota", "remain")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["公司名称", "本年开票金额", "本月开票金额", "开票额度", "剩余可开发票额度"]
        data = [[r["company"].header_name, r["year_amt"], r["month_amt"], r["quota"], r["remain"]]
                for r in rows]
        data.append(["合计", totals["year_amt"], totals["month_amt"], totals["quota"], totals["remain"]])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("剩余发票额度查询", headers, data, company=company_arg, period=(None, asof))
    return render(request, "finance/invoice_quota_report.html", {
        "rows": rows, "totals": totals, "asof": asof,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen}})


@login_required
def overdue_report(request):
    """逾期明细表：逐张逾期发票（应收/应付），多公司联合，按期末日判定逾期天数。"""
    from apps.opening.reports import overdue_invoice_list
    visible, chosen = _company_scope(request)
    asof = _parse_date(request.GET.get("to")) or timezone.localdate()
    kind = request.GET.get("kind") or "all"   # ar / ap / all
    can_ar = request.user.has_perm("finance.view_salesinvoice")
    can_ap = request.user.has_perm("finance.view_purchaseinvoice")
    rows = []
    if kind in ("ar", "all") and can_ar:
        for r in overdue_invoice_list(SalesInvoice, "customer", chosen, asof):
            rows.append({**r, "kind": "应收", "kind_code": "ar"})
    if kind in ("ap", "all") and can_ap:
        for r in overdue_invoice_list(PurchaseInvoice, "supplier", chosen, asof):
            rows.append({**r, "kind": "应付", "kind_code": "ap"})
    rows.sort(key=lambda r: (-r["overdue_days"], r["company"].code))
    total = sum((r["outstanding"] for r in rows), Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["公司", "类型", "往来对象", "单据号", "发票号", "开票日期", "账期(天)",
                   "到期日", "逾期天数", "逾期金额(未核销)", "账龄段"]
        data = [[r["company"].short_name or str(r["company"]), r["kind"], str(r["partner"] or ""),
                 r["doc_no"], r["invoice_no"], r["doc_date"], r["term_days"], r["due_date"],
                 r["overdue_days"], r["outstanding"], r["bucket"]] for r in rows]
        data.append(["合计", "", "", "", "", "", "", "", "", total, ""])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("逾期明细表", headers, data, company=company_arg,
                             period=(None, asof))
    return render(request, "finance/overdue_report.html", {
        "rows": rows, "total": total, "asof": asof, "kind": kind,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "can_ar": can_ar, "can_ap": can_ap})


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
    # src=menu → 从「报表」菜单的应付/应收余额表进入，返回到对应菜单报表；
    # 默认从总览下钻进入，返回到 M9 按往来对象的余额表。
    menu = request.GET.get("src") == "menu"
    if kind == "payable":
        cfg = {"title": "供应商往来明细账",
               "back_url": "payables_report" if menu else "payable_partners_report"}
    else:
        cfg = {"title": "客户往来明细账",
               "back_url": "receivables_report" if menu else "receivable_partners_report"}
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
        "src": "menu" if menu else "", **cfg})


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
    from django.db.models import Sum

    from apps.core.money import round_money
    from apps.opening.reports import sales_revenue_cost_by_outbound
    from .models import ExpenseRecord
    company = resolve_company(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    rows = sales_revenue_cost_by_outbound(company, dfrom, dto) if company else []
    # 佣金列：仅总经理可见，按产品归集当期佣金，毛利=收入−成本−佣金
    show_commission = _is_gm(request.user)
    if show_commission and company:
        comm = {r["product_id"]: round_money(r["a"] or Decimal("0.00")) for r in
                ExpenseRecord.objects.filter(company=company, category="commission",
                                             date__gte=dfrom, date__lte=dto)
                .exclude(product=None).values("product_id").annotate(a=Sum("amount"))}
        for r in rows:
            pid = r["product"].pk if r["product"] else None
            c = comm.get(pid, Decimal("0.00"))
            r["commission"] = c
            r["profit"] = r["revenue"] - r["cost"] - c
            r["margin"] = (r["profit"] / r["revenue"] * 100).quantize(Decimal("0.1")) \
                if r["revenue"] else Decimal("0.0")
    keys = ("revenue", "cost", "commission", "profit") if show_commission else ("revenue", "cost", "profit")
    totals = {k: sum((r.get(k, Decimal("0.00")) for r in rows), Decimal("0.00")) for k in keys}
    totals["margin"] = (totals["profit"] / totals["revenue"] * 100).quantize(Decimal("0.1")) \
        if totals["revenue"] else Decimal("0.0")
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        if show_commission:
            headers = ["商品编码", "商品名称", "销售数量", "销售收入(不含税)", "销售成本", "佣金", "销售毛利", "毛利率%"]
            out = [[(r["product"].code if r["product"] else ""), (r["product"].name if r["product"] else ""),
                    r["qty"], r["revenue"], r["cost"], r.get("commission", Decimal("0.00")), r["profit"], r["margin"]]
                   for r in rows]
            out.append(["合计", "", "", totals["revenue"], totals["cost"], totals["commission"],
                        totals["profit"], totals["margin"]])
        else:
            headers = ["商品编码", "商品名称", "销售数量", "销售收入(不含税)", "销售成本", "销售毛利", "毛利率%"]
            out = [[(r["product"].code if r["product"] else ""), (r["product"].name if r["product"] else ""),
                    r["qty"], r["revenue"], r["cost"], r["profit"], r["margin"]] for r in rows]
            out.append(["合计", "", "", totals["revenue"], totals["cost"], totals["profit"], totals["margin"]])
        return xlsx_response("销售收入成本计算表(按出库)", headers, out, company=company, period=(dfrom, dto))
    return render(request, "finance/sales_cost_by_outbound.html", {
        "rows": rows, "totals": totals, "active_company": company,
        "date_from": dfrom, "date_to": dto, "show_commission": show_commission})


@login_required
@permission_required("finance.view_bankjournal", raise_exception=True)
def bank_journal_export(request):
    """导出银行存款日记账为 Excel。

    选定单一账户：经典存折式（沿用 export_bank_journal）。
    未选账户（全部账户，支持多公司）：用通用导出，含 公司/银行账户 列与各账户合计。
    """
    from django.http import HttpResponse

    from .excel import export_bank_journal

    visible = list(get_visible_companies(request.user))
    sel_ids = request.GET.getlist("company")
    chosen = [c for c in visible if str(c.pk) in sel_ids] if sel_ids else list(visible)
    date_from = _parse_date(request.GET.get("from"))
    date_to = _parse_date(request.GET.get("to"))
    entry_type = request.GET.get("entry_type") or None

    acc_id = request.GET.get("account")
    if acc_id:
        account = get_object_or_404(BankAccount, pk=acc_id, company__in=chosen)
        opening, rows, closing = _journal_rows(account.company, account, date_from, date_to, entry_type)
        content = export_bank_journal(account, rows, opening=opening, closing=closing,
                                      date_from=date_from, date_to=date_to)
        resp = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="bank_journal_{account.pk}.xlsx"'
        return resp

    # 全部账户（可多公司）→ 通用导出
    from apps.core.exports import xlsx_response
    accounts = list(BankAccount.objects.filter(company__in=chosen)
                    .select_related("company").order_by("company__code", "name"))
    opening, rows, closing = _journal_rows_multi(accounts, date_from, date_to, entry_type)
    income_total = sum((r["j"].amount for r in rows if r["j"].direction == "in"), Decimal("0.00"))
    outgo_total = sum((r["j"].amount for r in rows if r["j"].direction == "out"), Decimal("0.00"))
    headers = ["公司", "银行账户", "日期", "业务类型", "摘要", "对方单位", "来源单据",
               "收入", "支出", "账户余额"]
    data = [["期初余额", "", "", "", "", "", "", "", "", opening]]
    for r in rows:
        j = r["j"]
        data.append([
            r["account"].company.short_name or str(r["account"].company),
            r["account"].name, j.date, j.get_entry_type_display(), j.summary or "",
            j.counterparty or "", j.source_no or "",
            j.amount if j.direction == "in" else "",
            j.amount if j.direction == "out" else "", r["balance"],
        ])
    data.append(["本期合计", "", "", "", "", "", "", income_total, outgo_total, ""])
    data.append(["期末余额", "", "", "", "", "", "", "", "", closing])
    company_arg = chosen[0] if len(chosen) == 1 else None
    return xlsx_response("银行存款日记账", headers, data,
                         company=company_arg, period=(date_from, date_to) if (date_from or date_to) else None)


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
    return render(request, "finance/other_cashflow_form.html",
                  {"form": form, "title": "其他收支登记", "submit_label": "登记"})


@login_required
@permission_required("finance.add_bankjournal", raise_exception=True)
def other_cashflow_edit(request, pk):
    """修改手工登记的其他收支（仅 source_type=Other、未对账）。"""
    from .forms import OtherCashflowForm
    from .services import SettlementError, update_other_cashflow

    from .services import other_cashflow_block_reason
    company = get_active_company(request, list(get_visible_companies(request.user)))
    journal = get_object_or_404(BankJournal, pk=pk, company=company)
    back = f"{reverse_lazy('bank_journal_report')}?account={journal.bank_account_id}"
    reason = other_cashflow_block_reason(journal, timezone.localdate())
    if reason:
        messages.error(request, f"不可修改：{reason}")
        return redirect(back)

    if request.method == "POST":
        form = OtherCashflowForm(request.POST, company=company)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                update_other_cashflow(
                    journal=journal, user=request.user, doc_date=cd["doc_date"],
                    bank_account=cd["bank_account"], direction=cd["direction"],
                    amount=cd["amount"], entry_type=cd["entry_type"],
                    counterparty=cd.get("counterparty", ""), summary=cd.get("summary", ""),
                    txn_no=cd.get("txn_no", ""))
            except SettlementError as e:
                messages.error(request, str(e))
            else:
                messages.success(request, "其他收支已修改")
                return redirect(f"{reverse_lazy('bank_journal_report')}?account={journal.bank_account_id}")
    else:
        form = OtherCashflowForm(company=company, initial={
            "doc_date": journal.date, "bank_account": journal.bank_account_id,
            "direction": journal.direction, "entry_type": journal.entry_type,
            "amount": journal.amount, "counterparty": journal.counterparty,
            "summary": journal.summary, "txn_no": journal.txn_no})
    return render(request, "finance/other_cashflow_form.html",
                  {"form": form, "title": "修改其他收支", "submit_label": "保存"})


@login_required
@permission_required("finance.delete_bankjournal", raise_exception=True)
def other_cashflow_delete(request, pk):
    """删除手工登记的其他收支（仅 source_type=Other、当月、未对账）。"""
    from .services import SettlementError, delete_other_cashflow, other_cashflow_block_reason

    company = get_active_company(request, list(get_visible_companies(request.user)))
    journal = get_object_or_404(BankJournal, pk=pk, company=company)
    acc_id = journal.bank_account_id
    reason = other_cashflow_block_reason(journal, timezone.localdate())
    if reason:
        messages.error(request, f"不可删除：{reason}")
        return redirect(f"{reverse_lazy('bank_journal_report')}?account={acc_id}")
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
    """票据余额表：在手/已开未用的应收、应付票据。支持多公司联合查询。"""
    visible, chosen = _company_scope(request)
    ar = [n for n in NoteReceivable.objects.filter(company__in=chosen)
          .select_related("customer", "company").order_by("company__code", "doc_no")
          if n.unused > 0 and n.status != NoteReceivable.Status.VOID]
    ap = [n for n in NotePayable.objects.filter(company__in=chosen)
          .select_related("supplier", "company").order_by("company__code", "doc_no")
          if n.unused > 0 and n.status != NotePayable.Status.VOID]
    ar_total = sum((n.unused for n in ar), start=Decimal("0.00"))
    ap_total = sum((n.unused for n in ap), start=Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["公司", "类别", "单号", "票据号", "出票/开票日", "到期日", "往来对象", "票面", "已用", "未用"]
        rows = ([[n.company.short_name or str(n.company), "应收票据", n.doc_no, n.note_no,
                  n.draw_date, n.due_date, str(n.customer or ""), n.amount, n.settled_amount, n.unused]
                 for n in ar]
                + [[n.company.short_name or str(n.company), "应付票据", n.doc_no, n.note_no,
                    n.draw_date, n.due_date, str(n.supplier or ""), n.amount, n.settled_amount, n.unused]
                   for n in ap])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("票据余额表", headers, rows, company=company_arg)
    return render(request, "finance/notes_balance_report.html", {
        "ar": ar, "ap": ap, "ar_total": ar_total, "ap_total": ap_total,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
    })


# ============================= 借调往来余额表（M6-2）==========================
@login_required
@permission_required("finance.view_borrowtransaction", raise_exception=True)
def borrow_report(request):
    from .models import BorrowTransaction
    visible, chosen = _company_scope(request)
    cmap = {c.pk: c for c in chosen}
    agg = {}
    for t in BorrowTransaction.objects.filter(company__in=chosen):
        key = (t.company_id, t.counterparty)
        agg[key] = agg.get(key, Decimal("0.00")) + t.signed_amount
    rows = [{"company": cmap[cid], "counterparty": cp or "（未指定）", "balance": v}
            for (cid, cp), v in sorted(agg.items(), key=lambda kv: (cmap[kv[0][0]].code, kv[0][1]))]
    total = sum((r["balance"] for r in rows), start=Decimal("0.00"))
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("借调往来余额表", ["公司", "对手单位", "往来余额"],
                             [[r["company"].short_name or str(r["company"]),
                               r["counterparty"], r["balance"]] for r in rows], company=company_arg)
    return render(request, "finance/borrow_report.html",
                  {"rows": rows, "total": total,
                   "visible_companies": visible, "chosen_ids": {c.pk for c in chosen}})


# ============================= 费用（佣金/销售/管理/财务）======================
EXPENSE_CATS = {
    "commission": ("佣金", True),    # gm_only=True
    "sales": ("销售费用", False),
    "admin": ("管理费用", False),
    "finance": ("财务费用", False),
}


def _is_gm(user):
    from apps.accounts import roles
    return user.is_superuser or roles.GM in getattr(user, "role_names", [])


def _can_expense(user, gm_only):
    from apps.accounts import roles
    if gm_only:
        return _is_gm(user)
    return user.is_superuser or bool(set(getattr(user, "role_names", []))
                                     & {roles.GM, roles.FINANCE, roles.CASHIER})


@login_required
def expense_page(request, cat):
    """费用录入/列表（按类别）。佣金仅总经理可见可录。"""
    from decimal import Decimal, InvalidOperation

    from django.core.exceptions import PermissionDenied
    from django.http import Http404

    from apps.core.scope import get_active_company, get_visible_companies
    from apps.masterdata.models import Customer, Product
    from .models import ExpenseRecord
    if cat not in EXPENSE_CATS:
        raise Http404
    label, gm_only = EXPENSE_CATS[cat]
    if not _can_expense(request.user, gm_only):
        raise PermissionDenied(f"无权查看{label}")
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        if request.POST.get("action") == "delete":
            ExpenseRecord.objects.filter(pk=request.POST.get("id"), company=company,
                                         category=cat).delete()
            messages.success(request, "已删除该记录。")
            return redirect("expense_page", cat=cat)
        d = _parse_date(request.POST.get("date"))
        cust = Customer.objects.filter(pk=request.POST.get("customer") or 0, company=company).first()
        prod = Product.objects.filter(pk=request.POST.get("product") or 0, company=company).first()
        try:
            amt = Decimal(request.POST.get("amount") or "")
        except (InvalidOperation, TypeError):
            amt = None
        if not d:
            messages.error(request, "请选择日期")
        elif amt is None:
            messages.error(request, "请输入有效金额")
        else:
            ExpenseRecord.objects.create(
                company=company, created_by=request.user, category=cat, date=d,
                customer=cust, product=prod, person=request.POST.get("person", "").strip(),
                amount=amt, remark=request.POST.get("remark", "").strip())
            messages.success(request, f"已登记{label} {amt}")
        return redirect("expense_page", cat=cat)

    rows = (ExpenseRecord.objects.filter(company=company, category=cat)
            .select_related("customer", "product").order_by("-date", "-id"))
    total = sum((r.amount for r in rows), Decimal("0.00"))
    customers = Customer.objects.filter(company=company, is_active=True).order_by("code")
    products = Product.objects.filter(company=company, is_active=True).order_by("code")
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["日期", "客户", "产品", "人员名称", "金额", "备注"]
        data = [[r.date, str(r.customer or ""), str(r.product or ""), r.person, r.amount, r.remark]
                for r in rows]
        data.append(["合计", "", "", "", total, ""])
        return xlsx_response(f"{label}明细", headers, data, company=company)
    return render(request, "finance/expense_page.html", {
        "cat": cat, "label": label, "rows": rows, "total": total,
        "customers": customers, "products": products, "active_company": company,
        "today": timezone.localdate()})


@login_required
def expense_summary_report(request):
    """费用汇总表：按 类别 + 维度(月/人员/客户/产品) 汇总金额。佣金仅总经理可见。"""
    from decimal import Decimal

    from django.core.exceptions import PermissionDenied

    from .models import ExpenseRecord
    is_gm = _is_gm(request.user)
    cat_opts = [(k, v[0]) for k, v in EXPENSE_CATS.items() if is_gm or not v[1]]
    if not cat_opts:
        raise PermissionDenied("无权查看费用汇总")
    cat = request.GET.get("cat") or cat_opts[0][0]
    if cat not in EXPENSE_CATS:
        cat = cat_opts[0][0]
    label, gm_only = EXPENSE_CATS[cat]
    if not _can_expense(request.user, gm_only):
        raise PermissionDenied(f"无权查看{label}")

    group = request.GET.get("group") or "month"
    group = group if group in ("month", "person", "customer", "product") else "month"
    group_label = {"month": "月份", "person": "人员", "customer": "客户", "product": "产品"}[group]

    visible, chosen = _company_scope(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(month=1, day=1)
    dto = _parse_date(request.GET.get("to")) or today

    qs = (ExpenseRecord.objects.filter(company__in=chosen, category=cat,
                                       date__gte=dfrom, date__lte=dto)
          .select_related("customer", "product"))
    bucket = {}
    for r in qs:
        if group == "month":
            key = r.date.strftime("%Y-%m")
        elif group == "person":
            key = r.person or "（未填）"
        elif group == "customer":
            key = str(r.customer) if r.customer else "（未指定）"
        else:
            key = str(r.product) if r.product else "（未指定）"
        e = bucket.setdefault(key, {"key": key, "amount": Decimal("0.00"), "count": 0})
        e["amount"] += r.amount
        e["count"] += 1
    rows = sorted(bucket.values(), key=lambda x: x["key"])
    total = sum((r["amount"] for r in rows), Decimal("0.00"))

    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = [group_label, "笔数", "金额"]
        data = [[r["key"], r["count"], r["amount"]] for r in rows]
        data.append(["合计", sum(r["count"] for r in rows), total])
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response(f"{label}汇总表（按{group_label}）", headers, data,
                             company=company_arg, period=(dfrom, dto))
    return render(request, "finance/expense_summary.html", {
        "rows": rows, "total": total, "label": label, "cat": cat, "cat_opts": cat_opts,
        "group": group, "group_label": group_label,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "date_from": dfrom, "date_to": dto})


@login_required
def customer_sales_report(request):
    """客户销售分析表（按出库）：多公司、可选按产品明细。佣金仅总经理可见。"""
    from django.core.exceptions import PermissionDenied

    from apps.opening.reports import customer_sales_analysis
    if not (request.user.is_superuser or _is_gm(request.user)
            or request.user.has_perm("sales.view_salesoutbound")
            or request.user.has_perm("finance.view_salesinvoice")):
        raise PermissionDenied("无权查看客户销售分析")
    visible, chosen = _company_scope(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    by_product = request.GET.get("by_product") == "1"
    show_commission = _is_gm(request.user)
    data = customer_sales_analysis(chosen, dfrom, dto, by_product, show_commission)

    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        cols = ["公司", "客户", "产品", "销售数量", "销售收入(不含税)", "销售成本"]
        if show_commission:
            cols.append("佣金")
        cols.append("毛利率%")

        def line(company, customer, product, a):
            row = [company, customer, product, a["qty"], a["revenue"], a["cost"]]
            if show_commission:
                row.append(a["commission"])
            row.append(a["margin"])
            return row
        out = []
        for cp in data:
            cname = cp["company"].short_name or str(cp["company"])
            for cc in cp["customers"]:
                custname = str(cc["customer"] or "")
                if by_product:
                    for p in cc["prods"]:
                        out.append(line(cname, custname, str(p["product"] or ""), p))
                    out.append(line("", custname + " 小计", "", cc["sub"]))
                else:
                    out.append(line(cname, custname, "", cc["sub"]))
            out.append(line(cname + " 合计", "", "", cp["tot"]))
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("客户销售分析表(按出库)", cols, out, company=company_arg, period=(dfrom, dto))
    return render(request, "finance/customer_sales.html", {
        "data": data, "by_product": by_product, "show_commission": show_commission,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "date_from": dfrom, "date_to": dto})


@login_required
def management_profit_report(request):
    """管理利润表（按出库）：多公司本期/本年列，可选内部交易抵销。仅总经理可见。"""
    from django.core.exceptions import PermissionDenied

    from apps.opening.reports import management_profit
    if not _is_gm(request.user):
        raise PermissionDenied("管理利润表仅总经理可见")
    visible, chosen = _company_scope(request)
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    eliminate = request.GET.get("eliminate") == "1"
    cols, total = management_profit(chosen, dfrom, dto, eliminate)

    if eliminate:
        row_defs = [("rev", "销售收入"), ("irev", "内部销售收入"), ("net_rev", "净销售收入"),
                    ("cost", "销售成本"), ("icost", "内部销售成本"), ("net_cost", "净销售成本"),
                    ("comm", "销售佣金"), ("profit", "销售利润"), ("margin", "销售毛利率")]
    else:
        row_defs = [("rev", "销售收入"), ("cost", "销售成本"), ("comm", "销售佣金"),
                    ("profit", "销售利润"), ("margin", "销售毛利率")]

    all_cols = cols + ([total] if total else [])
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["项目"]
        for c in all_cols:
            nm = (c["company"].short_name or str(c["company"])) if c["company"] else "合计"
            headers += [f"{nm}(本期)", f"{nm}(本年)"]
        data = []
        for key, lbl in row_defs:
            row = [lbl]
            for c in all_cols:
                if key == "margin":
                    row += [f'{c["cur"]["margin"]}%', f'{c["ytd"]["margin"]}%']
                else:
                    row += [c["cur"][key], c["ytd"][key]]
            data.append(row)
        company_arg = chosen[0] if len(chosen) == 1 else None
        return xlsx_response("管理利润表(按出库)", headers, data, company=company_arg, period=(dfrom, dto))
    col_heads = [(c["company"].short_name or str(c["company"])) if c["company"] else "合计"
                 for c in all_cols]
    table_rows = []
    for key, lbl in row_defs:
        cells = []
        for c in all_cols:
            if key == "margin":
                cells.append((f'{c["cur"]["margin"]}%', f'{c["ytd"]["margin"]}%'))
            else:
                cells.append((c["cur"][key], c["ytd"][key]))
        table_rows.append({"label": lbl, "key": key, "cells": cells})
    return render(request, "finance/management_profit.html", {
        "col_heads": col_heads, "table_rows": table_rows, "eliminate": eliminate,
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "date_from": dfrom, "date_to": dto})
