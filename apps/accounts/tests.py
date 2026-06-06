"""登录防爆破测试（SEC-2）。"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.core.models import AuditLog


@override_settings(LOGIN_FAILURE_LIMIT=3, LOGIN_LOCKOUT_SECONDS=900)
class LoginLockoutTests(TestCase):
    def setUp(self):
        cache.clear()
        get_user_model().objects.create_user(username="u1", password="goodpass123")

    def _post(self, pwd):
        return self.client.post("/login/", {"username": "u1", "password": pwd},
                                SERVER_NAME="localhost")

    def test_locked_after_limit_blocks_even_correct_password(self):
        for _ in range(3):
            self._post("wrong")
        resp = self._post("goodpass123")  # 已锁定，正确密码也拒绝
        self.assertContains(resp, "已临时锁定")
        self.assertFalse(resp.wsgi_request.user.is_authenticated)
        # 审计记录了失败登录
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.LOGIN).exists())

    def test_success_before_limit_and_clears(self):
        self._post("wrong")  # 1 次失败
        resp = self.client.post("/login/", {"username": "u1", "password": "goodpass123"},
                                SERVER_NAME="localhost", follow=True)
        self.assertTrue(resp.context["user"].is_authenticated)
        # 成功后计数清零，可继续登录
        self.assertFalse(cache.get("loginfail:u1:127.0.0.1"))


class UserManagementTests(TestCase):
    """用户管理（M13）：仅管理员可建/改用户、分角色与可见公司。"""

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group
        from apps.core.models import Company
        U = get_user_model()
        cls.admin = U.objects.create_superuser(username="boss", password="x")
        cls.normal = U.objects.create_user(username="staff", password="x")
        cls.c1 = Company.objects.create(code="C1", name="安博诺", short_name="安博诺")
        for n in ["总经理", "出纳", "采购", "销售", "财务"]:
            Group.objects.get_or_create(name=n)

    def test_non_admin_forbidden(self):
        self.client.force_login(self.normal)
        self.assertEqual(self.client.get("/users/", SERVER_NAME="localhost").status_code, 403)

    def test_admin_creates_user_with_role_and_company(self):
        from django.contrib.auth.models import Group
        self.client.force_login(self.admin)
        resp = self.client.post("/users/new/", {
            "username": "caigou", "display_name": "采购员", "is_active": "on",
            "password": "pw12345678",
            "roles": [Group.objects.get(name="采购").pk],
            "companies": [self.c1.pk],
        }, SERVER_NAME="localhost", follow=True)
        self.assertEqual(resp.status_code, 200)
        u = get_user_model().objects.get(username="caigou")
        self.assertTrue(u.check_password("pw12345678"))
        self.assertEqual(u.role_names, ["采购"])
        self.assertIn(self.c1, u.companies.all())

    def test_edit_keeps_password_when_blank(self):
        u = get_user_model().objects.create_user(username="keep", password="orig12345")
        self.client.force_login(self.admin)
        self.client.post(f"/users/{u.pk}/edit/", {
            "username": "keep", "display_name": "改名", "is_active": "on",
            "password": "", "roles": [], "companies": [],
        }, SERVER_NAME="localhost", follow=True)
        u.refresh_from_db()
        self.assertEqual(u.display_name, "改名")
        self.assertTrue(u.check_password("orig12345"))   # 留空不改密码

    def test_cannot_deactivate_self(self):
        self.client.force_login(self.admin)
        self.client.post(f"/users/{self.admin.pk}/edit/", {
            "username": "boss", "display_name": "", "password": "",
            "roles": [], "companies": [],   # is_active 不勾 = 停用
        }, SERVER_NAME="localhost")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)            # 自己仍启用
