"""Which Polymarket Up/Down windows to follow, and their outcome token ids.

The grid is assets x timeframes (default btc eth sol xrp doge bnb x 5m 15m 1h 1d). For
each pair the universe follows the current window and the next one, so the socket always
holds a token that has not resolved (the market channel closes once every subscribed
token has). Slugs come from ``updown_quote.window_slug``:

* 5m / 15m — the window runs from the slug's epoch for 300 / 900 s;
* 1h — the Eastern hour the slug names;
* 1d — noon ET the day before to noon ET on the date the slug names (23 or 25 hours
  on DST change days).

Token ids come from ``new_market`` announcements (Up/Down markets are announced about
24 h ahead) or, failing that, one Gamma ``/markets?slug=`` read per window (a miss is
retried at most every 30 s). This is metadata, not price polling.

A window that has ended stays followed for 30 s, and until its ``market_resolved``
arrives, which the server only sends for subscribed tokens. The wait is capped per
timeframe (``AWAIT_RESOLUTION_S``): 5 min for 5m/15m, 45 min for 1h/1d. Measured on
2026-09-17 from the window end, the resolution reached the socket after ~2.5 min for
5m/15m, 12-28 min for 1h and ~15 min for 1d. ``await_resolution_s=0`` drops every
ended window after the 30 s.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

import httpx

from config import POLYMARKET_GAMMA_API
from logging_setup import get_logger
from polymarket_exec.connectors.updown_quote import _ET, _LONG_NAME, _token_ids, window_slug
from polymarket_exec.marketdata.clob_messages import NewMarketEvent

log = get_logger("marketdata.universe")

ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
TIMEFRAMES = ("5m", "15m", "1h", "1d")
CURRENT = "current"
NEXT = "next"

REFRESH_S = 2.0
LOOKUP_CONCURRENCY = 4
NEGATIVE_RETRY_S = 30.0
DROP_AFTER_END_S = 30.0
# Longest wait for an ended window's market_resolved, by timeframe (see the docstring).
AWAIT_RESOLUTION_S: Mapping[str, float] = {"5m": 300.0, "15m": 300.0,
                                           "1h": 2700.0, "1d": 2700.0}
ANNOUNCED_CAP = 4096  # about 1.6 days of announcements for the default grid
HTTP_TIMEOUT_S = 10.0

_CLOCK_S = {"5m": 300, "15m": 900}

Tokens = tuple[str, str, "str | None"]  # (up token, down token, condition id)


@dataclass(frozen=True)
class MarketRef:
    asset: str
    timeframe: str
    slug: str
    window_start: float  # epoch seconds
    window_end: float
    up_token: str
    down_token: str
    condition_id: str | None = None


@dataclass(frozen=True)
class UniverseUpdate:
    tokens: frozenset[str]  # every token to keep subscribed
    opened: tuple[MarketRef, ...]  # windows that just became an (asset, timeframe)'s current
    groups: dict[tuple[str, str], frozenset[str]]  # the same tokens by (asset, timeframe)


@dataclass(frozen=True)
class UniverseStatus:
    markets: int  # windows followed
    tokens: int
    lookups: int  # Gamma requests made
    lookup_errors: int  # requests that failed
    misses: int  # answered, but the market is not listed (yet)
    announced: int  # new_market announcements held
    last_error: str | None


def window_bounds(asset: str, timeframe: str, at: datetime) -> tuple[str, float, float]:
    """(slug, start, end) of the window live at ``at`` (tz-aware); times in epoch seconds."""
    slug = window_slug(asset, timeframe, at)
    step = _CLOCK_S.get(timeframe)
    if step is not None:
        start = float(slug.rsplit("-", 1)[1])
        return slug, start, start + step
    et = at.astimezone(_ET)
    if timeframe == "1h":
        # replace() keeps ``fold``: the repeated 1am on the fall-back day starts at the
        # right instant (both of those hours share one slug).
        start = et.replace(minute=0, second=0, microsecond=0).timestamp()
        return slug, start, start + 3600.0
    resolves = et.date() if et.hour < 12 else et.date() + timedelta(days=1)
    end = datetime.combine(resolves, dtime(12), tzinfo=_ET).timestamp()
    start = datetime.combine(resolves - timedelta(days=1), dtime(12), tzinfo=_ET).timestamp()
    return slug, start, end


def next_window(asset: str, timeframe: str, at: datetime) -> tuple[str, float, float]:
    """The window that starts when the one live at ``at`` ends."""
    end = window_bounds(asset, timeframe, at)[2]
    return window_bounds(asset, timeframe, datetime.fromtimestamp(end, UTC))


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT_S)


class MarketUniverse:
    """Tracks the followed windows. ``refresh``/``observe``/``mark_resolved`` run on one
    event loop; the read methods are safe from other threads (state is swapped whole)."""

    def __init__(
        self,
        assets: Iterable[str] = ASSETS,
        timeframes: Iterable[str] = TIMEFRAMES,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        gamma_api: str = POLYMARKET_GAMMA_API,
        time_fn: Callable[[], float] = time.time,
        lookup_concurrency: int = LOOKUP_CONCURRENCY,
        negative_retry_s: float = NEGATIVE_RETRY_S,
        drop_after_end_s: float = DROP_AFTER_END_S,
        await_resolution_s: float | Mapping[str, float] | None = None,
        announced_cap: int = ANNOUNCED_CAP,
    ) -> None:
        """``await_resolution_s``: one wait for every timeframe, or waits by timeframe
        (timeframes not named keep ``AWAIT_RESOLUTION_S``)."""
        self.assets = tuple(dict.fromkeys(assets))
        self.timeframes = tuple(dict.fromkeys(timeframes))
        self._client_factory = client_factory or _default_client
        self._client: httpx.AsyncClient | None = None
        self._gamma_api = gamma_api
        self._time_fn = time_fn
        self._concurrency = max(1, lookup_concurrency)
        self._negative_retry_s = negative_retry_s
        self._drop_after_end_s = drop_after_end_s
        if await_resolution_s is None or isinstance(await_resolution_s, Mapping):
            waits = {**AWAIT_RESOLUTION_S, **(await_resolution_s or {})}
            self._resolution_waits = waits
            self._resolution_wait_default = max(waits.values())
        else:
            self._resolution_waits = {}
            self._resolution_wait_default = float(await_resolution_s)
        self._announced_cap = max(1, announced_cap)
        self._last_error: str | None = None
        self._grid = self._valid_grid()
        self._prefixes = self._family_prefixes()
        self._announced: dict[str, Tokens] = {}
        self._found: dict[str, Tokens] = {}
        self._failed: dict[str, float] = {}  # slug -> when its lookup last failed
        self._resolved: set[str] = set()
        self._tracked: dict[str, MarketRef] = {}
        self._selected: dict[tuple[str, str, str], MarketRef] = {}
        self._by_token: dict[str, tuple[MarketRef, str]] = {}
        self._lookups = 0
        self._lookup_errors = 0
        self._misses = 0

    @property
    def grid(self) -> tuple[tuple[str, str], ...]:
        """The (asset, timeframe) pairs followed (pairs without a slug scheme are skipped)."""
        return self._grid

    def _valid_grid(self) -> tuple[tuple[str, str], ...]:
        grid = []
        probe = datetime(2026, 1, 1, tzinfo=UTC)
        for asset in self.assets:
            for timeframe in self.timeframes:
                try:
                    window_slug(asset, timeframe, probe)
                except ValueError as exc:
                    self._note_error(f"{asset} {timeframe} skipped: {exc}")
                    continue
                grid.append((asset, timeframe))
        return tuple(grid)

    def _family_prefixes(self) -> tuple[str, ...]:
        prefixes = []
        for asset, timeframe in self._grid:
            if timeframe in _CLOCK_S:
                prefixes.append(f"{asset}-updown-{timeframe}-")
            else:
                prefixes.append(f"{_LONG_NAME[asset]}-up-or-down-")
        return tuple(dict.fromkeys(prefixes))

    # --- reads (any thread) ---------------------------------------------------------

    def market(self, asset: str, timeframe: str, which: str = CURRENT) -> MarketRef | None:
        return self._selected.get((asset, timeframe, which))

    def markets(self) -> tuple[MarketRef, ...]:
        return tuple(self._tracked.values())

    def window_for_token(self, token_id: str) -> tuple[MarketRef, str] | None:
        """(window, "up" | "down") for a followed token."""
        return self._by_token.get(token_id)

    def known_tokens(self, slug: str) -> Tokens | None:
        return self._announced.get(slug) or self._found.get(slug)

    def groups(self, now: float | None = None) -> dict[tuple[str, str], frozenset[str]]:
        """Tokens to keep subscribed, by (asset, timeframe); a window's two tokens stay together."""
        now = self._time_fn() if now is None else now
        out: dict[tuple[str, str], set[str]] = {}
        for ref in tuple(self._tracked.values()):
            if self._keep(ref, now):
                out.setdefault((ref.asset, ref.timeframe), set()).update(
                    (ref.up_token, ref.down_token))
        return {key: frozenset(tokens) for key, tokens in out.items()}

    def tokens(self, now: float | None = None) -> frozenset[str]:
        return frozenset().union(*self.groups(now).values())

    def status(self) -> UniverseStatus:
        return UniverseStatus(
            markets=len(self._tracked),
            tokens=len(self.tokens()),
            lookups=self._lookups,
            lookup_errors=self._lookup_errors,
            misses=self._misses,
            announced=len(self._announced),
            last_error=self._last_error,
        )

    # --- updates (the hub's loop) ---------------------------------------------------

    def observe(self, event: NewMarketEvent) -> None:
        """Keep the token ids of an announced market from a followed family."""
        if not event.slug.startswith(self._prefixes):
            return
        aligned = _token_ids({"clobTokenIds": list(event.token_ids),
                              "outcomes": list(event.outcomes)})
        if aligned is None:
            return
        self._announced.pop(event.slug, None)
        self._announced[event.slug] = (aligned[0], aligned[1], event.condition_id or None)
        while len(self._announced) > self._announced_cap:
            del self._announced[next(iter(self._announced))]
        self._failed.pop(event.slug, None)

    def mark_resolved(self, token_id: str) -> MarketRef | None:
        """Note that a followed window resolved; it is dropped once past the 30 s grace."""
        hit = self._by_token.get(token_id)
        if hit is None:
            return None
        self._resolved.add(hit[0].slug)
        return hit[0]

    async def refresh(self, now: float | None = None) -> UniverseUpdate:
        now = self._time_fn() if now is None else now
        at = datetime.fromtimestamp(now, UTC)
        wanted: list[tuple[str, str, str, tuple[str, float, float]]] = []
        for asset, timeframe in self._grid:
            current = window_bounds(asset, timeframe, at)
            upcoming = window_bounds(asset, timeframe, datetime.fromtimestamp(current[2], UTC))
            wanted.append((asset, timeframe, CURRENT, current))
            wanted.append((asset, timeframe, NEXT, upcoming))
        self._failed = {slug: at_s for slug, at_s in self._failed.items()
                        if now - at_s < self._negative_retry_s}
        missing = [bounds[0] for *_, bounds in wanted
                   if self.known_tokens(bounds[0]) is None and bounds[0] not in self._failed]
        await self._lookup_all(dict.fromkeys(missing))

        tracked = {slug: ref for slug, ref in self._tracked.items() if self._keep(ref, now)}
        selected: dict[tuple[str, str, str], MarketRef] = {}
        for asset, timeframe, which, (slug, start, end) in wanted:
            tokens = self.known_tokens(slug)
            if tokens is None:
                continue
            ref = MarketRef(asset, timeframe, slug, start, end, *tokens)
            selected[(asset, timeframe, which)] = ref
            tracked[slug] = ref
        opened = []
        for key, ref in selected.items():
            previous = self._selected.get(key)
            if key[2] == CURRENT and (previous is None or previous.slug != ref.slug):
                opened.append(ref)
        self._tracked = tracked
        self._selected = selected
        self._by_token = {
            token: (ref, side)
            for ref in tracked.values()
            for token, side in ((ref.up_token, "up"), (ref.down_token, "down"))
        }
        self._resolved = {slug for slug in self._resolved if slug in tracked}
        self._found = {slug: tokens for slug, tokens in self._found.items() if slug in tracked}
        groups = self.groups(now)
        return UniverseUpdate(frozenset().union(*groups.values()), tuple(opened), groups)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _keep(self, ref: MarketRef, now: float) -> bool:
        ended_for = now - ref.window_end
        if ended_for <= self._drop_after_end_s:
            return True
        wait = self._resolution_waits.get(ref.timeframe, self._resolution_wait_default)
        return ref.slug not in self._resolved and ended_for <= wait

    async def _lookup_all(self, slugs: Iterable[str]) -> None:
        slugs = list(slugs)
        if not slugs:
            return
        if self._client is None:
            self._client = self._client_factory()
        client = self._client
        gate = asyncio.Semaphore(self._concurrency)
        await asyncio.gather(*(self._lookup(client, slug, gate) for slug in slugs))

    async def _lookup(self, client: httpx.AsyncClient, slug: str, gate: asyncio.Semaphore
                      ) -> None:
        async with gate:
            self._lookups += 1
            try:
                resp = await client.get(f"{self._gamma_api}/markets", params={"slug": slug})
                resp.raise_for_status()
                rows = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                self._lookup_errors += 1
                self._failed[slug] = self._time_fn()
                self._note_error(f"{type(exc).__name__}: {exc}")
                return
        market = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) \
            else None
        tokens = _token_ids(market) if market is not None else None
        if market is None or tokens is None:
            self._misses += 1
            self._failed[slug] = self._time_fn()
            return
        condition = market.get("conditionId")
        self._found[slug] = (tokens[0], tokens[1], str(condition) if condition else None)

    def _note_error(self, text: str) -> None:
        text = (text.splitlines() or [""])[0][:200]
        if text != self._last_error:
            log.warning("marketdata.universe_error", error=text)
        self._last_error = text
