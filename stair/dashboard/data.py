"""Builds the dashboard view model from the journal and a read-only exchange view."""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from pathlib import Path

from ..core.models import ZERO, AddState, Side
from ..core.serde import to_plain
from ..exchange import ExchangeError, ExchangeGateway, UnknownResult
from ..storage import Store
from ..strategy import Cycle


def cycle_view(cycle: Cycle) -> dict:
    view = cycle.summary()
    view.update({
        "created_at": cycle.created_at,
        "symbol": cycle.rules.symbol,
        "adds": [{"id": a.id, "side": a.side, "qty": a.qty, "price": a.price, "exit_price": a.exit_price,
                  "state": a.state, "filled": cycle.orders[a.entry_id].filled,
                  "result": cycle.add_result(a), "note": a.note} for a in cycle.adds.values()],
        "levels": [{"id": lv.id, "kind": "resistance" if lv.side is Side.SHORT else "support",
                    "price": lv.price, "exit_price": lv.exit_price, "add_id": lv.add_id, "source": lv.source}
                   for lv in cycle.levels],
        "orders": [{"client_id": o.client_id, "purpose": o.purpose, "side": o.side,
                    "position_side": o.position_side, "qty": o.qty, "filled": o.filled,
                    "price": o.price, "type": o.order_type, "tif": o.tif, "status": o.status}
                   for o in cycle.orders.values() if not o.is_terminal],
    })
    return view


def closed_view(finished_ts: float, cycle: Cycle) -> dict:
    long, short = cycle.books[Side.LONG], cycle.books[Side.SHORT]
    realized, fees = long.realized + short.realized, long.fees + short.fees
    closed_adds = [a for a in cycle.adds.values() if a.state is AddState.CLOSED and cycle.add_result(a)]
    return {"id": cycle.id, "symbol": cycle.rules.symbol, "first_side": cycle.first_side,
            "e0": cycle.e0, "q0": cycle.q0, "branch": cycle.branch, "base_net_spread": cycle.base_net_spread(),
            "started": cycle.created_at, "finished": finished_ts, "adds_closed": len(closed_adds),
            "adds_result": sum((cycle.add_result(a) for a in closed_adds), ZERO),
            "realized": realized, "fees": fees, "funding": cycle.funding,
            "net": realized + cycle.funding - fees, "note": cycle.note}


class DashboardData:
    def __init__(self, db_path: Path, env: str, symbol: str, gateway: ExchangeGateway | None,
                 exchange_ttl_s: float = 3.0):
        self.db_path, self.env, self.symbol, self.gateway = db_path, env, symbol, gateway
        self.ttl = exchange_ttl_s
        self._lock = threading.Lock()
        self._exchange: dict = {}
        self._exchange_at = 0.0

    def _exchange_view(self) -> dict:
        if self.gateway is None:
            return {"enabled": False}
        with self._lock:
            if time.time() - self._exchange_at < self.ttl:
                return self._exchange
            try:
                quote = self.gateway.quote(self.symbol, last=False)  # the page shows bid, ask and mark
                view = {"enabled": True, "error": None, "at": time.time(),
                        "positions": self.gateway.positions(None),
                        "open_orders": [o.client_id for o in self.gateway.open_orders(self.symbol)],
                        "quote": quote}
            except (ExchangeError, UnknownResult) as exc:
                view = {**self._exchange, "enabled": True, "error": str(exc)}
            self._exchange, self._exchange_at = view, time.time()
            return view

    def snapshot(self) -> dict:
        store = Store(self.db_path)  # one connection per request; sqlite objects are per thread
        try:
            cycle = store.load_cycle()
            closed = [closed_view(ts, c) for ts, c in store.finished_cycles()]
            events = [{"ts": ts, "cycle": cid, "kind": kind, "payload": payload}
                      for ts, cid, kind, payload in store.recent_events(40)]
            heartbeat = store.get("heartbeat")
            auto_levels = store.get("auto_levels")
        finally:
            store.close()
        net_total = sum((c["net"] for c in closed), Decimal(0))
        return to_plain({"env": self.env, "symbol": self.symbol, "now": time.time(),
                         "heartbeat": heartbeat, "auto_levels": auto_levels, "exchange": self._exchange_view(),
                         "cycle": cycle_view(cycle) if cycle else None,
                         "closed": closed, "closed_net_total": net_total, "events": events})
