"""认证与首页路由。登录/登出用 Django 内置视图。"""

from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("company/switch/", views.switch_company, name="switch_company"),
]
