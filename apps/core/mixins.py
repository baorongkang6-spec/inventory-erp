"""可复用的「公司维度」视图基类。

保证：
- 列表只显示当前账套（active_company）的数据；
- 新增时自动把 company 设为当前账套、created_by 设为当前用户，
  并在校验前注入 company，使「公司内编码唯一」约束能在表单层正确校验；
- 全程要求登录。
"""

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin

from .scope import get_active_company, get_visible_companies


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
