"""Control component: user commands travel through the store's queue to the running bot.
The CLI uses it today; a local web UI (Q23) can reuse the same functions."""
from __future__ import annotations

import time

from ..storage import Store

KINDS = {"enter", "level_add", "level_rm", "pause", "resume", "close", "ack_recovery"}


def submit(store: Store, kind: str, payload: dict | None = None, wait_s: float = 6.0) -> tuple[str, str | None]:
    """Queue a command and wait briefly for the runner's answer.
    Returns ("pending", None) when no runner picked it up in time."""
    if kind not in KINDS:
        raise ValueError(f"unknown command {kind}")
    command_id = store.enqueue(kind, payload or {})
    deadline = time.time() + wait_s
    while time.time() < deadline:
        status, result = store.command_result(command_id)
        if status != "pending":
            return status, result
        time.sleep(0.05)
    return "pending", None
