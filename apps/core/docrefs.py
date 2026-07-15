"""单据来源跳转：把流水/日记账里的 (source_type, source_id) 映射到原单据详情 URL。

库存流水 StockMove 与银行日记账 BankJournal 都用 source_type/source_id 记录其来源单据；
本模块集中维护「类型 → 详情路由名」映射，避免散落在各模板/视图里。
无对应详情（期初、作废反冲、其他收支等）返回空串。
"""

from django.urls import NoReverseMatch, reverse

# source_type → URL 名（详情页接受单个 pk 参数）
SOURCE_URL_NAMES = {
    "PurchaseInbound": "inbound_detail",
    "SalesOutbound": "outbound_detail",
    "SalesOrder": "order_detail",
    "Payment": "payment_detail",
    "Receipt": "receipt_detail",
    "PurchaseInvoice": "purchase_invoice_detail",
    "SalesInvoice": "sales_invoice_detail",
}


def doc_url(source_type, source_id):
    """返回原单据详情 URL；无法定位（无类型/无id/无详情页）时返回空串。"""
    name = SOURCE_URL_NAMES.get(source_type or "")
    if not name or not source_id:
        return ""
    try:
        return reverse(name, args=[source_id])
    except (NoReverseMatch, ValueError):
        return ""


def invoice_url(invoice_kind, invoice_id):
    """票据冲销/明细里按发票类型(sales/purchase)定位发票详情。"""
    name = {"sales": "sales_invoice_detail", "purchase": "purchase_invoice_detail"}.get(invoice_kind)
    if not name or not invoice_id:
        return ""
    try:
        return reverse(name, args=[invoice_id])
    except (NoReverseMatch, ValueError):
        return ""
