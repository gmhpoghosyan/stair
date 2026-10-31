from decimal import Decimal as D

from conftest import cancels, fill, places
from stair.core.models import Side
from stair.levels import Candle, LevelConfig, analyze, pivots
from test_engine import hedged


def candles(closes, highs=None, lows=None, volumes=None):
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c
        lo = lows[i] if lows else c
        v = volumes[i] if volumes else 100
        out.append(Candle(i * 60_000, D(str(c)), D(str(h)), D(str(lo)), D(str(c)), D(str(v)), i * 60_000 + 59_999))
    return out


def test_pivot_needs_left_and_right_confirmation():
    values = [D(v) for v in [1, 2, 3, 9, 3, 2, 1, 1]]
    ph = pivots(values, 2, 2, high=True)
    assert ph[5] == D(9)  # pivot at bar 3, confirmed 2 bars later
    assert [i for i, v in enumerate(ph) if v is not None] == [5]
    pl = pivots([D(v) for v in [5, 4, 1, 4, 5, 6]], 2, 2, high=False)
    assert pl[4] == D(1)


def test_levels_hold_last_pivot_and_ignore_unconfirmed():
    cfg = LevelConfig(left_bars=2, right_bars=2)
    highs = [10, 11, 15, 11, 10, 10, 12, 18, 12]  # 18 at bar 7 is not confirmed yet
    lows = [9, 8, 5, 8, 9, 9, 9, 9, 9]
    snap = analyze(candles([10] * 9, highs, lows), cfg)
    assert snap.resistance == D(15) and snap.support == D(5)
    assert snap.resistance_ms == 2 * 60_000


def test_support_break_signal_needs_volume():
    cfg = LevelConfig(left_bars=2, right_bars=2, volume_thresh=20)
    lows = [9, 8, 5, 8, 9, 9, 9, 9, 4]
    closes = [10, 10, 10, 10, 10, 10, 10, 10, 4.5]
    quiet = analyze(candles(closes, None, lows), cfg)
    assert quiet.support == D(5) and quiet.signal is None
    loud = analyze(candles(closes, None, lows, [100] * 8 + [1000]), cfg)
    assert loud.signal == "support_broken"


def test_auto_levels_follow_pivots_and_cancel_unfilled_adds(cycle):
    hedged(cycle)  # long 1 @ 100, short 1 @ 100.5
    assert cycle.sync_auto_levels(D("105"), D("95")) == []
    res = next(lv for lv in cycle.levels if lv.side is Side.SHORT)
    assert res.source == "auto" and res.price == D("105")
    adds = places(cycle.on_quote(D("100"), D("100.01"), 10.0))
    assert sorted(o.price for o in adds) == [D("95"), D("105")]
    short_add = next(o for o in adds if o.position_side is Side.SHORT)
    fill(cycle, short_add.client_id, "0.1", "105")  # filled add keeps its exit after the level moves
    long_add = next(o for o in adds if o.position_side is Side.LONG)
    out = cycle.sync_auto_levels(D("106"), D("94"))
    assert cancels(out) == [long_add.client_id]
    assert {(lv.side, lv.price) for lv in cycle.levels} == {(Side.SHORT, D("106")), (Side.LONG, D("94"))}
    assert cycle.sync_auto_levels(D("106"), D("94")) == []  # unchanged pivots do nothing
