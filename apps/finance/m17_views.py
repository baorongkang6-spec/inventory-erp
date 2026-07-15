"""M17：往来对冲 + 关联票据拆借 视图。"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import Company
from apps.core.scope import get_active_company, get_visible_companies, resolve_company
from apps.masterdata.models import BusinessPartner

from .models import NoteLoan, NoteReceivable, PartnerOffset, PurchaseInvoice, SalesInvoice
from .services import (
    SettlementError,
    create_partner_offset,
    lend_note_receivable,
    return_note_loan,
    reverse_partner_offset,
)


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_money(s):
    try:
        return Decimal(str(s).replace(",", "").strip())
    except (InvalidOperation, AttributeError, TypeError):
        return Decimal("0")


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def partner_offset_list(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    qs = PartnerOffset.objects.filter(company=company).select_related(
        "partner").order_by("-doc_date", "-id") if company else PartnerOffset.objects.none()
    return render(request, "finance/partner_offset_list.html", {
        "rows": qs, "active_company": company,
        "can_add": request.user.has_perm("finance.add_salesinvoice"),
    })


@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
def partner_offset_create(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("partner_offset_list")
    partners = (BusinessPartner.objects
                .filter(company=company, is_active=True, is_customer=True, is_supplier=True)
                .order_by("code"))
    partner_id = request.GET.get("partner") or request.POST.get("partner")
    partner = partners.filter(pk=partner_id).first() if partner_id else None
    ar_invs, ap_invs = [], []
    if partner:
        ar_invs = [i for i in SalesInvoice.objects.filter(
            company=company, customer_id=partner.pk, status=SalesInvoice.Status.REGISTERED
        ).order_by("doc_date") if i.outstanding > 0]
        ap_invs = [i for i in PurchaseInvoice.objects.filter(
            company=company, supplier_id=partner.pk, status=PurchaseInvoice.Status.REGISTERED
        ).order_by("doc_date") if i.outstanding > 0]

    if request.method == "POST" and partner:
        doc_date = _parse_date(request.POST.get("doc_date"))
        if not doc_date:
            messages.error(request, "请填写对冲日期")
        else:
            ar_lines, ap_lines = [], []
            for inv in ar_invs:
                amt = _parse_money(request.POST.get(f"ar-{inv.pk}"))
                if amt > 0:
                    ar_lines.append({"invoice": inv, "amount": amt})
            for inv in ap_invs:
                amt = _parse_money(request.POST.get(f"ap-{inv.pk}"))
                if amt > 0:
                    ap_lines.append({"invoice": inv, "amount": amt})
            try:
                doc = create_partner_offset(
                    company=company, user=request.user, doc_date=doc_date,
                    partner=partner,
                    ar_lines=ar_lines, ap_lines=ap_lines,
                    remark=request.POST.get("remark") or "")
            except SettlementError as e:
                messages.error(request, str(e))
            else:
                messages.success(request, f"已登记往来对冲 {doc.doc_no}，金额 {doc.amount}")
                return redirect("partner_offset_detail", pk=doc.pk)

    from django.utils import timezone
    return render(request, "finance/partner_offset_form.html", {
        "active_company": company, "partners": partners, "partner": partner,
        "ar_invs": ar_invs, "ap_invs": ap_invs,
        "doc_date": request.POST.get("doc_date") or timezone.localdate().isoformat(),
        "remark": request.POST.get("remark") or "",
    })


@login_required
@permission_required("finance.view_salesinvoice", raise_exception=True)
def partner_offset_detail(request, pk):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(
        PartnerOffset.objects.select_related("partner"),
        pk=pk, company=company)
    return render(request, "finance/partner_offset_detail.html", {
        "doc": doc, "active_company": company,
        "can_reverse": (doc.status == PartnerOffset.Status.REGISTERED
                        and request.user.has_perm("finance.add_salesinvoice")),
    })


@login_required
@permission_required("finance.add_salesinvoice", raise_exception=True)
@require_POST
def partner_offset_reverse(request, pk):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(PartnerOffset, pk=pk, company=company)
    try:
        reverse_partner_offset(doc, user=request.user)
    except SettlementError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"已撤销往来对冲 {doc.doc_no}")
    return redirect("partner_offset_detail", pk=pk)


@login_required
@permission_required("finance.view_notereceivable", raise_exception=True)
def note_loan_list(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    qs = (NoteLoan.objects.filter(company=company)
          .select_related("counterparty_company", "note_receivable", "note_payable", "mirror")
          .order_by("-doc_date", "-id") if company else NoteLoan.objects.none())
    return render(request, "finance/note_loan_list.html", {
        "rows": qs, "active_company": company,
        "can_add": request.user.has_perm("finance.add_notereceivable"),
    })


@login_required
@permission_required("finance.add_notereceivable", raise_exception=True)
def note_loan_create(request):
    """拆出应收票据给关联公司。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("note_loan_list")
    counterparts = Company.objects.exclude(pk=company.pk).order_by("code")
    notes = [n for n in NoteReceivable.objects.filter(company=company)
             .exclude(status=NoteReceivable.Status.VOID).order_by("-draw_date")
             if n.unused > 0]
    if request.method == "POST":
        note = get_object_or_404(NoteReceivable, pk=request.POST.get("note"), company=company)
        borrower = get_object_or_404(Company, pk=request.POST.get("borrower"))
        doc_date = _parse_date(request.POST.get("doc_date"))
        amount = _parse_money(request.POST.get("amount"))
        if not doc_date:
            messages.error(request, "请填写拆借日期")
        else:
            try:
                lend = lend_note_receivable(
                    company=company, user=request.user, doc_date=doc_date,
                    note=note, borrower_company=borrower, amount=amount,
                    remark=request.POST.get("remark") or "")
            except SettlementError as e:
                messages.error(request, str(e))
            else:
                messages.success(request, f"已拆借 {lend.doc_no}，金额 {lend.amount}")
                return redirect("note_loan_detail", pk=lend.pk)
    from django.utils import timezone
    return render(request, "finance/note_loan_form.html", {
        "active_company": company, "counterparts": counterparts, "notes": notes,
        "doc_date": request.POST.get("doc_date") or timezone.localdate().isoformat(),
        "remark": request.POST.get("remark") or "",
    })


@login_required
@permission_required("finance.view_notereceivable", raise_exception=True)
def note_loan_detail(request, pk):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(
        NoteLoan.objects.select_related(
            "counterparty_company", "note_receivable", "note_payable", "mirror"),
        pk=pk, company=company)
    return render(request, "finance/note_loan_detail.html", {
        "doc": doc, "active_company": company,
        "can_return": (doc.status == NoteLoan.Status.OPEN and doc.outstanding > 0
                       and request.user.has_perm("finance.add_notereceivable")),
    })


@login_required
@permission_required("finance.add_notereceivable", raise_exception=True)
@require_POST
def note_loan_return(request, pk):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    doc = get_object_or_404(NoteLoan, pk=pk, company=company)
    amount = _parse_money(request.POST.get("amount"))
    return_date = _parse_date(request.POST.get("return_date"))
    from django.utils import timezone
    if not return_date:
        return_date = timezone.localdate()
    try:
        return_note_loan(doc, user=request.user, amount=amount, return_date=return_date,
                         remark=request.POST.get("remark") or "")
    except SettlementError as e:
        messages.error(request, str(e))
    else:
        messages.success(request, f"已登记归还 {amount}")
    return redirect("note_loan_detail", pk=pk)
