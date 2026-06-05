"""月底对账快照（M5-3，SPEC §8.2）。

记一次对账：某公司、某类别、截止日；逐行系统余额 vs 外部余额 + 差异。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import ZERO_MONEY


class ReconciliationRun(CompanyScopedModel):
    class Category(models.TextChoices):
        BANK = "bank", "银行存款"
        NOTE_RECEIVABLE = "note_recv", "应收票据"
        STOCK = "stock", "商品（金额）"
        RECEIVABLE = "receivable", "应收账款"
        PAYABLE = "payable", "应付账款"

    category = models.CharField("对账类别", max_length=16, choices=Category.choices)
    as_of_date = models.DateField("截止日期")
    created_at = models.DateTimeField("对账时间", auto_now_add=True)

    class Meta:
        verbose_name = "对账记录"
        verbose_name_plural = "对账记录"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_category_display()} @ {self.as_of_date}"


class ReconciliationLine(models.Model):
    run = models.ForeignKey(
        ReconciliationRun, on_delete=models.CASCADE, related_name="lines", verbose_name="对账记录"
    )
    item_label = models.CharField("项目", max_length=128)
    system_amount = models.DecimalField("系统余额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    external_amount = models.DecimalField("外部余额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    diff = models.DecimalField("差异", max_digits=18, decimal_places=2, default=ZERO_MONEY)

    class Meta:
        verbose_name = "对账明细"
        verbose_name_plural = "对账明细"

    def __str__(self) -> str:
        return f"{self.item_label} 差异 {self.diff}"
