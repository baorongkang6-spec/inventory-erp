"""初始化种子数据：三家公司账套、五个角色（Group）、演示用户、少量基础资料。

幂等：可重复执行（全部 get_or_create）。
用法：uv run python manage.py seed_init [--demo]
  --demo 时额外建演示用户与样例商品/客户/供应商，并设统一密钥便于联调。
"""

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

# 各角色默认拥有的 masterdata 权限点（RBAC：角色 → 权限）。
# 菜单可见性另由 roles.menu_flags 控制；这里让 Django 权限与角色保持一致。
ROLE_PERMS = {
    roles.GM: ["view_product", "view_customer", "view_supplier"],
    roles.CASHIER: ["view_product", "view_customer", "view_supplier"],
    roles.PURCHASER: ["view_product",
                      "add_supplier", "change_supplier", "view_supplier", "delete_supplier"],
    roles.SALES: ["view_product",
                  "add_customer", "change_customer", "view_customer", "delete_customer"],
    roles.FINANCE: ["view_product",
                    "view_customer", "change_customer",
                    "view_supplier", "change_supplier"],
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
    def _seed_roles(self):
        self.groups = {}
        for role in roles.ALL_ROLES:
            group, _ = Group.objects.get_or_create(name=role)
            perms = Permission.objects.filter(
                content_type__app_label="masterdata",
                codename__in=ROLE_PERMS.get(role, []),
            )
            group.permissions.set(perms)
            self.groups[role] = group
            self.stdout.write(f"  · 角色 {role}（{perms.count()} 权限点）")

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
