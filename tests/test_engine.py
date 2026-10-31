from decimal import Decimal as D

import pytest

from conftest import cancels, fill, places
from stair.core.models import AddState, CycleState, OrderStatus, Purpose, Side
from stair.strategy import EngineError


def hedged(cycle, first=Side.LONG, e0="100", q0="1"):
    entry = places(cycle.enter(first, D(q0), D(e0), post_only=False))[0]
    fill(cycle, entry.client_id, q0, e0)
    hedge_price = "100.5" if first is Side.LONG else "99.5"
    intents = cycle.on_quote(D(hedge_price), D(hedge_price) + D("0.01"), 1.0) if first is Side.LONG \
        else cycle.on_quote(D(hedge_price) - D("0.01"), D(hedge_price), 1.0)
    hedge = places(intents)[0]
    fill(cycle, hedge.client_id, q0, hedge.price)
    assert cycle.state is CycleState.HEDGED
    return entry, hedge


def test_entry_fill_leads_to_single_position(cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=True))[0]
    assert entry.tif == "GTX" and entry.side == "BUY" and entry.position_side is Side.LONG
    assert cycle.state is CycleState.WAIT_ENTRY
    fill(cycle, entry.client_id, "1", "100")
    assert cycle.state is CycleState.SINGLE_POSITION
    assert cycle.positive_threshold() == D("100.5") and cycle.negative_threshold() == D("99.7")


def test_t22_t02_partial_entry_hedges_only_filled_qty(cycle):
    entry = places(cycle.enter(Side.LONG, D("10"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "4", "100")
    assert cycle.state is CycleState.ENTRY_PARTIAL and cycle.q0 == D("4")
    out = cycle.on_quote(D("100.6"), D("100.61"), 1.0)  # positive threshold reached
    assert cancels(out) == [entry.client_id] and not places(out)  # Q30 default: cancel remainder first
    assert cycle.on_quote(D("100.6"), D("100.61"), 2.0) == []      # no duplicate cancel
    hedge = places(cycle.on_status(entry.client_id, OrderStatus.CANCELED))[0]
    assert hedge.qty == D("4") and hedge.side == "SELL" and hedge.position_side is Side.SHORT
    assert hedge.price == D("100.5") and cycle.state is CycleState.HEDGE_PENDING


def test_t03_first_threshold_wins_and_second_is_blocked(cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "1", "100")
    hedge = places(cycle.on_quote(D("99.6"), D("99.61"), 1.0))[0]  # negative branch first
    assert cycle.branch == "negative" and hedge.order_type == "MARKET"
    assert cycle.on_quote(D("101"), D("101.01"), 2.0) == []  # positive crossing creates nothing
    assert cycle.on_quote(D("99"), D("99.01"), 3.0) == []
    assert len(cycle.hedge_ids) == 1


def test_t23_partial_hedge_waits_for_remainder(cycle):
    entry = places(cycle.enter(Side.SHORT, D("4"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "4", "100")
    hedge = places(cycle.on_quote(D("100.29"), D("100.3"), 1.0))[0]  # short first: ask >= 100.3
    assert hedge.side == "BUY" and hedge.position_side is Side.LONG and hedge.qty == D("4")
    fill(cycle, hedge.client_id, "1.5", "100.3")
    assert cycle.state is CycleState.HEDGE_PENDING and cycle.delta == D("-2.5")
    assert cycle.on_quote(D("101"), D("101.01"), 2.0) == []
    fill(cycle, hedge.client_id, "2.5", "100.3")
    assert cycle.state is CycleState.HEDGED and cycle.delta == 0


def test_hedge_rejection_rearms_then_pauses(cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "1", "100")
    for attempt in range(cycle.cfg.hedge_max_attempts):
        hedge = places(cycle.on_quote(D("100.6"), D("100.61"), float(attempt + 1)))[0]
        cycle.on_status(hedge.client_id, OrderStatus.REJECTED, "margin")
    assert cycle.state is CycleState.PAUSED and "hedge incomplete" in cycle.note
    retry = places(cycle.resume())[0]
    assert retry.purpose is Purpose.HEDGE and cycle.state is CycleState.HEDGE_PENDING


def test_t10_duplicate_fill_is_ignored(cycle):
    from stair.core.models import Fill
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    f = Fill(424242, entry.client_id, D("0.5"), D("100"), D("0"))
    cycle.on_fill(f)
    cycle.on_fill(f)
    assert cycle.q0 == D("0.5") and cycle.books[Side.LONG].qty == D("0.5")


def test_t04_t05_short_add_round_trip_and_rearm(cycle):
    hedged(cycle)
    level = cycle.add_level(Side.SHORT, D("105"), D("104.5"))
    add_order = places(cycle.on_quote(D("103"), D("103.01"), 10.0))[0]
    assert (add_order.side, add_order.position_side, add_order.tif) == ("SELL", Side.SHORT, "GTX")
    assert add_order.qty == D("0.1") and add_order.price == D("105")
    # Sitting at the level does not create more adds (T05).
    assert places(cycle.on_quote(D("105"), D("105.01"), 11.0)) == []
    exit_order = places(fill(cycle, add_order.client_id, "0.1", "105"))[0]
    assert (exit_order.side, exit_order.position_side, exit_order.price) == ("BUY", Side.SHORT, D("104.5"))
    assert cycle.books[Side.SHORT].qty == D("1.1")
    fill(cycle, exit_order.client_id, "0.1", "104.5")
    add = cycle.adds[add_order.add_id]
    assert add.state is AddState.CLOSED and cycle.books[Side.SHORT].qty == D("1")  # back to base (T04)
    assert cycle.add_result(add) == D("0.05") and level.add_id is None
    again = places(cycle.on_quote(D("104.5"), D("104.51"), 12.0))  # level re-armed (R11)
    assert len(again) == 1 and again[0].price == D("105")


def test_t18_averages_after_two_adds(cycle):
    hedged(cycle)  # long 1 @ 100, short 1 @ 100.5
    cycle.add_level(Side.LONG, D("90"), D("91"))
    cycle.add_level(Side.SHORT, D("110"), D("109"))
    long_add, short_add = places(cycle.on_quote(D("100"), D("100.01"), 10.0))
    fill(cycle, places(fill(cycle, long_add.client_id, "0.1", "90"))[0].client_id, "0.1", "91")
    fill(cycle, places(fill(cycle, short_add.client_id, "0.1", "110"))[0].client_id, "0.1", "109")
    long, short = cycle.books[Side.LONG], cycle.books[Side.SHORT]
    assert long.qty == short.qty == D("1")
    assert long.avg.quantize(D("0.000001")) == D("99.090909")
    assert short.avg.quantize(D("0.000001")) == D("101.363636")
    assert (long.realized + short.realized).quantize(D("0.000001")) == D("-1.572727")
    # Open pair PnL plus realized = 0.7 before costs, with no double counting.
    assert (short.avg - long.avg + long.realized + short.realized).quantize(D("0.000001")) == D("0.7")


def test_t20_average_filter_blocks_worsening_adds(cycle):
    hedged(cycle)  # EL 100, ES 100.5
    cycle.add_level(Side.LONG, D("100.2"))   # above EL: would worsen the long average
    cycle.add_level(Side.SHORT, D("100.4"))  # below ES: would worsen the short average
    assert places(cycle.on_quote(D("100.3"), D("100.31"), 10.0)) == []


def test_partial_add_entry_is_cut_when_exit_reached(cycle):
    hedged(cycle)
    cycle.add_level(Side.SHORT, D("105"), D("104.5"))
    add_order = places(cycle.on_quote(D("103"), D("103.01"), 10.0))[0]
    fill(cycle, add_order.client_id, "0.04", "105")
    assert cancels(cycle.on_quote(D("104.4"), D("104.5"), 11.0)) == [add_order.client_id]
    exit_order = places(cycle.on_status(add_order.client_id, OrderStatus.CANCELED))[0]
    assert exit_order.qty == D("0.04")


def test_pause_cancels_unfilled_adds_and_blocks_new_ones(cycle):
    hedged(cycle)
    cycle.add_level(Side.SHORT, D("105"))
    add_order = places(cycle.on_quote(D("103"), D("103.01"), 10.0))[0]
    assert cancels(cycle.pause()) == [add_order.client_id]
    cycle.on_status(add_order.client_id, OrderStatus.CANCELED)
    assert places(cycle.on_quote(D("103"), D("103.01"), 11.0)) == []
    cycle.resume()
    assert len(places(cycle.on_quote(D("103"), D("103.01"), 12.0))) == 1


def test_pause_is_refused_before_hedge(cycle):
    places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))
    with pytest.raises(EngineError):
        cycle.pause()


def test_t11_close_cancels_then_flattens_both_sides(cycle):
    hedged(cycle)
    cycle.add_level(Side.SHORT, D("105"))
    add_order = places(cycle.on_quote(D("103"), D("103.01"), 10.0))[0]
    fill(cycle, add_order.client_id, "0.05", "105")
    out = cycle.close()
    assert cancels(out) == [add_order.client_id] and not places(out)
    closes = places(cycle.on_status(add_order.client_id, OrderStatus.CANCELED))
    by_side = {o.position_side: o for o in closes}
    assert by_side[Side.LONG].qty == D("1") and by_side[Side.LONG].side == "SELL"
    assert by_side[Side.SHORT].qty == D("1.05") and by_side[Side.SHORT].side == "BUY"
    assert all(o.order_type == "MARKET" for o in closes)
    fill(cycle, by_side[Side.LONG].client_id, "1", "103")
    assert cycle.state is CycleState.CLOSING  # one side still open
    fill(cycle, by_side[Side.SHORT].client_id, "1.05", "103")
    assert cycle.state is CycleState.FINISHED
    assert not [o for o in cycle.orders.values() if not o.is_terminal]


def test_unfilled_entry_finishes_cycle(cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=True))[0]
    cycle.on_status(entry.client_id, OrderStatus.REJECTED, "GTX would take")
    assert cycle.state is CycleState.FINISHED
