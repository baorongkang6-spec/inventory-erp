"""销售出库单（SPEC §3.2）。M1 仅普通销售；归还留待 M6。

保存即过账：每行调用 inventory.post_outbound 按移动加权均价结转成本、减少库存。
库存不足整单回滚（不允许负库存）。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import ZERO_MONEY, ZERO_QTY
from apps.masterdata.models import Customer, Product


class SalesOutbound(CompanyScopedModel):
    class SalesType(models.TextChoices):
        SALE = "sale", "销售"
        RETURN = "return", "归还"  # M6（借调归还）

    class Status(models.TextChoices):
        POSTED = "posted", "已过账"
        VOID = "void", "已作废"

    doc_no = models.CharField("出库单号", max_length=32)
    doc_date = models.DateField("出库日期")
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, null=True, blank=True, verbose_name="客户"
    )
    sales_type = models.CharField(
        "销售方式", max_length=16, choices=SalesType.choices, default=SalesType.SALE
    )
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.POSTED)
    remark = models.CharField("备注", max_length=255, blank=True)
    total_quantity = models.DecimalField("总数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
    total_cost = models.DecimalField("总成本", max_digits=18, decimal_places=2, default=ZERO_MONEY)

    class Meta:
        verbose_name = "销售出库单"
        verbose_name_plural = "销售出库单"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_outbound_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no


class SalesOutboundLine(models.Model):
    outbound = models.ForeignKey(
        SalesOutbound, on_delete=models.CASCADE, related_name="lines", verbose_name="出库单"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="商品")
    quantity = models.DecimalField("数量", max_digits=18, decimal_places=3)
    unit_cost = models.DecimalField("结转单位成本", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount = models.DecimalField("结转成本", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    stock_move = models.ForeignKey(
        "inventory.StockMove", on_delete=models.PROTECT, null=True, blank=True, verbose_name="对应流水"
    )

    class Meta:
        verbose_name = "销售出库明细"
        verbose_name_plural = "销售出库明细"

    def __str__(self) -> str:
        return f"{self.product} x {self.quantity}"
