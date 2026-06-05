"""资金往来路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("bank-accounts/", views.BankAccountListView.as_view(), name="bankaccount_list"),
    path("bank-accounts/new/", views.BankAccountCreateView.as_view(), name="bankaccount_create"),
    path("bank-accounts/<int:pk>/edit/", views.BankAccountUpdateView.as_view(), name="bankaccount_update"),
    path("bank-accounts/<int:pk>/delete/", views.BankAccountDeleteView.as_view(), name="bankaccount_delete"),
    # 采购发票（→应付）
    path("purchase-invoices/", views.PurchaseInvoiceListView.as_view(), name="purchase_invoice_list"),
    path("purchase-invoices/new/", views.purchase_invoice_create, name="purchase_invoice_create"),
    path("purchase-invoices/<int:pk>/", views.PurchaseInvoiceDetailView.as_view(), name="purchase_invoice_detail"),
]
