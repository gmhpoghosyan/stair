"""SQLite journal: cycle snapshots, an append-only event log, raw trades and the command queue.
State lives on disk so a restart can reconcile instead of starting blind (section 10).
Each symbol has at most one active cycle, stored under kv key `cycle:<SYMBOL>`."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from ..core.models import Trade
from ..core.serde import from_plain, to_plain
from ..strategy.engine import Cycle

journal = logging.getLogger("stair.journal")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, cycle_id TEXT, kind TEXT NOT NULL, payload TEXT);
CREATE TABLE IF NOT EXISTS trades (
    symbol TEXT NOT NULL, trade_id INTEGER NOT NULL, order_id INTEGER NOT NULL, time_ms INTEGER NOT NULL,
    raw TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (symbol, trade_id));
CREATE TABLE IF NOT EXISTS cycles (id TEXT PRIMARY KEY, finished_ts REAL NOT NULL, snapshot TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', result TEXT);
CREATE INDEX IF NOT EXISTS trades_unprocessed ON trades(symbol, time_ms) WHERE processed=0;
CREATE INDEX IF NOT EXISTS commands_pending ON commands(id) WHERE status='pending';
"""
_CYCLE = "cycle:"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._db.executescript(_SCHEMA)

    def _migrate(self) -> None:
        """Journals from the single-symbol version: trades were keyed by trade id alone (ids are per
        symbol on Binance) and the one active cycle sat under kv key 'cycle'. Dropped trades are read
        again from the exchange; fills already applied are skipped by trade id (T10)."""
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(trades)")]
        if cols and "symbol" not in cols:
            self._db.execute("DROP TABLE trades")
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE name='kv'").fetchone():
            row = self._db.execute("SELECT value FROM kv WHERE key='cycle'").fetchone()
            if row:
                symbol = json.loads(row[0])["rules"]["symbol"]
                self._db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (_CYCLE + symbol, row[0]))
                self._db.execute("DELETE FROM kv WHERE key='cycle'")

    def close(self) -> None:
        self._db.close()

    # key/value

    def get(self, key: str):
        row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, key: str, value) -> None:
        self._db.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(to_plain(value))))

    # cycle snapshots

    def save_cycle(self, cycle: Cycle) -> None:
        if cycle.is_active:
            self.set(_CYCLE + cycle.symbol, cycle)
            return
        self._db.execute("INSERT OR REPLACE INTO cycles(id, finished_ts, snapshot) VALUES(?, ?, ?)",
                         (cycle.id, time.time(), json.dumps(to_plain(cycle))))
        self._db.execute("DELETE FROM kv WHERE key=?", (_CYCLE + cycle.symbol,))

    def load_cycles(self) -> dict[str, Cycle]:
        """Active cycles by symbol."""
        rows = self._db.execute("SELECT key, value FROM kv WHERE key LIKE ? ORDER BY key", (_CYCLE + "%",)).fetchall()
        return {key[len(_CYCLE):]: from_plain(Cycle, json.loads(value)) for key, value in rows}

    def last_finished_cycle(self) -> Cycle | None:
        row = self._db.execute("SELECT snapshot FROM cycles ORDER BY finished_ts DESC LIMIT 1").fetchone()
        return from_plain(Cycle, json.loads(row[0])) if row else None

    def finished_cycles(self, limit: int = 500) -> list[tuple[float, Cycle]]:
        """Oldest first."""
        rows = self._db.execute("SELECT finished_ts, snapshot FROM cycles ORDER BY finished_ts DESC LIMIT ?",
                                (limit,)).fetchall()
        return [(r[0], from_plain(Cycle, json.loads(r[1]))) for r in reversed(rows)]

    # event log

    def log(self, kind: str, payload=None, cycle_id: str | None = None) -> None:
        text = json.dumps(to_plain(payload))
        self._db.execute("INSERT INTO events(ts, cycle_id, kind, payload) VALUES(?, ?, ?, ?)",
                         (time.time(), cycle_id, kind, text))
        journal.info("%-8s %-8s %s", cycle_id or "-", kind, text)  # the run command writes it to a file

    def recent_events(self, limit: int = 30) -> list[tuple]:
        return self._db.execute("SELECT ts, cycle_id, kind, payload FROM events ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()[::-1]

    # trades (idempotent by symbol and trade id)

    def add_trades(self, symbol: str, trades: list[Trade]) -> None:
        self._db.executemany(
            "INSERT OR IGNORE INTO trades(symbol, trade_id, order_id, time_ms, raw) VALUES(?, ?, ?, ?, ?)",
            [(symbol, t.trade_id, t.order_id, t.time_ms, json.dumps(to_plain(t))) for t in trades])

    def unprocessed_trades(self, symbol: str, since_ms: int) -> list[Trade]:
        rows = self._db.execute("SELECT raw FROM trades WHERE symbol=? AND processed=0 AND time_ms>=? "
                                "ORDER BY trade_id", (symbol, since_ms)).fetchall()
        return [from_plain(Trade, json.loads(r[0])) for r in rows]

    def mark_processed(self, symbol: str, trade_id: int) -> None:
        self._db.execute("UPDATE trades SET processed=1 WHERE symbol=? AND trade_id=?", (symbol, trade_id))

    def last_trade_id(self, symbol: str) -> int | None:
        return self._db.execute("SELECT MAX(trade_id) FROM trades WHERE symbol=?", (symbol,)).fetchone()[0]

    # command queue

    def enqueue(self, kind: str, payload: dict) -> int:
        cur = self._db.execute("INSERT INTO commands(ts, kind, payload) VALUES(?, ?, ?)",
                               (time.time(), kind, json.dumps(payload)))
        return cur.lastrowid

    def pending_commands(self) -> list[tuple[int, str, dict]]:
        rows = self._db.execute("SELECT id, kind, payload FROM commands WHERE status='pending' ORDER BY id").fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def has_pending_commands(self) -> bool:
        return self._db.execute("SELECT 1 FROM commands WHERE status='pending' LIMIT 1").fetchone() is not None

    def finish_command(self, command_id: int, ok: bool, result: str) -> None:
        self._db.execute("UPDATE commands SET status=?, result=? WHERE id=?",
                         ("done" if ok else "error", result, command_id))

    def command_result(self, command_id: int) -> tuple[str, str | None]:
        row = self._db.execute("SELECT status, result FROM commands WHERE id=?", (command_id,)).fetchone()
        return row[0], row[1]
