from django.contrib import admin

from .models import AuditLog, Company


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "short_name", "is_related", "is_active")
    list_filter = ("is_related", "is_active")
    search_fields = ("code", "name", "short_name")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "company", "action", "target_type", "target_id", "summary")
    list_filter = ("action", "company")
    search_fields = ("summary", "target_type", "target_id")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False
