"""数据范围（公司账套）解析逻辑，供视图与上下文处理器共用。

规则（SPEC §2）：
- 超级用户或「可见全部公司」的用户（如总经理/出纳）→ 看全部启用公司。
- 其他用户 → 只看被显式授权的公司集合。
- 「当前账套」存在 session，用于录入/列表默认过滤；必须落在可见集合内。
"""

from .models import Company

ACTIVE_COMPANY_SESSION_KEY = "active_company_id"


def get_visible_companies(user):
    """返回该用户可见的公司 QuerySet（按编号排序）。"""
    base = Company.objects.filter(is_active=True)
    if user.is_superuser or getattr(user, "can_view_all_companies", False):
        return base
    return base.filter(pk__in=user.companies.values_list("pk", flat=True))


def get_active_company(request, visible=None):
    """解析当前账套：取 session 中选择的公司，校验仍在可见集合内，否则取首个。"""
    if visible is None:
        visible = list(get_visible_companies(request.user))
    else:
        visible = list(visible)
    if not visible:
        return None

    chosen_id = request.session.get(ACTIVE_COMPANY_SESSION_KEY)
    if chosen_id is not None:
        for c in visible:
            if c.pk == chosen_id:
                return c
    return visible[0]


def set_active_company(request, company_id):
    """切换当前账套（仅当目标在可见集合内才生效），返回是否成功。"""
    visible_ids = {c.pk for c in get_visible_companies(request.user)}
    if company_id in visible_ids:
        request.session[ACTIVE_COMPANY_SESSION_KEY] = company_id
        return True
    return False


def resolve_company(request, visible=None):
    """报表下钻用：URL ?company= 指定且在可见集合内则用之，否则退回当前账套。

    让总览表能直接点进「某家公司」的明细报表，而不必先切账套。
    """
    if visible is None:
        visible = list(get_visible_companies(request.user))
    cid = request.GET.get("company")
    if cid:
        for c in visible:
            if str(c.pk) == cid:
                return c
    return get_active_company(request, visible)
