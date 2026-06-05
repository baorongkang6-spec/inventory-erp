from django.contrib import admin

from .models import StockBalance, StockMove


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    list_display = ("company", "product", "quantity", "amount", "avg_price")
    list_filter = ("company",)
    search_fields = ("product__code", "product__name")

    def has_add_permission(self, request):
        return False  # 结存由过账维护，不手工增删


@admin.register(StockMove)
class StockMoveAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "product", "direction", "quantity",
                    "unit_price", "amount", "balance_quantity", "balance_amount", "source_no")
    list_filter = ("company", "direction")
    search_fields = ("product__code", "product__name", "source_no")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in StockMove._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
