"""采购入库路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("inbound/", views.InboundListView.as_view(), name="inbound_list"),
    path("inbound/new/", views.inbound_create, name="inbound_create"),
    path("inbound/<int:pk>/", views.InboundDetailView.as_view(), name="inbound_detail"),
    path("inbound/<int:pk>/print/", views.inbound_print, name="inbound_print"),
    path("inbound/<int:pk>/edit/", views.inbound_edit, name="inbound_edit"),
    path("inbound/<int:pk>/void/", views.inbound_void, name="inbound_void"),
]
