"""资金往来：银行账户（M2-1）。

后续在本 app 内逐步加入：采购/销售发票、付款/收款、银行日记账、核销。
"""

from django.db import models

from apps.core.models import CompanyScopedModel
from apps.core.money import ZERO_MONEY


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
