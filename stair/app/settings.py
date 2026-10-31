"""Loads config.toml and API keys from .env. Live trading needs an explicit second switch (R15, T13)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..core.serde import from_plain
from ..strategy import StrategyConfig

LIVE_CONFIRM_VAR = "STAIR_LIVE_CONFIRM"
LIVE_CONFIRM_VALUE = "I_ACCEPT_REAL_MONEY_RISK"


@dataclass(frozen=True)
class Settings:
    env: str
    api_key: str
    api_secret: str
    data_dir: Path
    poll_interval_s: float
    fast_poll_interval_s: float
    trigger_price: str
    reconcile_every_s: float
    strategy: StrategyConfig

    @property
    def db_path(self) -> Path:
        return self.data_dir / f"stair-{self.env}.sqlite"  # demo and live journals never mix

    @property
    def lock_path(self) -> Path:
        return self.data_dir / f"stair-{self.env}.lock"


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(config_path: Path = Path("config.toml"), env_path: Path = Path(".env"),
                  env_override: str | None = None) -> Settings:
    config = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    exchange = config.get("exchange", {})
    runtime = config.get("runtime", {})
    env = (env_override or exchange.get("env", "demo")).lower()
    if env not in ("demo", "live"):
        raise SystemExit(f"exchange.env must be demo or live, got {env!r}")
    secrets = {**read_dotenv(env_path), **os.environ}
    if env == "live" and secrets.get(LIVE_CONFIRM_VAR) != LIVE_CONFIRM_VALUE:
        raise SystemExit(f"live trading is locked. Demo, then backtest, then live (R15). To unlock set "
                         f"{LIVE_CONFIRM_VAR}={LIVE_CONFIRM_VALUE} in the environment.")
    prefix = f"BINANCE_{env.upper()}_"
    key, secret = secrets.get(prefix + "API_KEY"), secrets.get(prefix + "API_SECRET")
    if not key or not secret:
        raise SystemExit(f"missing {prefix}API_KEY / {prefix}API_SECRET in {env_path} or environment")
    return Settings(env=env, api_key=key, api_secret=secret,
                    data_dir=Path(runtime.get("data_dir", "data")),
                    poll_interval_s=float(runtime.get("poll_interval_s", 1.0)),
                    fast_poll_interval_s=float(runtime.get("fast_poll_interval_s", 0.25)),
                    trigger_price=runtime.get("trigger_price", "book"),
                    reconcile_every_s=float(runtime.get("reconcile_every_s", 15.0)),
                    strategy=from_plain(StrategyConfig, config.get("strategy", {})))
