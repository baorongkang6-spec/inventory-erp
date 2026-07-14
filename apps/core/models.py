"""核心模型：公司账套、公司维度抽象基类、操作日志。

SPEC §1：单一系统 + 多公司账套。每条业务数据都带「所属公司」标签，
三家公司各算各账（独立期初/报表/互不串账），关联交易可在系统内自动联动。
"""

from django.conf import settings
from django.db import models


class Company(models.Model):
    """公司账套（关联方）。SPEC §1.1：C1 安博诺 / C2 恒本源 / C3 鸿威达。

    `is_related` 标识其为系统内的关联企业 —— 关联交易自动联动（M4）只在这些
    公司之间发生。外部客户/供应商不是 Company，建在 masterdata 里。
    """

    code = models.CharField("公司编号", max_length=8, unique=True)  # C1 / C2 / C3
    name = models.CharField("公司全称", max_length=128, unique=True)
    short_name = models.CharField("简称", max_length=32, blank=True)
    full_name = models.CharField(
        "法定全称（报表/单据抬头）", max_length=128, blank=True,
        help_text="如「安博诺新材料科技（上海）有限公司」；用于 Excel 表头与单据打印抬头。留空则用公司全称。")
    is_related = models.BooleanField("系统内关联企业", default=True)
    is_active = models.BooleanField("启用", default=True)
    opening_locked = models.BooleanField(
        "期初已启用(锁定)", default=False,
        help_text="启用后期初数据不可修改/重导；未启用时可清空重导。")
    period_closed_through = models.DateField(
        "会计期间已结账至", null=True, blank=True,
        help_text="该日及之前的业务单据不可修改/删除；月结时按顺序逐月推进。")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "公司账套"
        verbose_name_plural = "公司账套"
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} {self.short_name or self.name}"

    @property
    def header_name(self) -> str:
        """报表/单据抬头用的法定全称；未设则退回公司全称。"""
        return self.full_name or self.name


class CompanyScopedQuerySet(models.QuerySet):
    """带公司维度过滤的便捷查询集。"""

    def for_company(self, company):
        return self.filter(company=company)

    def for_companies(self, companies):
        return self.filter(company__in=companies)


class CompanyScopedModel(models.Model):
    """所有「带公司标签」的业务数据的抽象基类。

    强制每条记录归属一家公司，并记录创建/更新审计字段。
    业务模型继承它即可获得多账套隔离能力。
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        verbose_name="所属公司",
        related_name="%(class)s_set",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="创建人",
        related_name="+",
    )

    objects = CompanyScopedQuerySet.as_manager()

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """操作日志雏形。SPEC §10：联动、冲销、结转都要可追溯。

    M0 先建表 + 通用写入入口；后续里程碑在关键写操作处调用 record()。
    """

    class Action(models.TextChoices):
        CREATE = "create", "新增"
        UPDATE = "update", "修改"
        DELETE = "delete", "删除"
        VOID = "void", "作废"
        LINK = "link", "关联联动"
        OFFSET = "offset", "冲销核销"
        LOGIN = "login", "登录"

    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, null=True, blank=True, verbose_name="所属公司"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="操作人",
    )
    action = models.CharField("动作", max_length=16, choices=Action.choices)
    target_type = models.CharField("对象类型", max_length=64, blank=True)
    target_id = models.CharField("对象ID", max_length=64, blank=True)
    summary = models.CharField("摘要", max_length=255, blank=True)
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        verbose_name = "操作日志"
        verbose_name_plural = "操作日志"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.get_action_display()} {self.target_type}#{self.target_id}"

    @classmethod
    def record(cls, *, actor=None, company=None, action, target=None, summary=""):
        """统一的日志写入入口。target 为任意模型实例（可空）。"""
        target_type = ""
        target_id = ""
        if target is not None:
            target_type = target.__class__.__name__
            target_id = str(getattr(target, "pk", ""))
        return cls.objects.create(
            actor=actor,
            company=company,
            action=action,
            target_type=target_type,
            target_id=target_id,
            summary=summary,
        )
