"""Threshold and accounting formulas from spec sections 3, 4 and 6 (USDT-M linear contracts)."""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .models import Side

ONE = Decimal(1)


def positive_hedge_price(first: Side, e0: Decimal, q0: Decimal, f0: Decimal,
                         fee_rate: Decimal, spread: Decimal) -> Decimal:
    """Price of the opposite side that leaves `spread` of the entry notional after both
    opening fees (R05, R18): minimum short price for a first long, maximum long price
    for a first short."""
    if first is Side.LONG:
        return (e0 * (ONE + spread) + f0 / q0) / (ONE - fee_rate)
    return (e0 * (ONE - spread) - f0 / q0) / (ONE + fee_rate)


def negative_hedge_price(first: Side, e0: Decimal, adverse: Decimal) -> Decimal:
    """Price after an `adverse` move against the first position, before costs (R06, Q26)."""
    return e0 * (ONE - adverse) if first is Side.LONG else e0 * (ONE + adverse)


def gross_hedge_spread(first: Side, e0: Decimal, q0: Decimal, hedge_price: Decimal) -> Decimal:
    return q0 * (hedge_price - e0) if first is Side.LONG else q0 * (e0 - hedge_price)


def net_hedge_spread(first: Side, e0: Decimal, q0: Decimal, f0: Decimal,
                     hedge_price: Decimal, fee_rate: Decimal) -> Decimal:
    """Left side of the section 3 inequality with an estimated hedge fee rate."""
    return gross_hedge_spread(first, e0, q0, hedge_price) - f0 - q0 * hedge_price * fee_rate


def round_to(value: Decimal, step: Decimal, up: bool) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return units * step


def weighted_avg(qty: Decimal, avg: Decimal, fill_qty: Decimal, fill_price: Decimal) -> Decimal:
    """E_new = (Q × E + q × p) / (Q + q), section 6."""
    return (qty * avg + fill_qty * fill_price) / (qty + fill_qty)


def side_pnl(side: Side, qty: Decimal, entry: Decimal, price: Decimal) -> Decimal:
    return qty * (price - entry) if side is Side.LONG else qty * (entry - price)
