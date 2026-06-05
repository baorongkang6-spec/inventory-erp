"""登录防爆破 + 登录审计（SEC-2，DEPLOY §2）。

同一「用户名 + IP」连续失败达 LOGIN_FAILURE_LIMIT 次，锁定 LOGIN_LOCKOUT_SECONDS。
用 Django 缓存计数（单机 Waitress 部署足够；多进程需共享缓存，见 DEV_NOTES）。
登录成败写 AuditLog 备查。
"""

from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.core.cache import cache
from django.dispatch import receiver

from apps.core.models import AuditLog


def client_ip(request) -> str:
    if request is None:
        return ""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")  # 经花生壳/代理时取首个
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _key(username, ip) -> str:
    return f"loginfail:{username}:{ip}"


def is_locked(username, ip) -> bool:
    return cache.get(_key(username, ip), 0) >= settings.LOGIN_FAILURE_LIMIT


def clear_failures(username, ip) -> None:
    cache.delete(_key(username, ip))


@receiver(user_login_failed)
def _on_login_failed(sender, credentials=None, request=None, **kwargs):
    username = (credentials or {}).get("username", "") or ""
    ip = client_ip(request)
    key = _key(username, ip)
    count = (cache.get(key, 0) or 0) + 1
    cache.set(key, count, settings.LOGIN_LOCKOUT_SECONDS)
    AuditLog.record(
        action=AuditLog.Action.LOGIN,
        summary=f"登录失败 用户「{username}」IP {ip}（第 {count} 次）",
    )


@receiver(user_logged_in)
def _on_login_success(sender, request, user, **kwargs):
    ip = client_ip(request)
    clear_failures(user.get_username(), ip)
    AuditLog.record(
        actor=user, action=AuditLog.Action.LOGIN, summary=f"登录成功 IP {ip}",
    )
