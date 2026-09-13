"""Polymarket connector — discovers the current BTC 5-minute binary market window.

Extracted and refactored from *polymarket_bot/paper.py* (lines 275-362).
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from polymarket_exec.core.exceptions import FeedError, MarketDiscoveryError
from polymarket_exec.core.interfaces import AbstractMarketConnector
from polymarket_exec.core.types import MarketWindow

FIVE_MINUTES = 300  # 5 * 60 seconds


class PolymarketConnector(AbstractMarketConnector):
    """Discover BTC 5m Up/Down markets on Polymarket via the Gamma API.

    Parameters:
        client: An *httpx.AsyncClient* instance (shared or dedicated).
        api_base: Root URL of the Polymarket Gamma API.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_base: str = "https://gamma-api.polymarket.com",
    ) -> None:
        self._client = client
        self._api_base = api_base.rstrip("/")

    # ------------------------------------------------------------------
    # AbstractMarketConnector
    # ------------------------------------------------------------------

    async def discover_current_window(self) -> MarketWindow:
        """Return the active :class:`MarketWindow` for the current time period.

        The method tries the *current*, *next*, and *previous* 5-minute windows
        because Polymarket may rotate markets slightly before/after the exact
        boundary.

        Raises:
            MarketDiscoveryError: when no active market can be found.
            FeedError: on HTTP errors from the Gamma API.
        """
        now = int(time.time())
        current_start = now - (now % FIVE_MINUTES)

        for start_ts in (
            current_start,
            current_start + FIVE_MINUTES,
            current_start - FIVE_MINUTES,
        ):
            slug = f"btc-updown-5m-{start_ts}"
            market = await self._try_slug(slug)
            if market is not None:
                up_price, down_price = _outcome_prices(market)
                end_ts = start_ts + FIVE_MINUTES
                return MarketWindow(
                    slug=slug,
                    question=market.get("question", ""),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    up_price=up_price,
                    down_price=down_price,
                )

        raise MarketDiscoveryError(
            "Could not discover current BTC 5-minute Polymarket market "
            f"(tried windows around {current_start})."
        )

    async def health_check(self) -> dict:
        """Quick API probe.

        Returns a dict with ``status`` (``"ok"`` | ``"degraded"`` | ``"down"``),
        ``latency_ms``, and ``detail``.
        """
        import time as _time

        t0 = _time.perf_counter()
        try:
            # Lightweight probe — just hit the events endpoint with a nonsense
            # slug so we get a fast 200/empty rather than a heavy payload.
            resp = await self._client.get(
                f"{self._api_base}/events",
                params={"slug": "health-check-probe", "limit": 1},
                timeout=5.0,
            )
            resp.raise_for_status()
            latency_ms = (_time.perf_counter() - t0) * 1000
            return {
                "status": "ok",
                "latency_ms": round(latency_ms, 2),
                "detail": f"Gamma API responded in {latency_ms:.0f}ms",
            }
        except httpx.HTTPStatusError as exc:
            latency_ms = (_time.perf_counter() - t0) * 1000
            return {
                "status": "degraded",
                "latency_ms": round(latency_ms, 2),
                "detail": f"HTTP {exc.response.status_code} from Gamma API",
            }
        except httpx.RequestError as exc:
            latency_ms = (_time.perf_counter() - t0) * 1000
            return {
                "status": "down",
                "latency_ms": round(latency_ms, 2),
                "detail": f"Request error: {exc.__class__.__name__}: {exc}",
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _try_slug(self, slug: str) -> dict[str, Any] | None:
        """Attempt to resolve *slug* via the markets or events endpoint.

        Returns the raw market dict, or ``None`` if not found.
        """
        try:
            data = await self._gamma_get("markets", {"slug": slug})
            market = _first(data)
            if market is not None:
                return market

            event_data = await self._gamma_get("events", {"slug": slug})
            event = _first(event_data)
            markets = event.get("markets") if event else None
            if isinstance(markets, list) and markets:
                return markets[0] if isinstance(markets[0], dict) else None
            return None
        except httpx.HTTPStatusError:
            return None

    async def _gamma_get(
        self, endpoint: str, params: dict[str, Any]
    ) -> Any:
        """GET from the Gamma API, raising FeedError on HTTP failures."""
        try:
            resp = await self._client.get(
                f"{self._api_base}/{endpoint}", params=params
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise FeedError(
                f"Polymarket Gamma API returned HTTP {exc.response.status_code} "
                f"for {endpoint}: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise FeedError(
                f"Polymarket Gamma API request failed for {endpoint}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Pure helper functions (no I/O)
# ---------------------------------------------------------------------------


def _outcome_prices(market: dict[str, Any]) -> tuple[float, float]:
    """Extract (up_price, down_price) from a Gamma market dict.

    The ``outcomePrices`` field may be a JSON string or a list.  We normalise
    it and then use the ``outcomes`` labels to determine which price is Up
    vs Down.
    """
    prices = _json_list(market.get("outcomePrices"))
    outcomes = _json_list(market.get("outcomes"))
    if len(prices) != 2:
        raise MarketDiscoveryError(
            f"BTC market did not expose two outcome prices (got {len(prices)})."
        )
    up_idx = 0
    if len(outcomes) == 2:
        labels = [str(x).lower() for x in outcomes]
        if "up" in labels:
            up_idx = labels.index("up")
    down_idx = 1 - up_idx
    return float(prices[up_idx]), float(prices[down_idx])


def _json_list(value: Any) -> list[Any]:
    """Normalise a value that may be a JSON-encoded list string into a list."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _first(value: Any) -> dict[str, Any] | None:
    """Safely extract the first dict from a list response."""
    if isinstance(value, list) and value:
        first = value[0]
        return first if isinstance(first, dict) else None
    return None
