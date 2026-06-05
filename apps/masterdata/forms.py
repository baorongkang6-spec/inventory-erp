"""基础资料表单。company / 审计字段由视图注入，表单不暴露。"""

from django import forms

from .models import Customer, Product, Supplier

_BOOTSTRAP_SKIP = (forms.CheckboxInput,)


class BootstrapModelForm(forms.ModelForm):
    """给所有字段套上 Bootstrap 样式类。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, _BOOTSTRAP_SKIP):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            else:
                widget.attrs.setdefault("class", "form-control")


class CompanyScopedModelForm(BootstrapModelForm):
    """company 字段不在表单里（由视图注入），但「公司内编码唯一」约束需要它。

    Django 默认会把表单外的字段从唯一性校验中排除，导致 (company, code)
    约束被跳过、最终以 IntegrityError 500 报错。这里把 company 移出排除集，
    使重复编码在表单层就报友好错误。前提：视图已在校验前给 instance.company 赋值。
    """

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.discard("company")
        return exclude


class ProductForm(CompanyScopedModelForm):
    class Meta:
        model = Product
        fields = [
            "code", "name", "spec", "unit", "category",
            "default_tax_rate", "is_active", "remark",
        ]


class CustomerForm(CompanyScopedModelForm):
    class Meta:
        model = Customer
        fields = [
            "code", "name", "contact", "phone", "tax_no",
            "address", "related_company", "is_active", "remark",
        ]


class SupplierForm(CompanyScopedModelForm):
    class Meta:
        model = Supplier
        fields = [
            "code", "name", "contact", "phone", "tax_no",
            "address", "related_company", "is_active", "remark",
        ]
