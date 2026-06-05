from django.contrib import admin

from .models import (
    BankAccount,
    BankJournal,
    Payment,
    PaymentAllocation,
    PurchaseInvoice,
    PurchaseInvoiceLine,
)


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "bank_name", "account_no", "opening_balance", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "bank_name", "account_no")


class PurchaseInvoiceLineInline(admin.TabularInline):
    model = PurchaseInvoiceLine
    extra = 0
    readonly_fields = ("product", "description", "amount_untaxed", "tax_rate",
                       "tax_amount", "amount_taxed", "source_inbound_line")
    can_delete = False


@admin.register(PurchaseInvoice)
class PurchaseInvoiceAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "doc_date", "supplier", "amount_taxed",
                    "settled_amount", "status")
    list_filter = ("company", "status")
    search_fields = ("doc_no", "invoice_no")
    date_hierarchy = "doc_date"
    inlines = [PurchaseInvoiceLineInline]

    def has_add_permission(self, request):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "doc_date", "bank_account", "supplier",
                    "amount", "settled_amount", "status")
    list_filter = ("company", "status")
    search_fields = ("doc_no",)
    date_hierarchy = "doc_date"

    def has_add_permission(self, request):
        return False


@admin.register(PaymentAllocation)
class PaymentAllocationAdmin(admin.ModelAdmin):
    list_display = ("payment", "invoice", "amount", "created_at")
    search_fields = ("payment__doc_no", "invoice__doc_no")


@admin.register(BankJournal)
class BankJournalAdmin(admin.ModelAdmin):
    list_display = ("date", "company", "bank_account", "direction", "amount",
                    "counterparty", "summary", "source_no", "is_imported")
    list_filter = ("company", "bank_account", "direction", "is_imported")
    search_fields = ("summary", "counterparty", "source_no")
    date_hierarchy = "date"
