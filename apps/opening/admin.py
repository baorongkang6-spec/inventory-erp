from django.contrib import admin

from .models import ReconciliationLine, ReconciliationRun


class ReconciliationLineInline(admin.TabularInline):
    model = ReconciliationLine
    extra = 0
    readonly_fields = ("item_label", "system_amount", "external_amount", "diff")
    can_delete = False


@admin.register(ReconciliationRun)
class ReconciliationRunAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "category", "as_of_date", "created_by")
    list_filter = ("company", "category")
    date_hierarchy = "as_of_date"
    inlines = [ReconciliationLineInline]
