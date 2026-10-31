"""Dashboard component: read-only local web view of positions, the active cycle and closed cycles.
Depends on core, strategy, exchange (read calls only) and storage."""
from .data import DashboardData
from .server import make_server

__all__ = ["DashboardData", "make_server"]
