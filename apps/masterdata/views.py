"""商品 / 客户 / 供应商 的列表与增删改（基于 core.crud 通用基类，公司维度过滤）。"""

from django.urls import reverse_lazy

from apps.core.crud import (
    ScopedCreateView,
    ScopedDeleteView,
    ScopedListView,
    ScopedUpdateView,
)

from .forms import CustomerForm, ExpenseCategoryForm, ProductForm, SupplierForm
from .models import Customer, ExpenseCategory, Product, Supplier


# --- 商品 ---------------------------------------------------------------------
class ProductListView(ScopedListView):
    model = Product
    title = "商品"
    columns = [("编码", "code"), ("名称", "name"), ("规格", "spec"),
               ("单位", "unit"), ("分类", "category"), ("启用", "is_active")]
    create_url_name = "product_create"
    update_url_name = "product_update"
    delete_url_name = "product_delete"


class ProductCreateView(ScopedCreateView):
    model = Product
    form_class = ProductForm
    title = "商品"
    success_url = reverse_lazy("product_list")


class ProductUpdateView(ScopedUpdateView):
    model = Product
    form_class = ProductForm
    title = "商品"
    success_url = reverse_lazy("product_list")


class ProductDeleteView(ScopedDeleteView):
    model = Product
    title = "商品"
    success_url = reverse_lazy("product_list")


# --- 客户 ---------------------------------------------------------------------
class CustomerListView(ScopedListView):
    model = Customer
    title = "客户"
    columns = [("编码", "code"), ("名称", "name"), ("联系人", "contact"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    create_url_name = "customer_create"
    update_url_name = "customer_update"
    delete_url_name = "customer_delete"


class CustomerCreateView(ScopedCreateView):
    model = Customer
    form_class = CustomerForm
    title = "客户"
    success_url = reverse_lazy("customer_list")


class CustomerUpdateView(ScopedUpdateView):
    model = Customer
    form_class = CustomerForm
    title = "客户"
    success_url = reverse_lazy("customer_list")


class CustomerDeleteView(ScopedDeleteView):
    model = Customer
    title = "客户"
    success_url = reverse_lazy("customer_list")


# --- 供应商 -------------------------------------------------------------------
class SupplierListView(ScopedListView):
    model = Supplier
    title = "供应商"
    columns = [("编码", "code"), ("名称", "name"), ("联系人", "contact"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    create_url_name = "supplier_create"
    update_url_name = "supplier_update"
    delete_url_name = "supplier_delete"


class SupplierCreateView(ScopedCreateView):
    model = Supplier
    form_class = SupplierForm
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


class SupplierUpdateView(ScopedUpdateView):
    model = Supplier
    form_class = SupplierForm
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


class SupplierDeleteView(ScopedDeleteView):
    model = Supplier
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


# --- 费用类别（M6 其他费用）---------------------------------------------------
class ExpenseCategoryListView(ScopedListView):
    model = ExpenseCategory
    title = "费用类别"
    columns = [("费用类别", "name"), ("计入存货成本", "include_in_cost"), ("启用", "is_active")]
    create_url_name = "expensecategory_create"
    update_url_name = "expensecategory_update"
    delete_url_name = "expensecategory_delete"


class ExpenseCategoryCreateView(ScopedCreateView):
    model = ExpenseCategory
    form_class = ExpenseCategoryForm
    title = "费用类别"
    success_url = reverse_lazy("expensecategory_list")


class ExpenseCategoryUpdateView(ScopedUpdateView):
    model = ExpenseCategory
    form_class = ExpenseCategoryForm
    title = "费用类别"
    success_url = reverse_lazy("expensecategory_list")


class ExpenseCategoryDeleteView(ScopedDeleteView):
    model = ExpenseCategory
    title = "费用类别"
    success_url = reverse_lazy("expensecategory_list")
