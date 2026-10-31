"""Exchange params, storage and the runner wired to a simulated gateway."""
from decimal import Decimal as D

import pytest

from conftest import fill, places
from stair.core.models import CycleState, Order, OrderStatus, Purpose, Side
from stair.exchange.binance import BinanceFutures, order_params
from stair.app.reload import SourceWatcher
from stair.exchange import UnknownResult
from stair.execution import Runner
from stair.storage import Store
from stair.strategy import StrategyConfig


def test_t07_hedge_orders_carry_position_side_and_no_reduce_only():
    reduce_short = Order("st-x-C1", Purpose.CLOSE, Side.SHORT, False, D("0.10"), price=D("104.50"), tif="GTC")
    params = order_params("SOLUSDT", reduce_short)
    assert params["side"] == "BUY" and params["positionSide"] == "SHORT"
    assert "reduceOnly" not in params
    assert params["quantity"] == "0.1" and params["price"] == "104.5" and params["timeInForce"] == "GTC"
    market = order_params("SOLUSDT", Order("st-x-C1", Purpose.CLOSE, Side.LONG, False, D("1"), order_type="MARKET"))
    assert market["side"] == "SELL" and "price" not in market and "timeInForce" not in market


def test_signature_matches_binance_documented_example():
    gw = BinanceFutures("key", "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j")
    signed = gw.sign({"symbol": "LTCBTC", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": 1,
                      "price": "0.1", "recvWindow": 5000, "timestamp": 1499827319559})
    assert signed.endswith("signature=c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")


def test_snapshot_round_trip(tmp_path, cycle):
    entry = places(cycle.enter(Side.LONG, D("1"), D("100"), post_only=False))[0]
    fill(cycle, entry.client_id, "1", "100", "0.02")
    store = Store(tmp_path / "t.sqlite")
    store.save_cycle(cycle)
    loaded = store.load_cycle()
    assert loaded == cycle and isinstance(loaded.books[Side.LONG].avg, D)
    assert loaded.positive_threshold() == cycle.positive_threshold()


@pytest.fixture
def runner(tmp_path, gateway):
    r = Runner(gateway, Store(tmp_path / "r.sqlite"), StrategyConfig(retry_cooldown_s=0), reconcile_every_s=0)
    r.preflight()
    return r


def test_runner_full_flow_on_simulated_exchange(runner, gateway):
    runner.store.enqueue("enter", {"side": "long", "qty": "1", "price": "100"})
    runner.tick(1.0)
    cycle = runner.cycle
    entry_id = cycle.entry_id
    gateway.execute(entry_id, "1", "100", fee="0.02")
    runner.tick(2.0)
    assert cycle.state is CycleState.SINGLE_POSITION
    gateway.book.bid, gateway.book.ask = D("100.6"), D("100.61")
    runner.tick(3.0)
    hedge_id = cycle.hedge_ids[0]
    assert cycle.state is CycleState.HEDGE_PENDING
    gateway.execute(hedge_id, "1", "100.6", fee="0.04")
    runner.tick(4.0)
    assert cycle.state is CycleState.HEDGED and cycle.recovery is None
    assert runner.store.load_cycle().state is CycleState.HEDGED  # persisted
    runner.tick(5.0)  # replayed trades are not double counted (T10)
    assert cycle.books[Side.LONG].qty == cycle.books[Side.SHORT].qty == D("1")
    runner.store.enqueue("close", {})
    runner.tick(6.0)
    for cid in [c for c in gateway.orders if "-C" in c]:
        gateway.execute(cid, gateway.orders[cid]["qty"], "100.6")
    runner.tick(7.0)
    assert runner.cycle is None and runner.store.last_finished_cycle().state is CycleState.FINISHED


def test_unknown_place_result_is_queried_not_resent(runner, gateway):
    gateway.fail_next_place_unknown = True
    runner.store.enqueue("enter", {"side": "long", "qty": "1", "price": "100"})
    runner.tick(1.0)  # the same tick's sync finds the order by client id
    runner.tick(2.0)
    entry = runner.cycle.entry
    assert entry.exchange_id is not None and gateway.place_calls == [entry.client_id]


def test_foreign_position_triggers_recovery(runner, gateway):
    runner.store.enqueue("enter", {"side": "long", "qty": "1", "price": "100"})
    runner.tick(1.0)
    cycle = runner.cycle
    gateway.execute(cycle.entry_id, "1", "100")
    runner.tick(2.0)
    rogue = Order("manual-1", Purpose.ENTRY, Side.LONG, True, D("2"), price=D("100"), tif="GTC")
    gateway.place("SOLUSDT", rogue)
    gateway.execute("manual-1", "2", "100")
    runner.tick(3.0)
    runner.tick(4.0)
    assert cycle.recovery and "LONG 3" in cycle.recovery
    gateway.book.bid = D("101")
    runner.tick(5.0)
    assert not cycle.hedge_ids  # price-driven actions halted while in RECOVERY


def test_enter_refused_when_symbol_not_flat(runner, gateway):
    gateway.place("SOLUSDT", Order("manual-2", Purpose.ENTRY, Side.SHORT, True, D("1"), price=D("1"), tif="GTC"))
    gateway.execute("manual-2", "1", "100")
    cid = runner.store.enqueue("enter", {"side": "long", "qty": "1", "price": "100"})
    runner.tick(1.0)
    assert runner.store.command_result(cid)[0] == "error" and runner.cycle is None


def _entered(runner, gateway, qty="1"):
    runner.store.enqueue("enter", {"side": "long", "qty": qty, "price": "100"})
    runner.tick(1.0)
    return runner.cycle


def test_quiet_unhedged_tick_reads_only_the_book(runner, gateway):
    cycle = _entered(runner, gateway)
    gateway.execute(cycle.entry_id, "1", "100")
    runner.tick(2.0)
    assert cycle.state is CycleState.SINGLE_POSITION
    calls = []
    for name in ("open_orders", "query", "trades", "positions"):
        setattr(gateway, name, lambda *a, _n=name, _f=getattr(gateway, name), **k: calls.append(_n) or _f(*a, **k))
    quote_args = []
    gateway.quote = lambda symbol, **k: quote_args.append(k) or gateway.book
    runner.reconcile_every_s = 1e9
    runner.tick(3.0)
    assert calls == [] and quote_args == [{"last": False, "mark": False}]


def test_market_hedge_filled_in_place_response_is_hedged_same_tick(runner, gateway):
    cycle = _entered(runner, gateway)
    gateway.execute(cycle.entry_id, "1", "100")
    runner.tick(2.0)
    place = gateway.place

    def place_and_fill(symbol, order):
        place(symbol, order)
        if order.order_type == "MARKET":
            gateway.execute(order.client_id, order.qty, "99.6")
        return gateway._report(order.client_id)

    gateway.place = place_and_fill
    gateway.book.bid, gateway.book.ask = D("99.6"), D("99.61")  # negative branch, MARKET hedge
    runner.tick(3.0)
    assert cycle.state is CycleState.HEDGED and cycle.hedge_filled == D("1")


def test_q30_cancel_with_unseen_fill_hedges_same_tick(runner, gateway):
    cycle = _entered(runner, gateway, qty="10")
    gateway.execute(cycle.entry_id, "4", "100")
    runner.tick(2.0)
    assert cycle.state is CycleState.ENTRY_PARTIAL
    cancel = gateway.cancel

    def fill_then_cancel(symbol, client_id):
        gateway.execute(client_id, "1", "100")  # races the cancel: the response reports 5 executed
        return cancel(symbol, client_id)

    gateway.cancel = fill_then_cancel
    gateway.book.bid, gateway.book.ask = D("100.6"), D("100.61")  # positive threshold
    runner.tick(3.0)
    assert cycle.q0 == D("5") and cycle.state is CycleState.HEDGE_PENDING
    assert cycle.orders[cycle.hedge_ids[0]].qty == D("5")


def test_malformed_command_is_an_error_not_a_crash(runner, gateway):
    cid = runner.store.enqueue("enter", {"side": "long", "qty": "1,5", "price": "100"})
    runner.tick(1.0)
    status, result = runner.store.command_result(cid)
    assert status == "error" and "InvalidOperation" in result and not runner.store.has_pending_commands()


def test_failed_call_mid_tick_still_saves_applied_fills(runner, gateway):
    cycle = _entered(runner, gateway)
    gateway.execute(cycle.entry_id, "1", "100")

    def down(symbol, **k):
        raise UnknownResult("timeout")

    gateway.quote = down
    runner.reconcile_every_s = 0

    def positions_down(symbol):
        raise UnknownResult("timeout")

    gateway.positions = positions_down
    with pytest.raises(UnknownResult):
        runner.tick(2.0)
    assert runner.store.load_cycle().state is CycleState.SINGLE_POSITION


def test_source_watcher_sees_a_config_edit(tmp_path):
    import os
    cfg = tmp_path / "config.toml"
    cfg.write_text("leverage = 10")
    watcher = SourceWatcher([cfg], every_s=0)
    assert not watcher.changed()
    mtime = cfg.stat().st_mtime_ns
    os.utime(cfg, ns=(mtime, mtime + 1_000_000_000))
    assert watcher.changed() and watcher.changed()  # stays fired until the restart


def test_run_returns_between_ticks_when_asked_to_stop(runner):
    ticks = []
    runner.tick = ticks.append
    runner.run(0.0, stop=lambda: len(ticks) >= 2)
    assert len(ticks) == 2


def test_runner_rests_adds_on_the_exchange_and_exits_on_fill(runner, gateway):
    cycle = _entered(runner, gateway)
    gateway.execute(cycle.entry_id, "1", "100")
    runner.tick(2.0)
    gateway.book.bid, gateway.book.ask = D("100.6"), D("100.61")
    runner.tick(3.0)
    gateway.execute(cycle.hedge_ids[0], "1", "100.5")
    runner.tick(4.0)  # HEDGED, then both adds go out as resting limits in the same tick
    resting = {o["order"].position_side: o["order"] for o in gateway.orders.values()
               if o["order"].purpose is Purpose.ADD and o["status"] is OrderStatus.NEW}
    assert resting[Side.SHORT].price == D("101.51") and resting[Side.LONG].price == D("99")
    gateway.execute(resting[Side.SHORT].client_id, "0.1", "101.51")
    runner.tick(5.0)
    exits = [o["order"] for o in gateway.orders.values() if o["order"].purpose is Purpose.ADD_EXIT]
    assert [(e.position_side, e.side, e.price) for e in exits] == [(Side.SHORT, "BUY", D("101.00"))]
    assert cycle.books[Side.SHORT].qty == D("1.1") and cycle.recovery is None
