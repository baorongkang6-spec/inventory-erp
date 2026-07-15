"""基础资料路由：商品 / 客户 / 供应商 的列表与增删改。"""

from django.urls import path

from . import views

urlpatterns = [
    # 商品
    path("products/", views.ProductListView.as_view(), name="product_list"),
    path("products/new/", views.ProductCreateView.as_view(), name="product_create"),
    path("products/<int:pk>/edit/", views.ProductUpdateView.as_view(), name="product_update"),
    path("products/<int:pk>/delete/", views.ProductDeleteView.as_view(), name="product_delete"),
    # 往来单位（SPEC §21）
    path("partners/", views.PartnerListView.as_view(), name="partner_list"),
    path("partners/new/", views.PartnerCreateView.as_view(), name="partner_create"),
    path("partners/<int:pk>/edit/", views.PartnerUpdateView.as_view(), name="partner_update"),
    path("partners/<int:pk>/delete/", views.PartnerDeleteView.as_view(), name="partner_delete"),
    # 客户（兼容）
    path("customers/", views.CustomerListView.as_view(), name="customer_list"),
    path("customers/new/", views.CustomerCreateView.as_view(), name="customer_create"),
    path("customers/<int:pk>/edit/", views.CustomerUpdateView.as_view(), name="customer_update"),
    path("customers/<int:pk>/delete/", views.CustomerDeleteView.as_view(), name="customer_delete"),
    # 供应商（兼容）
    path("suppliers/", views.SupplierListView.as_view(), name="supplier_list"),
    path("suppliers/new/", views.SupplierCreateView.as_view(), name="supplier_create"),
    path("suppliers/<int:pk>/edit/", views.SupplierUpdateView.as_view(), name="supplier_update"),
    path("suppliers/<int:pk>/delete/", views.SupplierDeleteView.as_view(), name="supplier_delete"),
    # 一键复制基础资料到其他公司（管理员）
    path("copy/", views.copy_masterdata, name="masterdata_copy"),
    # 开具发票额度录入（按公司·月份）
    path("invoice-quota/", views.invoice_quota, name="invoice_quota"),
    # 费用类别
    path("expense-categories/", views.ExpenseCategoryListView.as_view(), name="expensecategory_list"),
    path("expense-categories/new/", views.ExpenseCategoryCreateView.as_view(), name="expensecategory_create"),
    path("expense-categories/<int:pk>/edit/", views.ExpenseCategoryUpdateView.as_view(), name="expensecategory_update"),
    path("expense-categories/<int:pk>/delete/", views.ExpenseCategoryDeleteView.as_view(), name="expensecategory_delete"),
]
