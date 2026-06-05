"""资金往来表单。"""

from apps.core.forms import CompanyScopedModelForm

from .models import BankAccount


class BankAccountForm(CompanyScopedModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_no", "opening_balance", "is_active", "remark"]
