"""资金往来路由。"""

from django.urls import path

from . import views

urlpatterns = [
    path("bank-accounts/", views.BankAccountListView.as_view(), name="bankaccount_list"),
    path("bank-accounts/new/", views.BankAccountCreateView.as_view(), name="bankaccount_create"),
    path("bank-accounts/<int:pk>/edit/", views.BankAccountUpdateView.as_view(), name="bankaccount_update"),
    path("bank-accounts/<int:pk>/delete/", views.BankAccountDeleteView.as_view(), name="bankaccount_delete"),
    # 采购发票（→应付）
    path("purchase-invoices/", views.PurchaseInvoiceListView.as_view(), name="purchase_invoice_list"),
    path("purchase-invoices/new/", views.purchase_invoice_create, name="purchase_invoice_create"),
    path("purchase-invoices/<int:pk>/", views.PurchaseInvoiceDetailView.as_view(), name="purchase_invoice_detail"),
    # 付款登记
    path("payments/", views.PaymentListView.as_view(), name="payment_list"),
    path("payments/new/", views.payment_create, name="payment_create"),
    path("payments/<int:pk>/", views.PaymentDetailView.as_view(), name="payment_detail"),
    path("payments/<int:pk>/allocate/", views.payment_allocate, name="payment_allocate"),
    # 销售发票（→应收）
    path("sales-invoices/", views.SalesInvoiceListView.as_view(), name="sales_invoice_list"),
    path("sales-invoices/new/", views.sales_invoice_create, name="sales_invoice_create"),
    path("sales-invoices/<int:pk>/", views.SalesInvoiceDetailView.as_view(), name="sales_invoice_detail"),
    path("sales-invoices/<int:pk>/edit/", views.sales_invoice_edit, name="sales_invoice_edit"),
    # 收款登记 + 应收核销
    path("receipts/", views.ReceiptListView.as_view(), name="receipt_list"),
    path("receipts/new/", views.receipt_create, name="receipt_create"),
    path("receipts/<int:pk>/", views.ReceiptDetailView.as_view(), name="receipt_detail"),
    path("receipts/<int:pk>/allocate/", views.receipt_allocate, name="receipt_allocate"),
    # 报表（M2-6）
    path("reports/bank-accounts/", views.bank_accounts_report, name="bank_accounts_report"),
    path("reports/bank-journal/", views.bank_journal_report, name="bank_journal_report"),
    path("reports/bank-journal/export/", views.bank_journal_export, name="bank_journal_export"),
    path("reports/bank-journal/import/", views.bank_journal_import, name="bank_journal_import"),
    path("reports/bank-journal/template/", views.bank_journal_template, name="bank_journal_template"),
    path("reports/bank-reconcile/", views.bank_reconcile, name="bank_reconcile"),
    path("other-cashflow/new/", views.other_cashflow_create, name="other_cashflow_create"),
    path("other-cashflow/<int:pk>/delete/", views.other_cashflow_delete, name="other_cashflow_delete"),
    path("reports/payables/", views.payables_report, name="payables_report"),
    path("reports/receivables/", views.receivables_report, name="receivables_report"),
    path("reports/sales-revenue-cost/", views.sales_revenue_cost_report, name="sales_revenue_cost_report"),
    path("reports/payable-partners/", views.payable_partners_report, name="payable_partners_report"),
    path("reports/payable-ledger/", views.payable_partner_ledger, name="payable_partner_ledger"),
    path("reports/receivable-partners/", views.receivable_partners_report, name="receivable_partners_report"),
    path("reports/receivable-ledger/", views.receivable_partner_ledger, name="receivable_partner_ledger"),
    path("reports/receivable-notes/", views.receivable_notes_report, name="receivable_notes_report"),
    path("reports/receivable-note-ledger/", views.receivable_note_ledger, name="receivable_note_ledger"),
    # 票据（M3）
    path("notes-receivable/", views.NoteReceivableListView.as_view(), name="note_receivable_list"),
    path("notes-receivable/new/", views.note_receivable_create, name="note_receivable_create"),
    path("notes-receivable/<int:pk>/settle/", views.note_receivable_settle, name="note_receivable_settle"),
    path("notes-receivable/<int:pk>/endorse/", views.note_receivable_endorse, name="note_receivable_endorse"),
    path("notes-receivable/export/", views.note_receivable_export, name="note_receivable_export"),
    path("notes-receivable/import/", views.note_receivable_import, name="note_receivable_import"),
    path("notes-payable/", views.NotePayableListView.as_view(), name="note_payable_list"),
    path("notes-payable/new/", views.note_payable_create, name="note_payable_create"),
    path("notes-payable/<int:pk>/settle/", views.note_payable_settle, name="note_payable_settle"),
    path("notes-payable/export/", views.note_payable_export, name="note_payable_export"),
    path("notes-payable/import/", views.note_payable_import, name="note_payable_import"),
    path("reports/notes-balance/", views.notes_balance_report, name="notes_balance_report"),
    path("reports/borrow/", views.borrow_report, name="borrow_report"),
]
