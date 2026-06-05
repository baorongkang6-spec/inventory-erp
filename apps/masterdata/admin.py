from django.contrib import admin

from .models import Customer, Product, Supplier


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "spec", "unit", "company", "default_tax_rate", "is_active")
    list_filter = ("company", "is_active", "category")
    search_fields = ("code", "name", "spec")


class PartnerAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "contact", "phone", "company", "related_company", "is_active")
    list_filter = ("company", "is_active", "related_company")
    search_fields = ("code", "name", "contact", "phone", "tax_no")


@admin.register(Customer)
class CustomerAdmin(PartnerAdmin):
    pass


@admin.register(Supplier)
class SupplierAdmin(PartnerAdmin):
    pass
