from django.contrib import admin

from .models import SalesOutbound, SalesOutboundLine


class SalesOutboundLineInline(admin.TabularInline):
    model = SalesOutboundLine
    extra = 0
    readonly_fields = ("product", "quantity", "unit_cost", "amount", "stock_move")
    can_delete = False


@admin.register(SalesOutbound)
class SalesOutboundAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "doc_date", "customer", "sales_type",
                    "total_quantity", "total_cost", "status")
    list_filter = ("company", "sales_type", "status")
    search_fields = ("doc_no",)
    date_hierarchy = "doc_date"
    inlines = [SalesOutboundLineInline]

    def has_add_permission(self, request):
        return False
