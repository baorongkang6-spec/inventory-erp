"""期初导入（M5-1）。导入与对账授权给财务（finance.add/view_purchaseinvoice）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import redirect, render

from apps.accounts import roles
from apps.core.scope import get_active_company, get_visible_companies

from .imports import IMPORTERS, TEMPLATES, build_template
from .models import ReconciliationLine, ReconciliationRun
from .reports import account_balance_table, overview_table, recon_lines


def _parse_date(s):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def opening_template(request, kind):
    if kind not in TEMPLATES:
        messages.error(request, "未知模板")
        return redirect("opening_import")
    resp = HttpResponse(build_template(kind), content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="opening_{kind}.xlsx"'
    return resp


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def opening_import(request):
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        kind = request.POST.get("kind")
        upload = request.FILES.get("file")
        if kind not in IMPORTERS or not upload:
            messages.error(request, "请选择类别并上传文件")
        else:
            label, fn = IMPORTERS[kind]
            try:
                created, skipped, errors = fn(company, request.user, upload)
            except Exception as e:  # noqa: BLE001 解析失败等
                messages.error(request, f"{label} 导入失败：{e}")
            else:
                messages.success(request, f"{label} 导入完成：成功 {created}，跳过重复 {skipped}")
                for e in errors[:15]:
                    messages.warning(request, e)
        return redirect("opening_import")

    cards = [{"kind": k, "label": label} for k, (label, _) in IMPORTERS.items()]
    from django.conf import settings
    return render(request, "opening/opening_import.html",
                  {"cards": cards, "opening_date": settings.OPENING_DATE})


def _is_overview(user):
    return user.is_superuser or bool(set(user.role_names) & roles.OVERVIEW_ROLES)


@login_required
def overview(request):
    """总经理/出纳跨公司总览表（SPEC §9.1 / M7-4：可选日期区间，默认月初~今天）。"""
    if not _is_overview(request.user):
        raise PermissionDenied("仅总经理/出纳可查看跨公司总览表")
    from django.utils import timezone
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    companies = list(get_visible_companies(request.user))
    blocks = overview_table(companies, dfrom, dto)
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["类别", "公司", "期初", "本期收入", "本期发出", "期末结存"]
        rows = []
        for b in blocks:
            for r in b["rows"]:
                rows.append([b["label"], r["company"].short_name or str(r["company"]),
                             r["opening"], r["income"], r["outgo"], r["ending"]])
            rows.append([b["label"] + " 合计", "", b["totals"]["opening"], b["totals"]["income"],
                         b["totals"]["outgo"], b["totals"]["ending"]])
        return xlsx_response(f"跨公司总览表_{dfrom}_{dto}", headers, rows)
    return render(request, "opening/overview.html", {
        "companies": companies, "blocks": blocks,
        "date_from": dfrom, "date_to": dto,
    })


# ============================= 账户余额表（M7-6 / #8）========================
@login_required
def account_balance(request):
    """三公司明细账户余额表：银行分账户 / 库存按品种 / 应付按供应商 / 应收按客户。

    口径同总览：期初(区间前累计)+本期收入−本期发出=期末。默认月初~今天。
    """
    if not (_is_overview(request.user)
            or request.user.has_perm("finance.view_bankjournal")
            or request.user.has_perm("finance.view_purchaseinvoice")):
        raise PermissionDenied("无权查看账户余额表")
    from django.utils import timezone
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    companies = list(get_visible_companies(request.user))
    sections = account_balance_table(companies, dfrom, dto)
    blocks = [
        {"key": "bank", "label": "银行存款明细账户", "rows": sections["bank"]},
        {"key": "receivable", "label": "应收账款（按客户）", "rows": sections["receivable"]},
        {"key": "payable", "label": "应付账款（按供应商）", "rows": sections["payable"]},
        {"key": "stock", "label": "库存商品（按品种·金额）", "rows": sections["stock"]},
    ]
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["类别", "公司", "账户/科目", "期初", "本期收入", "本期发出", "期末余额"]
        rows = [[b["label"], r["company"].short_name or str(r["company"]), r["name"],
                 r["opening"], r["income"], r["outgo"], r["ending"]]
                for b in blocks for r in b["rows"]]
        return xlsx_response(f"账户余额表_{dfrom}_{dto}", headers, rows)
    return render(request, "opening/account_balance.html", {
        "companies": companies, "blocks": blocks,
        "date_from": dfrom, "date_to": dto,
    })


# ============================= 月底对账（M5-3）================================
@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def reconciliation(request):
    from decimal import Decimal, InvalidOperation

    from django.utils import timezone

    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    categories = ReconciliationRun.Category.choices
    category = request.GET.get("category") or request.POST.get("category") or "bank"
    sys_lines = recon_lines(company, category)
    result = None

    if request.method == "POST":
        as_of = request.POST.get("as_of") or str(timezone.localdate())
        run = ReconciliationRun.objects.create(
            company=company, created_by=request.user, category=category, as_of_date=as_of)
        result = []
        for i, line in enumerate(sys_lines):
            raw = (request.POST.get(f"ext-{i}") or "").strip()
            try:
                ext = Decimal(raw) if raw else line["system_amount"]
            except (InvalidOperation, ValueError):
                ext = line["system_amount"]
            diff = ext - line["system_amount"]
            ReconciliationLine.objects.create(
                run=run, item_label=line["label"], system_amount=line["system_amount"],
                external_amount=ext, diff=diff)
            result.append({"label": line["label"], "system": line["system_amount"],
                           "external": ext, "diff": diff})
        messages.success(request, "对账完成，已保存对账记录")

    return render(request, "opening/reconciliation.html", {
        "categories": categories, "category": category,
        "sys_lines": sys_lines, "result": result, "today": timezone.localdate(),
    })
