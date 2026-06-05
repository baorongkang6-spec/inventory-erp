"""基础资料表单。company / 审计字段由视图注入，表单不暴露。"""

from apps.core.forms import CompanyScopedModelForm

from .models import Customer, Product, Supplier


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
