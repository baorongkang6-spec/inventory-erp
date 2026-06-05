"""库存结存与流水（数量金额式，移动加权平均）。SPEC §4。

- StockBalance：每个(公司, 商品)一行，记当前 数量/金额/移动加权均价。
- StockMove：不可变流水台账，每次出入库一条，含过账后的结存快照，
  既是审计证据，也是「数量金额式明细账（收入/发出/结存）」的数据源。

真正的过账逻辑在 services.py，模型只存数据。
"""

from django.db import models

from apps.core.models import Company, CompanyScopedModel
from apps.core.money import ZERO_MONEY, ZERO_QTY
from apps.masterdata.models import Product


class StockBalance(CompanyScopedModel):
    """某商品在某公司账套下的当前结存。(company, product) 唯一。"""

    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, verbose_name="商品", related_name="stock_balance"
    )
    quantity = models.DecimalField("结存数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
    amount = models.DecimalField("结存金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    avg_price = models.DecimalField("移动加权均价", max_digits=18, decimal_places=2, default=ZERO_MONEY)

    class Meta:
        verbose_name = "库存结存"
        verbose_name_plural = "库存结存"
        ordering = ["company", "product__code"]
        constraints = [
            models.UniqueConstraint(fields=["company", "product"], name="uniq_stock_company_product")
        ]
        permissions = [("view_amount", "可查看库存金额")]  # 采购/销售无此权限 → 只看数量(SPEC §9.2)

    def __str__(self) -> str:
        return f"{self.product} 结存 {self.quantity}@{self.avg_price}"


class StockMove(models.Model):
    """库存流水（不可变）。direction=in 入库 / out 出库。"""

    class Direction(models.TextChoices):
        IN = "in", "入库"
        OUT = "out", "出库"

    company = models.ForeignKey(Company, on_delete=models.PROTECT, verbose_name="所属公司")
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, verbose_name="商品", related_name="stock_moves"
    )
    direction = models.CharField("方向", max_length=4, choices=Direction.choices)

    quantity = models.DecimalField("数量", max_digits=18, decimal_places=3)
    unit_price = models.DecimalField("单价", max_digits=18, decimal_places=2)
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2)

    # 过账后的结存快照（用于台账「结存」列与对账追溯）
    balance_quantity = models.DecimalField("结存数量", max_digits=18, decimal_places=3)
    balance_amount = models.DecimalField("结存金额", max_digits=18, decimal_places=2)
    balance_price = models.DecimalField("结存均价", max_digits=18, decimal_places=2)

    # 来源单据（泛指，避免对 purchasing/sales 形成硬依赖）
    source_type = models.CharField("来源单类型", max_length=32, blank=True)
    source_id = models.CharField("来源单ID", max_length=32, blank=True)
    source_no = models.CharField("来源单号", max_length=64, blank=True)

    created_at = models.DateTimeField("过账时间", auto_now_add=True)

    class Meta:
        verbose_name = "库存流水"
        verbose_name_plural = "库存流水"
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["company", "product", "created_at"])]

    def __str__(self) -> str:
        return f"[{self.get_direction_display()}] {self.product} {self.quantity}@{self.unit_price}"
