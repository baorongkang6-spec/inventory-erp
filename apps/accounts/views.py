"""登录后首页（按角色区分）、账套切换、带防爆破的登录视图。"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.scope import get_active_company, get_visible_companies, set_active_company

from . import roles
from .security import client_ip, is_locked


class LockoutLoginView(LoginView):
    """登录视图：被锁定的「用户名+IP」直接拒绝，避免继续爆破。"""

    def post(self, request, *args, **kwargs):
        username = request.POST.get("username", "")
        if is_locked(username, client_ip(request)):
            form = self.get_form()
            form.add_error(
                None,
                f"登录失败次数过多，账号已临时锁定，请约 "
                f"{settings.LOGIN_LOCKOUT_SECONDS // 60} 分钟后再试。",
            )
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)


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
    context.update(_overdue_summary(user, visible))
    return render(request, "home.html", context)


def _overdue_summary(user, visible):
    """首页逾期提醒：按今天判定，统计可见公司逾期应收/应付笔数与金额（受权限控制）。"""
    from decimal import Decimal

    from django.utils import timezone

    from apps.finance.models import PurchaseInvoice, SalesInvoice
    from apps.opening.reports import overdue_invoice_list
    today = timezone.localdate()
    out = {"overdue_ar_count": 0, "overdue_ar_amount": Decimal("0.00"),
           "overdue_ap_count": 0, "overdue_ap_amount": Decimal("0.00")}
    if user.has_perm("finance.view_salesinvoice"):
        ar = overdue_invoice_list(SalesInvoice, "customer", visible, today)
        out["overdue_ar_count"] = len(ar)
        out["overdue_ar_amount"] = sum((r["outstanding"] for r in ar), Decimal("0.00"))
    if user.has_perm("finance.view_purchaseinvoice"):
        ap = overdue_invoice_list(PurchaseInvoice, "supplier", visible, today)
        out["overdue_ap_count"] = len(ap)
        out["overdue_ap_amount"] = sum((r["outstanding"] for r in ap), Decimal("0.00"))
    out["has_overdue"] = bool(out["overdue_ar_count"] or out["overdue_ap_count"])
    return out


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


# ============================= 用户管理（M13，仅管理员）======================
from functools import wraps  # noqa: E402

from django.core.exceptions import PermissionDenied  # noqa: E402
from django.shortcuts import get_object_or_404  # noqa: E402

from .forms import UserForm  # noqa: E402
from .models import User  # noqa: E402


def _admin_only(view):
    """仅超级管理员可访问；已登录的非管理员返回 403（而非跳登录）。"""
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("仅管理员可管理用户")
        return view(request, *args, **kwargs)
    return _wrapped


@login_required
@_admin_only
def user_list(request):
    users = User.objects.prefetch_related("groups", "companies").order_by("username")
    rows = []
    for u in users:
        rows.append({
            "u": u,
            "roles": "、".join(u.role_names) or "—",
            "scope": "全部公司" if u.can_view_all_companies
                     else ("、".join(str(c) for c in u.companies.all()) or "—"),
        })
    return render(request, "accounts/user_list.html", {"rows": rows})


@login_required
@_admin_only
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        u = form.save()
        messages.success(request, f"已新增用户：{u.get_username()}")
        return redirect("user_list")
    return render(request, "accounts/user_form.html", {"form": form, "title": "新增用户"})


@login_required
@_admin_only
def user_edit(request, pk):
    user_obj = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user_obj)
    if request.method == "POST" and form.is_valid():
        # 不允许停用自己，避免把自己锁在外面
        if user_obj.pk == request.user.pk and not form.cleaned_data.get("is_active"):
            messages.error(request, "不能停用当前登录的自己")
        else:
            form.save()
            messages.success(request, f"已保存用户：{user_obj.get_username()}")
            return redirect("user_list")
    return render(request, "accounts/user_form.html",
                  {"form": form, "title": f"编辑用户：{user_obj.get_username()}",
                   "edit_user": user_obj})


# ============================= 修改密码（自助，所有登录用户）=================
@login_required
def password_change(request):
    """用户自助修改密码：校验原密码 + 密码强度，改后保持登录不掉线。"""
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm

    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)   # 改密后保持当前会话
            messages.success(request, "密码已修改")
            return redirect("home")
    else:
        form = PasswordChangeForm(request.user)
    for f in form.fields.values():
        f.widget.attrs.setdefault("class", "form-control")
    return render(request, "accounts/password_change.html", {"form": form})
