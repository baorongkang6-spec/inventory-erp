"""期初导入（M5-1）。导入与对账授权给财务（finance.add/view_purchaseinvoice）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts import roles
from apps.core.scope import get_active_company, get_visible_companies, resolve_company

from .imports import IMPORTERS, TEMPLATES, build_combined_template, build_template, import_combined
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
    """合并模板（kind=all，一个工作簿多 sheet）或单类模板（兼容旧链接）。"""
    if kind == "all":
        resp = HttpResponse(build_combined_template(), content_type=XLSX)
        resp["Content-Disposition"] = 'attachment; filename="opening_all.xlsx"'
        return resp
    if kind not in TEMPLATES:
        messages.error(request, "未知模板")
        return redirect("opening_import")
    resp = HttpResponse(build_template(kind), content_type=XLSX)
    resp["Content-Disposition"] = f'attachment; filename="opening_{kind}.xlsx"'
    return resp


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def opening_import(request):
    """一个工作簿（每类一个 sheet）一次导入全部期初。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if company is None:
        messages.error(request, "无可用公司账套")
        return redirect("home")

    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "请上传期初数据文件")
        else:
            try:
                results = import_combined(company, request.user, upload)
            except Exception as e:  # noqa: BLE001 解析失败等
                messages.error(request, f"导入失败：{e}")
            else:
                if not results:
                    messages.warning(request, "文件中未找到期初数据 sheet，请用「下载模板」的文件填写")
                for r in results:
                    messages.success(
                        request, f"{r['label']}：成功 {r['created']}，跳过重复 {r['skipped']}"
                        + (f"，{len(r['errors'])} 行有问题" if r["errors"] else ""))
                    for e in r["errors"][:8]:
                        messages.warning(request, f"{r['label']} {e}")
        return redirect("opening_import")

    from django.conf import settings
    headers = [{"label": label, "cols": "、".join(cols)}
               for k, label, cols in
               [(k, IMPORTERS[k][0], TEMPLATES[k]) for k in IMPORTERS]]
    return render(request, "opening/opening_import.html",
                  {"headers": headers, "opening_date": settings.OPENING_DATE})


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
        return xlsx_response("跨公司总览表", headers, rows, period=(dfrom, dto))
    return render(request, "opening/overview.html", {
        "companies": companies, "blocks": blocks,
        "date_from": dfrom, "date_to": dto,
    })


# ============================= 查询中心（M11）================================
@login_required
def query_center(request):
    """跨公司组合查询：选事项 + 公司(多选) + 日期 + 关键字/方向/类型/状态。"""
    if not (_is_overview(request.user)
            or request.user.has_perm("finance.view_bankjournal")
            or request.user.has_perm("finance.view_purchaseinvoice")
            or request.user.has_perm("inventory.view_stockbalance")):
        raise PermissionDenied("无权使用查询中心")
    from django.utils import timezone

    from .query import DIRECTION_CHOICES, STATUS_CHOICES, SUBJECTS, run_query

    visible = list(get_visible_companies(request.user))
    today = timezone.localdate()
    subject = request.GET.get("subject") or "stock_moves"
    if subject not in SUBJECTS:
        subject = "stock_moves"
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today

    # 公司多选：未提交则默认全部可见
    sel_ids = request.GET.getlist("company")
    if sel_ids:
        chosen = [c for c in visible if str(c.pk) in sel_ids]
    else:
        chosen = list(visible)

    params = {"q": (request.GET.get("q") or "").strip(),
              "direction": request.GET.get("direction", ""),
              "entry_type": request.GET.get("entry_type", ""),
              "status": request.GET.get("status", "")}
    result = run_query(subject, chosen, dfrom, dto, params)

    if request.GET.get("export") == "xlsx" and result["columns"]:
        from apps.core.exports import xlsx_response
        rows = list(result["rows"])
        if result["totals"]:
            rows.append(result["totals"])
        label = SUBJECTS[subject]["label"]
        return xlsx_response(label, result["columns"], rows, period=(dfrom, dto),
                             company=chosen[0] if len(chosen) == 1 else None)

    from apps.finance.models import BankJournal
    return render(request, "opening/query_center.html", {
        "subjects": SUBJECTS, "subject": subject, "meta": SUBJECTS[subject],
        "visible_companies": visible, "chosen_ids": {c.pk for c in chosen},
        "date_from": dfrom, "date_to": dto, "params": params,
        "result": result,
        "direction_choices": DIRECTION_CHOICES, "status_choices": STATUS_CHOICES,
        "entry_type_choices": BankJournal.EntryType.choices,
    })


# ============================= 账户余额表（M7-6 / #8）========================
@login_required
def account_balance(request):
    """明细账户余额表：银行分账户 / 库存按品种 / 应付按供应商 / 应收按客户。

    支持多公司联合查询（公司不选=全部可见公司）。
    口径：期初(区间前累计)+本期收入−本期发出=期末。默认月初~今天。
    """
    if not (_is_overview(request.user)
            or request.user.has_perm("finance.view_bankjournal")
            or request.user.has_perm("finance.view_purchaseinvoice")):
        raise PermissionDenied("无权查看账户余额表")
    from django.utils import timezone
    today = timezone.localdate()
    dfrom = _parse_date(request.GET.get("from")) or today.replace(day=1)
    dto = _parse_date(request.GET.get("to")) or today
    visible = list(get_visible_companies(request.user))
    sel = request.GET.getlist("company")
    companies = [c for c in visible if str(c.pk) in sel] if sel else list(visible)
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
        company_arg = companies[0] if len(companies) == 1 else None
        return xlsx_response("账户余额表", headers, rows, company=company_arg, period=(dfrom, dto))
    return render(request, "opening/account_balance.html", {
        "companies": companies, "blocks": blocks,
        "visible_companies": visible, "chosen_ids": {c.pk for c in companies},
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

    saved_run = None
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
        saved_run = run
        messages.success(request, "对账完成，已保存对账记录")

    return render(request, "opening/reconciliation.html", {
        "categories": categories, "category": category,
        "sys_lines": sys_lines, "result": result, "saved_run": saved_run,
        "today": timezone.localdate(),
    })


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def reconciliation_history(request):
    """对账历史：列出本账套已保存的对账记录（每月对账留痕）。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    runs = (ReconciliationRun.objects.filter(company=company)
            .select_related("created_by").prefetch_related("lines")
            if company else ReconciliationRun.objects.none())
    rows = []
    for run in runs:
        lines = list(run.lines.all())
        has_diff = any(ln.diff != 0 for ln in lines)
        rows.append({"run": run, "n": len(lines), "has_diff": has_diff})
    return render(request, "opening/reconciliation_history.html",
                  {"rows": rows, "active_company": company})


@login_required
@permission_required("finance.view_purchaseinvoice", raise_exception=True)
def reconciliation_detail(request, pk):
    """对账记录详情（含对账人）+ 导出 Excel。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    run = get_object_or_404(ReconciliationRun, pk=pk, company=company)
    lines = list(run.lines.all())
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["项目", "系统余额", "外部余额", "差异"]
        data = [[ln.item_label, ln.system_amount, ln.external_amount, ln.diff] for ln in lines]
        return xlsx_response(f"月底对账-{run.get_category_display()}", headers, data,
                             company=company,
                             extra_meta=[f"截止日期：{run.as_of_date}",
                                         f"对账人：{run.created_by or '—'}"])
    return render(request, "opening/reconciliation_detail.html",
                  {"run": run, "lines": lines, "active_company": company})
