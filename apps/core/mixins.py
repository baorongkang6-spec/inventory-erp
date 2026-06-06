"""可复用的「公司维度」视图基类。

保证：
- 列表只显示当前账套（active_company）的数据；
- 新增时自动把 company 设为当前账套、created_by 设为当前用户，
  并在校验前注入 company，使「公司内编码唯一」约束能在表单层正确校验；
- 全程要求登录。
"""

from datetime import datetime

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db.models import Q

from .scope import get_active_company, get_visible_companies


def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


class FilteredListMixin:
    """通用列表筛选（M7-6 / #9）：?q 关键字模糊匹配 + ?from/?to 业务日期区间。

    子类声明：
    - search_fields：模糊匹配字段名列表（支持跨表如 "supplier__name"）；
    - date_filter_field：日期区间过滤字段（如 "doc_date"）。
    都为空时不显示筛选条。保持其它 super().get_queryset() 的账套过滤不变。
    """

    search_fields: list = []
    date_filter_field = None
    q_placeholder = "关键字"
    # 导出列：[(表头, accessor)]，accessor 同 exports.resolve_cell。
    # 缺省回退到 ScopedListView 的 columns（显示列）。
    export_columns: list = []
    export_filename = "导出"

    def get_export_columns(self):
        return self.export_columns or getattr(self, "columns", [])

    def get(self, request, *args, **kwargs):
        if request.GET.get("export") == "xlsx" and self.get_export_columns():
            return self._export_xlsx()
        return super().get(request, *args, **kwargs)

    def _export_xlsx(self):
        from .exports import resolve_cell, xlsx_response
        cols = self.get_export_columns()
        headers = [h for h, _ in cols]
        rows = [[resolve_cell(obj, acc) for _, acc in cols]
                for obj in self.get_queryset()]
        company = self.get_active_company() if hasattr(self, "get_active_company") else None
        return xlsx_response(self.export_filename, headers, rows, company=company)

    def get_queryset(self):
        qs = super().get_queryset()
        q = (self.request.GET.get("q") or "").strip()
        if q and self.search_fields:
            cond = Q()
            for f in self.search_fields:
                cond |= Q(**{f"{f}__icontains": q})
            qs = qs.filter(cond)
        if self.date_filter_field:
            df = _parse_date(self.request.GET.get("from"))
            dt = _parse_date(self.request.GET.get("to"))
            if df:
                qs = qs.filter(**{f"{self.date_filter_field}__gte": df})
            if dt:
                qs = qs.filter(**{f"{self.date_filter_field}__lte": dt})
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        params = self.request.GET.copy()
        params["export"] = "xlsx"
        ctx["filter"] = {
            "q": self.request.GET.get("q", ""),
            "from": self.request.GET.get("from", ""),
            "to": self.request.GET.get("to", ""),
            "has_q": bool(self.search_fields),
            "has_date": bool(self.date_filter_field),
            "q_placeholder": self.q_placeholder,
            "can_export": bool(self.get_export_columns()),
            "export_url": "?" + params.urlencode(),
        }
        return ctx


class ModelPermRequiredMixin(PermissionRequiredMixin):
    """按「动作 + 模型」自动推导所需 Django 权限点（RBAC，SPEC §2）。

    perm_action ∈ {view, add, change, delete}；未授权且已登录 → 403，未登录 → 跳登录。
    超级用户天然拥有全部权限。权限到角色的分配见 seed_init.ROLE_PERMS。
    """

    perm_action = "view"

    def get_permission_required(self):
        meta = self.model._meta
        return (f"{meta.app_label}.{self.perm_action}_{meta.model_name}",)


class CompanyScopedMixin(LoginRequiredMixin, ModelPermRequiredMixin):
    """读取当前账套并据此过滤查询集；同时要求对应模型权限。"""

    def get_active_company(self):
        visible = list(get_visible_companies(self.request.user))
        return get_active_company(self.request, visible)

    def get_queryset(self):
        company = self.get_active_company()
        if company is None:
            return self.model.objects.none()
        return self.model.objects.for_company(company)


class CompanyScopedFormMixin(CompanyScopedMixin):
    """新增/编辑：在校验前注入 company，新增时记录 created_by。"""

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # 校验（含 company 内唯一约束）前就绑定公司
        form.instance.company = self.get_active_company()
        return form

    def form_valid(self, form):
        if form.instance.pk is None and form.instance.created_by_id is None:
            form.instance.created_by = self.request.user
        return super().form_valid(form)
