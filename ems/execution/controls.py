"""What every placement checks first: the operator's mode, the kill switch and the spread.

- :func:`requested_mode`: the operator's PAPER/LIVE selection, read each pass.
- :func:`kill_switch_path` / :func:`kill_switch_active`: the kill switch file. While it exists
  no strategy places a new order. A file that cannot be checked counts as present.
- :func:`check_passive`: every order only rests. A buy at or above the best ask, or a sell at
  or below the best bid, is refused before anything is written (``PlacementRefused``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ems import config as _config  # type: ignore[import-untyped]
from ems import db as _db  # type: ignore[import-untyped]
from ems.execution.tape import as_float

# The operator's PAPER/LIVE choice. The key keeps its historical name so existing databases
# still read.
MODE_KEY = "polymarket_bot.requested_mode"

_PRICE_EPS = 1e-9


class PlacementRefused(RuntimeError):
    """Orders were not placed, and nothing was written. ``reason`` is a short code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class PassiveOrder(Protocol):
    """What :func:`check_passive` reads from an order."""

    @property
    def order_side(self) -> str: ...

    @property
    def token_id(self) -> str: ...

    @property
    def price(self) -> float: ...


async def requested_mode() -> str:
    """The operator's PAPER/LIVE selection (the dashboard's toggle), lower case."""
    raw = await _db.get_config(MODE_KEY, _config.BOT_MODE)
    return str(raw or "paper").strip().lower()


def kill_switch_path(path: Path | str | None = None) -> Path:
    """``path``, else the configured kill switch file (read at call time)."""
    return Path(path) if path is not None else Path(_config.KILL_SWITCH_PATH)


def kill_switch_active(path: Path | str | None = None) -> bool:
    """True while the kill switch file exists, or when it cannot be checked."""
    try:
        return kill_switch_path(path).exists()
    except OSError:
        return True  # cannot tell: place nothing


def check_passive(order: PassiveOrder, best_asks: Mapping[str, float | None],
                  best_bids: Mapping[str, float | None]) -> None:
    """Refuse an order that would cross: a buy at or above the best ask, a sell at or below
    the best bid."""
    sell = order.order_side == "SELL"
    book, name = (best_bids, "bid") if sell else (best_asks, "ask")
    if order.token_id not in book:
        raise PlacementRefused(
            f"no_{name}", f"No best {name} was given for token {order.token_id}, so the "
            f"{'sale' if sell else 'bid'} cannot be checked against the spread."
        )
    quote = book[order.token_id]
    if quote is None:
        return  # that side of the book is empty: nothing to cross
    value = as_float(quote)
    if value is None or not 0.0 <= value <= 1.0:
        raise PlacementRefused(
            f"bad_{name}", f"The best {name} {quote!r} for token {order.token_id} is not a price."
        )
    if sell and order.price <= value + _PRICE_EPS:
        raise PlacementRefused(
            "would_cross",
            f"A sale at {order.price:.3f} would meet the bid at {value:.3f}; sales only ever "
            "rest above the bid.",
        )
    if not sell and order.price >= value - _PRICE_EPS:
        raise PlacementRefused(
            "would_cross",
            f"A bid at {order.price:.3f} would meet the ask at {value:.3f}; bids only ever rest "
            "below the ask.",
        )
