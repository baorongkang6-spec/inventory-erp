"""采购入库路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("inbound/", views.InboundListView.as_view(), name="inbound_list"),
    path("inbound/new/", views.inbound_create, name="inbound_create"),
    path("inbound/<int:pk>/", views.InboundDetailView.as_view(), name="inbound_detail"),
]
