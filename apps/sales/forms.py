"""销售出库录入表单：单头 + 多行明细 formset。出库不录单价（成本系统结转）。"""

from django import forms

from apps.core.forms import BootstrapForm
from apps.core.money import DEFAULT_TAX_RATE
from apps.masterdata.models import Customer, Product

from .models import SalesOutbound


class OutboundHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="出库日期")
    sales_type = forms.ChoiceField(label="销售方式", choices=SalesOutbound.SalesType.choices)
    customer = forms.ModelChoiceField(
        label="客户/归还对象", queryset=Customer.objects.none(), required=False, empty_label="（未指定）"
    )
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["customer"].queryset = Customer.objects.filter(
                company=company, is_active=True
            )


class OutboundLineForm(BootstrapForm):
    product = forms.ModelChoiceField(
        label="商品", queryset=Product.objects.none(), required=False, empty_label="—"
    )
    quantity = forms.DecimalField(label="数量", required=False, max_digits=18, decimal_places=3, min_value=0)
    sale_unit_price = forms.DecimalField(label="销售单价(不含税)", required=False,
                                         max_digits=18, decimal_places=2, min_value=0)
    tax_rate = forms.DecimalField(label="税率", required=False, max_digits=5, decimal_places=4,
                                  min_value=0, max_value=1, initial=DEFAULT_TAX_RATE)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(
                company=company, is_active=True
            )

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        qty = cleaned.get("quantity")
        if not product and not qty and not cleaned.get("sale_unit_price"):
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if not product:
            self.add_error("product", "请选择商品")
        if qty is None or qty <= 0:
            self.add_error("quantity", "数量必须大于 0")
        if cleaned.get("tax_rate") is None:
            cleaned["tax_rate"] = DEFAULT_TAX_RATE
        return cleaned


class BaseOutboundLineFormSet(forms.BaseFormSet):
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
        valid_lines = [
            f.cleaned_data for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("_empty")
        ]
        if not valid_lines:
            raise forms.ValidationError("至少录入一行明细")
        self.valid_lines = valid_lines


OutboundLineFormSet = forms.formset_factory(
    OutboundLineForm, formset=BaseOutboundLineFormSet, extra=8
)
