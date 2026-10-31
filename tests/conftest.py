from __future__ import annotations

import itertools
from decimal import Decimal as D

import pytest

from stair.core.models import (Cancel, Fill, MarketRules, OrderReport, OrderStatus, PositionView, Place, Quote,
                               Side, Trade)
from stair.exchange import OrderNotFound, UnknownResult
from stair.strategy import Cycle, StrategyConfig


def rules(maker="0", taker="0") -> MarketRules:
    return MarketRules(symbol="SOLUSDT", tick_size=D("0.01"), step_size=D("0.01"), min_qty=D("0.01"),
                       min_notional=D("5"), maker_fee=D(maker), taker_fee=D(taker))


@pytest.fixture
def cycle() -> Cycle:
    return Cycle.new(StrategyConfig(retry_cooldown_s=0), rules(), now=0.0)


_trade_ids = itertools.count(1)


def fill(cycle: Cycle, client_id: str, qty, price, fee="0"):
    return cycle.on_fill(Fill(next(_trade_ids), client_id, D(str(qty)), D(str(price)), D(str(fee))))


def places(intents) -> list:
    return [i.order for i in intents if isinstance(i, Place)]


def cancels(intents) -> list[str]:
    return [i.client_id for i in intents if isinstance(i, Cancel)]


class FakeGateway:
    """In-memory exchange used to drive the real runner (same contract as BinanceFutures)."""
    env = "demo"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.trade_log: list[Trade] = []
        self.book = Quote(bid=D("100"), ask=D("100.01"), last=D("100"), mark=D("100"))
        self.ids = itertools.count(1000)
        self.trade_ids = itertools.count(1)
        self.place_calls: list[str] = []
        self.fail_next_place_unknown = False
        self.hedge = True

    # helpers for tests
    def execute(self, client_id: str, qty, price, fee="0"):
        o = self.orders[client_id]
        qty = D(str(qty))
        o["executed"] += qty
        o["status"] = OrderStatus.FILLED if o["executed"] >= o["qty"] else OrderStatus.PARTIALLY_FILLED
        self.trade_log.append(Trade(next(self.trade_ids), o["id"], qty, D(str(price)), D(str(fee)), "USDT", 0))

    def _report(self, cid):
        o = self.orders[cid]
        return OrderReport(cid, o["id"], o["status"], o["executed"])

    # gateway contract
    def server_time_ms(self): return 0
    def market_rules(self, symbol): return rules()
    def quote(self, symbol, *, last=True, mark=True): return self.book
    def hedge_mode(self): return self.hedge
    def set_hedge_mode(self, enabled): self.hedge = enabled
    def set_leverage(self, symbol, leverage): pass
    def margin_type(self, symbol): return "cross"
    def set_margin_type(self, symbol, cross): pass
    def balances(self): return {"USDT": D("1000")}
    def to_usdt(self, amount, asset): return amount
    def funding_since(self, symbol, start_ms): return D(0)
    def closed_candles(self, symbol, interval, limit=500): return []

    def positions(self, symbol):
        qty = {Side.LONG: D(0), Side.SHORT: D(0)}
        for cid, o in self.orders.items():
            sign = 1 if o["order"].opening else -1
            qty[o["order"].position_side] += sign * o["executed"]
        return [PositionView(s, qty[s], D(0), D(0), 10, "cross", D(0)) for s in Side]

    def open_orders(self, symbol):
        return [self._report(c) for c, o in self.orders.items()
                if o["status"] in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)]

    def place(self, symbol, order):
        self.place_calls.append(order.client_id)
        self.orders[order.client_id] = {"id": next(self.ids), "qty": order.qty, "executed": D(0),
                                        "status": OrderStatus.NEW, "order": order}
        if self.fail_next_place_unknown:
            self.fail_next_place_unknown = False
            raise UnknownResult("timeout")
        return self._report(order.client_id)

    def cancel(self, symbol, client_id):
        o = self.orders.get(client_id)
        if o is None or o["status"] not in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED):
            raise OrderNotFound(-2011, "Unknown order sent.")
        o["status"] = OrderStatus.CANCELED
        return self._report(client_id)

    def query(self, symbol, client_id):
        if client_id not in self.orders:
            raise OrderNotFound(-2013, "Order does not exist.")
        return self._report(client_id)

    def trades(self, symbol, *, from_id=None, start_ms=None):
        return [t for t in self.trade_log if from_id is None or t.trade_id >= from_id]


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()
