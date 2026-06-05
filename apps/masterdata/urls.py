"""基础资料路由：商品 / 客户 / 供应商 的列表与增删改。"""

from django.urls import path

from . import views

urlpatterns = [
    # 商品
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<int:pk>/edit/", views.ProductUpdateView.as_view(), name="product_update"),
    path("products/<int:pk>/delete/", views.ProductDeleteView.as_view(), name="product_delete"),
    # 客户
    path("customers/", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/new/", views.CustomerCreateView.as_view(), name="customer_create"),
    path("customers/<int:pk>/edit/", views.CustomerUpdateView.as_view(), name="customer_update"),
    path("customers/<int:pk>/delete/", views.CustomerDeleteView.as_view(), name="customer_delete"),
    # 供应商
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/new/", views.SupplierCreateView.as_view(), name="supplier_create"),
    path("suppliers/<int:pk>/edit/", views.SupplierUpdateView.as_view(), name="supplier_update"),
    path("suppliers/<int:pk>/delete/", views.SupplierDeleteView.as_view(), name="supplier_delete"),
    # 费用类别
    path("expense-categories/", views.ExpenseCategoryListView.as_view(), name="expensecategory_list"),
    path("expense-categories/new/", views.ExpenseCategoryCreateView.as_view(), name="expensecategory_create"),
    path("expense-categories/<int:pk>/edit/", views.ExpenseCategoryUpdateView.as_view(), name="expensecategory_update"),
    path("expense-categories/<int:pk>/delete/", views.ExpenseCategoryDeleteView.as_view(), name="expensecategory_delete"),
]
