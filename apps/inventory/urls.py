"""库存报表路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("stock/", views.StockReportView.as_view(), name="stock_report"),
    path("stock-balance/", views.StockProductsReportView.as_view(), name="stock_products_report"),
    path("ledger/", views.StockLedgerView.as_view(), name="stock_ledger"),
]
