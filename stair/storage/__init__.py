"""Storage component: on-disk journal and command queue. Depends on core and strategy (snapshots)."""
from .store import Store

__all__ = ["Store"]
