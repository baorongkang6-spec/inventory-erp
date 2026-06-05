"""项目根路由。"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("masterdata/", include("apps.masterdata.urls")),
    path("", include("apps.accounts.urls")),
]
