"""基础资料：商品、往来单位（客户/供应商角色）。均为公司维度。

SPEC §1.2：每家公司库存品种 ≤ 100。
SPEC §6.1：商品默认税率 13%。
SPEC §21：客户/供应商合并为「往来单位」统一编码；`is_customer` / `is_supplier` 角色开关。
关联企业挂 related_company，供跨公司镜像联动。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import DEFAULT_TAX_RATE  # noqa: F401


class Product(CompanyScopedModel):
    """库存商品（主数据）。实际库存数量/金额/移动加权单价在库存模块维护。"""

    code = models.CharField("商品编码", max_length=32)
    name = models.CharField("商品名称", max_length=128)
    spec = models.CharField("规格型号", max_length=128, blank=True)
    unit = models.CharField("计量单位", max_length=16, blank=True)
    category = models.CharField("分类", max_length=64, blank=True)
    default_tax_rate = models.DecimalField(
        "默认税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "商品"
        verbose_name_plural = "商品"
        ordering = ["company", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_product_company_code"
            )
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class CustomerManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_customer=True)


class SupplierManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_supplier=True)


class BusinessPartner(CompanyScopedModel):
    """往来单位（SPEC §21）：统一编码；可同时是客户与供应商。"""

    code = models.CharField("编码", max_length=32)
    name = models.CharField("名称", max_length=128)
    contact = models.CharField("联系人", max_length=64, blank=True)
    phone = models.CharField("电话", max_length=32, blank=True)
    tax_no = models.CharField("税号", max_length=32, blank=True)
    address = models.CharField("地址", max_length=255, blank=True)
    related_company = models.ForeignKey(
        "core.Company",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name="对应关联企业",
        related_name="+",
        help_text="当对方是系统内关联企业（C1/C2/C3）时选择；用于关联交易自动联动。",
    )
    is_customer = models.BooleanField("客户", default=False)
    is_supplier = models.BooleanField("供应商", default=False)
    is_active = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "往来单位"
        verbose_name_plural = "往来单位"
        ordering = ["company", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_partner_company_code"
            ),
            models.CheckConstraint(
                condition=models.Q(is_customer=True) | models.Q(is_supplier=True),
                name="partner_must_have_role",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if not self.is_customer and not self.is_supplier:
            raise ValidationError("至少勾选「客户」或「供应商」之一")


class Customer(BusinessPartner):
    """兼容代理：仅客户角色；create/save 自动打 is_customer。"""

    objects = CustomerManager()

    class Meta:
        proxy = True
        verbose_name = "客户"
        verbose_name_plural = "客户"

    def clean(self):
        self.is_customer = True
        super().clean()

    def save(self, *args, **kwargs):
        self.is_customer = True
        super().save(*args, **kwargs)


class Supplier(BusinessPartner):
    """兼容代理：仅供应商角色；create/save 自动打 is_supplier。"""

    objects = SupplierManager()

    class Meta:
        proxy = True
        verbose_name = "供应商"
        verbose_name_plural = "供应商"

    def clean(self):
        self.is_supplier = True
        super().clean()

    def save(self, *args, **kwargs):
        self.is_supplier = True
        super().save(*args, **kwargs)


class ExpenseCategory(CompanyScopedModel):
    """其他费用类别（SPEC §6.2）。"""

    name = models.CharField("费用类别", max_length=64)
    include_in_cost = models.BooleanField("计入存货成本", default=False)
    is_active = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "费用类别"
        verbose_name_plural = "费用类别"
        ordering = ["company", "name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uniq_expcat_company_name")
        ]

    def __str__(self) -> str:
        return self.name


class InvoiceQuota(CompanyScopedModel):
    """每月可开具发票额度（按公司）。不含税金额。"""

    amount = models.DecimalField("每月可开票额度(不含税)", max_digits=18, decimal_places=2, default=0)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "开票额度"
        verbose_name_plural = "开票额度"
        ordering = ["company"]
        constraints = [
            models.UniqueConstraint(fields=["company"], name="uniq_quota_company")
        ]

    def __str__(self) -> str:
        return f"{self.company} {self.amount}"
