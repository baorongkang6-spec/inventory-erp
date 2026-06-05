"""资金往来表单。"""

from django import forms

from apps.core.forms import BootstrapForm, CompanyScopedModelForm
from apps.core.money import DEFAULT_TAX_RATE
from apps.masterdata.models import Product, Supplier

from .models import BankAccount


class BankAccountForm(CompanyScopedModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_no", "opening_balance", "is_active", "remark"]


# --- 采购发票 -----------------------------------------------------------------
class PurchaseInvoiceHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="开票日期")
    supplier = forms.ModelChoiceField(label="供应商", queryset=Supplier.objects.none())
    invoice_no = forms.CharField(label="发票号码", required=False, max_length=64)
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(
                company=company, is_active=True
            )


class PurchaseInvoiceLineForm(BootstrapForm):
    product = forms.ModelChoiceField(
        label="商品", queryset=Product.objects.none(), required=False, empty_label="—（可不选）"
    )
    description = forms.CharField(label="摘要", required=False, max_length=128)
    amount_untaxed = forms.DecimalField(
        label="不含税金额", required=False, max_digits=18, decimal_places=2, min_value=0
    )
    tax_rate = forms.DecimalField(
        label="税率", required=False, max_digits=5, decimal_places=4,
        min_value=0, max_value=1, initial=DEFAULT_TAX_RATE,
    )

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(
                company=company, is_active=True
            )

    def clean(self):
        cleaned = super().clean()
        amt = cleaned.get("amount_untaxed")
        product = cleaned.get("product")
        desc = cleaned.get("description")
        if amt in (None, "") and not product and not desc:
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if amt is None or amt <= 0:
            self.add_error("amount_untaxed", "不含税金额必须大于 0")
        if cleaned.get("tax_rate") is None:
            cleaned["tax_rate"] = DEFAULT_TAX_RATE
        return cleaned


class BasePurchaseInvoiceLineFormSet(forms.BaseFormSet):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["company"] = self.company
        return kwargs

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        valid = [f.cleaned_data for f in self.forms
                 if f.cleaned_data and not f.cleaned_data.get("_empty")]
        if not valid:
            raise forms.ValidationError("至少录入一行明细")
        self.valid_lines = valid


PurchaseInvoiceLineFormSet = forms.formset_factory(
    PurchaseInvoiceLineForm, formset=BasePurchaseInvoiceLineFormSet, extra=8
)
