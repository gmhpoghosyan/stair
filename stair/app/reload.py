"""Applies code and config edits without touching the terminal.

`run` and `dashboard` re-launch themselves as a child process under `supervise`. The child
watches the package sources plus config.toml and .env, exits with RESTART_EXIT when one
changes (the bot only between ticks, never mid-request), and the supervisor starts it again.
A child that crashes, e.g. on a half-saved file, is restarted on the next file change.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

CHILD_VAR = "STAIR_RELOAD_CHILD"  # "first" or "restart" inside the supervised process
RESTART_EXIT = 3
_PACKAGE = Path(__file__).resolve().parents[1]


def is_child() -> bool:
    return CHILD_VAR in os.environ


def is_restart() -> bool:
    return os.environ.get(CHILD_VAR) == "restart"


class SourceWatcher:
    """Reports a change to the package sources or `extra` files, scanning at most once per `every_s`."""

    def __init__(self, extra: list[Path] = (), every_s: float = 1.0):
        self.extra, self.every_s = [Path(p) for p in extra], every_s
        self._snapshot = self._scan()
        self._checked = time.monotonic()
        self.fired = False

    def _scan(self) -> dict[Path, int | None]:
        mtimes: dict[Path, int | None] = {}
        # Python only: the dashboard page is read per request and reloads itself in the browser.
        for f in [*_PACKAGE.rglob("*.py"), *self.extra]:
            try:
                mtimes[f] = f.stat().st_mtime_ns
            except OSError:
                mtimes[f] = None
        return mtimes

    def changed(self) -> bool:
        if not self.fired and time.monotonic() - self._checked >= self.every_s:
            self._checked = time.monotonic()
            self.fired = self._scan() != self._snapshot
        return self.fired


def supervise(argv: list[str], watched: list[Path]) -> int:
    restart = False
    while True:
        env = {**os.environ, CHILD_VAR: "restart" if restart else "first"}
        child = subprocess.Popen([sys.executable, "-m", "stair", *argv], env=env)
        try:
            code = child.wait()
        except KeyboardInterrupt:
            try:
                child.wait(timeout=15)  # the child got Ctrl+C too and is stopping cleanly
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                child.kill()
            return 130
        restart = True
        if code == RESTART_EXIT:
            print("[reload] change detected, restarting", flush=True)
            continue
        if code == 0:
            return 0
        print(f"[reload] exited with code {code}; it restarts on the next file change", flush=True)
        watcher = SourceWatcher(watched)
        try:
            while not watcher.changed():
                time.sleep(0.5)
        except KeyboardInterrupt:
            return code
