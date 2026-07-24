"""商品流水台账 / 库存余额表共用的收入·发出展示口径。

作废冲正按原业务方向以负数计入（收入红字 / 发出红字），与真实 StockMove.direction
（结存累加）分离：结存仍按方向加减，报表「本期收入/发出」与台账列一致。
"""

from decimal import Decimal

from apps.core.money import ZERO_MONEY, ZERO_QTY

from .models import StockMove

Z = ZERO_MONEY
ZQ = ZERO_QTY


def is_void_reversal(move) -> bool:
    """作废产生的冲正流水（入库作废→OUT，出库作废→IN）。"""
    st = move.source_type or ""
    sn = move.source_no or ""
    return st.endswith("Void") or sn.startswith("作废")


def period_flow_cols(move):
    """本期收入/发出列贡献：(in_qty, in_amt, out_qty, out_amt)。

    - 正常入库 → 收入正数；正常出库 → 发出正数
    - 作废入库（冲正 OUT）→ 收入负数；作废出库（冲正 IN）→ 发出负数
    """
    qty, amt = move.quantity, move.amount
    is_in = move.direction == StockMove.Direction.IN
    if is_void_reversal(move):
        if is_in:
            return ZQ, Z, -qty, -amt
        return -qty, -amt, ZQ, Z
    if is_in:
        return qty, amt, ZQ, Z
    return ZQ, Z, qty, amt


def ledger_display_cols(move):
    """台账一行的收入/发出展示值（空位用 None）+ 摘要。"""
    in_qty, in_amt, out_qty, out_amt = period_flow_cols(move)
    if is_void_reversal(move):
        summary = "作废冲正"
        if in_qty:
            return in_qty, in_amt, None, None, summary
        return None, None, out_qty, out_amt, summary
    if in_qty:
        return in_qty, in_amt, None, None, move.get_direction_display()
    return None, None, out_qty, out_amt, move.get_direction_display()
