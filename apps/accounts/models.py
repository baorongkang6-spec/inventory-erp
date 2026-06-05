"""用户与数据范围。角色用 Django Group 实现（见 roles.py）。

SPEC §2：RBAC（用户 → 角色 → 权限点）；一人可兼多角色（叠加）；
数据范围可限定某用户只看某公司或多公司。
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """自定义用户：在 Django 内置 auth 基础上增加「数据范围」。

    角色通过 groups（Django 内置）叠加，因此这里不放角色字段。
    """

    display_name = models.CharField("显示名", max_length=64, blank=True)
    companies = models.ManyToManyField(
        "core.Company",
        verbose_name="可见公司",
        blank=True,
        related_name="members",
        help_text="该用户可访问的公司账套；勾选「可见全部公司」时此项忽略。",
    )
    can_view_all_companies = models.BooleanField(
        "可见全部公司",
        default=False,
        help_text="总经理/出纳等跨公司汇总角色应勾选。",
    )

    class Meta:
        verbose_name = "用户"
        verbose_name_plural = "用户"

    def __str__(self) -> str:
        return self.display_name or self.get_username()

    @property
    def role_names(self):
        """该用户当前拥有的角色（Group）名称列表。"""
        return list(self.groups.values_list("name", flat=True))
