"""期初与对账路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("overview/", views.overview, name="overview_report"),
    path("query/", views.query_center, name="query_center"),
    path("account-balance/", views.account_balance, name="account_balance_report"),
    path("reconciliation/", views.reconciliation, name="reconciliation"),
    path("reconciliation/history/", views.reconciliation_history, name="reconciliation_history"),
    path("reconciliation/<int:pk>/", views.reconciliation_detail, name="reconciliation_detail"),
    path("opening/", views.opening_import, name="opening_import"),
    path("opening/clear/", views.opening_clear, name="opening_clear"),
    path("opening/lock/", views.opening_lock, name="opening_lock"),
    path("opening/unlock/", views.opening_unlock, name="opening_unlock"),
    path("opening/template/<str:kind>/", views.opening_template, name="opening_template"),
]
