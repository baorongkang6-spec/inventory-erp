"""M0 关键不变量测试：公司隔离、公司内编码唯一、RBAC、数据范围。"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from apps.accounts import roles
from apps.core.models import Company
from apps.masterdata.forms import ProductForm
from apps.masterdata.models import Customer, Product, Supplier

User = get_user_model()


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        cls.c2 = Company.objects.create(code="C2", name="恒本源", short_name="恒本源")
        # 角色组 + 权限
        cls.groups = {}
        for role, codenames in {
            roles.GM: ["view_product", "view_customer", "view_supplier"],
            roles.SALES: ["view_product", "add_customer", "view_customer"],
            roles.PURCHASER: ["view_product", "add_supplier", "view_supplier"],
        }.items():
            g = Group.objects.create(name=role)
            g.permissions.set(Permission.objects.filter(
                content_type__app_label="masterdata", codename__in=codenames))
            cls.groups[role] = g

    def make_user(self, username, role, *, view_all=False, companies=()):
        u = User.objects.create_user(username=username, password="x")
        u.groups.add(self.groups[role])
        u.can_view_all_companies = view_all
        u.save()
        u.companies.set(companies)
        return u


class CompanyScopeTests(Base):
    def test_list_is_company_scoped(self):
        Product.objects.create(company=self.c1, code="P1", name="C1货")
        Product.objects.create(company=self.c2, code="PX", name="C2货")
        u = self.make_user("gm", roles.GM, view_all=True)
        self.client.force_login(u)
        # 默认账套为可见公司中第一个（C1）
        resp = self.client.get(reverse("product_list"))
        self.assertContains(resp, "C1货")
        self.assertNotContains(resp, "C2货")

    def test_unique_code_per_company(self):
        Product.objects.create(company=self.c1, code="DUP", name="原")
        # 同公司重复编码 → 表单校验失败
        f = ProductForm(data={"code": "DUP", "name": "新", "default_tax_rate": "0.13"})
        f.instance.company = self.c1
        self.assertFalse(f.is_valid())
        # 不同公司同编码 → 允许
        f2 = ProductForm(data={"code": "DUP", "name": "新", "default_tax_rate": "0.13"})
        f2.instance.company = self.c2
        self.assertTrue(f2.is_valid())


class RBACTests(Base):
    def test_readonly_role_cannot_create(self):
        self.client.force_login(self.make_user("gm", roles.GM, view_all=True))
        self.assertEqual(self.client.get(reverse("product_create")).status_code, 403)

    def test_sales_customer_not_supplier(self):
        self.client.force_login(self.make_user("sales", roles.SALES, companies=[self.c1]))
        self.assertEqual(self.client.get(reverse("customer_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("supplier_create")).status_code, 403)

    def test_purchaser_supplier_not_customer(self):
        self.client.force_login(self.make_user("pur", roles.PURCHASER, companies=[self.c1]))
        self.assertEqual(self.client.get(reverse("supplier_create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("customer_create")).status_code, 403)


class CreateBindsCompanyTests(Base):
    def test_create_binds_active_company_and_creator(self):
        u = self.make_user("sales", roles.SALES, companies=[self.c1])
        self.client.force_login(u)
        resp = self.client.post(reverse("customer_create"),
                                {"code": "K1", "name": "客户甲", "is_active": "on"})
        self.assertEqual(resp.status_code, 302)
        cust = Customer.objects.get(code="K1")
        self.assertEqual(cust.company, self.c1)
        self.assertEqual(cust.created_by, u)
