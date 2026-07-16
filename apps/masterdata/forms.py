"""基础资料表单。company / 审计字段由视图注入，表单不暴露。"""

from apps.core.forms import CompanyScopedModelForm

from .models import BusinessPartner, Customer, ExpenseCategory, Product, Supplier


class ProductForm(CompanyScopedModelForm):
    class Meta:
        model = Product
        fields = [
            "code", "name", "spec", "unit", "category",
            "default_tax_rate", "is_active", "remark",
        ]


class BusinessPartnerForm(CompanyScopedModelForm):
    class Meta:
        model = BusinessPartner
        fields = [
            "code", "name", "contact", "phone", "tax_no", "address",
            "related_company", "is_customer", "is_supplier", "is_active", "remark",
        ]

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("is_customer") and not cleaned.get("is_supplier"):
            from django.core.exceptions import ValidationError
            raise ValidationError("至少勾选「客户」或「供应商」之一")
        return cleaned


class CustomerForm(CompanyScopedModelForm):
    class Meta:
        model = Customer
        fields = [
            "code", "name", "contact", "phone", "tax_no",
            "address", "related_company", "is_active", "remark",
        ]

    def clean(self):
        cleaned = super().clean()
        self.instance.is_customer = True
        return cleaned


class SupplierForm(CompanyScopedModelForm):
    class Meta:
        model = Supplier
        fields = [
            "code", "name", "contact", "phone", "tax_no",
            "address", "related_company", "is_active", "remark",
        ]

    def clean(self):
        cleaned = super().clean()
        self.instance.is_supplier = True
        return cleaned


class ExpenseCategoryForm(CompanyScopedModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "include_in_cost", "is_active", "remark"]


# --- 其他费用录入行（采购入库/销售出库共用，M6）------------------------------
from django import forms  # noqa: E402
from apps.core.forms import BootstrapForm  # noqa: E402


class ExpenseLineForm(BootstrapForm):
    category = forms.ModelChoiceField(
        label="费用类别", queryset=ExpenseCategory.objects.none(), required=False, empty_label="—")
    amount = forms.DecimalField(label="金额", required=False, max_digits=18, decimal_places=2, min_value=0)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["category"].queryset = ExpenseCategory.objects.filter(
                company=company, is_active=True)

    def clean(self):
        cleaned = super().clean()
        cat, amt = cleaned.get("category"), cleaned.get("amount")
        if not cat and not amt:
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if not cat:
            self.add_error("category", "请选择费用类别")
        if amt is None or amt <= 0:
            self.add_error("amount", "金额必须大于 0")
        return cleaned


class BaseExpenseFormSet(forms.BaseFormSet):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["company"] = self.company
        return kwargs

    @property
    def expense_lines(self):
        return [f.cleaned_data for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("_empty")]


ExpenseFormSet = forms.formset_factory(ExpenseLineForm, formset=BaseExpenseFormSet, extra=1)
