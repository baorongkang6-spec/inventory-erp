"""基础资料：商品、客户、供应商。均为公司维度（各公司独立维护）。

SPEC §1.2：每家公司库存品种 ≤ 100。
SPEC §6.1：发票默认税率 13%，可按行改 —— 商品上存「默认税率」供录单带出。
客户/供应商预留 related_company：当对方就是系统内的关联企业时挂上，M4 关联交易
自动联动据此识别对手账套。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import DEFAULT_TAX_RATE  # noqa: F401  (保持旧引用路径可用)


class Product(CompanyScopedModel):
    """库存商品（主数据）。实际库存数量/金额/移动加权单价在 M1 的库存模块维护。"""

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


class Partner(CompanyScopedModel):
    """客户/供应商的公共抽象基类。"""

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
        help_text="当对方是系统内关联企业（C1/C2/C3）时选择；用于 M4 关联交易自动联动。",
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Customer(Partner):
    """客户（销售对象）。"""

    class Meta:
        verbose_name = "客户"
        verbose_name_plural = "客户"
        ordering = ["company", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_customer_company_code"
            )
        ]


class Supplier(Partner):
    """供应商（采购对象）。"""

    class Meta:
        verbose_name = "供应商"
        verbose_name_plural = "供应商"
        ordering = ["company", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"], name="uniq_supplier_company_code"
            )
        ]


class ExpenseCategory(CompanyScopedModel):
    """其他费用类别（SPEC §6.2）。可自行增加；「是否计入存货成本」开关。

    计入成本（如运费）→ 采购入库时按行分摊抬高入库成本（影响移动加权）；
    不计入（如差旅费）→ 作期间费用单列。
    """

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
