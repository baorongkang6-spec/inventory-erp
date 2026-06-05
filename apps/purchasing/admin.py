from django.contrib import admin

from .models import PurchaseInbound, PurchaseInboundLine


class PurchaseInboundLineInline(admin.TabularInline):
    model = PurchaseInboundLine
    extra = 0
    readonly_fields = ("product", "quantity", "unit_price", "amount", "stock_move")
    can_delete = False


@admin.register(PurchaseInbound)
class PurchaseInboundAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "doc_date", "supplier", "purchase_type",
                    "total_quantity", "total_amount", "status")
    list_filter = ("company", "purchase_type", "status")
    search_fields = ("doc_no",)
    date_hierarchy = "doc_date"
    inlines = [PurchaseInboundLineInline]

    def has_add_permission(self, request):
        return False  # 入库须经录入界面过账，不在 admin 手工建
