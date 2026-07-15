"""采购入库与采购订单路由。"""

from django.urls import path

from . import order_views, views

urlpatterns = [
    path("orders/", order_views.PurchaseOrderListView.as_view(), name="purchase_order_list"),
    path("orders/new/", order_views.purchase_order_create, name="purchase_order_create"),
    path("orders/backfill/", order_views.purchase_order_backfill_list, name="purchase_order_backfill_list"),
    path("orders/backfill/<int:supplier_id>/", order_views.purchase_order_backfill_supplier, name="purchase_order_backfill_supplier"),
    path("orders/progress/", order_views.purchase_order_progress, name="purchase_order_progress"),
    path("orders/<int:pk>/", order_views.PurchaseOrderDetailView.as_view(), name="purchase_order_detail"),
    path("orders/<int:pk>/edit/", order_views.purchase_order_edit, name="purchase_order_edit"),
    path("orders/<int:pk>/receive/", order_views.purchase_order_receive, name="purchase_order_receive"),
    path("orders/<int:pk>/invoice/", order_views.purchase_order_invoice, name="purchase_order_invoice"),
    path("orders/<int:pk>/void/", order_views.purchase_order_void, name="purchase_order_void"),
    path("inbound/", views.InboundListView.as_view(), name="inbound_list"),
    path("inbound/new/", views.inbound_create, name="inbound_create"),
    path("inbound/<int:pk>/", views.InboundDetailView.as_view(), name="inbound_detail"),
    path("inbound/<int:pk>/print/", views.inbound_print, name="inbound_print"),
    path("inbound/<int:pk>/edit/", views.inbound_edit, name="inbound_edit"),
    path("inbound/<int:pk>/void/", views.inbound_void, name="inbound_void"),
    path("inbound/<int:pk>/delete/", views.inbound_delete, name="inbound_delete"),
]
