"""模板上下文：把当前用户的「可见公司」与「当前账套」注入所有模板。

数据范围（SPEC §2）：每个用户可限定只看某公司或多公司。顶部公司切换器
据此渲染，列表/表单按 active_company 过滤。
"""

from apps.accounts.roles import OVERVIEW_ROLES, menu_flags

from .scope import get_active_company, get_visible_companies


def company_scope(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"visible_companies": [], "active_company": None, "menu": {}}

    visible = list(get_visible_companies(user))
    active = get_active_company(request, visible)
    is_overview = user.is_superuser or bool(set(user.role_names) & OVERVIEW_ROLES)
    return {
        "visible_companies": visible,
        "active_company": active,
        "menu": menu_flags(user),
        "is_overview": is_overview,
    }
