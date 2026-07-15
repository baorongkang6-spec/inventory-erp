"""期初导入（M5-1）。导入与对账授权给财务（finance.add/view_purchaseinvoice）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts import roles
from apps.core.period import close_period, get_report_dates, last_month_range, suggested_close_through, unclose_period
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
        if company.opening_locked:
            messages.error(request, "期初已启用锁定，不可再导入；如需修改请先「解锁期初」。")
            return redirect("opening_import")
        upload = request.FILES.get("file")
        if not upload:
            messages.error(request, "请上传期初数据文件")
        else:
            try:
                results = import_combined(
                    company, request.user, upload,
                    replace_existing=not company.opening_locked)
            except Exception as e:  # noqa: BLE001 解析失败等
                messages.error(request, f"导入失败：{e}")
            else:
                if not results:
                    messages.warning(request, "文件中未找到期初数据 sheet，请用「下载模板」的文件填写")
                for r in results:
                    parts = [f"新增 {r['created']}"]
                    if r.get("updated"):
                        parts.append(f"更新 {r['updated']}")
                    if r["skipped"]:
                        parts.append(f"跳过 {r['skipped']}")
                    msg = f"{r['label']}：{ '，'.join(parts) }"
                    if r["errors"]:
                        msg += f"，{len(r['errors'])} 行有问题"
                    messages.success(request, msg)
                    for e in r["errors"][:8]:
                        messages.warning(request, f"{r['label']} {e}")
        return redirect("opening_import")

    from django.conf import settings
    headers = [{"label": label, "cols": "、".join(cols)}
               for k, label, cols in
               [(k, IMPORTERS[k][0], TEMPLATES[k]) for k in IMPORTERS]]
    return render(request, "opening/opening_import.html",
                  {"headers": headers, "opening_date": settings.OPENING_DATE,
                   "company": company, "opening_locked": company.opening_locked,
                   "clear_blocked": _opening_clear_block_reason(company),
                   "clear_kinds": _opening_clear_kind_status(company)})


OPENING_CLEAR_KINDS = (
    ("stock", "期初库存"),
    ("payable", "期初应付"),
    ("receivable", "期初应收"),
    ("bank", "期初银行存款"),
    ("note_receivable", "期初应收票据"),
    ("note_payable", "期初应付票据"),
    ("goods_shipped", "期初发出商品"),
    ("ap_accrual", "期初应付账款-暂估"),
)


def _opening_clear_kind_block_reason(company, kind):
    """单类期初可否清空（未锁定时）。可清返回 None。"""
    from apps.finance.models import (NotePayable, NoteReceivable,
                                     PurchaseInvoice, SalesInvoice)
    from apps.inventory.models import StockMove
    if kind == "stock":
        if StockMove.objects.filter(company=company).exclude(source_type="Opening").exists():
            return "已有期初之后的库存业务"
        return None
    if kind == "payable":
        for inv in PurchaseInvoice.objects.filter(company=company, is_opening=True):
            if inv.settled_amount > 0:
                return f"期初应付 {inv.supplier} 已有核销"
        return None
    if kind == "receivable":
        for inv in SalesInvoice.objects.filter(company=company, is_opening=True):
            if inv.settled_amount > 0:
                return f"期初应收 {inv.customer} 已有核销"
        return None
    if kind == "bank":
        return None
    if kind == "note_receivable":
        for n in NoteReceivable.objects.filter(company=company, is_opening=True):
            if n.settled_amount > 0:
                return f"期初应收票据 {n.doc_no} 已有使用"
        return None
    if kind == "note_payable":
        for n in NotePayable.objects.filter(company=company, is_opening=True):
            if n.settled_amount > 0:
                return f"期初应付票据 {n.doc_no} 已有使用"
        return None
    if kind == "goods_shipped":
        from apps.finance.models import SalesInvoiceLine
        if SalesInvoiceLine.objects.filter(
                source_outbound_line__outbound__company=company,
                source_outbound_line__outbound__is_opening=True).exists():
            return "期初发出商品已有开票关联"
        return None
    if kind == "ap_accrual":
        from apps.finance.models import PurchaseInvoiceLine
        if PurchaseInvoiceLine.objects.filter(
                source_inbound_line__inbound__company=company,
                source_inbound_line__inbound__is_opening=True).exists():
            return "期初应付暂估已有收票关联"
        return None
    return "未知类别"


def _opening_clear_kind_status(company):
    """供模板渲染：各类期初可否分类清空。"""
    return [{"kind": k, "label": label,
             "blocked": _opening_clear_kind_block_reason(company, k)}
            for k, label in OPENING_CLEAR_KINDS]


def _opening_clear_block_reason(company):
    """期初尚处「设置阶段」才可清空：一旦有日常业务（非期初流水/发票/收付款），返回原因。"""
    from apps.finance.models import (Payment, PurchaseInvoice, Receipt,
                                     SalesInvoice)
    from apps.inventory.models import StockMove
    if StockMove.objects.filter(company=company).exclude(source_type="Opening").exists():
        return "已有期初之后的库存业务"
    if PurchaseInvoice.objects.filter(company=company, is_opening=False).exists() \
            or SalesInvoice.objects.filter(company=company, is_opening=False).exists():
        return "已有日常采购/销售发票"
    if Payment.objects.filter(company=company).exists() or Receipt.objects.filter(company=company).exists():
        return "已有付款/收款记录"
    return None


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def opening_clear(request):
    """清空本账套期初数据（仅未锁定 + 仍处设置阶段时）。清空后可重新导入修正。"""
    from django.db import transaction

    from apps.finance.models import (NotePayable, NoteReceivable,
                                     PurchaseInvoice, SalesInvoice)
    from apps.inventory.models import StockBalance, StockMove
    from apps.finance.models import BankAccount
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method != "POST":
        return redirect("opening_import")
    if company.opening_locked:
        messages.error(request, "期初已锁定，请先解锁再清空。")
        return redirect("opening_import")
    reason = _opening_clear_block_reason(company)
    if reason:
        messages.error(request, f"不可清空期初：{reason}（请逐笔调整或先作废相关业务）。")
        return redirect("opening_import")
    with transaction.atomic():
        for inv in list(PurchaseInvoice.objects.filter(company=company, is_opening=True)):
            inv.lines.all().delete(); inv.delete()
        for inv in list(SalesInvoice.objects.filter(company=company, is_opening=True)):
            inv.lines.all().delete(); inv.delete()
        NoteReceivable.objects.filter(company=company, is_opening=True).delete()
        NotePayable.objects.filter(company=company, is_opening=True).delete()
        StockMove.objects.filter(company=company, source_type="Opening").delete()
        StockBalance.objects.filter(company=company).delete()
        BankAccount.objects.filter(company=company).update(opening_balance=0)
        from apps.purchasing.models import PurchaseInbound
        from apps.sales.models import SalesOutbound
        for doc in list(SalesOutbound.objects.filter(company=company, is_opening=True)):
            doc.lines.all().delete()
            doc.delete()
        for doc in list(PurchaseInbound.objects.filter(company=company, is_opening=True)):
            doc.lines.all().delete()
            doc.delete()
    messages.success(request, "已清空本账套期初数据，可重新「下载模板」修正后导入。")
    return redirect("opening_import")


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def opening_clear_kind(request, kind):
    """清空单类期初（未锁定时）。已有日常业务时仍可清空未受影响的类别（如应付/应收/银行）。"""
    from django.db import transaction

    from apps.finance.models import (BankAccount, NotePayable, NoteReceivable,
                                     PurchaseInvoice, SalesInvoice)
    from apps.inventory.models import StockBalance, StockMove
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method != "POST":
        return redirect("opening_import")
    if company.opening_locked:
        messages.error(request, "期初已锁定，请先解锁再清空。")
        return redirect("opening_import")
    labels = dict(OPENING_CLEAR_KINDS)
    if kind not in labels:
        messages.error(request, "未知期初类别")
        return redirect("opening_import")
    reason = _opening_clear_kind_block_reason(company, kind)
    if reason:
        messages.error(request, f"不可清空{labels[kind]}：{reason}")
        return redirect("opening_import")
    with transaction.atomic():
        if kind == "stock":
            StockMove.objects.filter(company=company, source_type="Opening").delete()
            StockBalance.objects.filter(company=company).delete()
        elif kind == "payable":
            for inv in list(PurchaseInvoice.objects.filter(company=company, is_opening=True)):
                inv.lines.all().delete()
                inv.delete()
        elif kind == "receivable":
            for inv in list(SalesInvoice.objects.filter(company=company, is_opening=True)):
                inv.lines.all().delete()
                inv.delete()
        elif kind == "bank":
            BankAccount.objects.filter(company=company).update(opening_balance=0)
        elif kind == "note_receivable":
            NoteReceivable.objects.filter(company=company, is_opening=True).delete()
        elif kind == "note_payable":
            NotePayable.objects.filter(company=company, is_opening=True).delete()
        elif kind == "goods_shipped":
            from apps.sales.models import SalesOutbound
            for doc in list(SalesOutbound.objects.filter(company=company, is_opening=True)):
                doc.lines.all().delete()
                doc.delete()
        elif kind == "ap_accrual":
            from apps.purchasing.models import PurchaseInbound
            for doc in list(PurchaseInbound.objects.filter(company=company, is_opening=True)):
                doc.lines.all().delete()
                doc.delete()
    messages.success(request, f"已清空{labels[kind]}，可重新导入修正。")
    return redirect("opening_import")


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def opening_lock(request):
    """启用期初：锁定，之后不可修改/重导。"""
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method == "POST":
        company.opening_locked = True
        company.save(update_fields=["opening_locked"])
        messages.success(request, "期初已启用并锁定。")
    return redirect("opening_import")


@login_required
def opening_unlock(request):
    """解锁期初（仅管理员）：解锁后方可再次清空/导入修改。"""
    if not request.user.is_superuser:
        raise PermissionDenied("仅管理员可解锁期初")
    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method == "POST":
        company.opening_locked = False
        company.save(update_fields=["opening_locked"])
        messages.success(request, "期初已解锁，可清空/重导修改；改完请重新启用。")
    return redirect("opening_import")


def _is_overview(user):
    return user.is_superuser or bool(set(user.role_names) & roles.OVERVIEW_ROLES)


@login_required
def overview(request):
    """总经理/出纳跨公司总览表（SPEC §9.1 / M7-4：可选日期区间，默认月初~今天）。"""
    if not _is_overview(request.user):
        raise PermissionDenied("仅总经理/出纳可查看跨公司总览表")
    from django.utils import timezone
    today = timezone.localdate()
    dfrom, dto = get_report_dates(request)
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
    dfrom, dto = get_report_dates(request)
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
    dfrom, dto = get_report_dates(request)
    visible = list(get_visible_companies(request.user))
    sel = request.GET.getlist("company")
    companies = [c for c in visible if str(c.pk) in sel] if sel else list(visible)
    sections = account_balance_table(companies, dfrom, dto)
    blocks = [
        {"key": "bank", "label": "银行存款明细账户", "rows": sections["bank"]},
        {"key": "receivable", "label": "应收账款（按客户）", "rows": sections["receivable"]},
        {"key": "payable", "label": "应付账款（按供应商·已收票）", "rows": sections["payable"]},
        {"key": "ap_accrual", "label": "应付账款-暂估（按供应商·不含税）", "rows": sections["ap_accrual"]},
        {"key": "stock", "label": "库存商品（按品种·金额）", "rows": sections["stock"]},
        {"key": "goods_shipped", "label": "发出商品（按品种·成本）", "rows": sections["goods_shipped"]},
    ]
    from apps.core.money import ZERO_MONEY
    for b in blocks:
        b["total"] = {col: sum((r[col] for r in b["rows"]), ZERO_MONEY)
                      for col in ("opening", "income", "outgo", "ending")}
    if request.GET.get("export") == "xlsx":
        from apps.core.exports import xlsx_response
        headers = ["类别", "公司", "账户/科目", "期初", "本期收入", "本期发出", "期末余额"]
        rows = []
        for b in blocks:
            rows += [[b["label"], r["company"].short_name or str(r["company"]), r["name"],
                      r["opening"], r["income"], r["outgo"], r["ending"]]
                     for r in b["rows"]]
            if b["rows"]:
                t = b["total"]
                rows.append([b["label"], "", "合计",
                             t["opening"], t["income"], t["outgo"], t["ending"]])
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
    today = timezone.localdate()
    _, default_as_of = last_month_range(today)
    if request.method == "POST":
        as_of = request.POST.get("as_of") or str(default_as_of)
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
        "today": today, "default_as_of": default_as_of,
        "period_closed_through": company.period_closed_through,
        "suggested_close": suggested_close_through(company, today),
        "can_period_close": request.user.has_perm("finance.add_purchaseinvoice"),
    })


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def period_close(request):
    """月结结账：锁定截止日及之前业务不可改删。"""
    from django.utils import timezone
    from apps.core.models import AuditLog

    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method != "POST" or company is None:
        return redirect("reconciliation")
    today = timezone.localdate()
    close_through = _parse_date(request.POST.get("close_through"))
    if not close_through:
        close_through = suggested_close_through(company, today)
    if not close_through:
        messages.error(request, "当前无可结账期间（上月底可能已结账）。")
        return redirect("reconciliation")
    try:
        close_period(company, close_through, today=today)
    except ValueError as e:
        messages.error(request, str(e))
    else:
        company.refresh_from_db()
        AuditLog.record(
            actor=request.user, company=company, action=AuditLog.Action.UPDATE,
            target=company, summary=f"月结结账至 {company.period_closed_through:%Y-%m-%d}",
        )
        messages.success(request, f"已结账至 {company.period_closed_through:%Y-%m-%d}，该日及之前业务不可修改/删除。")
    return redirect("reconciliation")


@login_required
@permission_required("finance.add_purchaseinvoice", raise_exception=True)
def period_unclose(request):
    """反结账：回退一个已结期间（特殊情况）。"""
    from apps.core.models import AuditLog

    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method != "POST" or company is None:
        return redirect("reconciliation")
    if not request.user.is_superuser:
        raise PermissionDenied("反结账仅管理员可操作")
    old = company.period_closed_through
    try:
        unclose_period(company)
    except ValueError as e:
        messages.error(request, str(e))
    else:
        company.refresh_from_db()
        AuditLog.record(
            actor=request.user, company=company, action=AuditLog.Action.UPDATE,
            target=company,
            summary=f"反结账 {old:%Y-%m-%d} → {company.period_closed_through or '未结账'}",
        )
        new = company.period_closed_through
        msg = f"已反结账，当前结账至 {new:%Y-%m-%d}。" if new else "已完全反结账，所有期间可修改。"
        messages.success(request, msg)
    return redirect("reconciliation")


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
