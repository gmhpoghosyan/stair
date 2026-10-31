"""Cycle state machine: first entry -> base hedge -> repeated adds -> manual close.

Pure logic with no I/O. The execution component feeds fills, order statuses and
quotes in and sends the returned intents to an exchange gateway. The backtest is
meant to drive the same object (spec section 10).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from ..core import pricing
from ..core.models import (ZERO, AddState, Cancel, CycleState, Fill, Intent, MarketRules, Order,
                           OrderStatus, Place, Purpose, Side)
from .config import StrategyConfig

POSITIVE, NEGATIVE = "positive", "negative"
_CODES = {Purpose.ENTRY: "E", Purpose.HEDGE: "H", Purpose.ADD_OPEN: "A",
          Purpose.ADD_EXIT: "X", Purpose.CLOSE: "C"}


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
class Level:
    """A support (opens LONG adds) or resistance (opens SHORT adds) given by the user (R08)."""
    id: int
    side: Side
    price: Decimal
    exit_price: Decimal | None = None
    add_id: str | None = None  # add currently bound to this level
    source: str = "manual"     # manual | auto (pivot levels component)


@dataclass
class Add:
    id: str
    side: Side
    level_id: int
    qty: Decimal
    price: Decimal
    exit_price: Decimal
    entry_id: str
    state: AddState = AddState.PENDING_ENTRY
    exit_id: str | None = None  # working exit order, if any
    exit_ids: list[str] = field(default_factory=list)
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
    adds: dict[str, Add] = field(default_factory=dict)
    levels: list[Level] = field(default_factory=list)
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

    def _level_price(self, side: Side, price: Decimal) -> Decimal:
        return pricing.round_to(price, self.rules.tick_size, up=side is Side.SHORT)

    def sync_auto_levels(self, resistance: Decimal | None, support: Decimal | None) -> list[Intent]:
        """Keep one auto resistance and one auto support in line with the latest pivots.
        A moved level cancels its unfilled add entry; a filled add keeps its exit."""
        out: list[Intent] = []
        for side, price in ((Side.SHORT, resistance), (Side.LONG, support)):
            current = next((lv for lv in self.levels if lv.source == "auto" and lv.side is side), None)
            if current and price is not None and current.price == self._level_price(side, price):
                continue
            if current:
                out += self.remove_level(current.id)
            if price is not None:
                self.add_level(side, price, source="auto")
        return out

    def add_level(self, side: Side, price: Decimal, exit_price: Decimal | None = None,
                  source: str = "manual") -> Level:
        tick = self.rules.tick_size
        price = self._level_price(side, price)
        if exit_price is not None:
            exit_price = pricing.round_to(exit_price, tick, up=side is Side.LONG)
            if (side is Side.SHORT and exit_price >= price) or (side is Side.LONG and exit_price <= price):
                raise EngineError("exit must be below a resistance level and above a support level")
        self.seq += 1  # ids are never reused, so adds and cooldowns stay bound to the right level
        level = Level(id=self.seq, side=side, price=price, exit_price=exit_price, source=source)
        self.levels.append(level)
        return level

    def remove_level(self, level_id: int) -> list[Intent]:
        level = self._level(level_id)
        if level is None:
            raise EngineError(f"no level {level_id}")
        self.levels.remove(level)
        out: list[Intent] = []
        if level.add_id:
            entry = self.orders[self.adds[level.add_id].entry_id]
            out += self._cancel(entry)  # an already filled add keeps its exit
        return out

    def pause(self) -> list[Intent]:
        """Q24 default: stop new adds, cancel unfilled add entries, keep managing exits."""
        if self.state is not CycleState.HEDGED:
            raise EngineError("pause applies only after the base hedge is formed; use close instead")
        self.state, self.resume_state = CycleState.PAUSED, CycleState.HEDGED
        out: list[Intent] = []
        for add in self._live_adds():
            if add.state in (AddState.PENDING_ENTRY, AddState.PARTIAL_ENTRY):
                out += self._cancel(self.orders[add.entry_id])
        return out

    def resume(self) -> list[Intent]:
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
        if self.state in (CycleState.HEDGED, CycleState.PAUSED):
            return self._manage_adds()
        if self.state is CycleState.CLOSING:
            return self._close_progress()
        return []

    # ---- internals ------------------------------------------------------

    def _order(self, purpose: Purpose, side: Side, opening: bool, qty: Decimal, *,
               price: Decimal | None = None, tif: str | None = None,
               order_type: str = "LIMIT", add_id: str | None = None) -> Order:
        self.seq += 1
        order = Order(client_id=f"st-{self.id}-{_CODES[purpose]}{self.seq}", purpose=purpose,
                      position_side=side, opening=opening, qty=qty, order_type=order_type,
                      price=price if order_type == "LIMIT" else None,
                      tif=tif if order_type == "LIMIT" else None, add_id=add_id)
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

    def _level(self, level_id: int) -> Level | None:
        return next((lv for lv in self.levels if lv.id == level_id), None)

    def _live_adds(self, side: Side | None = None) -> list[Add]:
        return [a for a in self.adds.values()
                if a.state is not AddState.CLOSED and (side is None or a.side is side)]

    def _progress(self, order: Order) -> list[Intent]:
        if order.purpose is Purpose.ENTRY:
            out = self._entry_progress(order)
        elif order.purpose is Purpose.HEDGE:
            out = self._hedge_progress(order)
        elif order.purpose in (Purpose.ADD_OPEN, Purpose.ADD_EXIT):
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

    def add_qty(self, price: Decimal) -> Decimal | None:
        """q = add_fraction × Q0 rounded down to the lot step (R09); None if below exchange
        minimums, since rounding up would add risk (section 9 proposal)."""
        q = pricing.round_to(self.q0 * self.cfg.add_fraction, self.rules.step_size, up=False)
        if q < self.rules.min_qty or q * price < self.rules.min_notional:
            return None
        return q

    def _improves_average(self, side: Side, price: Decimal) -> bool:
        avg = self.books[side].avg
        return price < avg if side is Side.LONG else price > avg

    def _default_exit(self, level: Level) -> Decimal:
        if level.exit_price is not None:
            return level.exit_price
        pct, tick = self.cfg.add_exit_pct, self.rules.tick_size
        if level.side is Side.SHORT:
            return pricing.round_to(level.price * (1 - pct), tick, up=False)
        return pricing.round_to(level.price * (1 + pct), tick, up=True)

    def _exit_reached(self, add: Add) -> bool:
        return self.ask <= add.exit_price if add.side is Side.SHORT else self.bid >= add.exit_price

    def _manage_adds(self) -> list[Intent]:
        out: list[Intent] = []
        for add in self._live_adds():
            entry = self.orders[add.entry_id]
            if add.state in (AddState.PENDING_ENTRY, AddState.PARTIAL_ENTRY):
                if entry.filled > 0 and self._exit_reached(add):
                    out += self._cancel(entry)  # take what filled to its exit
            elif add.exit_id is None and self._ready(f"exit:{add.id}"):
                out.append(self._place_add_exit(add))
        if self.state is CycleState.HEDGED:
            out += self._place_new_adds()
        return out

    def _place_new_adds(self) -> list[Intent]:
        out: list[Intent] = []
        for level in self.levels:
            side = level.side
            if level.add_id or not self._ready(f"level:{level.id}"):
                continue
            if len(self._live_adds(side)) >= self.cfg.max_adds_per_side:
                continue
            if self.cfg.require_avg_improvement and not self._improves_average(side, level.price):
                continue
            # Resting post-only orders: resistance above the bid, support below the ask.
            if (side is Side.SHORT and level.price <= self.bid) or (side is Side.LONG and level.price >= self.ask):
                continue
            qty = self.add_qty(level.price)
            if qty is None:
                continue
            order = self._order(Purpose.ADD_OPEN, side, True, qty, price=level.price, tif="GTX")
            add = Add(id=f"a{self.seq}", side=side, level_id=level.id, qty=qty, price=level.price,
                      exit_price=self._default_exit(level), entry_id=order.client_id)
            order.add_id = add.id
            self.adds[add.id] = add
            level.add_id = add.id
            out.append(Place(order))
        return out

    def _exit_filled(self, add: Add) -> Decimal:
        return sum((self.orders[x].filled for x in add.exit_ids), ZERO)

    def _place_add_exit(self, add: Add) -> Place:
        remaining = self.orders[add.entry_id].filled - self._exit_filled(add)
        order = self._order(Purpose.ADD_EXIT, add.side, False, remaining,
                            price=add.exit_price, tif="GTC", add_id=add.id)
        add.exit_id = order.client_id
        add.exit_ids.append(order.client_id)
        add.state = AddState.PARTIAL_EXIT if self._exit_filled(add) else AddState.PENDING_EXIT
        return Place(order)

    def _release_level(self, add: Add, cooldown: bool) -> None:
        level = self._level(add.level_id)
        if level and level.add_id == add.id:
            level.add_id = None  # re-arms the level (R11)
            if cooldown:
                self._cooldown(f"level:{level.id}")

    def _add_progress(self, order: Order) -> list[Intent]:
        add = self.adds[order.add_id]
        if order.purpose is Purpose.ADD_OPEN:
            if not order.is_terminal:
                if order.filled > 0:
                    add.state = AddState.PARTIAL_ENTRY
                return []
            if order.filled == 0:
                add.state = AddState.CLOSED
                add.note = f"entry {order.status.value} {order.reason or ''}".strip()
                self._release_level(add, cooldown=order.status is OrderStatus.REJECTED)
                return []
            add.state = AddState.OPEN
            if self.state is CycleState.CLOSING:
                return []
            return [self._place_add_exit(add)]
        if self._exit_filled(add) >= self.orders[add.entry_id].filled:
            add.state, add.exit_id = AddState.CLOSED, None
            self._release_level(add, cooldown=False)
        elif order.is_terminal:
            add.exit_id = None  # re-placed by _manage_adds after the cooldown
            self._cooldown(f"exit:{add.id}")
        elif order.filled > 0:
            add.state = AddState.PARTIAL_EXIT
        return []

    def add_result(self, add: Add) -> Decimal:
        """Virtual add result net of its own fees (section 6, metric 3)."""
        entry = self.orders[add.entry_id]
        exits = [self.orders[x] for x in add.exit_ids]
        closed = sum((o.filled for o in exits), ZERO)
        if not closed:
            return ZERO
        exit_avg = sum((o.notional for o in exits), ZERO) / closed
        entry_fee = entry.commission * closed / entry.filled
        return (pricing.side_pnl(add.side, closed, entry.avg_price, exit_avg)
                - entry_fee - sum((o.commission for o in exits), ZERO))

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
        return {
            "cycle": self.id, "state": self.display_state.value, "note": self.note,
            "recovery": self.recovery, "first_side": self.first_side and self.first_side.value,
            "e0": self.e0, "q0": self.q0, "f0": self.f0, "branch": self.branch,
            "thresholds": thresholds, "base_net_spread": self.base_net_spread(),
            "long": long, "short": short, "delta": self.delta,
            "spread_of_averages": short.avg - long.avg if long.qty and short.qty else None,
            "closed_adds_result": sum((self.add_result(a) for a in self.adds.values()), ZERO),
            "realized": realized, "unrealized": unrealized, "fees": fees, "funding": self.funding,
            "total": None if unrealized is None else realized + unrealized + self.funding - fees,
            "bid": self.bid, "ask": self.ask,
        }
