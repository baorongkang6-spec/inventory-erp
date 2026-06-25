"""资金往来：银行账户、采购发票（→应付账款）等。

后续在本 app 内逐步加入：付款/收款、银行日记账、核销、销售侧镜像。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import DEFAULT_TAX_RATE, ZERO_MONEY, ZERO_QTY
from apps.masterdata.models import Customer, Product, Supplier


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
    term_days = models.PositiveIntegerField("账期(天)", default=0,
                                            help_text="0=即期；到期日 = 开票日期 + 账期天数")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, verbose_name="供应商")
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    settled_amount = models.DecimalField("已核销金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    is_opening = models.BooleanField("期初", default=False)
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

    @property
    def due_date(self):
        """到期日 = 开票日期 + 账期天数。"""
        from datetime import timedelta
        return self.doc_date + timedelta(days=self.term_days or 0)

    def is_overdue(self, today=None):
        """是否逾期：已登记、仍有未核销、且已过到期日。"""
        from django.utils import timezone
        today = today or timezone.localdate()
        return (self.status == self.Status.REGISTERED
                and self.outstanding > 0 and self.due_date < today)

    @property
    def party(self):
        return self.supplier


class PurchaseInvoiceLine(models.Model):
    invoice = models.ForeignKey(
        PurchaseInvoice, on_delete=models.CASCADE, related_name="lines", verbose_name="发票"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True, verbose_name="商品"
    )
    description = models.CharField("摘要", max_length=128, blank=True)
    quantity = models.DecimalField("数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
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

    class EntryType(models.TextChoices):
        SETTLEMENT = "settlement", "往来结算"
        EXPENSE = "expense", "费用"
        TAX = "tax", "税费"
        PAYROLL = "payroll", "工资"
        TRANSFER = "transfer", "内部划转"
        NOTE_CASH = "note_cash", "票据兑现"
        OTHER = "other", "其他"

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, verbose_name="银行账户", related_name="journals"
    )
    date = models.DateField("日期")
    direction = models.CharField("方向", max_length=4, choices=Direction.choices)
    entry_type = models.CharField("业务类型", max_length=12, choices=EntryType.choices,
                                  default=EntryType.OTHER)
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2)
    counterparty = models.CharField("对方单位", max_length=128, blank=True)
    summary = models.CharField("摘要", max_length=255, blank=True)
    txn_no = models.CharField("交易流水号", max_length=64, blank=True,
                              help_text="网银流水唯一号；增量导入按「账户+流水号」去重。")
    is_imported = models.BooleanField("Excel导入", default=False)
    reconciled = models.BooleanField("已对账", default=False)
    reconcile_batch = models.ForeignKey(
        "BankReconcileBatch", on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="对账批次", related_name="matched_journals")
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


class BankReconcileBatch(CompanyScopedModel):
    """一次银行对账（导入网银流水与系统日记账勾对）的批次记录。SPEC §15 M8-3。"""

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, verbose_name="银行账户",
        related_name="reconcile_batches")
    filename = models.CharField("文件名", max_length=255, blank=True)
    period_from = models.DateField("起始日期", null=True, blank=True)
    period_to = models.DateField("截止日期", null=True, blank=True)
    matched_count = models.IntegerField("已匹配数", default=0)
    system_only_count = models.IntegerField("仅系统有数", default=0)
    bank_only_count = models.IntegerField("仅网银有数", default=0)

    class Meta:
        verbose_name = "银行对账批次"
        verbose_name_plural = "银行对账批次"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"对账 {self.bank_account} {self.created_at:%Y-%m-%d %H:%M}"


class Payment(CompanyScopedModel):
    """付款登记。保存即自动生成一条银行存款日记账（支出）。"""

    class Status(models.TextChoices):
        POSTED = "posted", "已登记"
        VOID = "void", "已作废"

    doc_no = models.CharField("付款单号", max_length=32)
    doc_date = models.DateField("付款日期")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, verbose_name="付款银行账户")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, null=True, blank=True,
                                 verbose_name="收款供应商")
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


class PaymentAllocation(models.Model):
    """付款 ↔ 采购发票 的核销记录（支持部分核销、一款多票、一票多款）。SPEC §7.1。"""

    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="allocations", verbose_name="付款"
    )
    invoice = models.ForeignKey(
        PurchaseInvoice, on_delete=models.PROTECT, related_name="allocations", verbose_name="采购发票"
    )
    amount = models.DecimalField("核销金额", max_digits=18, decimal_places=2)
    created_at = models.DateTimeField("核销时间", auto_now_add=True)

    class Meta:
        verbose_name = "应付核销"
        verbose_name_plural = "应付核销"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.payment.doc_no} ↔ {self.invoice.doc_no} {self.amount}"


# ============================= 销售侧（镜像采购侧）=============================
class SalesInvoice(CompanyScopedModel):
    """销售发票（开具即产生应收账款）。镜像 PurchaseInvoice。"""

    class Status(models.TextChoices):
        REGISTERED = "registered", "已开具"
        VOID = "void", "已作废"

    doc_no = models.CharField("登记单号", max_length=32)
    invoice_no = models.CharField("发票号码", max_length=64, blank=True)
    doc_date = models.DateField("开票日期")
    term_days = models.PositiveIntegerField("账期(天)", default=0,
                                            help_text="0=即期；到期日 = 开票日期 + 账期天数")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, verbose_name="客户")
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    settled_amount = models.DecimalField("已核销金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    is_opening = models.BooleanField("期初", default=False)
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.REGISTERED)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "销售发票"
        verbose_name_plural = "销售发票"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_sinvoice_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no

    @property
    def outstanding(self):
        """未核销（应收余额）。"""
        return self.amount_taxed - self.settled_amount

    @property
    def due_date(self):
        """到期日 = 开票日期 + 账期天数。"""
        from datetime import timedelta
        return self.doc_date + timedelta(days=self.term_days or 0)

    def is_overdue(self, today=None):
        """是否逾期：已开具、仍有未核销、且已过到期日。"""
        from django.utils import timezone
        today = today or timezone.localdate()
        return (self.status == self.Status.REGISTERED
                and self.outstanding > 0 and self.due_date < today)

    @property
    def party(self):
        return self.customer


class SalesInvoiceLine(models.Model):
    invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.CASCADE, related_name="lines", verbose_name="发票"
    )
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, null=True, blank=True, verbose_name="商品"
    )
    description = models.CharField("摘要", max_length=128, blank=True)
    quantity = models.DecimalField("数量", max_digits=18, decimal_places=3, default=ZERO_QTY)
    amount_untaxed = models.DecimalField("不含税金额", max_digits=18, decimal_places=2)
    tax_rate = models.DecimalField("税率", max_digits=5, decimal_places=4, default=DEFAULT_TAX_RATE)
    tax_amount = models.DecimalField("税额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    amount_taxed = models.DecimalField("含税金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    source_outbound_line = models.ForeignKey(
        "sales.SalesOutboundLine", on_delete=models.SET_NULL,
        null=True, blank=True, verbose_name="来源出库明细",
    )

    class Meta:
        verbose_name = "销售发票明细"
        verbose_name_plural = "销售发票明细"

    def __str__(self) -> str:
        return f"{self.product or self.description} {self.amount_taxed}"


class Receipt(CompanyScopedModel):
    """收款登记。保存即自动生成一条银行存款日记账（收入）。镜像 Payment。"""

    class Status(models.TextChoices):
        POSTED = "posted", "已登记"
        VOID = "void", "已作废"

    doc_no = models.CharField("收款单号", max_length=32)
    doc_date = models.DateField("收款日期")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, verbose_name="收款银行账户")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True,
                                 verbose_name="付款客户")
    amount = models.DecimalField("收款金额", max_digits=18, decimal_places=2)
    settled_amount = models.DecimalField("已核销金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    summary = models.CharField("摘要", max_length=255, blank=True)
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.POSTED)
    bank_journal = models.ForeignKey(
        BankJournal, on_delete=models.PROTECT, null=True, blank=True,
        verbose_name="对应日记账", related_name="+",
    )

    class Meta:
        verbose_name = "收款登记"
        verbose_name_plural = "收款登记"
        ordering = ["-doc_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_receipt_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no

    @property
    def unallocated(self):
        return self.amount - self.settled_amount


class ReceiptAllocation(models.Model):
    """收款 ↔ 销售发票 的核销记录。镜像 PaymentAllocation。"""

    receipt = models.ForeignKey(
        Receipt, on_delete=models.CASCADE, related_name="allocations", verbose_name="收款"
    )
    invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.PROTECT, related_name="allocations", verbose_name="销售发票"
    )
    amount = models.DecimalField("核销金额", max_digits=18, decimal_places=2)
    created_at = models.DateTimeField("核销时间", auto_now_add=True)

    class Meta:
        verbose_name = "应收核销"
        verbose_name_plural = "应收核销"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.receipt.doc_no} ↔ {self.invoice.doc_no} {self.amount}"


# ============================= 票据（M3）=====================================
class NoteReceivable(CompanyScopedModel):
    """应收票据（收到的银行承兑汇票等）。SPEC §7.2 / §7.4。

    用途（按未用额分次使用）：① 核销应收账款（冲销售发票）；
    ② 背书转让给供应商抵付应付账款（冲采购发票，置「已背书」）。
    """

    class Status(models.TextChoices):
        ON_HAND = "on_hand", "在手"
        ENDORSED = "endorsed", "已背书"
        SETTLED = "settled", "已结算"
        VOID = "void", "已作废"

    doc_no = models.CharField("登记单号", max_length=32)
    note_no = models.CharField("票据号码", max_length=64, blank=True)
    draw_date = models.DateField("出票日期")
    due_date = models.DateField("到期日期", null=True, blank=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, null=True, blank=True, verbose_name="出票/来源客户"
    )
    amount = models.DecimalField("票面金额", max_digits=18, decimal_places=2)
    settled_amount = models.DecimalField("已用金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.ON_HAND)
    remark = models.CharField("备注", max_length=255, blank=True)
    is_imported = models.BooleanField("Excel导入", default=False)
    is_opening = models.BooleanField("期初", default=False)

    class Meta:
        verbose_name = "应收票据"
        verbose_name_plural = "应收票据"
        ordering = ["-draw_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_notercv_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no

    @property
    def unused(self):
        return self.amount - self.settled_amount


class NotePayable(CompanyScopedModel):
    """应付票据（开给供应商的票据）。用途：抵减应付账款（冲采购发票）。"""

    class Status(models.TextChoices):
        ISSUED = "issued", "已开出"
        SETTLED = "settled", "已结算"
        VOID = "void", "已作废"

    doc_no = models.CharField("登记单号", max_length=32)
    note_no = models.CharField("票据号码", max_length=64, blank=True)
    draw_date = models.DateField("开票日期")
    due_date = models.DateField("到期日期", null=True, blank=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, verbose_name="收票供应商")
    amount = models.DecimalField("票面金额", max_digits=18, decimal_places=2)
    settled_amount = models.DecimalField("已用金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    status = models.CharField("状态", max_length=12, choices=Status.choices, default=Status.ISSUED)
    remark = models.CharField("备注", max_length=255, blank=True)
    is_imported = models.BooleanField("Excel导入", default=False)
    is_opening = models.BooleanField("期初", default=False)

    class Meta:
        verbose_name = "应付票据"
        verbose_name_plural = "应付票据"
        ordering = ["-draw_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["company", "doc_no"], name="uniq_notepay_company_docno")
        ]

    def __str__(self) -> str:
        return self.doc_no

    @property
    def unused(self):
        return self.amount - self.settled_amount


class NoteSettlement(models.Model):
    """票据冲销记录（统一三类用途）。

    note_kind + note_id 指向票据；invoice_kind + invoice_id 指向被冲发票。
    - 应收票据 → 冲应收（销售发票）：endorse=False
    - 应收票据 → 背书抵应付（采购发票）：endorse=True
    - 应付票据 → 抵应付（采购发票）
    用泛指字段避免多张关联表；金额在服务层校验。
    """

    class NoteKind(models.TextChoices):
        RECEIVABLE = "ar_note", "应收票据"
        PAYABLE = "ap_note", "应付票据"

    class InvoiceKind(models.TextChoices):
        SALES = "sales", "销售发票(应收)"
        PURCHASE = "purchase", "采购发票(应付)"

    company = models.ForeignKey("core.Company", on_delete=models.PROTECT, verbose_name="所属公司")
    note_kind = models.CharField("票据类型", max_length=10, choices=NoteKind.choices)
    note_id = models.PositiveIntegerField("票据ID")
    note_no = models.CharField("票据单号", max_length=32, blank=True)
    invoice_kind = models.CharField("发票类型", max_length=10, choices=InvoiceKind.choices)
    invoice_id = models.PositiveIntegerField("发票ID")
    invoice_no = models.CharField("发票单号", max_length=32, blank=True)
    amount = models.DecimalField("冲销金额", max_digits=18, decimal_places=2)
    is_endorsement = models.BooleanField("背书抵付", default=False)
    created_at = models.DateTimeField("冲销时间", auto_now_add=True)

    class Meta:
        verbose_name = "票据冲销"
        verbose_name_plural = "票据冲销"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.note_no} → {self.invoice_no} {self.amount}"


class NoteDisposal(CompanyScopedModel):
    """应收票据处置：到期兑付 / 贴现（票据 → 银行存款）。

    区别于背书(票→抵应付)、核销应收(票进来抵应收)：这是把持有的票变现金。
    - 到期兑付：票面进银行存款（net=票面、贴现息=0）。
    - 贴现：实收净额进银行存款，贴现息=票面−净额，记一笔财务费用。
    两者都「消耗」票据未用额。可撤销（恢复票据 + 删银行日记账/财务费用）。
    """

    class Kind(models.TextChoices):
        COLLECT = "collect", "到期兑付"
        DISCOUNT = "discount", "贴现"

    note = models.ForeignKey(NoteReceivable, on_delete=models.PROTECT,
                             related_name="disposals", verbose_name="应收票据")
    kind = models.CharField("处置方式", max_length=10, choices=Kind.choices)
    date = models.DateField("处置日期")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, verbose_name="收款银行账户")
    amount = models.DecimalField("票面金额", max_digits=18, decimal_places=2)        # 消耗票面
    discount_fee = models.DecimalField("贴现息", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    net_amount = models.DecimalField("实收净额", max_digits=18, decimal_places=2)     # 进银行
    bank_journal = models.ForeignKey(BankJournal, on_delete=models.SET_NULL, null=True, blank=True,
                                     verbose_name="银行日记账")
    expense = models.ForeignKey("ExpenseRecord", on_delete=models.SET_NULL, null=True, blank=True,
                                verbose_name="贴现息费用记录")
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "票据处置"
        verbose_name_plural = "票据处置"
        ordering = ["-date", "-id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.note.doc_no} {self.amount}"


class ExpenseEntry(CompanyScopedModel):
    """其他费用记录（SPEC §6.2）。来自采购入库/销售出库录入的费用行。

    included_in_cost=True 表示该笔已分摊进存货成本（采购入库）；
    False 为期间费用（单列统计）。
    """

    class Kind(models.TextChoices):
        PURCHASE = "purchase", "采购入库"
        SALES = "sales", "销售出库"

    date = models.DateField("日期")
    kind = models.CharField("来源类型", max_length=12, choices=Kind.choices)
    category = models.ForeignKey(
        "masterdata.ExpenseCategory", on_delete=models.PROTECT, verbose_name="费用类别"
    )
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2)
    included_in_cost = models.BooleanField("计入存货成本", default=False)
    source_no = models.CharField("来源单号", max_length=64, blank=True)
    source_type = models.CharField("来源单类型", max_length=32, blank=True)
    source_id = models.CharField("来源单ID", max_length=32, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "其他费用"
        verbose_name_plural = "其他费用"
        ordering = ["-date", "-id"]

    def __str__(self) -> str:
        return f"{self.category} {self.amount}"


class BorrowTransaction(CompanyScopedModel):
    """借调往来（SPEC §4.1）。性质类其他应付，不涉税。

    借入（借调入库）→ direction=in，往来增加；
    归还（归还出库）→ direction=out，往来减少。
    某对手单位余额 = Σ借入金额 − Σ归还金额。
    """

    class Direction(models.TextChoices):
        IN = "in", "借入"
        OUT = "out", "归还"

    counterparty = models.CharField("对手单位", max_length=128)
    direction = models.CharField("方向", max_length=4, choices=Direction.choices)
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2)
    date = models.DateField("日期")
    source_type = models.CharField("来源单类型", max_length=32, blank=True)
    source_id = models.CharField("来源单ID", max_length=32, blank=True)
    source_no = models.CharField("来源单号", max_length=64, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "借调往来"
        verbose_name_plural = "借调往来"
        ordering = ["counterparty", "date", "id"]

    def __str__(self) -> str:
        return f"{self.counterparty} [{self.get_direction_display()}] {self.amount}"

    @property
    def signed_amount(self):
        return self.amount if self.direction == self.Direction.IN else -self.amount


class ExpenseRecord(CompanyScopedModel):
    """费用记录：佣金/销售费用/管理费用/财务费用。佣金仅总经理可见可录。"""

    class Category(models.TextChoices):
        COMMISSION = "commission", "佣金"
        SALES = "sales", "销售费用"
        ADMIN = "admin", "管理费用"
        FINANCE = "finance", "财务费用"

    category = models.CharField("类别", max_length=16, choices=Category.choices)
    date = models.DateField("日期")
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True,
                                 verbose_name="客户")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, null=True, blank=True,
                                verbose_name="产品")
    person = models.CharField("人员名称", max_length=64, blank=True)
    amount = models.DecimalField("金额", max_digits=18, decimal_places=2, default=ZERO_MONEY)
    remark = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "费用记录"
        verbose_name_plural = "费用记录"
        ordering = ["-date", "-id"]

    def __str__(self) -> str:
        return f"{self.get_category_display()} {self.amount}"
