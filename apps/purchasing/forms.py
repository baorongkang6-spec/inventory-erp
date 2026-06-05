"""采购入库录入表单：单头 + 多行明细 formset。

product/supplier 下拉按当前账套过滤。空行（未填商品）跳过，至少要有一行有效明细。
"""

from django import forms

from apps.core.forms import BootstrapForm
from apps.masterdata.models import Product, Supplier

from .models import PurchaseInbound


class InboundHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="入库日期")
    purchase_type = forms.ChoiceField(label="采购方式", choices=PurchaseInbound.PurchaseType.choices)
    supplier = forms.ModelChoiceField(
        label="供应商/出借方", queryset=Supplier.objects.none(), required=False, empty_label="（外购/未指定）"
    )
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(
                company=company, is_active=True
            )


class InboundLineForm(BootstrapForm):
    product = forms.ModelChoiceField(
        label="商品", queryset=Product.objects.none(), required=False, empty_label="—"
    )
    quantity = forms.DecimalField(label="数量", required=False, max_digits=18, decimal_places=3, min_value=0)
    unit_price = forms.DecimalField(label="成本单价", required=False, max_digits=18, decimal_places=2, min_value=0)

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
        price = cleaned.get("unit_price")
        # 空行（完全没填）→ 标记跳过
        if not product and not qty and not price:
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if not product:
            self.add_error("product", "请选择商品")
        if qty is None or qty <= 0:
            self.add_error("quantity", "数量必须大于 0")
        if price is None or price < 0:
            self.add_error("unit_price", "请填写成本单价")
        return cleaned


class BaseInboundLineFormSet(forms.BaseFormSet):
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


InboundLineFormSet = forms.formset_factory(
    InboundLineForm, formset=BaseInboundLineFormSet, extra=8
)
