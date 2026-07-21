"""库存流水结存快照重算（修改/删除中间流水后保持一致）。"""

from apps.core.money import ZERO_MONEY, ZERO_QTY, round_money, round_qty

from .models import StockBalance, StockMove


def normalize_balance_qty_amount(bal_qty, bal_amount):
    """数量归零时金额一并清零（与过账服务、报表口径一致）。"""
    if bal_qty == 0:
        return ZERO_QTY, ZERO_MONEY
    return bal_qty, bal_amount


def _step_running(bal_qty, bal_amount, move):
    """按方向累加结存；数量归零时金额一并清零（与过账服务一致）。"""
    is_in = move.direction == StockMove.Direction.IN
    if is_in:
        bal_qty = round_qty(bal_qty + move.quantity)
        bal_amount = round_money(bal_amount + move.amount)
    else:
        bal_qty = round_qty(bal_qty - move.quantity)
        bal_amount = round_money(bal_amount - move.amount)
    if bal_qty == 0:
        return ZERO_QTY, ZERO_MONEY, ZERO_MONEY
    return bal_qty, bal_amount, round_money(bal_amount / bal_qty)


def rebalance_product(company, product) -> None:
    """按时间顺序重算某商品全部流水的结存快照，并同步 StockBalance。"""
    bal_qty, bal_amount = ZERO_QTY, ZERO_MONEY
    moves = StockMove.objects.filter(company=company, product=product).order_by("created_at", "id")
    for m in moves:
        bal_qty, bal_amount, bal_price = _step_running(bal_qty, bal_amount, m)
        if (m.balance_quantity != bal_qty or m.balance_amount != bal_amount
                or m.balance_price != bal_price):
            m.balance_quantity = bal_qty
            m.balance_amount = bal_amount
            m.balance_price = bal_price
            m.save(update_fields=["balance_quantity", "balance_amount", "balance_price"])

    bal, _ = StockBalance.objects.get_or_create(company=company, product=product)
    if bal_qty == 0:
        avg = ZERO_MONEY
    else:
        avg = round_money(bal_amount / bal_qty)
    bal.quantity = bal_qty
    bal.amount = bal_amount
    bal.avg_price = avg
    bal.save(update_fields=["quantity", "amount", "avg_price", "updated_at"])


def rebalance_products(company, product_ids) -> None:
    from apps.masterdata.models import Product
    for pid in product_ids:
        product = Product.objects.filter(pk=pid).first()
        if product:
            rebalance_product(company, product)
