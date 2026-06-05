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
