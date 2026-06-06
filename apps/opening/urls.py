"""期初与对账路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.overview, name="overview_report"),
    path("account-balance/", views.account_balance, name="account_balance_report"),
    path("reconciliation/", views.reconciliation, name="reconciliation"),
    path("opening/", views.opening_import, name="opening_import"),
    path("opening/template/<str:kind>/", views.opening_template, name="opening_template"),
]
