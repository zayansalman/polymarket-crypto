"""Live inputs for Kelly horse-race: the BTC 15m window, K, X, the last hour, and the book.

Each read returns its value or raises :class:`NotReady` with a stable ``code`` and a plain
English ``message`` for the card. ``NotReady.final`` says the window can never have it (the
runner then records the window with that reason); otherwise the runner tries again next pass.

Where each input comes from
---------------------------
- The window: ``hub.market("btc", "15m")`` (the market-data hub; this strategy asks for the
  market as owner ``OWNER``), with its bounds, Up/Down tokens and condition id. The hub rolls
  windows up to ~2 s after :00/:15/:30/:45, so a window with ``now`` outside [start, end) is
  not used. A missing condition id is looked up once on Gamma (``/markets?slug=``).
- ``K``, the price to beat: the Chainlink TWAP-60s print whose observation second is the
  window's start. Gamma's ``priceToBeat`` equals it to every digit (research note, 2026-09-22),
  so Gamma's ``/events?slug=`` is the fallback when the hub does not hold that print (the app
  started after the open, or the feed dropped then). Gamma publishes it only minutes into the
  window, so it is asked again every ``GAMMA_RETRY_S`` until the decision cutoff. The print
  arrives about 2 s late, so for ``OPEN_PRINT_WAIT_S`` after the open it is only pending.
- ``X``, the price now: the newest TWAP-60s print, observed within ``PRICE_MAX_AGE_S``. It is
  the series the window settles on, so ``ln(X / K)`` compares like with like.
- The last hour: sixty 1-minute log returns from 61 completed Binance BTCUSDT candles
  (``config.BINANCE_API_BASE`` ``/api/v3/klines``); a candle still forming is dropped.
- The book: CLOB REST ``/book`` for the chosen token: best bid and the size resting there
  (the depth ahead of a new bid at that price), best ask, tick size and the venue's minimum
  order. Levels come worst to best, so the best is found by price, not by position.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ems import config as _config  # type: ignore[import-untyped]
from ems.execution.tape import BROWSER_HEADERS, HttpClient, http_status
from ems.kelly_horse_race.maths import MINUTE_RETURNS

ASSET = "btc"
TIMEFRAME = "15m"
SYMBOL = "BTCUSDT"
OWNER = "kelly horse-race"  # shown on the FEEDS card; release(OWNER) drops only this strategy's
TWAP60 = "chainlink_twap60"

PRICE_MAX_AGE_S = 5.0
OPEN_PRINT_WAIT_S = 10.0
GAMMA_RETRY_S = 30.0
KLINE_SETTLE_S = 2.0
HTTP_TIMEOUT_S = 10.0
GAMMA_API = "https://gamma-api.polymarket.com"
GAMMA_HEADERS: Mapping[str, str] = MappingProxyType(dict(BROWSER_HEADERS))

K_FROM_PRINT = "TWAP-60s print at the open"
K_FROM_GAMMA = "Gamma priceToBeat"


class NotReady(Exception):
    """An input is missing. ``final``: this window can never have it."""

    def __init__(self, code: str, message: str, *, final: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.final = final


@dataclass(frozen=True)
class Window:
    slug: str
    start: int
    end: int
    up_token: str
    down_token: str
    condition_id: str

    def token(self, side: str) -> str:
        return self.up_token if side == "Up" else self.down_token


@dataclass(frozen=True)
class PriceNow:
    value: float
    obs_ts: int


@dataclass(frozen=True)
class Book:
    """One token's book from CLOB REST ``/book``."""

    token_id: str
    best_bid: float | None
    bid_size: float | None  # shares resting at the best bid
    best_ask: float | None
    tick_size: float
    min_order_size: float


@dataclass
class Memory:
    """What the reads keep between passes: Gamma lookups' next try, per window slug."""

    gamma_retry_at: dict[str, float] = field(default_factory=dict)
    condition_ids: dict[str, str] = field(default_factory=dict)

    def forget_before(self, start: int) -> None:
        for book in (self.gamma_retry_at, self.condition_ids):
            for slug in [s for s in book if _slug_start(s) < start]:
                book.pop(slug, None)


def _slug_start(slug: str) -> int:
    try:
        return int(slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


async def window(hub: Any, client: HttpClient | None, now: float, memory: Memory) -> Window:
    """The BTC 15m window ``now`` is in, with its condition id."""
    if hub is None:
        raise NotReady("no_hub", "The market-data hub is not running.")
    hub.want(ASSET, TIMEFRAME, OWNER)
    ref = hub.market(ASSET, TIMEFRAME)
    if ref is None:
        raise NotReady("window_unknown", "The hub does not know the current BTC 15m window yet.")
    start, end = int(ref.window_start), int(ref.window_end)
    if not start <= now < end:
        raise NotReady("window_rolling", "The hub is rolling to the next window.")
    cid = ref.condition_id or memory.condition_ids.get(ref.slug)
    if not cid:
        cid = await _gamma_condition_id(client, ref.slug, now, memory)
    return Window(slug=ref.slug, start=start, end=end, up_token=ref.up_token,
                  down_token=ref.down_token, condition_id=cid)


async def _gamma_get(client: HttpClient | None, path: str, slug: str) -> Any:
    if client is None:
        raise RuntimeError("there is no HTTP client")
    resp = await client.get(f"{GAMMA_API}/{path}", params={"slug": slug},
                            headers=dict(GAMMA_HEADERS), timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


async def _gamma_condition_id(client: HttpClient | None, slug: str, now: float,
                              memory: Memory) -> str:
    key = f"cid:{slug}"
    if memory.gamma_retry_at.get(key, -math.inf) > now:
        raise NotReady("condition_id_unknown", "The window's market id is not known yet; "
                       "Gamma is asked again shortly.")
    memory.gamma_retry_at[key] = now + GAMMA_RETRY_S
    try:
        rows = await _gamma_get(client, "markets", slug)
    except Exception as exc:  # noqa: BLE001 - asked again later
        raise NotReady("condition_id_unknown", f"The window's market id could not be looked "
                       f"up on Gamma ({http_status(exc)}).") from exc
    for row in rows if isinstance(rows, list) else [rows]:
        if isinstance(row, Mapping) and row.get("slug") == slug and row.get("conditionId"):
            memory.condition_ids[slug] = str(row["conditionId"])
            return memory.condition_ids[slug]
    raise NotReady("condition_id_unknown", "Gamma does not list the window's market id yet.")


def _print_at(hub: Any, second: int) -> tuple[float | None, int | None]:
    """The TWAP-60s print observed at ``second``, and the newest observation second held."""
    newest: int | None = None
    for point in reversed(hub.prices(TWAP60, ASSET) or ()):
        s = int(point.obs_ms // 1000)
        newest = s if newest is None else max(newest, s)
        if s == second:
            return _positive(point.value), newest
        if s < second:
            break
    return None, newest


async def price_to_beat(hub: Any, client: HttpClient | None, win: Window, now: float,
                        memory: Memory, *, cutoff: float) -> tuple[float, str]:
    """``(K, where it came from)``. Final ``NotReady`` once ``cutoff`` passes without one."""
    value, newest = _print_at(hub, win.start)
    if value is not None:
        return value, K_FROM_PRINT
    if (newest is None or newest <= win.start) and now - win.start <= OPEN_PRINT_WAIT_S:
        raise NotReady("k_pending", "The TWAP-60s print at the open has not arrived yet.")
    why = "Gamma was not asked"
    if memory.gamma_retry_at.get(win.slug, -math.inf) <= now:
        memory.gamma_retry_at[win.slug] = now + GAMMA_RETRY_S
        try:
            rows = await _gamma_get(client, "events", win.slug)
            why = "Gamma has not published it yet"
            for row in rows if isinstance(rows, list) else [rows]:
                if not isinstance(row, Mapping) or row.get("slug") not in (None, win.slug):
                    continue
                meta = row.get("eventMetadata")
                gamma = _positive(meta.get("priceToBeat")) if isinstance(meta, Mapping) else None
                if gamma is not None:
                    return gamma, K_FROM_GAMMA
        except Exception as exc:  # noqa: BLE001 - asked again later
            why = f"the Gamma lookup failed ({http_status(exc)})"
    else:
        why = "Gamma is asked again shortly"
    message = f"The TWAP-60s print at the open is not held and {why}."
    if now >= cutoff:
        raise NotReady("k_missing", message, final=True)
    raise NotReady("k_missing", message)


def price_now(hub: Any, now: float) -> PriceNow:
    point = hub.price(TWAP60, ASSET)
    if point is None:
        raise NotReady("x_missing", "No TWAP-60s print is held yet.")
    value = _positive(point.value)
    if value is None:
        raise NotReady("x_invalid", f"The TWAP-60s print {point.value!r} is not a price.")
    obs = point.obs_ms / 1000.0
    if now - obs > PRICE_MAX_AGE_S:
        raise NotReady("x_stale", f"The newest TWAP-60s print is {now - obs:.0f} s old.")
    return PriceNow(value=value, obs_ts=int(obs))


async def minute_returns(client: HttpClient | None, now: float) -> tuple[float, ...]:
    """The last hour's sixty 1-minute log returns, oldest first."""
    if client is None:
        raise NotReady("klines_failed", "There is no HTTP client to read Binance.")
    end = int((now - KLINE_SETTLE_S) // 60) * 60  # when the newest completed minute closed
    count = MINUTE_RETURNS + 1
    first = end - 60 * count
    try:
        resp = await client.get(
            f"{_config.BINANCE_API_BASE}/api/v3/klines",
            params={"symbol": SYMBOL, "interval": "1m", "startTime": first * 1000,
                    "limit": count},
            timeout=HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001 - read again next pass
        raise NotReady("klines_failed", f"Binance 1-minute candles could not be read "
                       f"({http_status(exc)}).") from exc
    closes: dict[int, float] = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            open_s, close = int(row[0]) // 1000, _positive(row[4])
        except (TypeError, ValueError, IndexError):
            continue
        if close is not None and open_s + 60 <= now - KLINE_SETTLE_S:
            closes[open_s] = close
    opens = [first + 60 * i for i in range(count)]
    missing = [o for o in opens if o not in closes]
    if missing:
        raise NotReady("klines_incomplete", f"Binance returned {count - len(missing)} of the "
                       f"{count} completed 1-minute candles needed.")
    series = [closes[o] for o in opens]
    return tuple(math.log(series[i] / series[i - 1]) for i in range(1, count))


def _levels(raw: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for level in raw if isinstance(raw, list) else []:
        if not isinstance(level, Mapping):
            continue
        price, size = _positive(level.get("price")), _positive(level.get("size"))
        if price is not None and size is not None and price < 1.0:
            out.append((price, size))
    return out


async def read_book(client: HttpClient | None, token_id: str) -> Book:
    """One token's book from CLOB REST ``/book``."""
    if client is None:
        raise NotReady("book_failed", "There is no HTTP client to read the book.")
    try:
        resp = await client.get(f"{_config.POLYMARKET_CLOB_API}/book",
                                params={"token_id": token_id}, headers=BROWSER_HEADERS,
                                timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - read again next pass
        raise NotReady("book_failed", f"The order book could not be read "
                       f"({http_status(exc)}).") from exc
    if not isinstance(data, Mapping):
        raise NotReady("book_failed", "The order book answered in an unexpected shape.")
    bids, asks = _levels(data.get("bids")), _levels(data.get("asks"))
    best_bid = max(bids, key=lambda lv: lv[0], default=None)
    best_ask = min(asks, key=lambda lv: lv[0], default=None)
    tick = _positive(data.get("tick_size"))
    min_size = _positive(data.get("min_order_size"))
    if tick is None or tick >= 1.0 or min_size is None:
        raise NotReady("book_failed", "The order book did not give a tick size and a minimum "
                       "order.")
    return Book(
        token_id=token_id,
        best_bid=best_bid[0] if best_bid else None,
        bid_size=sum(s for p, s in bids if best_bid and abs(p - best_bid[0]) < 1e-9) or None,
        best_ask=best_ask[0] if best_ask else None,
        tick_size=tick,
        min_order_size=min_size,
    )
