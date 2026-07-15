"""销售出库单（SPEC §3.2）。M1 仅普通销售；归还留待 M6。

保存即过账：每行调用 inventory.post_outbound 按移动加权均价结转成本、减少库存。
库存不足整单回滚（不允许负库存）。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY
from apps.masterdata.models import Customer, Product


class SalesOutbound(CompanyScopedModel):
    class SalesType(models.TextChoices):
        SALE = "sale", "销售"
        LEND = "lend", "借出"      # M6 借调出库（借出给对方）
        RETURN = "return", "归还"  # M6 借调归还（归还借入的货）

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
    total_cost = models.DecimalField("结转成本合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_untaxed = models.DecimalField("不含税售额合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_tax = models.DecimalField("税额合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_taxed = models.DecimalField("含税售额合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    # 关联联动（M4）：本单面向关联公司时，自动在对方账套生成的镜像采购入库单
    mirror_inbound = models.ForeignKey(
        "purchasing.PurchaseInbound", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="关联镜像入库单",
    )
    is_opening = models.BooleanField("期初发出商品", default=False,
                                     help_text="期初导入的已出库未开票；不重复减库存")
    sales_order = models.ForeignKey(
        "SalesOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="outbounds", verbose_name="来源销售订单",
    )

    class Meta:
        verbose_name = "销售出库单"
        verbose_name_plural = "销售出库单"
        ordering = ["-doc_date", "-id"]
        permissions = [("void_salesoutbound", "作废销售出库单")]
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
    # 售价含税三价（M7：供销售发票联动；与结转成本相互独立）
    sale_unit_price = models.DecimalField("销售单价(不含税)", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE)
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    # 结转成本（移动加权，库存侧）
    unit_cost = models.DecimalField("结转单位成本", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount = models.DecimalField("结转成本", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    stock_move = models.ForeignKey(
        "inventory.StockMove", on_delete=models.PROTECT, null=True, blank=True, verbose_name="对应流水"
    )
    order_line = models.ForeignKey(
        "SalesOrderLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="outbound_lines", verbose_name="来源订单行",
    )

    class Meta:
        verbose_name = "销售出库明细"
        verbose_name_plural = "销售出库明细"

    def __str__(self) -> str:
        return f"{self.product} x {self.quantity}"


class SalesOrder(CompanyScopedModel):
    """销售订单（M18 业务主线，SPEC §20）。执行单为出库与发票。"""

    class Status(models.TextChoices):
        OPEN = "open", "执行中"
        CLOSED = "closed", "已关闭"
        VOID = "void", "已作废"

    class Progress(models.TextChoices):
        NONE = "none", "未发生"
        PARTIAL = "partial", "部分"
        FULL = "full", "全部"

    doc_no = models.CharField("订单号", max_length=32)
    doc_date = models.DateField("订单日期")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, verbose_name="客户")
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.OPEN)
    remark = models.CharField("备注", max_length=255, blank=True)
    total_quantity = models.DecimalField("订单总数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
    total_untaxed = models.DecimalField("不含税合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_tax = models.DecimalField("税额合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_taxed = models.DecimalField("含税合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    ship_status = models.CharField(
        "发货状态", max_length=12, choices=Progress.choices, default=Progress.NONE)
    invoice_status = models.CharField(
        "开票状态", max_length=12, choices=Progress.choices, default=Progress.NONE)
    receipt_status = models.CharField(
        "收款状态", max_length=12, choices=Progress.choices, default=Progress.NONE)

    class Meta:
        verbose_name = "销售订单"
        verbose_name_plural = "销售订单"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_salesorder_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no


class SalesOrderLine(models.Model):
    order = models.ForeignKey(
        SalesOrder, on_delete=models.CASCADE, related_name="lines", verbose_name="销售订单"
    )
    line_no = models.PositiveIntegerField("行号", default=10)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="商品")
    quantity = models.DecimalField("订单数量", max_digits=18, decimal_places=3)
    sale_unit_price = models.DecimalField("销售单价(不含税)", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE)
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)

    class Meta:
        verbose_name = "销售订单明细"
        verbose_name_plural = "销售订单明细"
        ordering = ["line_no", "id"]

    def __str__(self) -> str:
        return f"{self.order.doc_no}-{self.line_no} {self.product}"
