"""期初导入（M5-1）。导入与对账授权给财务（finance.add/view_purchaseinvoice）。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.http import HttpResponse
from django.shortcuts import redirect, render

from apps.core.scope import get_active_company, get_visible_companies

from .imports import IMPORTERS, TEMPLATES, build_template

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
