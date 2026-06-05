"""期初与对账路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("opening/", views.opening_import, name="opening_import"),
    path("opening/template/<str:kind>/", views.opening_template, name="opening_template"),
]
