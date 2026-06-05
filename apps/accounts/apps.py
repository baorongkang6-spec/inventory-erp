from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.accounts'
    verbose_name = '用户 / 角色 / 权限'

    def ready(self):
        from . import security  # noqa: F401  注册登录信号
