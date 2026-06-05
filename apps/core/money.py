"""金额/数量的小数精度与四舍五入约定（全系统统一）。

- 数量：3 位小数（适配吨/kg 等可拆分单位）
- 单价/金额：2 位小数
- 舍入：四舍五入（ROUND_HALF_UP，中国财务习惯），区别于 Python 默认的银行家舍入
"""

from decimal import ROUND_HALF_UP, Decimal

QUANTITY_DP = 3
PRICE_DP = 2
AMOUNT_DP = 2

DEFAULT_TAX_RATE = Decimal("0.13")  # SPEC §6.1 默认增值税率，可按行改

QUANTITY_QUANT = Decimal("0.001")
MONEY_QUANT = Decimal("0.01")
ZERO_QTY = Decimal("0.000")
ZERO_MONEY = Decimal("0.00")


def round_money(value) -> Decimal:
    """金额/单价四舍五入到 2 位。"""
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def round_qty(value) -> Decimal:
    """数量四舍五入到 3 位。"""
    return Decimal(value).quantize(QUANTITY_QUANT, rounding=ROUND_HALF_UP)
