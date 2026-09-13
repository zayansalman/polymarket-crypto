"""Binance connector — BTC spot price and recent close history.

Extracted and refactored from *polymarket_bot/paper.py* (lines 302-329).
"""

from __future__ import annotations

import os
import time
from collections import deque

import httpx

from polymarket_exec.core.exceptions import FeedError
from polymarket_exec.core.interfaces import AbstractPriceConnector

# Binance weight limit for raw /api/v3/klines is ~1,200 request weight per
# minute on the IP tier.  We keep a short ring-buffer of request timestamps
# and raise FeedError if we are clearly about to exceed a safe threshold.
_MAX_RECENT_REQUESTS = 60  # requests in the last 60 seconds
_RATE_LIMIT_WINDOW_SECONDS = 60


class BinanceConnector(AbstractPriceConnector):
    """Fetch BTC/USDT spot data from the Binance REST API.

    Parameters:
        client: An *httpx.AsyncClient* instance.
        api_base: Root URL of the Binance API (default: public spot API).
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_base: str | None = None,
    ) -> None:
        if api_base is None:
            api_base = os.getenv(
                "BINANCE_API_BASE", "https://data-api.binance.vision"
            )
        self._client = client
        self._api_base = api_base.rstrip("/")
        self._request_timestamps: deque[float] = deque(
            maxlen=_MAX_RECENT_REQUESTS
        )

    # ------------------------------------------------------------------
    # AbstractPriceConnector
    # ------------------------------------------------------------------

    async def get_spot_and_recent_closes(self) -> tuple[float, list[float]]:
        """Return ``(latest_close, all_closes)``.

        Fetches 90 1-second klines for BTCUSDT.  ``all_closes`` is ordered
        oldest-first and is used by
        :func:`polymarket_exec.strategy.pricing_model.sigma_per_second`.

        Raises:
            FeedError: on HTTP errors, empty responses, or rate-limit risk.
        """
        self._maybe_enforce_rate_limit()
        try:
            resp = await self._client.get(
                f"{self._api_base}/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1s", "limit": 90},
            )
            resp.raise_for_status()
            self._record_request()
        except httpx.HTTPStatusError as exc:
            raise FeedError(
                f"Binance returned HTTP {exc.response.status_code} for klines: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise FeedError(
                f"Binance klines request failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        rows = resp.json()
        closes = [float(row[4]) for row in rows if isinstance(row, (list, tuple)) and len(row) > 4]
        if not closes:
            raise FeedError("Binance returned no BTC closes.")
        return closes[-1], closes

    async def get_reference_price(self, window_start_ts: int) -> float:
        """Return the reference (opening) price for the window starting at
        *window_start_ts* (Unix seconds).

        This fetches the single 1-second candle at *window_start_ts* from
        Binance and returns its close price, which serves as the window's
        reference price.

        Raises:
            FeedError: on HTTP errors or empty responses.
        """
        self._maybe_enforce_rate_limit()
        try:
            resp = await self._client.get(
                f"{self._api_base}/api/v3/klines",
                params={
                    "symbol": "BTCUSDT",
                    "interval": "1s",
                    "startTime": window_start_ts * 1000,
                    "limit": 1,
                },
            )
            resp.raise_for_status()
            self._record_request()
        except httpx.HTTPStatusError as exc:
            raise FeedError(
                f"Binance returned HTTP {exc.response.status_code} for reference price: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise FeedError(
                f"Binance reference price request failed: {exc.__class__.__name__}: {exc}"
            ) from exc

        rows = resp.json()
        if not rows:
            raise FeedError(
                f"Binance returned no BTC window reference candle for ts={window_start_ts}."
            )
        return float(rows[0][4])

    async def health_check(self) -> dict:
        """Quick API probe using the exchangeInfo endpoint (lightweight).

        Returns a dict with ``status`` (``"ok"`` | ``"degraded"`` | ``"down"``),
        ``latency_ms``, and ``detail``.
        """
        t0 = time.perf_counter()
        try:
            resp = await self._client.get(
                f"{self._api_base}/api/v3/exchangeInfo",
                params={"symbol": "BTCUSDT"},
                timeout=5.0,
            )
            resp.raise_for_status()
            latency_ms = (time.perf_counter() - t0) * 1000
            return {
                "status": "ok",
                "latency_ms": round(latency_ms, 2),
                "detail": f"Binance API responded in {latency_ms:.0f}ms",
            }
        except httpx.HTTPStatusError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            return {
                "status": "degraded",
                "latency_ms": round(latency_ms, 2),
                "detail": f"HTTP {exc.response.status_code} from Binance",
            }
        except httpx.RequestError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            return {
                "status": "down",
                "latency_ms": round(latency_ms, 2),
                "detail": f"Request error: {exc.__class__.__name__}: {exc}",
            }

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _record_request(self) -> None:
        """Log a request timestamp for rate-limit tracking."""
        self._request_timestamps.append(time.time())

    def _maybe_enforce_rate_limit(self) -> None:
        """Raise FeedError if we have exceeded the safe request threshold."""
        now = time.time()
        # Purge timestamps older than the window
        while (
            self._request_timestamps
            and self._request_timestamps[0] < now - _RATE_LIMIT_WINDOW_SECONDS
        ):
            self._request_timestamps.popleft()

        if len(self._request_timestamps) >= _MAX_RECENT_REQUESTS:
            raise FeedError(
                f"Binance rate-limit safety: {len(self._request_timestamps)} requests "
                f"in the last {_RATE_LIMIT_WINDOW_SECONDS}s (limit {_MAX_RECENT_REQUESTS})."
            )
