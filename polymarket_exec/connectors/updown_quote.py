"""Live top-of-book quote for the current window of any crypto Up/Down market.

The dashboard's order-size ticket prices the operator's share count against the
market they have selected (asset × timeframe), whether or not the trading loop
is running. This module resolves the selection's *current* window and reads
both outcome books from the CLOB.

Slug schemes (verified against Gamma, 2026-09):

* ``5m`` / ``15m`` — clock-floored: ``{asset}-updown-{tf}-{window_start_epoch}``.
* ``1h`` — ET start hour: ``{name}-up-or-down-{month}-{day}-{year}-{h}{am|pm}-et``.
* ``1d`` — resolves at noon ET: ``{name}-up-or-down-on-{month}-{day}-{year}``,
  dated the noon it resolves on (today before noon ET, else tomorrow).

Prices come from the CLOB ``POST /books`` batch endpoint, never Gamma's
``bestAsk`` — Gamma's cached quote was measured ~10¢ behind the live book.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from config import POLYMARKET_CLOB_API, POLYMARKET_GAMMA_API

_ET = ZoneInfo("America/New_York")

# Asset slug prefix → the long name Polymarket uses in 1h / 1d slugs.
_LONG_NAME: dict[str, str] = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "xrp": "xrp",
    "doge": "dogecoin",
    "bnb": "bnb",
}

_CLOCK_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}


@dataclass(frozen=True)
class UpDownQuote:
    """One read of a market's two books. ``error`` set ⇒ prices are ``None``."""

    asset: str
    timeframe: str
    fetched_at: float
    slug: str | None = None
    up_ask: float | None = None
    up_bid: float | None = None
    down_ask: float | None = None
    down_bid: float | None = None
    min_order_size: float | None = None
    error: str | None = None

    def age_seconds(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.fetched_at)


def window_slug(asset: str, timeframe: str, now: datetime) -> str:
    """Slug of the window live at ``now`` (tz-aware) for ``asset``/``timeframe``."""
    if timeframe in _CLOCK_SECONDS:
        step = _CLOCK_SECONDS[timeframe]
        ts = int(now.timestamp())
        return f"{asset}-updown-{timeframe}-{ts - ts % step}"
    name = _LONG_NAME.get(asset)
    if name is None:
        raise ValueError(f"unknown asset {asset!r}")
    et = now.astimezone(_ET)
    if timeframe == "1h":
        hour = et.hour % 12 or 12
        ampm = "am" if et.hour < 12 else "pm"
        return f"{name}-up-or-down-{_date_part(et)}-{hour}{ampm}-et"
    if timeframe == "1d":
        resolves = et if et.hour < 12 else et + timedelta(days=1)
        return f"{name}-up-or-down-on-{_date_part(resolves)}"
    raise ValueError(f"unknown timeframe {timeframe!r}")


def daily_reference_instant(resolves_at: datetime) -> datetime:
    """The noon-ET close a ``1d`` market compares its settlement close against.

    A daily Up/Down market asks whether the noon-ET close on its resolution
    date beat the noon-ET close on the calendar day before. Both legs are
    noon *wall-clock* ET, so the gap is 24h only on a normal day: it is 25h
    across the November fall-back and 23h across the March spring-forward.
    Subtracting a flat 86400s therefore reads the wrong minute on exactly
    those two days a year (verified on Gamma against
    ``ethereum-up-or-down-on-november-2`` and ``-on-march-8``).

    ``resolves_at`` is the market's ``endDate`` (tz-aware); only its ET
    calendar date is used, so an endDate that is not exactly noon still
    lands on the right reference day.
    """
    resolution_noon = resolves_at.astimezone(_ET).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    # Wall-clock arithmetic on an aware datetime: the previous calendar day's
    # noon keeps 12:00 ET and picks up that day's own UTC offset.
    return resolution_noon - timedelta(days=1)


def _date_part(et: datetime) -> str:
    return f"{et.strftime('%B').lower()}-{et.day}-{et.year}"


def _token_ids(market: dict[str, Any]) -> tuple[str, str] | None:
    """(up_token, down_token) aligned to the market's outcome labels."""
    try:
        tokens = _as_list(market.get("clobTokenIds"))
        outcomes = [str(o).lower() for o in _as_list(market.get("outcomes"))]
    except ValueError:
        return None
    if len(tokens) != 2:
        return None
    up = outcomes.index("up") if "up" in outcomes else 0
    return str(tokens[up]), str(tokens[1 - up])


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, list) else []


def _best(levels: Any) -> float | None:
    """Best price of a CLOB level array — books list worst-to-best."""
    if not isinstance(levels, list) or not levels:
        return None
    try:
        return float(levels[-1]["price"])
    except (KeyError, TypeError, ValueError):
        return None


class UpDownQuoteClient:
    """Resolves a selection's current window and reads its two books.

    Token ids are cached per slug (they are fixed for a window's lifetime), so
    a steady poll costs one CLOB request; Gamma is hit once per new window.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._tokens: dict[str, tuple[str, str]] = {}

    async def fetch(
        self, asset: str, timeframe: str, now: datetime | None = None
    ) -> UpDownQuote:
        now = now or datetime.now(UTC)
        base = {"asset": asset, "timeframe": timeframe, "fetched_at": now.timestamp()}
        try:
            slug = window_slug(asset, timeframe, now)
            tokens = await self._resolve_tokens(slug)
            if tokens is None:
                return UpDownQuote(**base, slug=slug, error=f"no market for {slug}")
            books = await self._books(tokens)
        except (httpx.HTTPError, ValueError) as e:
            return UpDownQuote(**base, error=f"{type(e).__name__}: {e}")
        up, down = books.get(tokens[0], {}), books.get(tokens[1], {})
        return UpDownQuote(
            **base,
            slug=slug,
            up_ask=_best(up.get("asks")),
            up_bid=_best(up.get("bids")),
            down_ask=_best(down.get("asks")),
            down_bid=_best(down.get("bids")),
            min_order_size=_float_or_none(up.get("min_order_size")),
        )

    async def _resolve_tokens(self, slug: str) -> tuple[str, str] | None:
        if slug not in self._tokens:
            r = await self._client.get(
                f"{POLYMARKET_GAMMA_API}/markets", params={"slug": slug}
            )
            r.raise_for_status()
            rows = r.json()
            tokens = _token_ids(rows[0]) if isinstance(rows, list) and rows else None
            if tokens is None:
                return None
            self._tokens = {slug: tokens}  # only the live window is ever needed
        return self._tokens[slug]

    async def _books(self, tokens: tuple[str, str]) -> dict[str, dict[str, Any]]:
        r = await self._client.post(
            f"{POLYMARKET_CLOB_API}/books", json=[{"token_id": t} for t in tokens]
        )
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise ValueError("CLOB /books returned a non-list payload")
        return {str(b.get("asset_id")): b for b in rows if isinstance(b, dict)}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
