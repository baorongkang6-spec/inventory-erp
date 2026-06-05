"""登录后首页（按角色区分）与账套切换。"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.scope import get_active_company, get_visible_companies, set_active_company

from . import roles


@login_required
def home(request):
    """角色化首页：

    - 总经理/出纳：提示总览（M5 实现总览表，这里先占位）。
    - 采购/销售：只看库存数量（M1 起有真实数量）。
    - 财务：资金/票据/对账入口（后续里程碑）。
    M0 阶段统一给出「可用功能 = 基础资料」的导航。
    """
    user = request.user
    visible = list(get_visible_companies(user))
    active = get_active_company(request, visible)
    user_roles = set(user.role_names)

    context = {
        "active_company": active,
        "visible_companies": visible,
        "user_roles": sorted(user_roles),
        "is_overview_role": bool(user_roles & roles.OVERVIEW_ROLES) or user.is_superuser,
        "is_inventory_only": bool(user_roles & roles.INVENTORY_ONLY_ROLES)
        and not (user_roles - roles.INVENTORY_ONLY_ROLES),
        "role_descriptions": roles.ROLE_DESCRIPTIONS,
    }
    return render(request, "home.html", context)


@require_POST
@login_required
def switch_company(request):
    """切换当前账套（仅可在用户可见公司间切换）。"""
    company_id = request.POST.get("company_id")
    try:
        company_id = int(company_id)
    except (TypeError, ValueError):
        messages.error(request, "无效的公司")
        return redirect(request.META.get("HTTP_REFERER", "home"))

    if set_active_company(request, company_id):
        messages.success(request, "已切换账套")
    else:
        messages.error(request, "无权访问该公司账套")
    return redirect(request.META.get("HTTP_REFERER", "home"))
