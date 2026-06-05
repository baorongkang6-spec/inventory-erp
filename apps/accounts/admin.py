from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """在默认用户管理基础上加入「数据范围」字段。角色 = groups。"""

    list_display = ("username", "display_name", "can_view_all_companies", "is_staff", "is_active")
    list_filter = ("can_view_all_companies", "is_staff", "is_active", "groups")
    filter_horizontal = ("companies", "groups", "user_permissions")

    fieldsets = DjangoUserAdmin.fieldsets + (
        ("数据范围", {"fields": ("display_name", "can_view_all_companies", "companies")}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("数据范围", {"fields": ("display_name", "can_view_all_companies", "companies")}),
    )
