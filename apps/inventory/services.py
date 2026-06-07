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
from django.utils import timezone

from apps.core.money import ZERO_MONEY, round_money, round_qty
from apps.masterdata.models import Product

from .models import StockBalance, StockMove


class InventoryError(Exception):
    """库存业务错误基类。"""


class InsufficientStockError(InventoryError):
    """库存不足（违反不允许负库存）。"""

    def __init__(self, product: Product, available: Decimal, requested: Decimal, message: str = ""):
        self.product = product
        self.available = available
        self.requested = requested
        super().__init__(
            message or f"库存不足：{product} 现有 {available}，需出库 {requested}"
        )


def _get_balance_for_update(company, product) -> StockBalance:
    """取（或建）结存行并加行锁，避免并发过账串改。"""
    balance, _ = StockBalance.objects.select_for_update().get_or_create(
        company=company, product=product
    )
    return balance


@transaction.atomic
def post_inbound(company, product, quantity, unit_price, *, amount=None, date=None,
                 source_type="", source_id="", source_no="") -> StockMove:
    """入库过账：数量、金额累加并重算移动加权均价，返回流水记录。

    amount 可显式给定入库总成本（用于其他费用计入成本后抬高入库成本，SPEC §6.2）；
    给定时记录单价按 amount/数量 反算，保证「单价×数量=金额」一致。
    """
    quantity = round_qty(quantity)
    unit_price = round_money(unit_price)
    if quantity <= 0:
        raise InventoryError("入库数量必须大于 0")
    if amount is None:
        amount = round_money(quantity * unit_price)
    else:
        amount = round_money(amount)
        unit_price = round_money(amount / quantity) if quantity > 0 else unit_price

    bal = _get_balance_for_update(company, product)
    bal.quantity = round_qty(bal.quantity + quantity)
    bal.amount = round_money(bal.amount + amount)
    bal.avg_price = round_money(bal.amount / bal.quantity) if bal.quantity > 0 else ZERO_MONEY
    bal.save(update_fields=["quantity", "amount", "avg_price", "updated_at"])

    return StockMove.objects.create(
        company=company, product=product, direction=StockMove.Direction.IN,
        date=date or timezone.localdate(),
        quantity=quantity, unit_price=unit_price, amount=amount,
        balance_quantity=bal.quantity, balance_amount=bal.amount, balance_price=bal.avg_price,
        source_type=source_type, source_id=str(source_id), source_no=source_no,
    )


@transaction.atomic
def reverse_move(move: StockMove, *, date=None, source_type="", source_id="", source_no="") -> StockMove:
    """精确反冲一笔历史流水（用于单据作废）。

    - 反冲入库：从结存中扣回原数量与原金额；若现存数量/金额不足（货已被后续消耗）→
      抛 InsufficientStockError，拒绝作废。
    - 反冲出库：把原数量与原成本加回结存。
    生成一笔方向相反的补偿流水，金额照原值，保证数量金额式账可追溯。
    """
    bal = _get_balance_for_update(move.company, move.product)
    if move.direction == StockMove.Direction.IN:
        if move.quantity > bal.quantity or move.amount > bal.amount:
            raise InsufficientStockError(
                move.product, bal.quantity, move.quantity,
                message=(
                    f"无法反冲入库：{move.product} 该批入库的商品/成本已被后续出库消耗。"
                    f"原入库 {move.quantity} 件 / 金额 {move.amount}，"
                    f"当前结存仅 {bal.quantity} 件 / 金额 {bal.amount}。"
                    f"请先作废引用本批货的出库单，再修改或作废本入库单。"
                ),
            )
        bal.quantity = round_qty(bal.quantity - move.quantity)
        bal.amount = round_money(bal.amount - move.amount)
        new_dir = StockMove.Direction.OUT
    else:
        bal.quantity = round_qty(bal.quantity + move.quantity)
        bal.amount = round_money(bal.amount + move.amount)
        new_dir = StockMove.Direction.IN

    if bal.quantity == 0:
        bal.amount = ZERO_MONEY
        bal.avg_price = ZERO_MONEY
    else:
        bal.avg_price = round_money(bal.amount / bal.quantity)
    bal.save(update_fields=["quantity", "amount", "avg_price", "updated_at"])

    return StockMove.objects.create(
        company=move.company, product=move.product, direction=new_dir,
        date=date or timezone.localdate(),
        quantity=move.quantity, unit_price=move.unit_price, amount=move.amount,
        balance_quantity=bal.quantity, balance_amount=bal.amount, balance_price=bal.avg_price,
        source_type=source_type, source_id=str(source_id), source_no=source_no,
    )


@transaction.atomic
def post_outbound(company, product, quantity, *, date=None,
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
        date=date or timezone.localdate(),
        quantity=quantity, unit_price=unit_price, amount=cost,
        balance_quantity=bal.quantity, balance_amount=bal.amount, balance_price=bal.avg_price,
        source_type=source_type, source_id=str(source_id), source_no=source_no,
    )
