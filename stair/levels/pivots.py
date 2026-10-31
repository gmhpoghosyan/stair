"""Support/resistance from confirmed pivots, following the logic of LuxAlgo's
"Support and Resistance Levels with Breaks" indicator (CC BY-NC-SA 4.0, © LuxAlgo):

    highUsePivot = fixnan(pivothigh(left, right)[1])   -> resistance
    lowUsePivot  = fixnan(pivotlow(left, right)[1])    -> support
    osc = 100 × (ema(volume, 5) − ema(volume, 10)) / ema(volume, 10)
    Support Broken    = crossunder(close, support)    and osc > threshold
    Resistance Broken = crossover(close, resistance)  and osc > threshold

Input must be closed candles only, so nothing here can see the future (T15).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Candle:
    open_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    close_ms: int


@dataclass
class LevelConfig:
    enabled: bool = True
    interval: str = "30m"        # R12: working levels on 30m/1h
    left_bars: int = 15
    right_bars: int = 15
    volume_thresh: float = 20.0
    refresh_s: float = 60.0
    candles: int = 500


@dataclass
class LevelSnapshot:
    interval: str
    resistance: Decimal | None
    support: Decimal | None
    resistance_ms: int | None    # open time of the pivot candle
    support_ms: int | None
    bar_close_ms: int            # last closed candle used
    close: Decimal
    volume_osc: float | None
    signal: str | None           # "support_broken" | "resistance_broken" on the last closed bar


def pivots(values: list[Decimal], left: int, right: int, high: bool) -> list[Decimal | None]:
    """Pine-style pivothigh/pivotlow: element i holds the pivot confirmed at bar i, i.e. the
    value of bar i − right when it beats the `left` bars before it and is not beaten by the
    `right` bars after it."""
    out: list[Decimal | None] = [None] * len(values)
    for i in range(left + right, len(values)):
        c = i - right
        center, before, after = values[c], values[c - left:c], values[c + 1:i + 1]
        if high and all(center > v for v in before) and all(center >= v for v in after):
            out[i] = center
        if not high and all(center < v for v in before) and all(center <= v for v in after):
            out[i] = center
    return out


def ema(values: list[float], length: int) -> list[float]:
    alpha, out = 2.0 / (length + 1), []
    for v in values:
        out.append(v if not out else alpha * v + (1 - alpha) * out[-1])
    return out


def analyze(candles: list[Candle], cfg: LevelConfig) -> LevelSnapshot | None:
    n = len(candles)
    if n < cfg.left_bars + cfg.right_bars + 2:
        return None
    ph = pivots([c.high for c in candles], cfg.left_bars, cfg.right_bars, high=True)
    pl = pivots([c.low for c in candles], cfg.left_bars, cfg.right_bars, high=False)
    res: list[Decimal | None] = []
    sup: list[Decimal | None] = []
    res_ms = sup_ms = None
    for i in range(n):
        # [1] then fixnan: a pivot confirmed on bar i−1 becomes the level from bar i on.
        if i >= 1 and ph[i - 1] is not None:
            res_ms = candles[i - 1 - cfg.right_bars].open_ms
            res.append(ph[i - 1])
        else:
            res.append(res[-1] if res else None)
        if i >= 1 and pl[i - 1] is not None:
            sup_ms = candles[i - 1 - cfg.right_bars].open_ms
            sup.append(pl[i - 1])
        else:
            sup.append(sup[-1] if sup else None)
    volume = [float(c.volume) for c in candles]
    fast, slow = ema(volume, 5), ema(volume, 10)
    osc = 100 * (fast[-1] - slow[-1]) / slow[-1] if slow[-1] else None
    last, prev = candles[-1], candles[-2]
    signal = None
    if osc is not None and osc > cfg.volume_thresh:
        if sup[-1] is not None and sup[-2] is not None and last.close < sup[-1] and prev.close >= sup[-2]:
            signal = "support_broken"
        elif res[-1] is not None and res[-2] is not None and last.close > res[-1] and prev.close <= res[-2]:
            signal = "resistance_broken"
    return LevelSnapshot(interval=cfg.interval, resistance=res[-1], support=sup[-1],
                         resistance_ms=res_ms, support_ms=sup_ms, bar_close_ms=last.close_ms,
                         close=last.close, volume_osc=osc, signal=signal)
