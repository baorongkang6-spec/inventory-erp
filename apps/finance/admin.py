from django.contrib import admin

from .models import (
    BankAccount,
    BankJournal,
    ExpenseEntry,
    NotePayable,
    NoteReceivable,
    NoteSettlement,
    Payment,
    PaymentAllocation,
    PurchaseInvoice,
    PurchaseInvoiceLine,
    Receipt,
    ReceiptAllocation,
    SalesInvoice,
    SalesInvoiceLine,
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


class SalesInvoiceLineInline(admin.TabularInline):
    model = SalesInvoiceLine
    extra = 0
    readonly_fields = ("product", "description", "amount_untaxed", "tax_rate",
                       "tax_amount", "amount_taxed", "source_outbound_line")
    can_delete = False


@admin.register(SalesInvoice)
class SalesInvoiceAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "doc_date", "customer", "amount_taxed",
                    "settled_amount", "status")
    list_filter = ("company", "status")
    search_fields = ("doc_no", "invoice_no")
    date_hierarchy = "doc_date"
    inlines = [SalesInvoiceLineInline]

    def has_add_permission(self, request):
        return False


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "doc_date", "bank_account", "customer",
                    "amount", "settled_amount", "status")
    list_filter = ("company", "status")
    search_fields = ("doc_no",)
    date_hierarchy = "doc_date"

    def has_add_permission(self, request):
        return False


@admin.register(ReceiptAllocation)
class ReceiptAllocationAdmin(admin.ModelAdmin):
    list_display = ("receipt", "invoice", "amount", "created_at")
    search_fields = ("receipt__doc_no", "invoice__doc_no")


@admin.register(NoteReceivable)
class NoteReceivableAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "draw_date", "due_date", "customer",
                    "amount", "settled_amount", "status")
    list_filter = ("company", "status")
    search_fields = ("doc_no", "note_no")
    date_hierarchy = "draw_date"

    def has_add_permission(self, request):
        return False


@admin.register(NotePayable)
class NotePayableAdmin(admin.ModelAdmin):
    list_display = ("doc_no", "company", "draw_date", "due_date", "supplier",
                    "amount", "settled_amount", "status")
    list_filter = ("company", "status")
    search_fields = ("doc_no", "note_no")
    date_hierarchy = "draw_date"

    def has_add_permission(self, request):
        return False


@admin.register(NoteSettlement)
class NoteSettlementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "note_kind", "note_no",
                    "invoice_kind", "invoice_no", "amount", "is_endorsement")
    list_filter = ("company", "note_kind", "invoice_kind", "is_endorsement")
    search_fields = ("note_no", "invoice_no")


@admin.register(ExpenseEntry)
class ExpenseEntryAdmin(admin.ModelAdmin):
    list_display = ("date", "company", "kind", "category", "amount", "included_in_cost", "source_no")
    list_filter = ("company", "kind", "included_in_cost", "category")
    search_fields = ("source_no",)
    date_hierarchy = "date"
