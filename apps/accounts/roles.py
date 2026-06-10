"""角色定义（对应 Django Group）。SPEC §2。

角色即 Group 名称常量；一人可兼多角色，权限按角色集合叠加。
ROLE_HOME 决定登录后跳转的首页侧重（总经理/出纳看总览，采购/销售看库存数量等）。
M0 仅用角色做菜单/首页区分；细粒度权限点（Permission）在各业务里程碑逐步挂接。
"""

GM = "总经理"
CASHIER = "出纳"
PURCHASER = "采购"
SALES = "销售"
FINANCE = "财务"

ALL_ROLES = [GM, CASHIER, PURCHASER, SALES, FINANCE]

# 各角色简介（用于 admin/首页展示）
ROLE_DESCRIPTIONS = {
    GM: "跨三家公司只读总览表",
    CASHIER: "只读总览 + 资金/票据录入",
    PURCHASER: "查看库存数量 + 采购入库录入",
    SALES: "查看库存数量 + 销售出库录入",
    FINANCE: "发票、收付款、日记账、对账",
}

# 看「跨公司总览」的角色（M5 总览表；M0 先用于首页区分）
OVERVIEW_ROLES = {GM, CASHIER}

# 只看「库存数量」的角色（SPEC §9.2）
INVENTORY_ONLY_ROLES = {PURCHASER, SALES}


def menu_flags(user):
    """根据用户角色集合，决定导航菜单各项是否可见（M0 粒度）。

    - 商品：所有角色都能看（采购/销售只看数量，其余可管理）。
    - 客户：销售/财务/出纳/总经理。
    - 供应商：采购/财务/出纳/总经理。
    超级用户全部可见。
    """
    if user.is_superuser:
        return {"products": True, "customers": True, "suppliers": True,
                "commission": True, "expenses": True}
    roles = set(user.role_names)
    return {
        "products": True,
        "customers": bool(roles & {SALES, FINANCE, CASHIER, GM}),
        "suppliers": bool(roles & {PURCHASER, FINANCE, CASHIER, GM}),
        "commission": GM in roles,                      # 佣金：仅总经理
        "expenses": bool(roles & {GM, FINANCE, CASHIER}),  # 销售/管理/财务费用
    }
