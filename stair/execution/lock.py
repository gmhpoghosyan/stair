"""Single-instance lock: one runner per data directory (section 9, two bot copies)."""
from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> InstanceLock:
        self._fh = open(self.path, "a+")
        self._fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise SystemExit(f"another runner holds {self.path}; stop it first")
        return self

    def __exit__(self, *exc) -> None:
        self._fh.close()
