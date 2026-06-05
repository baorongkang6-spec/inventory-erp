"""库存过账服务：移动加权平均（SPEC §4 / B1）。

约定：
- (数量, 金额) 为权威值，均价 = round(金额/数量, 2) 派生（单价 2 位，四舍五入）。
- 入库：数量、金额累加，重算均价。
- 出库：按当前均价结转成本；**不允许负库存**（不足即报错，调用方事务回滚）。
- 出库到清零：成本 = 剩余全部金额，使结存金额精确归零（消除舍入残值）。

所有写操作须在数据库事务内调用（项目已开 ATOMIC_REQUESTS）。
"""

from decimal import Decimal

from django.db import transaction

from apps.core.money import ZERO_MONEY, round_money, round_qty
from apps.masterdata.models import Product

from .models import StockBalance, StockMove


class InventoryError(Exception):
    """库存业务错误基类。"""


class InsufficientStockError(InventoryError):
    """库存不足（违反不允许负库存）。"""

    def __init__(self, product: Product, available: Decimal, requested: Decimal):
        self.product = product
        self.available = available
        self.requested = requested
        super().__init__(
            f"库存不足：{product} 现有 {available}，需出库 {requested}"
        )


def _get_balance_for_update(company, product) -> StockBalance:
    """取（或建）结存行并加行锁，避免并发过账串改。"""
    balance, _ = StockBalance.objects.select_for_update().get_or_create(
        company=company, product=product
    )
    return balance


@transaction.atomic
def post_inbound(company, product, quantity, unit_price, *,
                 source_type="", source_id="", source_no="") -> StockMove:
    """入库过账：数量、金额累加并重算移动加权均价，返回流水记录。"""
    quantity = round_qty(quantity)
    unit_price = round_money(unit_price)
    if quantity <= 0:
        raise InventoryError("入库数量必须大于 0")
    amount = round_money(quantity * unit_price)

    bal = _get_balance_for_update(company, product)
    bal.quantity = round_qty(bal.quantity + quantity)
    bal.amount = round_money(bal.amount + amount)
    bal.avg_price = round_money(bal.amount / bal.quantity) if bal.quantity > 0 else ZERO_MONEY
    bal.save(update_fields=["quantity", "amount", "avg_price", "updated_at"])

    return StockMove.objects.create(
        company=company, product=product, direction=StockMove.Direction.IN,
        quantity=quantity, unit_price=unit_price, amount=amount,
        balance_quantity=bal.quantity, balance_amount=bal.amount, balance_price=bal.avg_price,
        source_type=source_type, source_id=str(source_id), source_no=source_no,
    )


@transaction.atomic
def post_outbound(company, product, quantity, *,
                  source_type="", source_id="", source_no="") -> StockMove:
    """出库过账：按当前移动加权均价结转成本。库存不足抛 InsufficientStockError。"""
    quantity = round_qty(quantity)
    if quantity <= 0:
        raise InventoryError("出库数量必须大于 0")

    bal = _get_balance_for_update(company, product)
    if quantity > bal.quantity:
        raise InsufficientStockError(product, bal.quantity, quantity)

    if quantity == bal.quantity:
        # 全部出清：成本 = 剩余金额，结存精确归零
        unit_price = bal.avg_price
        cost = bal.amount
    else:
        unit_price = round_money(bal.amount / bal.quantity)
        cost = round_money(quantity * unit_price)

    bal.quantity = round_qty(bal.quantity - quantity)
    bal.amount = round_money(bal.amount - cost)
    if bal.quantity == 0:
        bal.amount = ZERO_MONEY
        bal.avg_price = ZERO_MONEY
    else:
        bal.avg_price = round_money(bal.amount / bal.quantity)
    bal.save(update_fields=["quantity", "amount", "avg_price", "updated_at"])

    return StockMove.objects.create(
        company=company, product=product, direction=StockMove.Direction.OUT,
        quantity=quantity, unit_price=unit_price, amount=cost,
        balance_quantity=bal.quantity, balance_amount=bal.amount, balance_price=bal.avg_price,
        source_type=source_type, source_id=str(source_id), source_no=source_no,
    )
