"""Core domain exceptions.

The typed domain model this package used to re-export (Side, Signal, Tick,
PaperOrder, PaperPosition, PaperSnapshot, ...) was never used by any live code
path — it was a parallel, unused vocabulary next to the real one in
`polymarket_bot`. Removed 2026-09-21; see the cleanup plan for the rebuild
that replaces it with one domain model the live path actually uses.
"""

from .exceptions import BtcBotError, FeedError

__all__ = [
    "BtcBotError",
    "FeedError",
]
