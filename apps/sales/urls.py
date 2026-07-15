"""销售出库路由。"""

from django.urls import path

from . import order_views, views

urlpatterns = [
    path("orders/", order_views.OrderListView.as_view(), name="order_list"),
    path("orders/new/", order_views.order_create, name="order_create"),
    path("orders/<int:pk>/", order_views.OrderDetailView.as_view(), name="order_detail"),
    path("orders/<int:pk>/edit/", order_views.order_edit, name="order_edit"),
    path("orders/<int:pk>/ship/", order_views.order_ship, name="order_ship"),
    path("orders/<int:pk>/invoice/", order_views.order_invoice, name="order_invoice"),
    path("orders/<int:pk>/void/", order_views.order_void, name="order_void"),
    path("outbound/", views.OutboundListView.as_view(), name="outbound_list"),
    path("outbound/new/", views.outbound_create, name="outbound_create"),
    path("outbound/<int:pk>/", views.OutboundDetailView.as_view(), name="outbound_detail"),
    path("outbound/<int:pk>/print/", views.outbound_print, name="outbound_print"),
    path("outbound/<int:pk>/cost-print/", views.outbound_cost_print, name="outbound_cost_print"),
    path("outbound/<int:pk>/edit/", views.outbound_edit, name="outbound_edit"),
    path("outbound/<int:pk>/void/", views.outbound_void, name="outbound_void"),
    path("outbound/<int:pk>/delete/", views.outbound_delete, name="outbound_delete"),
]
