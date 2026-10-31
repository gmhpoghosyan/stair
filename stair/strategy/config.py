from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class StrategyConfig:
    """Strategy parameters. Defaults follow the spec examples; open questions use the
    conservative defaults listed in docs/DECISIONS.md."""
    symbol: str = "SOLUSDT"
    leverage: int = 10                          # R03
    hedge_spread: Decimal = Decimal("0.005")    # h, R05
    adverse_move: Decimal = Decimal("0.003")    # a, R06
    hedge_fee_rate: Decimal | None = None       # fH; None = exchange taker rate (conservative)
    negative_hedge_order: str = "MARKET"        # Q09: MARKET | LIMIT
    hedge_max_attempts: int = 3                 # Q09: re-arms after an unfilled rejection
    add_fraction: Decimal = Decimal("0.1")      # add size = add_fraction x Q0
    add_trigger_pct: Decimal = Decimal("0.01")  # add when price is this far beyond a leg's entry price
    add_exit_pct: Decimal = Decimal("0.005")    # add exit: net profit on the add after its own fees
    retry_cooldown_s: float = 5.0
