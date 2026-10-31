"""Control component: command submission API. Depends on storage."""
from .commands import KINDS, submit

__all__ = ["KINDS", "submit"]
