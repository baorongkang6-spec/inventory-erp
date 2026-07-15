from django.contrib import admin

from .models import BusinessPartner, Customer, ExpenseCategory, Product, Supplier


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "spec", "unit", "company", "default_tax_rate", "is_active")
    list_filter = ("company", "is_active", "category")
    search_fields = ("code", "name", "spec")


@admin.register(BusinessPartner)
class BusinessPartnerAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_customer", "is_supplier", "contact", "phone",
                    "company", "related_company", "is_active")
    list_filter = ("company", "is_customer", "is_supplier", "is_active", "related_company")
    search_fields = ("code", "name", "contact", "phone", "tax_no")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "contact", "phone", "company", "related_company", "is_active")
    list_filter = ("company", "is_active", "related_company")
    search_fields = ("code", "name", "contact", "phone", "tax_no")


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "contact", "phone", "company", "related_company", "is_active")
    list_filter = ("company", "is_active", "related_company")
    search_fields = ("code", "name", "contact", "phone", "tax_no")


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "include_in_cost", "is_active")
    list_filter = ("company", "include_in_cost", "is_active")
    search_fields = ("name",)
