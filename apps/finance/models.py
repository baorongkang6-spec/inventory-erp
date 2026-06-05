"""资金往来：银行账户、采购发票（→应付账款）等。

后续在本 app 内逐步加入：付款/收款、银行日记账、核销、销售侧镜像。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY
from apps.masterdata.models import Product, Supplier


class BankAccount(CompanyScopedModel):
    """银行账户。每公司可建多个（基本户/一般户等）。SPEC §7.3。"""

    name = models.CharField("账户名称", max_length=64)          # 如「基本户」
    bank_name = models.CharField("开户行", max_length=128, blank=True)
    account_no = models.CharField("银行账号", max_length=64, blank=True)
    opening_balance = models.DecimalField(
        "期初余额", max_digits=18, decimal_places=2, default=ZERO_MONEY,
        help_text="启用日银行存款余额；后续以日记账增减。",
    )
    is_active = models.BooleanField("启用", default=True)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "银行账户"
        verbose_name_plural = "银行账户"
        ordering = ["company", "name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="uniq_bankaccount_company_name")
        ]

    def __str__(self) -> str:
        return self.name


class PurchaseInvoice(CompanyScopedModel):
    """采购发票（登记即产生应付账款）。SPEC §3.1 / §6.1 / §7.3。

    发票本身即「应付」单据：含税总额 = 应付原始额，settled_amount 记已核销额，
    未核销 = amount_taxed − settled_amount。
    """

    class Status(models.TextChoices):
        REGISTERED = "registered", "已登记"
        VOID = "void", "已作废"

    doc_no = models.CharField("登记单号", max_length=32)
    invoice_no = models.CharField("发票号码", max_length=64, blank=True)
    doc_date = models.DateField("开票日期")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, verbose_name="供应商")
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    settled_amount = models.DecimalField("已核销金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.REGISTERED)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "采购发票"
        verbose_name_plural = "采购发票"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_pinvoice_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no

    @property
    def outstanding(self):
        """未核销（应付余额）。"""
        return self.amount_taxed - self.settled_amount


class PurchaseInvoiceLine(models.Model):
    invoice = models.ForeignKey(
        PurchaseInvoice, on_delete=models.CASCADE, related_name="lines", verbose_name="发票"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True, verbose_name="商品"
    )
    description = models.CharField("摘要", max_length=128, blank=True)
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    # 关联来源入库单（可空；独立录入时为空）
    source_inbound_line = models.ForeignKey(
        "purchasing.PurchaseInboundLine", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="来源入库明细",
    )

    class Meta:
        verbose_name = "采购发票明细"
        verbose_name_plural = "采购发票明细"

    def __str__(self) -> str:
        return f"{self.product or self.description} {self.amount_taxed}"


class BankJournal(CompanyScopedModel):
    """银行存款日记账。来源：付款/收款自动生成、Excel 导入。SPEC §7.1。"""

    class Direction(models.TextChoices):
        IN = "in", "收入"
        OUT = "out", "支出"

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, verbose_name="银行账户", related_name="journals"
    )
    date = models.DateField("日期")
    direction = models.CharField("方向", max_length=4, choices=Direction.choices)
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2)
    counterparty = models.CharField("对方单位", max_length=128, blank=True)
    summary = models.CharField("摘要", max_length=255, blank=True)
    is_imported = models.BooleanField("Excel导入", default=False)
    source_type = models.CharField("来源类型", max_length=32, blank=True)
    source_id = models.CharField("来源ID", max_length=32, blank=True)
    source_no = models.CharField("来源单号", max_length=64, blank=True)

    class Meta:
        verbose_name = "银行存款日记账"
        verbose_name_plural = "银行存款日记账"
        ordering = ["date", "id"]
        indexes = [models.Index(fields=["company", "bank_account", "date"])]

    def __str__(self) -> str:
        return f"[{self.get_direction_display()}] {self.amount} {self.summary}"

    @property
    def signed_amount(self):
        """收入为正、支出为负，便于累计余额。"""
        return self.amount if self.direction == self.Direction.IN else -self.amount


class Payment(CompanyScopedModel):
    """付款登记。保存即自动生成一条银行存款日记账（支出）。"""

    class Status(models.TextChoices):
        POSTED = "posted", "已登记"
        VOID = "void", "已作废"

    doc_no = models.CharField("付款单号", max_length=32)
    doc_date = models.DateField("付款日期")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, verbose_name="付款银行账户")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, verbose_name="收款供应商")
    amount = models.DecimalField("付款金额", max_digits=18, decimal_places=2)
    settled_amount = models.DecimalField("已核销金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    summary = models.CharField("摘要", max_length=255, blank=True)
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.POSTED)
    bank_journal = models.ForeignKey(
        BankJournal, on_delete=models.PROTECT, null=True, blank=True,
        verbose_name="对应日记账", related_name="+",
    )

    class Meta:
        verbose_name = "付款登记"
        verbose_name_plural = "付款登记"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_payment_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no

    @property
    def unallocated(self):
        """未核销（可用于核销应付的剩余款）。"""
        return self.amount - self.settled_amount
