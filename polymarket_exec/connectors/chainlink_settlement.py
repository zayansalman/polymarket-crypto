"""Settlement-aligned Chainlink BTC/USD feed via Polymarket endpoints (issue #21).

Polymarket resolves ``btc-updown-5m-*`` markets on its **Chainlink BTC/USD**
data stream — "Up" iff close >= open — never on Binance or any other spot
market. This module provides the two halves of that feed:

REST reference + settlement (:class:`ChainlinkSettlementConnector`)
    ``GET https://polymarket.com/api/crypto/crypto-price?symbol=btc&``
    ``eventStartTime=<unix_s>&variant=fiveminute`` returns
    ``{openPrice, closePrice, completed, ...}``. ``openPrice`` is the
    Chainlink print at exactly ``t``; ``closePrice`` the print at ``t+300``
    (null while in progress). Verified: ``close(N) == open(N+1)`` exactly.

    Empirical caveats baked in here:

    * ``variant`` MUST be ``fiveminute`` — unrecognised values silently fall
      back to ``hourly``, a different code path that serves **Binance**
      prices floored to the hour.
    * Null responses are cached ~10s keyed on the full query string; every
      request carries a ``&_=<ms>`` cache-buster.
    * The first post-boundary print can be provisional and revised ~1s
      later: :meth:`get_reference_price` reads at ``t+3s`` (or later) and
      requires the value to be stable across two consecutive reads.
    * ~30-day lookback limit ("Timestamp too old for Chainlink API").
    * The WAF is fingerprint-based: the full Chrome header set
      (``User-Agent``, ``sec-ch-ua*``, ``Sec-Fetch-*``, ``Referer``) is
      required; with it, ~1 rps sustained is clean.

Live spot + sigma (:class:`ChainlinkWsFeed`)
    ``wss://ws-live-data.polymarket.com`` with
    ``Origin: https://polymarket.com`` and a browser UA. The subscribe
    message's ``filters`` field MUST be byte-exact compact JSON (no spaces)
    or updates silently never arrive (the snapshot still comes — trap);
    see :func:`build_subscribe_message`. The snapshot reply seeds ~60-120s
    of 1-second history (used for sigma); updates arrive ~1/s carrying
    ``value`` plus ``full_accuracy_value`` (1e18-scaled integer string, for
    exact comparisons). Keepalive is a literal ``PING`` text frame every
    30s. The run loop auto-reconnects with backoff and re-seeds from the
    snapshot on each reconnect.

NO-MIXING RULE: reference, spot, and sigma must all come from this Chainlink
feed. Binance price LEVELS must never be compared against Chainlink levels —
the measured basis (Chainlink ~ $50.7 BELOW Binance, std $3.8) is larger than
most real edges at the 5-minute scale. Binance is allowed only as a
volatility-*shape* fallback (returns, not levels) and for backtest tooling.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from polymarket_exec.core.exceptions import FeedError
from logging_setup import get_logger  # type: ignore[import-untyped]

log = get_logger("chainlink_settlement")

FIVE_MINUTES = 300

DEFAULT_REST_API = "https://polymarket.com/api/crypto/crypto-price"
DEFAULT_WS_URL = "wss://ws-live-data.polymarket.com"
WS_ORIGIN = "https://polymarket.com"
WS_TOPIC = "crypto_prices_chainlink"

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# Seconds after the boundary before the open print is trusted (provisional
# prints are revised ~1s after the boundary).
REFERENCE_SETTLE_DELAY_S = 3.0
# Max REST reads while waiting for two consecutive identical openPrice values.
REFERENCE_MAX_READS = 5
# Keepalive cadence for the literal PING text frame.
WS_PING_INTERVAL_S = 30.0


def chrome_headers(referer_slug: str | None = None) -> dict[str, str]:
    """Full Chrome fingerprint header set required by the Polymarket WAF.

    ``referer_slug`` should be the market/event slug (e.g.
    ``btc-updown-5m-1781091600``) so the Referer matches a real event page.
    """
    referer = (
        f"https://polymarket.com/event/{referer_slug}"
        if referer_slug
        else "https://polymarket.com/"
    )
    return {
        "User-Agent": CHROME_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer": referer,
    }


def build_subscribe_message(symbol: str = "btc/usd") -> str:
    """Build the WS subscribe frame for the Chainlink prices topic.

    The ``filters`` value is itself a JSON **string** and MUST be compact
    (no spaces after ``:`` or ``,``): the server string-matches it, and a
    non-compact encoding means updates silently never arrive even though
    the snapshot reply still comes. Unit-tested for byte-exactness.
    """
    filters = json.dumps({"symbol": symbol}, separators=(",", ":"))
    return json.dumps(
        {
            "action": "subscribe",
            "subscriptions": [
                {"topic": WS_TOPIC, "type": "update", "filters": filters}
            ],
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class WindowPrices:
    """One 5-minute window as reported by the crypto-price REST endpoint."""

    open_price: float | None
    close_price: float | None
    completed: bool


class ChainlinkSettlementConnector:
    """REST reference/settlement reads against the Polymarket crypto-price API.

    Parameters:
        client: shared ``httpx.AsyncClient``.
        api_base: crypto-price endpoint root.
        symbol: crypto-price symbol (``btc``).
        referer_slug: market slug used for the WAF Referer header.
        time_fn / sleep_fn: injectable clocks for tests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_base: str = DEFAULT_REST_API,
        symbol: str = "btc",
        referer_slug: str | None = None,
        time_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._api_base = api_base.rstrip("?&")
        self._symbol = symbol
        self._referer_slug = referer_slug
        self._time_fn = time_fn
        self._sleep_fn = sleep_fn

    # ------------------------------------------------------------------
    # Raw window read
    # ------------------------------------------------------------------

    async def fetch_window(self, event_start_ts: int) -> WindowPrices:
        """Fetch open/close prints for the window starting at *event_start_ts*.

        Always sends a ``&_=<ms>`` cache-buster: the API caches null
        responses ~10s keyed on the full query string, which would
        otherwise delay reading a fresh open right after the boundary.
        """
        params = {
            "symbol": self._symbol,
            "eventStartTime": str(int(event_start_ts)),
            "variant": "fiveminute",  # exact enum value — typos fall back to hourly/Binance
            "_": str(int(self._time_fn() * 1000)),
        }
        try:
            resp = await self._client.get(
                self._api_base,
                params=params,
                headers=chrome_headers(self._referer_slug),
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise FeedError(
                f"crypto-price returned HTTP {exc.response.status_code} for "
                f"eventStartTime={event_start_ts}: {exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise FeedError(
                f"crypto-price request failed: {exc.__class__.__name__}: {exc}"
            ) from exc
        except ValueError as exc:
            raise FeedError(f"crypto-price returned non-JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise FeedError(f"crypto-price returned non-object JSON: {type(data).__name__}")
        return WindowPrices(
            open_price=_as_float(data.get("openPrice")),
            close_price=_as_float(data.get("closePrice")),
            completed=bool(data.get("completed", False)),
        )

    # ------------------------------------------------------------------
    # Reference open (provisional-revision aware)
    # ------------------------------------------------------------------

    async def get_reference_price(self, window_start_ts: int) -> float:
        """Return the Chainlink open print for the window — the settlement reference.

        The first post-boundary print can be provisional and revised ~1s
        later, so this waits until ``t+3s`` (if called earlier) and then
        re-reads until the open is identical across two consecutive reads
        (bounded by ``REFERENCE_MAX_READS``).
        """
        wait = (window_start_ts + REFERENCE_SETTLE_DELAY_S) - self._time_fn()
        if 0 < wait <= REFERENCE_SETTLE_DELAY_S + 2:
            await self._sleep_fn(wait)

        prev: float | None = None
        for attempt in range(REFERENCE_MAX_READS):
            window = await self.fetch_window(window_start_ts)
            current = window.open_price
            if current is not None:
                if prev is not None and current == prev:
                    return current
                prev = current
            if attempt < REFERENCE_MAX_READS - 1:
                await self._sleep_fn(1.0)
        if prev is not None:
            log.warning(
                "chainlink_reference.unstable",
                window_start_ts=window_start_ts,
                value=prev,
            )
            return prev
        raise FeedError(
            f"crypto-price returned no openPrice for window {window_start_ts} "
            f"after {REFERENCE_MAX_READS} reads"
        )

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def get_recent_print(self, lag_seconds: int = 3) -> tuple[float, float] | None:
        """(print_ts, value) of a near-live Chainlink print via plain REST.

        ``openPrice`` is computed immediately for ANY ``eventStartTime`` (no
        event lookup), so the open of a "window" starting ``lag_seconds``
        ago IS the settlement-stream print at that second — a live spot for
        when the WS feed is down or rate-limited. ``closePrice`` would lag
        ~60s (it only fills when the completion job runs), so it is useless
        here. The small lag clears the ~1s provisional-revision window.
        """
        ts = int(self._time_fn()) - lag_seconds
        window = await self.fetch_window(ts)
        if window.open_price is None:
            return None
        return float(ts), window.open_price

    async def settle_window(self, window_start_ts: int) -> bool | None:
        """Resolve a finished window: True = Up, False = Down, None = not yet.

        Resolution rule (verified on-chain): **Up iff close >= open** — ties
        credit Up. ``completed`` flips ~60-70s after the window ends on
        plain polls, so when ``closePrice`` is still null we fast-settle by
        reading ``open(N+1)``, which equals ``close(N)`` exactly.
        """
        window = await self.fetch_window(window_start_ts)
        open_price = window.open_price
        close_price = window.close_price
        if open_price is None:
            return None
        if close_price is None:
            now = self._time_fn()
            if now < window_start_ts + FIVE_MINUTES:
                return None  # window still in progress
            # Fast settlement: close(N) == open(N+1) exactly.
            next_window = await self.fetch_window(window_start_ts + FIVE_MINUTES)
            close_price = next_window.open_price
            if close_price is None:
                return None
        return close_price >= open_price

    async def health_check(self) -> dict:
        """Probe the crypto-price endpoint with the current window."""
        t0 = time.perf_counter()
        now = int(self._time_fn())
        current_start = now - (now % FIVE_MINUTES)
        try:
            window = await self.fetch_window(current_start)
            latency_ms = (time.perf_counter() - t0) * 1000
            status = "ok" if window.open_price is not None else "degraded"
            return {
                "status": status,
                "latency_ms": round(latency_ms, 2),
                "detail": f"crypto-price open={window.open_price} completed={window.completed}",
            }
        except FeedError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000
            return {
                "status": "down",
                "latency_ms": round(latency_ms, 2),
                "detail": str(exc),
            }


# ---------------------------------------------------------------------------
# Live WS spot feed
# ---------------------------------------------------------------------------


class ChainlinkWsFeed:
    """Live Chainlink BTC/USD 1-second prints from the Polymarket WS feed.

    Maintains a rolling 1s series (seeded by the subscribe snapshot, then
    appended by updates) used for both the live spot level and sigma
    estimation. ``run()`` owns the connection: subscribe, literal-PING
    keepalive, reconnect with backoff, re-seed from the snapshot.

    Message parsing is deliberately tolerant about envelope shape (payload
    nesting, field naming) because the feed is reverse-engineered; the
    fields relied on are ``value`` (float), ``timestamp`` (s or ms), and
    ``full_accuracy_value`` (1e18-scaled int string).
    """

    def __init__(
        self,
        url: str = DEFAULT_WS_URL,
        symbol: str = "btc/usd",
        stale_after_s: float = 15.0,
        max_points: int = 600,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._url = url
        self._symbol = symbol
        self._stale_after_s = stale_after_s
        self._time_fn = time_fn
        self._series: deque[tuple[float, float]] = deque(maxlen=max_points)
        self._latest_full_accuracy: str | None = None
        self._last_update_monotonic: float | None = None
        self._stopped = False
        self._connected = False

    # ------------------------------------------------------------------
    # State accessors (read by the trading engine each tick)
    # ------------------------------------------------------------------

    def latest(self) -> tuple[float, float] | None:
        """Latest (print_ts_seconds, value) or None before the first point."""
        if not self._series:
            return None
        return self._series[-1]

    def latest_full_accuracy(self) -> str | None:
        """1e18-scaled integer string of the latest print (exact comparisons)."""
        return self._latest_full_accuracy

    def recent_closes(self, max_points: int = 120) -> list[float]:
        """Oldest-first list of recent 1s values for sigma estimation."""
        values = [v for _, v in self._series]
        return values[-max_points:]

    def is_fresh(self) -> bool:
        """True when a print arrived within the staleness window."""
        if self._last_update_monotonic is None:
            return False
        return (time.monotonic() - self._last_update_monotonic) <= self._stale_after_s

    def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------
    # Frame handling (pure — unit-testable without a socket)
    # ------------------------------------------------------------------

    def handle_message(self, raw: str | bytes | dict[str, Any]) -> int:
        """Absorb one WS frame; returns the number of price points absorbed.

        Non-JSON frames (e.g. the PONG reply to our literal PING) and
        frames for other topics are ignored.
        """
        if isinstance(raw, (str, bytes)):
            text = raw.decode() if isinstance(raw, bytes) else raw
            stripped = text.strip()
            if not stripped.startswith(("{", "[")):
                return 0  # PONG / other text keepalives
            try:
                message = json.loads(stripped)
            except json.JSONDecodeError:
                return 0
        else:
            message = raw
        points = _extract_price_points(message, self._symbol)
        absorbed = 0
        for ts, value, full_accuracy in points:
            if self._series and ts <= self._series[-1][0]:
                continue  # snapshot overlap on reconnect / duplicate prints
            self._series.append((ts, value))
            if full_accuracy is not None:
                self._latest_full_accuracy = full_accuracy
            absorbed += 1
        if absorbed:
            self._last_update_monotonic = time.monotonic()
        return absorbed

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect/subscribe/keepalive loop until :meth:`stop` is called."""
        import websockets

        backoff = 1.0
        while not self._stopped:
            try:
                async with self._connect(websockets) as ws:
                    await ws.send(build_subscribe_message(self._symbol))
                    self._connected = True
                    backoff = 1.0
                    last_ping = time.monotonic()
                    while not self._stopped:
                        try:
                            frame = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            pass
                        else:
                            self.handle_message(frame)
                        if time.monotonic() - last_ping >= WS_PING_INTERVAL_S:
                            await ws.send("PING")  # literal text keepalive
                            last_ping = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on any socket error
                # A 429 handshake rejection is the server rate-limiting
                # connection attempts; hammering it with the short backoff
                # only extends the ban window.
                if "429" in str(exc):
                    backoff = max(backoff, 60.0)
                log.warning(
                    "chainlink_ws.disconnected",
                    error=f"{type(exc).__name__}: {exc}",
                    retry_in_s=backoff,
                )
                self._connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
        self._connected = False

    def _connect(self, websockets_module: Any) -> Any:
        headers = {"Origin": WS_ORIGIN, "User-Agent": CHROME_UA}
        try:
            return websockets_module.connect(self._url, additional_headers=headers)
        except TypeError:
            # websockets < 14 uses extra_headers
            return websockets_module.connect(self._url, extra_headers=headers)

    async def health_check(self) -> dict:
        latest = self.latest()
        return {
            "status": "ok" if self.is_fresh() else "down",
            "latency_ms": 0.0,
            "detail": (
                f"connected={self._connected} points={len(self._series)} "
                f"latest={latest[1] if latest else None}"
            ),
        }


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_ts(ts: float) -> float:
    """Normalize a timestamp that may be in ms to seconds."""
    return ts / 1000.0 if ts > 1e12 else ts


def _point_from_dict(item: dict[str, Any], symbol: str) -> tuple[float, float, str | None] | None:
    """Extract (ts_seconds, value, full_accuracy_value) from one point dict."""
    item_symbol = item.get("symbol")
    if item_symbol is not None and str(item_symbol).lower() != symbol.lower():
        return None
    value = _as_float(item.get("value", item.get("price")))
    ts = _as_float(item.get("timestamp", item.get("time", item.get("t"))))
    if value is None or ts is None:
        return None
    full_accuracy = item.get("full_accuracy_value")
    return (_normalize_ts(ts), value, str(full_accuracy) if full_accuracy is not None else None)


def _extract_price_points(
    message: Any, symbol: str
) -> list[tuple[float, float, str | None]]:
    """Pull all chainlink price points out of one decoded WS message.

    Handles both shapes seen from ws-live-data: a single-update envelope
    (``payload`` is one point) and the subscribe snapshot (a list of points
    nested under ``payload``/``data``). Topic-tagged envelopes for other
    topics are ignored.
    """
    if isinstance(message, list):
        points: list[tuple[float, float, str | None]] = []
        for item in message:
            points.extend(_extract_price_points(item, symbol))
        return points
    if not isinstance(message, dict):
        return []
    topic = message.get("topic")
    if topic is not None and topic != WS_TOPIC:
        return []
    # Direct point?
    direct = _point_from_dict(message, symbol)
    if direct is not None:
        return [direct]
    # Nested containers.
    points = []
    for key in ("payload", "data", "history", "prices", "results"):
        if key in message:
            points.extend(_extract_price_points(message[key], symbol))
    return points
