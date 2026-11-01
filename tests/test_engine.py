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


def test_adds_follow_the_worked_example_and_move_with_the_leg_average(cycle):
    # Long 1 @ 100, short hedge 1 @ 100.5, zero fees: SHORT add at 100.5 +1%, LONG add at 100 -1%.
    hedged(cycle)
    short_add, long_add = sorted(places(cycle.on_quote(D("100.4"), D("100.41"), 10.0)), key=lambda o: o.side)[::-1]
    assert (short_add.purpose, short_add.side, short_add.position_side) == (Purpose.ADD, "SELL", Side.SHORT)
    assert short_add.price == D("101.51") and short_add.qty == D("0.1")  # 101.505 rounded away from market
    assert (long_add.side, long_add.position_side, long_add.price) == ("BUY", Side.LONG, D("99"))
    assert places(cycle.on_quote(D("103"), D("103.01"), 11.0)) == []  # one live add per leg
    exit_order = places(fill(cycle, short_add.client_id, "0.1", "101.505"))[0]
    assert (exit_order.purpose, exit_order.side, exit_order.position_side) == (Purpose.ADD_EXIT, "BUY", Side.SHORT)
    assert exit_order.price == D("100.99") and exit_order.qty == D("0.1")  # 101.505 x 0.995 = 100.997, rounded down
    assert places(cycle.on_quote(D("104"), D("104.01"), 12.0)) == []  # price rising further: nothing new
    fill(cycle, exit_order.client_id, "0.1", "100.99")
    short = cycle.books[Side.SHORT]
    assert short.qty == D("1") and short.avg.quantize(D("0.0001")) == D("100.5914")  # leg price now includes the add
    short_add_record = next(a for a in cycle.adds if a.side is Side.SHORT)
    assert short_add_record.closed and cycle.add_result(short_add_record) == D("0.0515")  # 0.1 x (101.505 - 100.99)
    nxt = places(cycle.on_quote(D("100.9"), D("100.91"), 13.0))
    assert [(o.position_side, o.price) for o in nxt] == [(Side.SHORT, D("101.60"))]  # 100.5914 x 1.01 = 101.597


def test_long_add_mirror_closes_on_the_way_up(cycle):
    hedged(cycle)
    long_add = next(o for o in places(cycle.on_quote(D("100.4"), D("100.41"), 10.0)) if o.position_side is Side.LONG)
    exit_order = places(fill(cycle, long_add.client_id, "0.1", "99"))[0]
    assert (exit_order.side, exit_order.position_side, exit_order.price) == ("SELL", Side.LONG, D("99.50"))
    fill(cycle, exit_order.client_id, "0.1", "99.5")
    assert cycle.books[Side.LONG].avg.quantize(D("0.0001")) == D("99.9091")
    nxt = [o for o in places(cycle.on_quote(D("100"), D("100.01"), 11.0)) if o.position_side is Side.LONG]
    assert nxt[0].price == D("98.91")  # 99.9091 x 0.99 = 98.91, rounded down


def test_add_exit_is_the_nearest_price_that_nets_the_target_after_fees():
    from conftest import rules
    from stair.strategy import Cycle, StrategyConfig
    maker, taker = D("0.0002"), D("0.0004")
    cycle = Cycle.new(StrategyConfig(retry_cooldown_s=0), rules(maker=str(maker), taker=str(taker)), now=0.0)
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "1", "100", D("100") * taker)
    hedge = places(cycle.on_quote(D("101"), D("101.01"), 1.0))[0]  # positive threshold, fee-adjusted
    fill(cycle, hedge.client_id, "1", hedge.price, hedge.price * taker)
    assert cycle.state is CycleState.HEDGED
    short_add = next(o for o in places(cycle.on_quote(D("100.9"), D("100.91"), 2.0)) if o.position_side is Side.SHORT)
    price, q = short_add.price, short_add.qty
    entry_fee = price * q * maker
    exit_order = places(fill(cycle, short_add.client_id, q, price, entry_fee))[0]

    def net(x):
        return q * (price - x) - entry_fee - x * q * maker

    target = D("0.005") * q * price
    assert net(exit_order.price) >= target > net(exit_order.price + cycle.rules.tick_size)


def test_partly_filled_add_is_cut_when_price_is_back_at_its_exit(cycle):
    hedged(cycle)
    short_add = next(o for o in places(cycle.on_quote(D("100.4"), D("100.41"), 10.0)) if o.position_side is Side.SHORT)
    fill(cycle, short_add.client_id, "0.04", "101.51")
    assert cancels(cycle.on_quote(D("100.9"), D("100.91"), 11.0)) == [short_add.client_id]
    exit_order = places(cycle.on_status(short_add.client_id, OrderStatus.CANCELED))[0]
    assert exit_order.qty == D("0.04") and exit_order.purpose is Purpose.ADD_EXIT


def test_close_cancels_resting_adds_and_flattens_including_an_open_add(cycle):
    hedged(cycle)
    resting = places(cycle.on_quote(D("100.4"), D("100.41"), 10.0))
    short_add = next(o for o in resting if o.position_side is Side.SHORT)
    long_add = next(o for o in resting if o.position_side is Side.LONG)
    exit_order = places(fill(cycle, short_add.client_id, "0.1", "101.51"))[0]
    out = cycle.close()
    assert sorted(cancels(out)) == sorted([long_add.client_id, exit_order.client_id]) and not places(out)
    cycle.on_status(long_add.client_id, OrderStatus.CANCELED)
    closes = places(cycle.on_status(exit_order.client_id, OrderStatus.CANCELED))
    assert {o.position_side: o.qty for o in closes} == {Side.LONG: D("1"), Side.SHORT: D("1.1")}


def negative_hedged(cycle, short_fill):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "1", "100")
    hedge = places(cycle.on_quote(D("99.6"), D("99.61"), 1.0))[0]  # negative branch
    fill(cycle, hedge.client_id, "1", short_fill)
    assert cycle.state is CycleState.HEDGED and cycle.spread_negative()


def test_negative_spread_adds_measure_from_the_other_leg(cycle):
    # Long 1 @ 100, short 1 @ 99.6: SHORT add 1% above the LONG leg, LONG add 1% below the SHORT leg.
    negative_hedged(cycle, "99.6")
    resting = {o.position_side: o for o in places(cycle.on_quote(D("99.6"), D("99.61"), 10.0))}
    assert resting[Side.SHORT].price == D("101.00") and resting[Side.LONG].price == D("98.60")  # 98.604 down
    exit_order = places(fill(cycle, resting[Side.SHORT].client_id, "0.1", "101"))[0]
    fill(cycle, exit_order.client_id, "0.1", exit_order.price)
    assert cycle.books[Side.SHORT].avg.quantize(D("0.0001")) == D("99.7273") and cycle.spread_negative()
    # The short leg moved, so the resting LONG add moves with it: 99.7273 x 0.99 = 98.73.
    out = cycle.on_quote(D("99.6"), D("99.61"), 11.0)
    assert cancels(out) == [resting[Side.LONG].client_id]
    assert [(o.position_side, o.price) for o in places(out)] == [(Side.SHORT, D("101.00"))]  # long avg unchanged
    cycle.on_status(resting[Side.LONG].client_id, OrderStatus.CANCELED)
    nxt = places(cycle.on_quote(D("99.6"), D("99.61"), 12.0))
    assert [(o.position_side, o.price) for o in nxt] == [(Side.LONG, D("98.73"))]


def test_positive_rules_return_once_the_spread_turns_positive(cycle):
    negative_hedged(cycle, "99.95")
    resting = {o.position_side: o for o in places(cycle.on_quote(D("99.9"), D("99.91"), 10.0))}
    assert resting[Side.SHORT].price == D("101.00") and resting[Side.LONG].price == D("98.95")
    exit_order = places(fill(cycle, resting[Side.SHORT].client_id, "0.1", "101"))[0]
    fill(cycle, exit_order.client_id, "0.1", exit_order.price)
    assert not cycle.spread_negative()  # short avg 110.05 / 1.1 = 100.0455 > 100
    out = cycle.on_quote(D("99.9"), D("99.91"), 11.0)
    assert cancels(out) == [resting[Side.LONG].client_id]
    assert [(o.position_side, o.price) for o in places(out)] == [(Side.SHORT, D("101.05"))]  # 100.0455 x 1.01
    cycle.on_status(resting[Side.LONG].client_id, OrderStatus.CANCELED)
    nxt = places(cycle.on_quote(D("99.9"), D("99.91"), 12.0))
    assert [(o.position_side, o.price) for o in nxt] == [(Side.LONG, D("99.00"))]  # own leg again: 100 x 0.99
    assert next(a for a in cycle.adds if a.entry_id == resting[Side.LONG].client_id).note == "moved to a new trigger"


def test_no_adds_below_exchange_minimums(cycle):
    hedged(cycle, q0="0.05")  # add of 0.005 rounds to 0 at step 0.01
    assert places(cycle.on_quote(D("100.4"), D("100.41"), 10.0)) == []
