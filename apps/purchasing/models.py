"""采购入库单（SPEC §3.1）。M1 仅外购；借调留待 M6（借调往来）。

保存即过账：每行调用 inventory.post_inbound 增加库存（移动加权）。
头上冗余 总数量/总金额 便于列表展示。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY
from apps.masterdata.models import Product, Supplier


class PurchaseInbound(CompanyScopedModel):
    class PurchaseType(models.TextChoices):
        EXTERNAL = "external", "外购"
        BORROW = "borrow", "借调"  # M6 实现（借调往来、不涉税）

    class Status(models.TextChoices):
        POSTED = "posted", "已过账"
        VOID = "void", "已作废"  # M4 起支持作废联动

    doc_no = models.CharField("入库单号", max_length=32)
    doc_date = models.DateField("入库日期")
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, null=True, blank=True, verbose_name="供应商"
    )
    purchase_type = models.CharField(
        "采购方式", max_length=16, choices=PurchaseType.choices, default=PurchaseType.EXTERNAL
    )
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.POSTED)
    remark = models.CharField("备注", max_length=255, blank=True)
    total_quantity = models.DecimalField("总数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
    total_amount = models.DecimalField("入库成本合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_untaxed = models.DecimalField("不含税合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_tax = models.DecimalField("税额合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_taxed = models.DecimalField("含税合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    # 关联联动（M4）：本单是由对方公司的销售出库自动镜像生成时，指向源出库单
    source_outbound = models.ForeignKey(
        "sales.SalesOutbound", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="关联来源出库单",
    )
    is_opening = models.BooleanField("期初应付暂估", default=False,
                                     help_text="期初导入的已入库未收票；不重复加库存")
    purchase_order = models.ForeignKey(
        "PurchaseOrder", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inbounds", verbose_name="来源采购订单",
    )

    class Meta:
        verbose_name = "采购入库单"
        verbose_name_plural = "采购入库单"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_inbound_company_docno")
        ]
        permissions = [("void_purchaseinbound", "作废采购入库单")]

    def __str__(self) -> str:
        return self.doc_no


class PurchaseInboundLine(models.Model):
    inbound = models.ForeignKey(
        PurchaseInbound, on_delete=models.CASCADE, related_name="lines", verbose_name="入库单"
    )
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="商品")
    quantity = models.DecimalField("数量", max_digits=18, decimal_places=3)
    unit_price = models.DecimalField("不含税单价", max_digits=18, decimal_places=2)
    # 含税三价（M7：单据上带出，供发票联动；库存成本仍按不含税 amount 入账）
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE)
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount = models.DecimalField("入库成本金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    stock_move = models.ForeignKey(
        "inventory.StockMove", on_delete=models.PROTECT, null=True, blank=True, verbose_name="对应流水"
    )
    order_line = models.ForeignKey(
        "PurchaseOrderLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inbound_lines", verbose_name="来源订单行",
    )

    class Meta:
        verbose_name = "采购入库明细"
        verbose_name_plural = "采购入库明细"

    def __str__(self) -> str:
        return f"{self.product} x {self.quantity}"


class PurchaseOrder(CompanyScopedModel):
    """采购订单（M18 业务主线，SPEC §20）。执行单为入库与发票。"""

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
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, verbose_name="供应商")
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.OPEN)
    remark = models.CharField("备注", max_length=255, blank=True)
    total_quantity = models.DecimalField("订单总数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
    total_untaxed = models.DecimalField("不含税合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_tax = models.DecimalField("税额合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    total_taxed = models.DecimalField("含税合计", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    receive_status = models.CharField(
        "收货状态", max_length=12, choices=Progress.choices, default=Progress.NONE)
    invoice_status = models.CharField(
        "收票状态", max_length=12, choices=Progress.choices, default=Progress.NONE)
    payment_status = models.CharField(
        "付款状态", max_length=12, choices=Progress.choices, default=Progress.NONE)

    class Meta:
        verbose_name = "采购订单"
        verbose_name_plural = "采购订单"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_purchaseorder_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines", verbose_name="采购订单"
    )
    line_no = models.PositiveIntegerField("行号", default=10)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name="商品")
    quantity = models.DecimalField("订单数量", max_digits=18, decimal_places=3)
    unit_price = models.DecimalField("不含税单价", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE)
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)

    class Meta:
        verbose_name = "采购订单明细"
        verbose_name_plural = "采购订单明细"
        ordering = ["line_no", "id"]

    def __str__(self) -> str:
        return f"{self.order.doc_no}-{self.line_no} {self.product}"
