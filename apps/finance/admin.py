from django.contrib import admin

from .models import BankAccount


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "bank_name", "account_no", "opening_balance", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "bank_name", "account_no")
