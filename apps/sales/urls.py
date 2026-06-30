"""销售出库路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("outbound/", views.OutboundListView.as_view(), name="outbound_list"),
    path("outbound/new/", views.outbound_create, name="outbound_create"),
    path("outbound/<int:pk>/", views.OutboundDetailView.as_view(), name="outbound_detail"),
    path("outbound/<int:pk>/print/", views.outbound_print, name="outbound_print"),
    path("outbound/<int:pk>/edit/", views.outbound_edit, name="outbound_edit"),
    path("outbound/<int:pk>/void/", views.outbound_void, name="outbound_void"),
    path("outbound/<int:pk>/delete/", views.outbound_delete, name="outbound_delete"),
]
