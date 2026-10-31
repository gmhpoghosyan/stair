"""Cycle state machine: first entry -> base hedge -> adds on each leg -> manual close.

Pure logic with no I/O. The execution component feeds fills, order statuses and
quotes in and sends the returned intents to an exchange gateway. The backtest is
meant to drive the same object (spec section 10).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from ..core import pricing
from ..core.models import (ZERO, Cancel, CycleState, Fill, Intent, MarketRules, Order, OrderStatus, Place,
                           Purpose, Side)
from .config import StrategyConfig

POSITIVE, NEGATIVE = "positive", "negative"
_CODES = {Purpose.ENTRY: "E", Purpose.HEDGE: "H", Purpose.ADD: "A", Purpose.ADD_EXIT: "X", Purpose.CLOSE: "C"}


class EngineError(Exception):
    """A command is not valid in the current cycle state."""


@dataclass
class Book:
    """One exchange position side under the weighted-average model (section 6)."""
    qty: Decimal = ZERO
    avg: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO

    def apply(self, side: Side, opening: bool, qty: Decimal, price: Decimal, fee: Decimal) -> None:
        self.fees += fee
        if opening:
            self.avg = pricing.weighted_avg(self.qty, self.avg, qty, price)
            self.qty += qty
            return
        self.realized += pricing.side_pnl(side, qty, self.avg, price)
        self.qty -= qty
        if self.qty <= 0:
            self.qty, self.avg = ZERO, ZERO


@dataclass
class Add:
    """add_fraction x Q0 on one leg, opened at the leg's trigger and closed at its own exit.
    A leg has at most one live add; the next one waits until this one is closed."""
    id: str
    side: Side
    entry_id: str
    trigger: Decimal
    exit_price: Decimal | None = None  # fixed once the entry is final
    exit_id: str | None = None  # working exit order, if any
    exit_ids: list[str] = field(default_factory=list)
    closed: bool = False
    note: str = ""


@dataclass
class Cycle:
    id: str
    cfg: StrategyConfig
    rules: MarketRules
    created_at: float
    state: CycleState = CycleState.WAIT_SIGNAL
    note: str = ""
    first_side: Side | None = None
    entry_id: str | None = None
    hedge_ids: list[str] = field(default_factory=list)
    branch: str | None = None  # POSITIVE | NEGATIVE, locked once chosen (R17)
    books: dict[Side, Book] = field(default_factory=lambda: {Side.LONG: Book(), Side.SHORT: Book()})
    orders: dict[str, Order] = field(default_factory=dict)
    adds: list[Add] = field(default_factory=list)
    seq: int = 0
    resume_state: CycleState | None = None
    recovery: str | None = None  # reason while halted for reconciliation
    funding: Decimal = ZERO
    now: float = 0.0
    bid: Decimal | None = None
    ask: Decimal | None = None
    retry_at: dict[str, float] = field(default_factory=dict)

    @classmethod
    def new(cls, cfg: StrategyConfig, rules: MarketRules, now: float) -> Cycle:
        return cls(id=secrets.token_hex(4), cfg=cfg, rules=rules, created_at=now, now=now)

    # ---- derived values -------------------------------------------------

    @property
    def display_state(self) -> CycleState:
        return CycleState.RECOVERY if self.recovery else self.state

    @property
    def is_active(self) -> bool:
        return self.state is not CycleState.FINISHED

    @property
    def entry(self) -> Order:
        return self.orders[self.entry_id]

    @property
    def e0(self) -> Decimal | None:
        return self.entry.avg_price if self.entry_id else None

    @property
    def q0(self) -> Decimal:
        """Actually filled first-entry quantity (R21), not the requested one."""
        return self.entry.filled if self.entry_id else ZERO

    @property
    def f0(self) -> Decimal:
        return self.entry.commission if self.entry_id else ZERO

    @property
    def hedge_fee_rate(self) -> Decimal:
        return self.cfg.hedge_fee_rate if self.cfg.hedge_fee_rate is not None else self.rules.taker_fee

    @property
    def hedge_filled(self) -> Decimal:
        return sum((self.orders[h].filled for h in self.hedge_ids), ZERO)

    @property
    def delta(self) -> Decimal:
        return self.books[Side.LONG].qty - self.books[Side.SHORT].qty

    def positive_threshold(self) -> Decimal:
        raw = pricing.positive_hedge_price(self.first_side, self.e0, self.q0, self.f0,
                                           self.hedge_fee_rate, self.cfg.hedge_spread)
        # Short target rounds up, long target rounds down, so the net spread holds (T17).
        return pricing.round_to(raw, self.rules.tick_size, up=self.first_side is Side.LONG)

    def negative_threshold(self) -> Decimal:
        raw = pricing.negative_hedge_price(self.first_side, self.e0, self.cfg.adverse_move)
        return pricing.round_to(raw, self.rules.tick_size, up=self.first_side is Side.SHORT)

    def add_trigger(self, side: Side) -> Decimal | None:
        """add_trigger_pct beyond the leg's entry price as the exchange shows it (no fees). Rounded away
        from the market, so the move is at least the full percentage."""
        book = self.books[side]
        if not book.qty:
            return None
        raw = pricing.add_trigger_price(side, book.avg, self.cfg.add_trigger_pct)
        return pricing.round_to(raw, self.rules.tick_size, up=side is Side.SHORT)

    def add_qty(self, price: Decimal) -> Decimal | None:
        """add_fraction x Q0 rounded down to the lot step; None below exchange minimums (never rounded up)."""
        q = pricing.round_to(self.q0 * self.cfg.add_fraction, self.rules.step_size, up=False)
        if q < self.rules.min_qty or q * price < self.rules.min_notional:
            return None
        return q

    def add_exit(self, add: Add) -> Decimal:
        """Exit that nets add_exit_pct on the add after its actual entry fee and a maker exit fee
        (the exit is a resting limit order). Rounded so the net is at least the target."""
        entry = self.orders[add.entry_id]
        raw = pricing.add_exit_price(add.side, entry.avg_price, entry.commission / entry.filled,
                                     self.rules.maker_fee, self.cfg.add_exit_pct)
        return pricing.round_to(raw, self.rules.tick_size, up=add.side is Side.LONG)

    def add_result(self, add: Add) -> Decimal:
        """Closed part of the add net of its own fees (its share of the entry fee plus exit fees)."""
        entry = self.orders[add.entry_id]
        exits = [self.orders[x] for x in add.exit_ids]
        closed = sum((o.filled for o in exits), ZERO)
        if not closed:
            return ZERO
        exit_avg = sum((o.notional for o in exits), ZERO) / closed
        return (pricing.side_pnl(add.side, closed, entry.avg_price, exit_avg)
                - entry.commission * closed / entry.filled - sum((o.commission for o in exits), ZERO))

    # ---- commands -------------------------------------------------------

    def enter(self, side: Side, qty: Decimal, price: Decimal, post_only: bool) -> list[Intent]:
        """First entry after the user-detected market event (R02, R04, R20)."""
        if self.state is not CycleState.WAIT_SIGNAL:
            raise EngineError(f"cannot enter in {self.state.value}")
        self.first_side = side
        order = self._order(Purpose.ENTRY, side, True, qty, price=price,
                            tif="GTX" if post_only else "GTC")
        self.entry_id = order.client_id
        self.state = CycleState.WAIT_ENTRY
        return [Place(order)]

    def resume(self) -> list[Intent]:
        """Re-sends the hedge remainder after the cycle paused on an incomplete hedge."""
        if self.state is not CycleState.PAUSED:
            raise EngineError(f"cannot resume from {self.state.value}")
        target, self.resume_state = self.resume_state or CycleState.HEDGED, None
        if target is CycleState.HEDGE_PENDING:
            if self.hedge_filled >= self.q0:
                self.state = CycleState.HEDGED
                return []
            return self._place_hedge()
        self.state = target
        return []

    def close(self) -> list[Intent]:
        """Manual full close of the cycle (R14). Q20 default: everything at market."""
        if self.state is CycleState.FINISHED:
            raise EngineError("cycle already finished")
        if self.state is CycleState.WAIT_SIGNAL:
            self.state, self.note = CycleState.FINISHED, "closed before entry"
            return []
        self.state, self.note = CycleState.CLOSING, "close requested"
        return self._close_progress()

    def enter_recovery(self, reason: str) -> None:
        self.recovery = reason

    def resolve_recovery(self) -> None:
        self.recovery = None

    # ---- exchange events ------------------------------------------------

    def on_fill(self, fill: Fill) -> list[Intent]:
        order = self.orders.get(fill.client_id)
        if order is None or not order.apply_fill(fill):
            return []
        self.books[order.position_side].apply(order.position_side, order.opening,
                                              fill.qty, fill.price, fill.commission)
        return self._progress(order)

    def on_status(self, client_id: str, status: OrderStatus, reason: str | None = None) -> list[Intent]:
        """Apply an exchange order status. Callers deliver fills before terminal statuses."""
        order = self.orders.get(client_id)
        if order is None or order.is_terminal:
            return []
        order.status = status
        if reason:
            order.reason = reason
        return self._progress(order)

    def on_quote(self, bid: Decimal, ask: Decimal, now: float) -> list[Intent]:
        self.bid, self.ask, self.now = bid, ask, now
        if self.recovery:
            return []  # price-driven decisions wait for reconciliation
        if self.state in (CycleState.ENTRY_PARTIAL, CycleState.SINGLE_POSITION):
            return self._check_hedge_triggers()
        if self.state is CycleState.HEDGED:
            return self._manage_adds()
        if self.state is CycleState.CLOSING:
            return self._close_progress()
        return []

    # ---- internals ------------------------------------------------------

    def _order(self, purpose: Purpose, side: Side, opening: bool, qty: Decimal, *,
               price: Decimal | None = None, tif: str | None = None, order_type: str = "LIMIT") -> Order:
        self.seq += 1
        order = Order(client_id=f"st-{self.id}-{_CODES[purpose]}{self.seq}", purpose=purpose,
                      position_side=side, opening=opening, qty=qty, order_type=order_type,
                      price=price if order_type == "LIMIT" else None,
                      tif=tif if order_type == "LIMIT" else None)
        self.orders[order.client_id] = order
        return order

    def _cancel(self, order: Order) -> list[Intent]:
        if order.is_terminal or order.cancel_requested:
            return []
        order.cancel_requested = True
        return [Cancel(order.client_id)]

    def _ready(self, key: str) -> bool:
        return self.now >= self.retry_at.get(key, 0.0)

    def _cooldown(self, key: str) -> None:
        self.retry_at[key] = self.now + self.cfg.retry_cooldown_s

    def _progress(self, order: Order) -> list[Intent]:
        if order.purpose is Purpose.ENTRY:
            out = self._entry_progress(order)
        elif order.purpose is Purpose.HEDGE:
            out = self._hedge_progress(order)
        elif order.purpose in (Purpose.ADD, Purpose.ADD_EXIT):
            out = self._add_progress(order)
        else:
            out = []
            if order.is_terminal and order.filled < order.qty:
                self._cooldown(f"close:{order.position_side.value}")
        if self.state is CycleState.CLOSING:
            out += self._close_progress()
        return out

    # first entry and base hedge

    def _entry_progress(self, order: Order) -> list[Intent]:
        if self.state not in (CycleState.WAIT_ENTRY, CycleState.ENTRY_PARTIAL):
            return []
        if not order.is_terminal:
            if order.filled > 0:
                self.state = CycleState.ENTRY_PARTIAL
            return []
        if order.filled == 0:
            self.state = CycleState.FINISHED
            self.note = f"entry {order.status.value} without fills {order.reason or ''}".strip()
            return []
        self.state = CycleState.SINGLE_POSITION
        return self._place_hedge() if self.branch else []

    def _check_hedge_triggers(self) -> list[Intent]:
        if self.branch is None:
            if not self._ready("hedge"):
                return []
            # Executable price of the hedge order (Q10 default): bid when selling, ask when buying.
            first_long = self.first_side is Side.LONG
            price = self.bid if first_long else self.ask
            pos, neg = self.positive_threshold(), self.negative_threshold()
            hit_pos = price >= pos if first_long else price <= pos
            hit_neg = price <= neg if first_long else price >= neg
            if not (hit_pos or hit_neg):
                return []
            self.branch = POSITIVE if hit_pos else NEGATIVE  # first reached wins (R17)
            self.note = f"{self.branch} hedge threshold reached at {price}"
        entry = self.entry
        if not entry.is_terminal:
            # Q30 default: cancel the entry remainder and fix Q0 at the final fill before hedging.
            return self._cancel(entry)
        return self._place_hedge() if self.state is CycleState.SINGLE_POSITION else []

    def _place_hedge(self) -> list[Intent]:
        side, qty = self.first_side.opposite, self.q0 - self.hedge_filled
        if self.branch == POSITIVE:
            order = self._order(Purpose.HEDGE, side, True, qty, price=self.positive_threshold(), tif="GTC")
        elif self.cfg.negative_hedge_order == "LIMIT":
            order = self._order(Purpose.HEDGE, side, True, qty, price=self.negative_threshold(), tif="GTC")
        else:
            order = self._order(Purpose.HEDGE, side, True, qty, order_type="MARKET")
        self.hedge_ids.append(order.client_id)
        self.state = CycleState.HEDGE_PENDING
        return [Place(order)]

    def _hedge_progress(self, order: Order) -> list[Intent]:
        if self.state is not CycleState.HEDGE_PENDING:
            return []
        if self.hedge_filled >= self.q0:
            self.state = CycleState.HEDGED
            self.note = f"base hedge formed, net spread {self.base_net_spread():.6f} USDT"
            return []
        if not order.is_terminal:
            return []  # partial hedge: keep waiting for the remainder (R22)
        if self.hedge_filled == 0 and len(self.hedge_ids) < self.cfg.hedge_max_attempts:
            self.state, self.branch = CycleState.SINGLE_POSITION, None
            self._cooldown("hedge")
            self.note = f"hedge {order.status.value} without fills ({order.reason}); thresholds re-armed"
            return []
        self.state, self.resume_state = CycleState.PAUSED, CycleState.HEDGE_PENDING
        self.note = (f"hedge incomplete {self.hedge_filled}/{self.q0} after {order.status.value} "
                     f"({order.reason}); resume re-sends the remainder, close exits")
        return []

    def base_net_spread(self) -> Decimal | None:
        """Actual base spread after both opening fees, from real fills."""
        filled = self.hedge_filled
        if not filled:
            return None
        hedges = [self.orders[h] for h in self.hedge_ids]
        avg = sum((o.notional for o in hedges), ZERO) / filled
        fees = sum((o.commission for o in hedges), ZERO)
        return pricing.gross_hedge_spread(self.first_side, self.e0, filled, avg) - self.f0 - fees

    # adds

    def _live_add(self, side: Side) -> Add | None:
        return next((a for a in self.adds if a.side is side and not a.closed), None)

    def _exit_filled(self, add: Add) -> Decimal:
        return sum((self.orders[x].filled for x in add.exit_ids), ZERO)

    def _manage_adds(self) -> list[Intent]:
        out: list[Intent] = []
        for side in Side:
            add = self._live_add(side)
            if add is None:
                if self._ready(f"add:{side.value}"):
                    out += self._place_add(side)
                continue
            entry = self.orders[add.entry_id]
            if not entry.is_terminal:
                if entry.filled and self._exit_reached(add):
                    out += self._cancel(entry)  # partly filled and already back at its exit: take what filled
            elif add.exit_id is None and self._ready(f"exit:{add.id}"):
                out.append(self._place_add_exit(add))
        return out

    def _place_add(self, side: Side) -> list[Intent]:
        """A resting limit at the trigger: it fills when price gets there, even while the bot is offline."""
        trigger = self.add_trigger(side)
        qty = self.add_qty(trigger) if trigger is not None else None
        if qty is None:
            return []
        order = self._order(Purpose.ADD, side, True, qty, price=trigger, tif="GTC")
        add = Add(id=f"a{self.seq}", side=side, entry_id=order.client_id, trigger=trigger)
        order.add_id = add.id
        self.adds.append(add)
        return [Place(order)]

    def _exit_reached(self, add: Add) -> bool:
        target = self.add_exit(add)
        return self.ask <= target if add.side is Side.SHORT else self.bid >= target

    def _place_add_exit(self, add: Add) -> Place:
        if add.exit_price is None:
            add.exit_price = self.add_exit(add)
        remaining = self.orders[add.entry_id].filled - self._exit_filled(add)
        order = self._order(Purpose.ADD_EXIT, add.side, False, remaining, price=add.exit_price, tif="GTC")
        order.add_id = add.id
        add.exit_id = order.client_id
        add.exit_ids.append(order.client_id)
        return Place(order)

    def _add_progress(self, order: Order) -> list[Intent]:
        add = next(a for a in self.adds if a.id == order.add_id)
        if order.purpose is Purpose.ADD:
            if not order.is_terminal:
                return []
            if order.filled == 0:
                add.closed = True
                add.note = f"entry {order.status.value} {order.reason or ''}".strip()
                if order.status is OrderStatus.REJECTED:
                    self._cooldown(f"add:{add.side.value}")
                return []
            return [] if self.state is CycleState.CLOSING else [self._place_add_exit(add)]
        if self._exit_filled(add) >= self.orders[add.entry_id].filled:
            # Closed. The leg's entry price keeps this add in its average, so the next trigger moves with it.
            add.closed, add.exit_id = True, None
        elif order.is_terminal:
            add.exit_id = None  # re-placed for the remainder after the cooldown
            self._cooldown(f"exit:{add.id}")
        return []

    # close

    def _close_progress(self) -> list[Intent]:
        working = [o for o in self.orders.values() if not o.is_terminal and o.purpose is not Purpose.CLOSE]
        out: list[Intent] = []
        for order in working:
            out += self._cancel(order)
        if working:
            return out  # wait for final fills of cancelled orders before sizing the close
        closing = [o for o in self.orders.values() if o.purpose is Purpose.CLOSE and not o.is_terminal]
        for side in Side:
            qty = self.books[side].qty
            busy = any(o.position_side is side for o in closing)
            if qty > 0 and not busy and self._ready(f"close:{side.value}"):
                order = self._order(Purpose.CLOSE, side, False, qty, order_type="MARKET")
                closing.append(order)
                out.append(Place(order))
        if not closing and all(b.qty == 0 for b in self.books.values()):
            self.state, self.note = CycleState.FINISHED, "cycle closed"
        return out

    # reporting

    def summary(self) -> dict:
        long, short = self.books[Side.LONG], self.books[Side.SHORT]
        mid = (self.bid + self.ask) / 2 if self.bid is not None and self.ask is not None else None
        unrealized = None
        if mid is not None:
            unrealized = (pricing.side_pnl(Side.LONG, long.qty, long.avg, mid)
                          + pricing.side_pnl(Side.SHORT, short.qty, short.avg, mid))
        realized, fees = long.realized + short.realized, long.fees + short.fees
        thresholds = None
        if self.q0 and self.state in (CycleState.ENTRY_PARTIAL, CycleState.SINGLE_POSITION):
            thresholds = {"positive": self.positive_threshold(), "negative": self.negative_threshold()}
        add_triggers = None  # per leg: where the next add happens; None while that leg's add is open
        if self.state is CycleState.HEDGED:
            add_triggers = {}
            for side in Side:
                add = self._live_add(side)
                waiting = add is not None and not self.orders[add.entry_id].filled
                add_triggers[side.value] = add.trigger if waiting else None if add else self.add_trigger(side)
        return {
            "cycle": self.id, "state": self.display_state.value, "note": self.note,
            "recovery": self.recovery, "first_side": self.first_side and self.first_side.value,
            "e0": self.e0, "q0": self.q0, "f0": self.f0, "branch": self.branch,
            "thresholds": thresholds, "base_net_spread": self.base_net_spread(),
            "long": long, "short": short, "delta": self.delta,
            "spread_of_averages": short.avg - long.avg if long.qty and short.qty else None,
            "add_triggers": add_triggers,
            "closed_adds_result": sum((self.add_result(a) for a in self.adds), ZERO),
            "realized": realized, "unrealized": unrealized, "fees": fees, "funding": self.funding,
            "total": None if unrealized is None else realized + unrealized + self.funding - fees,
            "bid": self.bid, "ask": self.ask,
        }
