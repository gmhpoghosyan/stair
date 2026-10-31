"""Domain types shared by every component. No I/O here."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

ZERO = Decimal(0)


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG


class CycleState(str, Enum):
    """Cycle states from spec section 8."""
    IDLE = "IDLE"
    WAIT_SIGNAL = "WAIT_SIGNAL"
    WAIT_ENTRY = "WAIT_ENTRY"
    ENTRY_PARTIAL = "ENTRY_PARTIAL"
    SINGLE_POSITION = "SINGLE_POSITION"
    HEDGE_PENDING = "HEDGE_PENDING"
    HEDGED = "HEDGED"
    CLOSING = "CLOSING"
    PAUSED = "PAUSED"
    RECOVERY = "RECOVERY"
    FINISHED = "FINISHED"


class Purpose(str, Enum):
    ENTRY = "ENTRY"
    HEDGE = "HEDGE"
    ADD = "ADD"
    ADD_EXIT = "ADD_EXIT"
    CLOSE = "CLOSE"


class OrderStatus(str, Enum):
    PENDING = "PENDING"  # created locally, exchange has not confirmed it yet
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}


@dataclass
class MarketRules:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    min_notional: Decimal
    maker_fee: Decimal
    taker_fee: Decimal


@dataclass
class Fill:
    trade_id: int
    client_id: str
    qty: Decimal
    price: Decimal
    commission: Decimal  # in USDT


@dataclass
class Order:
    client_id: str
    purpose: Purpose
    position_side: Side
    opening: bool  # True increases the position side, False reduces it
    qty: Decimal
    order_type: str = "LIMIT"  # LIMIT | MARKET
    price: Decimal | None = None
    tif: str | None = None  # GTC | GTX (post-only)
    add_id: str | None = None  # ADD / ADD_EXIT orders: the add they belong to
    status: OrderStatus = OrderStatus.PENDING
    filled: Decimal = ZERO
    notional: Decimal = ZERO
    commission: Decimal = ZERO
    trade_ids: list[int] = field(default_factory=list)
    exchange_id: int | None = None
    cancel_requested: bool = False
    sent_at: float | None = None
    reason: str | None = None

    @property
    def side(self) -> str:
        """Binance BUY/SELL for this position side and direction (spec section 7)."""
        if self.position_side is Side.LONG:
            return "BUY" if self.opening else "SELL"
        return "SELL" if self.opening else "BUY"

    @property
    def avg_price(self) -> Decimal | None:
        return self.notional / self.filled if self.filled else None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL or self.filled >= self.qty

    def apply_fill(self, fill: Fill) -> bool:
        """Returns False for a fill already counted, so replays never double count (T10)."""
        if fill.trade_id in self.trade_ids:
            return False
        self.trade_ids.append(fill.trade_id)
        self.filled += fill.qty
        self.notional += fill.qty * fill.price
        self.commission += fill.commission
        self.status = OrderStatus.FILLED if self.filled >= self.qty else OrderStatus.PARTIALLY_FILLED
        return True


@dataclass
class Place:
    order: Order


@dataclass
class Cancel:
    client_id: str


Intent = Place | Cancel


# Normalised exchange views, produced by exchange gateways.

@dataclass
class Quote:
    bid: Decimal
    ask: Decimal
    last: Decimal | None = None
    mark: Decimal | None = None


@dataclass
class OrderReport:
    client_id: str
    exchange_id: int
    status: OrderStatus
    executed_qty: Decimal
    reason: str | None = None


@dataclass
class Trade:
    trade_id: int
    order_id: int
    qty: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    time_ms: int


@dataclass
class PositionView:
    side: Side
    qty: Decimal  # absolute
    entry_price: Decimal
    unrealized: Decimal
    leverage: int
    margin_type: str
    liquidation_price: Decimal
    symbol: str = ""
    mark_price: Decimal | None = None
