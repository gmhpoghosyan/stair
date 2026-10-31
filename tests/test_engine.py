from decimal import Decimal as D


from conftest import cancels, fill, places
from stair.core.models import CycleState, OrderStatus, Purpose, Side


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


def test_t11_close_flattens_both_sides_at_market(cycle):
    hedged(cycle)
    closes = places(cycle.close())
    by_side = {o.position_side: o for o in closes}
    assert by_side[Side.LONG].qty == D("1") and by_side[Side.LONG].side == "SELL"
    assert by_side[Side.SHORT].qty == D("1") and by_side[Side.SHORT].side == "BUY"
    assert all(o.order_type == "MARKET" for o in closes)
    fill(cycle, by_side[Side.LONG].client_id, "1", "103")
    assert cycle.state is CycleState.CLOSING  # one side still open
    fill(cycle, by_side[Side.SHORT].client_id, "1", "103")
    assert cycle.state is CycleState.FINISHED
    assert not [o for o in cycle.orders.values() if not o.is_terminal]


def test_close_cancels_working_entry_before_flattening(cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "0.4", "100")
    out = cycle.close()
    assert cancels(out) == [entry.client_id] and not places(out)  # final fills first, then size the close
    closes = places(cycle.on_status(entry.client_id, OrderStatus.CANCELED))
    assert [(o.position_side, o.qty, o.order_type) for o in closes] == [(Side.LONG, D("0.4"), "MARKET")]


def test_unfilled_entry_finishes_cycle(cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=True))[0]
    cycle.on_status(entry.client_id, OrderStatus.REJECTED, "GTX would take")
    assert cycle.state is CycleState.FINISHED
