"""Feeds pivot levels from the levels component into the cycle and journals break signals."""
from __future__ import annotations

import logging

from ..core.models import Intent
from ..exchange import ExchangeGateway
from ..levels import LevelConfig, LevelSnapshot, analyze
from ..storage import Store
from ..strategy import Cycle

log = logging.getLogger("stair.levels")


class LevelFeed:
    def __init__(self, gateway: ExchangeGateway, store: Store, symbol: str, cfg: LevelConfig):
        self.gateway, self.store, self.symbol, self.cfg = gateway, store, symbol, cfg
        self.snapshot: LevelSnapshot | None = None
        self._last_fetch = 0.0

    def refresh(self, now: float) -> LevelSnapshot | None:
        if now - self._last_fetch < self.cfg.refresh_s:
            return self.snapshot
        self._last_fetch = now
        snap = analyze(self.gateway.closed_candles(self.symbol, self.cfg.interval, self.cfg.candles), self.cfg)
        if snap is None:
            return self.snapshot
        old = self.snapshot
        if old is None or (old.resistance, old.support) != (snap.resistance, snap.support):
            log.info("%s %s levels: resistance %s support %s", self.symbol, self.cfg.interval,
                     snap.resistance, snap.support)
            self.store.log("levels", snap)
        # One signal per closed bar (R20 candidates; entries stay manual for now).
        if snap.signal and (old is None or old.bar_close_ms != snap.bar_close_ms or old.signal != snap.signal):
            log.warning("SIGNAL %s %s on %s close %s (volume osc %.1f)", self.symbol, snap.signal,
                        self.cfg.interval, snap.close, snap.volume_osc)
            self.store.log("signal", snap)
        self.snapshot = snap
        self.store.set("auto_levels", snap)
        return snap

    def apply(self, cycle: Cycle) -> list[Intent]:
        if self.snapshot is None:
            return []
        return cycle.sync_auto_levels(self.snapshot.resistance, self.snapshot.support)
