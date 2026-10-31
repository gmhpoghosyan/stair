"""Orchestrator: wires gateway, cycle engine and store into a polling loop."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from ..core import pricing
from ..core.models import Cancel, CycleState, Intent, MarketRules, OrderStatus, Place, Quote, Side
from ..exchange import ExchangeError, ExchangeGateway, OrderNotFound, UnknownResult
from ..storage import Store
from ..strategy import Cycle, EngineError, StrategyConfig
from .sync import OrderSync

log = logging.getLogger("stair.runner")

# Hedge triggers and fills matter most while the cycle is not (or no longer) hedged.
_UNHEDGED = {CycleState.WAIT_ENTRY, CycleState.ENTRY_PARTIAL, CycleState.SINGLE_POSITION,
             CycleState.HEDGE_PENDING, CycleState.CLOSING}
_COMMAND_ERRORS = (EngineError, ExchangeError, UnknownResult, ValueError, KeyError, TypeError, ArithmeticError)


class Runner:
    def __init__(self, gateway: ExchangeGateway, store: Store, cfg: StrategyConfig, *,
                 trigger_price: str = "book", reconcile_every_s: float = 15.0,
                 funding_every_s: float = 300.0):
        if trigger_price not in ("book", "last", "mark"):
            raise ValueError("trigger_price must be book, last or mark")
        self.gateway, self.store, self.cfg = gateway, store, cfg
        self.symbol = cfg.symbol
        self.trigger_price = trigger_price
        self.reconcile_every_s, self.funding_every_s = reconcile_every_s, funding_every_s
        self.sync = OrderSync(gateway, store, self.symbol)
        self.rules: MarketRules | None = None
        self.cycle: Cycle | None = None
        self._mismatches = 0
        self._last_reconcile = 0.0
        self._last_funding = 0.0
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="stair-io")

    # ---- lifecycle ------------------------------------------------------

    def preflight(self) -> MarketRules:
        if not self.gateway.hedge_mode():
            raise SystemExit("account is in one-way mode; hedge mode is required (R01). "
                             "Run `python -m stair hedge-mode on` with no open positions or orders.")
        self.rules = self.gateway.market_rules(self.symbol)
        self.gateway.set_leverage(self.symbol, self.cfg.leverage)
        self.cycle = self.store.load_cycle()
        if self.cycle:
            self.cycle.rules = self.rules  # fees or filters may have changed while offline
            log.info("resuming cycle %s in %s; reconciling before new actions",
                     self.cycle.id, self.cycle.display_state.value)
            self._reconcile(time.time(), force=True)
        self.store.log("start", {"env": self.gateway.env, "symbol": self.symbol})
        return self.rules

    def run(self, poll_interval_s: float, fast_poll_interval_s: float | None = None,
            stop: Callable[[], bool] | None = None) -> None:
        """Polls at `fast_poll_interval_s` while a position is unhedged, else at `poll_interval_s`.
        Returns between ticks once `stop()` is true (used to restart on code changes)."""
        fast = fast_poll_interval_s or poll_interval_s
        while not (stop and stop()):
            started = time.time()
            ok = True
            try:
                self.tick(started)
            except (UnknownResult, ExchangeError) as exc:
                log.warning("tick failed, data may be stale: %s", exc)
                ok = False
            unhedged = self.cycle is not None and self.cycle.state in _UNHEDGED
            self._wait(started + (fast if unhedged else poll_interval_s), wake_on_command=ok)

    def _wait(self, deadline: float, wake_on_command: bool) -> None:
        """Sleeps until the next tick, waking early for a queued command so `enter` is sent at once."""
        while (left := deadline - time.time()) > 0:
            if wake_on_command and self.store.has_pending_commands():
                return
            time.sleep(min(left, 0.05))

    def tick(self, now: float) -> None:
        self.store.set("heartbeat", now)  # lets the dashboard tell a running bot from a stopped one
        self._handle_commands(now)
        cycle = self.cycle
        if cycle is None:
            return
        before = (cycle.display_state, cycle.note)
        try:
            self._step(cycle, now)
        finally:
            self._finish_tick(cycle, before)  # a failed call never leaves applied fills unsaved

    def _step(self, cycle: Cycle, now: float) -> None:
        self._execute(cycle, self.sync.sync(cycle, now), now)
        quote = self._quote()
        if quote is not None:
            bid, ask = self._trigger_prices(quote)
            self._execute(cycle, cycle.on_quote(bid, ask, now), now)
        # A MARKET hedge or a crossing order fills in its place response: read those trades now
        # rather than one poll later, so the next leg (hedge, hedged state) goes out this tick.
        for _ in range(3):
            if not self.sync.trades_pending:
                break
            self._execute(cycle, self.sync.follow_up(cycle, now), now)
        self._reconcile(now)
        if now - self._last_funding >= self.funding_every_s and cycle.q0:
            cycle.funding = self.gateway.funding_since(self.symbol, int(cycle.created_at * 1000))
            self._last_funding = now

    def _finish_tick(self, cycle: Cycle, before) -> None:
        after = (cycle.display_state, cycle.note)
        if after != before:
            log.info("cycle %s: %s %s", cycle.id, after[0].value, after[1])
            self.store.log("state", {"state": after[0].value, "note": after[1]}, cycle.id)
        self.store.save_cycle(cycle)
        if not cycle.is_active:
            log.info("cycle %s finished; waiting for the next manual entry (Q20 default)", cycle.id)
            self.cycle = None

    # ---- market data ----------------------------------------------------

    def _quote(self) -> Quote | None:
        try:
            return self.gateway.quote(self.symbol, last=self.trigger_price == "last",
                                      mark=self.trigger_price == "mark")
        except (UnknownResult, ExchangeError) as exc:
            log.warning("no fresh quote, skipping price-driven decisions: %s", exc)
            return None

    def _trigger_prices(self, quote: Quote) -> tuple[Decimal, Decimal]:
        """Q10: which price the thresholds look at. Default is the executable bid/ask."""
        if self.trigger_price == "last" and quote.last is not None:
            return quote.last, quote.last
        if self.trigger_price == "mark" and quote.mark is not None:
            return quote.mark, quote.mark
        return quote.bid, quote.ask

    # ---- order execution ------------------------------------------------

    def _execute(self, cycle: Cycle, intents: list[Intent], now: float) -> None:
        queue = list(intents)
        while queue:
            intent = queue.pop(0)
            if isinstance(intent, Place):
                queue += self._place(cycle, intent, now)
            elif isinstance(intent, Cancel):
                queue += self._cancel(cycle, intent)

    def _place(self, cycle: Cycle, intent: Place, now: float) -> list[Intent]:
        order = intent.order
        order.sent_at = now
        self.store.save_cycle(cycle)  # the client id is on disk before the request leaves
        self.store.log("place", order, cycle.id)
        log.info("place %s %s %s %s %s @ %s %s", order.client_id, order.purpose.value, order.side,
                 order.position_side.value, order.qty, order.price or "MKT", order.tif or "")
        try:
            report = self.gateway.place(self.symbol, order)
        except UnknownResult as exc:
            log.warning("place %s outcome unknown, will query before any retry: %s", order.client_id, exc)
            return []
        except ExchangeError as exc:
            log.warning("place %s rejected: %s", order.client_id, exc)
            return cycle.on_status(order.client_id, OrderStatus.REJECTED, str(exc))
        return self.sync.apply(cycle, report)

    def _cancel(self, cycle: Cycle, intent: Cancel) -> list[Intent]:
        order = cycle.orders[intent.client_id]
        self.store.log("cancel", {"client_id": intent.client_id}, cycle.id)
        log.info("cancel %s", intent.client_id)
        try:
            report = self.gateway.cancel(self.symbol, intent.client_id)
        except OrderNotFound:
            return []  # already final; the next sync reads its real status
        except (UnknownResult, ExchangeError) as exc:
            log.warning("cancel %s failed, will retry: %s", intent.client_id, exc)
            order.cancel_requested = False
            return []
        return self.sync.apply(cycle, report)

    def _parallel(self, *calls):
        futures = [self._pool.submit(call) for call in calls]
        return [f.result() for f in futures]

    # ---- reconciliation -------------------------------------------------

    def _reconcile(self, now: float, force: bool = False) -> None:
        cycle = self.cycle
        if cycle is None or (not force and now - self._last_reconcile < self.reconcile_every_s):
            return
        self._last_reconcile = now
        views = {p.side: p for p in self.gateway.positions(self.symbol)}
        self.store.set("exchange_positions", {s.value: views[s] for s in views})
        exchange = {s: views[s].qty if s in views else Decimal(0) for s in Side}
        local = {s: cycle.books[s].qty for s in Side}
        if exchange == local:
            self._mismatches = 0
            return
        self._mismatches += 1
        # Fills can be in flight between calls; two consecutive mismatches mean a real divergence.
        if (self._mismatches >= 2 or force) and not cycle.recovery:
            reason = (f"exchange LONG {exchange[Side.LONG]} SHORT {exchange[Side.SHORT]} vs local "
                      f"LONG {local[Side.LONG]} SHORT {local[Side.SHORT]}")
            cycle.enter_recovery(reason)
            log.error("RECOVERY: %s", reason)
            self.store.log("recovery", {"reason": reason}, cycle.id)

    # ---- commands -------------------------------------------------------

    def _handle_commands(self, now: float) -> None:
        for command_id, kind, payload in self.store.pending_commands():
            try:
                result, ok = self._command(kind, payload, now), True
            except _COMMAND_ERRORS as exc:  # bad input (e.g. --qty 1,5) must not crash the loop
                result, ok = f"{type(exc).__name__}: {exc}", False
            self.store.finish_command(command_id, ok, result)
            self.store.log("command", {"kind": kind, "payload": payload, "ok": ok, "result": result},
                           self.cycle.id if self.cycle else None)
            log.info("command %s %s -> %s", kind, payload, result)
            if self.cycle:
                self.store.save_cycle(self.cycle)

    def _require_cycle(self) -> Cycle:
        if self.cycle is None:
            raise EngineError("no active cycle")
        return self.cycle

    def _command(self, kind: str, payload: dict, now: float) -> str:
        if kind == "enter":
            return self._enter(payload, now)
        cycle = self._require_cycle()
        if kind == "resume":
            self._execute(cycle, cycle.resume(), now)
            return f"resumed into {cycle.state.value}"
        if kind == "close":
            self._execute(cycle, cycle.close(), now)
            return "closing: cancelling cycle orders, then closing both sides at market"
        if kind == "ack_recovery":
            cycle.resolve_recovery()
            self._mismatches = 0
            self._reconcile(now, force=True)
            return "recovery cleared" if not cycle.recovery else f"still mismatched: {cycle.recovery}"
        raise ValueError(f"unknown command {kind}")

    def _enter(self, payload: dict, now: float) -> str:
        if self.cycle is not None:
            raise EngineError(f"cycle {self.cycle.id} is still active (one cycle per symbol, Q05 default)")
        rules = self.rules
        side = Side(payload["side"].upper())
        qty = pricing.round_to(Decimal(payload["qty"]), rules.step_size, up=False)
        # Never pay more (long) or sell cheaper (short) than requested.
        price = pricing.round_to(Decimal(payload["price"]), rules.tick_size, up=side is Side.SHORT)
        if qty < rules.min_qty or qty * price < rules.min_notional:
            raise ValueError(f"{qty} @ {price} is below exchange minimums "
                             f"(min qty {rules.min_qty}, min notional {rules.min_notional})")
        # The three pre-entry reads are independent: one round trip instead of three.
        positions, open_orders, quote = self._parallel(
            lambda: self.gateway.positions(self.symbol),
            lambda: self.gateway.open_orders(self.symbol),
            lambda: self.gateway.quote(self.symbol, last=False, mark=False))
        if any(p.qty for p in positions):
            raise EngineError(f"{self.symbol} already has positions not owned by a cycle; flatten first")
        if open_orders:
            raise EngineError(f"{self.symbol} has open orders; cancel them first")
        crosses = price >= quote.ask if side is Side.LONG else price <= quote.bid
        post_only = bool(payload.get("post_only"))
        cycle = Cycle.new(self.cfg, rules, now)
        self.cycle = cycle
        self._execute(cycle, cycle.enter(side, qty, price, post_only), now)
        note = ""
        if crosses:
            note = (" (post-only will be rejected: price crosses the book)" if post_only
                    else " (crosses the book: fills now as taker, see Q07)")
        return f"cycle {cycle.id}: {side.value} {qty} @ {price}{' post-only' if post_only else ''}{note}"
