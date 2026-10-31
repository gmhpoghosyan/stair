from decimal import Decimal as D

from stair.core import pricing
from stair.core.models import Side

Q = D("0.000001")


def test_t01_zero_fee_thresholds():
    e0, q0, h, a = D("101"), D("1"), D("0.005"), D("0.003")
    assert pricing.positive_hedge_price(Side.LONG, e0, q0, D(0), D(0), h) == D("101.505")
    assert pricing.negative_hedge_price(Side.LONG, e0, a) == D("100.697")
    assert pricing.positive_hedge_price(Side.SHORT, e0, q0, D(0), D(0), h) == D("100.495")
    assert pricing.negative_hedge_price(Side.SHORT, e0, a) == D("101.303")


def test_t16_fee_adjusted_targets_leave_exact_net_spread():
    e0, q0, fee = D("100"), D("1"), D("0.0002")
    f0 = e0 * q0 * fee
    short = pricing.positive_hedge_price(Side.LONG, e0, q0, f0, fee, D("0.005"))
    long = pricing.positive_hedge_price(Side.SHORT, e0, q0, f0, fee, D("0.005"))
    assert short.quantize(Q) == D("100.540108")
    assert long.quantize(Q) == D("99.460108")
    for first, price in ((Side.LONG, short), (Side.SHORT, long)):
        assert pricing.net_hedge_spread(first, e0, q0, f0, price, fee).quantize(Q) == D("0.5")
    # Spec cross-check: a short at exactly 100.5 nets only 0.4599.
    assert pricing.net_hedge_spread(Side.LONG, e0, q0, f0, D("100.5"), fee).quantize(D("0.0001")) == D("0.4599")


def test_t17_rounding_to_tick_keeps_inequality():
    e0, q0, fee, tick = D("100"), D("1"), D("0.0004"), D("0.01")
    f0 = e0 * q0 * D("0.0002")
    target = q0 * e0 * D("0.005")
    short = pricing.round_to(pricing.positive_hedge_price(Side.LONG, e0, q0, f0, fee, D("0.005")), tick, up=True)
    long = pricing.round_to(pricing.positive_hedge_price(Side.SHORT, e0, q0, f0, fee, D("0.005")), tick, up=False)
    assert pricing.net_hedge_spread(Side.LONG, e0, q0, f0, short, fee) >= target
    assert pricing.net_hedge_spread(Side.SHORT, e0, q0, f0, long, fee) >= target


def test_weighted_average_of_partial_fills():
    """E_new = (Q x E + q x p) / (Q + q): a 0.4 fill at 100 then 0.6 at 101 averages 100.6."""
    assert pricing.weighted_avg(D("0.4"), D("100"), D("0.6"), D("101")) == D("100.6")
    e_new = pricing.weighted_avg(D("1"), D("100"), D("0.1"), D("90"))
    assert e_new.quantize(Q) == D("99.090909")
