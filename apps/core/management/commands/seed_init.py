"""初始化种子数据：三家公司账套、五个角色（Group）、演示用户、少量基础资料。

幂等：可重复执行（全部 get_or_create）。
用法：uv run python manage.py seed_init [--demo]
  --demo 时额外建演示用户与样例商品/客户/供应商，并设统一密钥便于联调。
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts import roles
from apps.core.models import Company
from apps.masterdata.models import Customer, Product, Supplier

User = get_user_model()

COMPANIES = [
    ("C1", "安博诺新材料科技（上海）有限公司", "安博诺"),
    ("C2", "上海恒本源化学有限公司", "恒本源"),
    ("C3", "鸿威达新材料科技（上海）有限公司", "鸿威达"),
]

# 各角色权限点（RBAC：角色 → 权限），格式 "app_label.codename"，跨应用。
# 菜单可见性另由 roles.menu_flags / 模板 perms 控制；这里让 Django 权限与角色一致。
# 说明：商品主数据仅超管维护（业务角色只读，已与客户确认）；
#       inventory.view_amount = 可看库存金额（采购/销售无 → 只看数量，SPEC §9.2）。
_VIEW_MASTERDATA = ["masterdata.view_product", "masterdata.view_customer", "masterdata.view_supplier"]
ROLE_PERMS = {
    # 总经理/出纳：跨公司只读总览，可看金额（SPEC §9.1）
    roles.GM: _VIEW_MASTERDATA + [
        "purchasing.view_purchaseinbound", "sales.view_salesoutbound",
        "inventory.view_stockbalance", "inventory.view_amount",
    ],
    roles.CASHIER: _VIEW_MASTERDATA + [
        "purchasing.view_purchaseinbound", "sales.view_salesoutbound",
        "inventory.view_stockbalance", "inventory.view_amount",
    ],
    # 采购：管供应商、建采购入库、看库存（仅数量）
    roles.PURCHASER: [
        "masterdata.view_product",
        "masterdata.add_supplier", "masterdata.change_supplier",
        "masterdata.view_supplier", "masterdata.delete_supplier",
        "purchasing.add_purchaseinbound", "purchasing.view_purchaseinbound",
        "inventory.view_stockbalance",
    ],
    # 销售：管客户、建销售出库、看库存（仅数量）
    roles.SALES: [
        "masterdata.view_product",
        "masterdata.add_customer", "masterdata.change_customer",
        "masterdata.view_customer", "masterdata.delete_customer",
        "sales.add_salesoutbound", "sales.view_salesoutbound",
        "inventory.view_stockbalance",
    ],
    # 财务：看全部往来、看单据、看库存含金额
    roles.FINANCE: _VIEW_MASTERDATA + [
        "masterdata.change_customer", "masterdata.change_supplier",
        "purchasing.view_purchaseinbound", "sales.view_salesoutbound",
        "inventory.view_stockbalance", "inventory.view_amount",
    ],
}

DEMO_PASSWORD = "erp12345"


class Command(BaseCommand):
    help = "初始化公司账套、角色与（可选）演示数据"

    def add_arguments(self, parser):
        parser.add_argument("--demo", action="store_true", help="附带创建演示用户与样例基础资料")

    @transaction.atomic
    def handle(self, *args, **options):
        self._seed_companies()
        self._seed_roles()
        if options["demo"]:
            self._seed_demo_users()
            self._seed_demo_masterdata()
            self._seed_demo_documents()
        self.stdout.write(self.style.SUCCESS("种子数据初始化完成。"))

    # --- 公司 -----------------------------------------------------------------
    def _seed_companies(self):
        self.companies = {}
        for code, name, short in COMPANIES:
            obj, created = Company.objects.get_or_create(
                code=code, defaults={"name": name, "short_name": short}
            )
            self.companies[code] = obj
            self.stdout.write(("  + " if created else "  · ") + f"公司 {code} {short}")

    # --- 角色（Group + 权限点）------------------------------------------------
    def _resolve_perms(self, dotted_list):
        """把 ["app.codename", ...] 解析成 Permission 列表（跨应用）。

        未建应用的权限（如 sales 在 M1-3 前）会被静默跳过并提示。
        """
        found = []
        for dotted in dotted_list:
            app_label, codename = dotted.split(".")
            perm = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if perm:
                found.append(perm)
            else:
                self.stdout.write(f"    （跳过未就绪权限 {dotted}）")
        return found

    def _seed_roles(self):
        self.groups = {}
        for role in roles.ALL_ROLES:
            group, _ = Group.objects.get_or_create(name=role)
            perms = self._resolve_perms(ROLE_PERMS.get(role, []))
            group.permissions.set(perms)
            self.groups[role] = group
            self.stdout.write(f"  · 角色 {role}（{len(perms)} 权限点）")

    # --- 演示用户 -------------------------------------------------------------
    def _seed_demo_users(self):
        # 超级管理员
        admin, created = User.objects.get_or_create(
            username="admin",
            defaults={"display_name": "系统管理员", "is_staff": True,
                      "is_superuser": True, "can_view_all_companies": True},
        )
        if created:
            admin.set_password(DEMO_PASSWORD)
            admin.save()
        self.stdout.write(("  + " if created else "  · ") + "用户 admin（超级管理员）")

        # (用户名, 显示名, 角色集合, 可见全部, 可见公司编号集合)
        demo = [
            ("gm", "总经理-王", [roles.GM], True, []),
            ("cashier", "出纳-李", [roles.CASHIER], True, []),
            ("purchaser", "采购-赵", [roles.PURCHASER], False, ["C1"]),
            ("sales", "销售-钱", [roles.SALES], False, ["C1"]),
            ("finance", "财务-孙", [roles.FINANCE], False, ["C1", "C2", "C3"]),
            # 一人兼多角色示例（SPEC G1）：既采购又销售，看 C2
            ("ps", "采购兼销售-周", [roles.PURCHASER, roles.SALES], False, ["C2"]),
        ]
        for username, display, role_list, view_all, company_codes in demo:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"display_name": display, "can_view_all_companies": view_all},
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
            user.groups.set([self.groups[r] for r in role_list])
            user.companies.set([self.companies[c] for c in company_codes])
            self.stdout.write(("  + " if created else "  · ")
                              + f"用户 {username}（{'/'.join(role_list)}）")

    # --- 样例基础资料 ---------------------------------------------------------
    def _seed_demo_masterdata(self):
        c1 = self.companies["C1"]
        c2 = self.companies["C2"]
        for code, name, spec, unit in [
            ("P001", "环氧树脂 A", "25kg/桶", "桶"),
            ("P002", "固化剂 B", "20kg/桶", "桶"),
            ("P003", "稀释剂 C", "200kg/桶", "桶"),
        ]:
            Product.objects.get_or_create(
                company=c1, code=code,
                defaults={"name": name, "spec": spec, "unit": unit},
            )
        # C2 一条，验证按公司隔离
        Product.objects.get_or_create(
            company=c2, code="P001",
            defaults={"name": "碳酸钙", "spec": "1t/袋", "unit": "袋"},
        )
        # 关联企业互为客户/供应商（related_company 预留 M4 用）
        Customer.objects.get_or_create(
            company=c1, code="CUST-C2",
            defaults={"name": c2.name, "related_company": c2},
        )
        Supplier.objects.get_or_create(
            company=c1, code="SUP-EXT01",
            defaults={"name": "上海外购化工有限公司"},
        )
        self.stdout.write("  · 样例商品/客户/供应商")

    # --- 样例单据（演示移动加权全流程）---------------------------------------
    def _seed_demo_documents(self):
        from datetime import date

        from apps.purchasing.models import PurchaseInbound
        from apps.purchasing.services import create_and_post_inbound
        from apps.sales.services import create_and_post_outbound

        c1 = self.companies["C1"]
        if PurchaseInbound.objects.filter(company=c1).exists():
            self.stdout.write("  · 样例单据已存在，跳过")
            return

        p001 = Product.objects.get(company=c1, code="P001")
        p002 = Product.objects.get(company=c1, code="P002")
        d = date(2026, 6, 1)
        # 两次入库演示移动加权：100@10 + 50@13 → 均价 11.00
        create_and_post_inbound(company=c1, user=None, doc_date=d, lines=[
            {"product": p001, "quantity": Decimal("100"), "unit_price": Decimal("10")},
            {"product": p002, "quantity": Decimal("20"), "unit_price": Decimal("8")},
        ])
        create_and_post_inbound(company=c1, user=None, doc_date=d, lines=[
            {"product": p001, "quantity": Decimal("50"), "unit_price": Decimal("13")},
        ])
        # 出库 60 → 按均价 11.00 结转成本 660，结存 90@11
        create_and_post_outbound(company=c1, user=None, doc_date=d, lines=[
            {"product": p001, "quantity": Decimal("60")},
        ])
        self.stdout.write("  · 样例单据（入库×2 + 出库×1，演示移动加权）")
