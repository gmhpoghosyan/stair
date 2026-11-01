"""Pulls order statuses and trades from the gateway and feeds them to the cycle.

Fills always go in before terminal statuses, and a terminal status is held back while
the exchange reports more executed quantity than the trades seen so far. Trades are
stored by id first, so a replay or restart never double counts (T10).

Trades are fetched only when an order report shows executed quantity the cycle has not
seen yet, plus a slow safety fetch, so a quiet tick costs at most one open-orders call.
"""
from __future__ import annotations

import logging

from ..core.models import TERMINAL, Fill, Intent, OrderReport, OrderStatus
from ..exchange import ExchangeGateway, OrderNotFound
from ..storage import Store
from ..strategy import Cycle

log = logging.getLogger("stair.sync")


class OrderSync:
    def __init__(self, gateway: ExchangeGateway, store: Store, symbol: str, send_grace_s: float = 10.0,
                 trades_every_s: float = 15.0):
        self.gateway, self.store, self.symbol, self.grace = gateway, store, symbol, send_grace_s
        self.trades_every_s = trades_every_s
        self.trades_pending = True  # first sync after a start always reads trades
        self._last_trades: float | None = None
        self._held: dict[str, OrderReport] = {}  # place/cancel responses waiting for their trades

    def sync(self, cycle: Cycle, now: float) -> list[Intent]:
        self._held.clear()  # fresh reports supersede them
        reports = self._order_reports(cycle, now)
        for report in reports.values():
            self._note_executed(cycle, report)
        intents = self.sync_trades(cycle, now)
        for report in reports.values():
            intents += apply_report(cycle, report)
        return intents

    def apply(self, cycle: Cycle, report: OrderReport) -> list[Intent]:
        """apply_report for a place/cancel response; unseen fills are flagged for follow_up."""
        if self._note_executed(cycle, report):
            self._held[report.client_id] = report
        return apply_report(cycle, report)

    def follow_up(self, cycle: Cycle, now: float) -> list[Intent]:
        """Reads the trades of fills a place/cancel response just reported, then re-applies the
        held responses, so e.g. a cancelled partial entry releases its hedge in the same tick."""
        held, self._held = self._held, {}
        intents = self.sync_trades(cycle, now)
        for report in held.values():
            intents += apply_report(cycle, report)
        return intents

    def sync_trades(self, cycle: Cycle, now: float) -> list[Intent]:
        due = self._last_trades is None or now - self._last_trades >= self.trades_every_s
        if self.trades_pending or due:
            self.trades_pending = False
            self._last_trades = now
            self._fetch_trades(cycle)
        return self._apply_trades(cycle)

    def _note_executed(self, cycle: Cycle, report: OrderReport) -> bool:
        order = cycle.orders.get(report.client_id)
        unseen = order is not None and report.executed_qty > order.filled
        self.trades_pending |= unseen
        return unseen

    def _order_reports(self, cycle: Cycle, now: float) -> dict[str, OrderReport]:
        active = [o for o in cycle.orders.values() if not o.is_terminal and o.sent_at is not None]
        if not active:
            return {}
        open_by_id = {r.client_id: r for r in self.gateway.open_orders(self.symbol)}
        reports: dict[str, OrderReport] = {}
        for order in active:
            report = open_by_id.get(order.client_id)
            if report is None:
                try:
                    report = self.gateway.query(self.symbol, order.client_id)
                except OrderNotFound:
                    if order.exchange_id is not None or now - order.sent_at < self.grace:
                        continue
                    # Never reached the exchange: safe to treat as rejected, nothing executed.
                    report = OrderReport(order.client_id, 0, OrderStatus.REJECTED, order.filled,
                                         reason="not found on exchange")
            if report.exchange_id:
                order.exchange_id = report.exchange_id
            reports[order.client_id] = report
        return reports

    def _since_ms(self, cycle: Cycle) -> int:
        return int(cycle.created_at * 1000) - 60_000

    def _fetch_trades(self, cycle: Cycle) -> None:
        last = self.store.last_trade_id(self.symbol)
        if last is None:
            fresh = self.gateway.trades(self.symbol, start_ms=self._since_ms(cycle))
        else:
            fresh = self.gateway.trades(self.symbol, from_id=last + 1)
        self.store.add_trades(self.symbol, fresh)

    def _apply_trades(self, cycle: Cycle) -> list[Intent]:
        by_exchange_id = {o.exchange_id: o for o in cycle.orders.values() if o.exchange_id}
        intents: list[Intent] = []
        for trade in self.store.unprocessed_trades(self.symbol, self._since_ms(cycle)):
            order = by_exchange_id.get(trade.order_id)
            if order is None:
                continue  # unknown or foreign order; reconciliation flags any position effect
            if trade.trade_id in order.trade_ids:
                self.store.mark_processed(self.symbol, trade.trade_id)  # read again, already applied (T10)
                continue
            fee = self.gateway.to_usdt(trade.commission, trade.commission_asset)
            fill = Fill(trade.trade_id, order.client_id, trade.qty, trade.price, fee)
            intents += cycle.on_fill(fill)
            self.store.mark_processed(self.symbol, trade.trade_id)
            self.store.log("fill", fill, cycle.id)
            log.info("fill %s %s %s @ %s fee %s", order.client_id, order.side, trade.qty, trade.price, fee)
        return intents


def apply_report(cycle: Cycle, report: OrderReport) -> list[Intent]:
    order = cycle.orders.get(report.client_id)
    if order is None:
        return []
    if report.exchange_id:
        order.exchange_id = report.exchange_id
    if report.status in TERMINAL and report.executed_qty > order.filled:
        return []  # wait until the trades for this order are visible
    if report.status is order.status:
        return []
    return cycle.on_status(report.client_id, report.status, report.reason)
