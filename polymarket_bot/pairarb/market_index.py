"""Outcome-token -> market metadata resolver for the Up/Down families.

Originally built for the 5-minute family (#182); that family was removed
2026-09-19 and the window scheme is now a parameter. Still used by the daily
altcoin scanner (``polymarket_bot/daily/market.py``) and the copytrade tools.

The on-chain fill feed (``feed.py``) only carries a ``token_id`` — an ERC-1155
CTF outcome-token id. It has no idea what market or outcome that token belongs
to; ``price_the_copy()`` needs a window slug, an ``outcome`` ("Up"/"Down") and a
``conditionId`` to price and settle a copy.

Every window's slug is a pure function of the clock
(``{asset}-updown-{tf}-{floor(now/window)*window}``, the #181 discovery), so
this keeps a small rolling cache instead of a general reverse index: fetch each tracked
asset's *current* and *previous* window from Gamma and index both outcome
tokens by id. The previous window stays indexed too, because a fill can arrive
attributed to a window that has already rolled over.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

GAMMA = "https://gamma-api.polymarket.com"

# Window length per timeframe label. The 5-minute entry was removed 2026-09-19.
TIMEFRAME_SECONDS: dict[str, int] = {"15m": 900, "1h": 3600, "1d": 86400}
DEFAULT_TIMEFRAME = "1h"


def window_slug(asset: str, ts: int, timeframe: str = DEFAULT_TIMEFRAME) -> str:
    """The window slug covering ``ts``, per the #181 clock-derived scheme."""
    try:
        window = TIMEFRAME_SECONDS[timeframe]
    except KeyError:
        raise ValueError(f"unknown timeframe {timeframe!r}") from None
    floor = (ts // window) * window
    return f"{asset}-updown-{timeframe}-{floor}"


@dataclass(frozen=True)
class MarketTokens:
    """One market's identity plus its two outcome tokens, Up-first."""

    slug: str
    condition_id: str
    up_token: str
    down_token: str


def parse_market(market: dict[str, Any]) -> MarketTokens | None:
    """Extract token ids from one Gamma market row, or ``None`` if malformed."""
    try:
        tokens = json.loads(market.get("clobTokenIds") or "[]")
        outcomes = [str(o).lower() for o in json.loads(market.get("outcomes") or "[]")]
    except (TypeError, ValueError):
        return None
    if len(tokens) != 2 or len(outcomes) != 2:
        return None
    idx_up = 0 if outcomes[0] == "up" else 1
    idx_down = 1 - idx_up
    slug = str(market.get("slug") or "")
    up_token = str(tokens[idx_up] or "")
    down_token = str(tokens[idx_down] or "")
    if not slug or not up_token or not down_token:
        return None
    return MarketTokens(
        slug=slug,
        condition_id=str(market.get("conditionId") or ""),
        up_token=up_token,
        down_token=down_token,
    )


class TokenIndex:
    """Rolling ``token_id -> (slug, outcome, condition_id)`` cache.

    Tracks a fixed asset list and, on :meth:`refresh`, indexes each asset's
    current and previous window of ``timeframe``. Cheap to call often —
    already-indexed windows are skipped without a network round trip.
    """

    def __init__(
        self, assets: list[str], timeframe: str = DEFAULT_TIMEFRAME
    ) -> None:
        if timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        self._assets = list(assets)
        self._timeframe = timeframe
        self._window_seconds = TIMEFRAME_SECONDS[timeframe]
        self._by_token: dict[str, tuple[str, str, str]] = {}
        self._indexed_slugs: set[str] = set()

    def resolve(self, token_id: str) -> tuple[str, str, str] | None:
        """Return ``(slug, outcome, condition_id)`` for a known token id."""
        return self._by_token.get(token_id)

    def _index(self, mt: MarketTokens) -> None:
        if mt.slug in self._indexed_slugs:
            return
        self._by_token[mt.up_token] = (mt.slug, "Up", mt.condition_id)
        self._by_token[mt.down_token] = (mt.slug, "Down", mt.condition_id)
        self._indexed_slugs.add(mt.slug)

    async def refresh(self, client: Any, now: int | None = None) -> None:
        """Fetch the current + previous window for every tracked asset.

        ``client`` needs only an async ``get(url, params=..., timeout=...)``
        returning something with ``.json()`` — an ``httpx.AsyncClient`` or a
        fake with the same shape. A failed fetch for one asset/window is
        skipped, not fatal — the next :meth:`refresh` call retries it.
        """
        now = now if now is not None else int(time.time())
        for asset in self._assets:
            for ts in (now, now - self._window_seconds):
                slug = window_slug(asset, ts, self._timeframe)
                if slug in self._indexed_slugs:
                    continue
                try:
                    r = await client.get(
                        f"{GAMMA}/markets", params={"slug": slug}, timeout=15.0
                    )
                    rows = r.json()
                except Exception:  # noqa: BLE001
                    continue
                if not rows:
                    continue
                mt = parse_market(rows[0])
                if mt:
                    self._index(mt)
