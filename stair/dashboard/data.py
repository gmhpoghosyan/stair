"""Builds the dashboard view model from the journal and a read-only exchange view."""
from __future__ import annotations

import threading
import time
from decimal import Decimal
from pathlib import Path

from ..core.models import Side
from ..core.serde import to_plain
from ..exchange import ExchangeError, ExchangeGateway, UnknownResult
from ..storage import Store
from ..strategy import Add, Cycle


def cycle_view(cycle: Cycle) -> dict:
    view = cycle.summary()
    view.update({
        "created_at": cycle.created_at,
        "symbol": cycle.rules.symbol,
        "adds": [add_view(cycle, a) for a in reversed(cycle.adds)],
        "orders": [{"client_id": o.client_id, "purpose": o.purpose, "side": o.side,
                    "position_side": o.position_side, "qty": o.qty, "filled": o.filled,
                    "price": o.price, "type": o.order_type, "tif": o.tif, "status": o.status}
                   for o in cycle.orders.values() if not o.is_terminal],
    })
    return view


def add_view(cycle: Cycle, add: Add) -> dict:
    entry = cycle.orders[add.entry_id]
    if add.closed:
        state = "closed" if entry.filled else "cancelled"
    elif entry.filled < entry.qty and not entry.is_terminal:
        state = "partly filled" if entry.filled else "waiting at trigger"
    else:
        state = "open, exit working" if add.exit_id else "open"
    exit_price = add.exit_price or (cycle.add_exit(add) if entry.filled else None)
    return {"id": add.id, "side": add.side, "qty": entry.qty, "filled": entry.filled, "trigger": add.trigger,
            "entry_price": entry.avg_price, "exit_price": exit_price, "state": state,
            "result": cycle.add_result(add), "note": add.note}


def closed_view(finished_ts: float, cycle: Cycle) -> dict:
    long, short = cycle.books[Side.LONG], cycle.books[Side.SHORT]
    realized, fees = long.realized + short.realized, long.fees + short.fees
    return {"id": cycle.id, "symbol": cycle.rules.symbol, "first_side": cycle.first_side,
            "e0": cycle.e0, "q0": cycle.q0, "branch": cycle.branch, "base_net_spread": cycle.base_net_spread(),
            "started": cycle.created_at, "finished": finished_ts,
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
                view = {"enabled": True, "error": None, "at": time.time(), "positions": self.gateway.positions(None)}
            except (ExchangeError, UnknownResult) as exc:
                view = {**self._exchange, "enabled": True, "error": str(exc)}
            self._exchange, self._exchange_at = view, time.time()
            return view

    def snapshot(self) -> dict:
        store = Store(self.db_path)  # one connection per request; sqlite objects are per thread
        try:
            cycles = store.load_cycles()
            closed = [closed_view(ts, c) for ts, c in store.finished_cycles()]
            heartbeat = store.get("heartbeat")
        finally:
            store.close()
        net_total = sum((c["net"] for c in closed), Decimal(0))
        return to_plain({"env": self.env, "now": time.time(), "heartbeat": heartbeat,
                         "exchange": self._exchange_view(),
                         "cycles": [cycle_view(c) for c in cycles.values()],
                         "closed": closed, "closed_net_total": net_total})
