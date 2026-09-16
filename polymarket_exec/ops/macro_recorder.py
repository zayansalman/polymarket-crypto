"""Always-on recorder for macro feeds: each source polled on its own cadence into SQLite.

Started from the dashboard lifespan (next to the flow recorder), so the macro calendar
accrues whether or not the bot loop runs. Every ``tick_s`` it polls each source that is
due: never tried, last success older than its cadence, or last failure older than its
retry delay (a source may raise ``RetryAfter`` to wait longer, e.g. on HTTP 429).

PR 1 sources are release calendars (BLS, BEA, Census, Fed, ForexFactory week); later
sources plug in as more ``MacroSource`` entries. Pure observation data for the hourly
BTC strategy; nothing here decides, gates or reads the trading mode.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime

import httpx

from logging_setup import get_logger
from polymarket_exec.connectors import macro_calendar as mc
from polymarket_exec.storage import macro_store as store

log = get_logger("macro_recorder")

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
CENSUS_CALENDAR_URL = "https://www.census.gov/economic-indicators/calendar-listview.html"
FED_CALENDAR_URL = "https://www.federalreserve.gov/json/calendar.json"
FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Neutral project identifier (no contact details). BLS refuses generic library UAs; this
# one was accepted over HTTP/2 when checked on 2026-09-15.
USER_AGENT = "polymarket-crypto-lab/macro-recorder (economic calendar research tool)"
HTTP_TIMEOUT_S = 20.0

DEFAULT_TICK_S = 60.0
MAX_RETRY_S = 900.0
# One attempt (requests, parsing, DB writes) must finish within this. The client timeout
# bounds each read, not the whole request, so a host sending a byte every few seconds
# could otherwise hold the pass and starve every source after it.
SOURCE_DEADLINE_S = 120.0
# ForexFactory's feed rate-limits hard; never try it again sooner than this.
FF_MIN_RETRY_S = 300.0

BLS_SCHEDULE = "bls:schedule"
BEA_SCHEDULE = "bea:schedule"
CENSUS_SCHEDULE = "census:schedule"
FED_CALENDAR = "fed:calendar"
FF_WEEK = "forexfactory:week"

PollFn = Callable[[httpx.AsyncClient, int], Awaitable[int]]


@dataclass(frozen=True)
class MacroSource:
    key: str  # "source:name"
    cadence_s: float
    poll: PollFn  # records one pull; returns rows recorded
    min_retry_s: float | None = None  # after a failure; default min(cadence_s, 900)
    deadline_s: float = SOURCE_DEADLINE_S  # an attempt still running after this fails

    @property
    def retry_s(self) -> float:
        if self.min_retry_s is not None:
            return self.min_retry_s
        return min(self.cadence_s, MAX_RETRY_S)


@dataclass(frozen=True)
class MacroFeedStatus:
    cadence_s: float
    ok: bool | None  # last attempt; None = not attempted yet
    last_ok_at: float | None
    last_attempt_at: float | None
    next_attempt_at: float | None
    rows: int | None  # rows recorded by the last successful pull
    detail: str | None  # why the last attempt failed
    retry_after: bool = False  # the failure asked for a longer wait (RetryAfter)


@dataclass(frozen=True)
class MacroSnapshot:
    taken_at: float
    started_at: float
    tick_s: float
    feeds: dict[str, MacroFeedStatus]


class RetryAfter(Exception):
    """Raised by a source to push its next attempt ``seconds`` out (rate limits)."""

    def __init__(self, seconds: float, detail: str) -> None:
        super().__init__(detail)
        self.seconds = float(seconds)
        self.detail = detail


class _Unusable(Exception):
    """The source answered, but not with anything recordable. The message is shown as-is."""


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_S,
        http2=True,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _where(url: httpx.URL | str) -> str:
    """host + path only: query strings (where API keys live) never reach logs or the card."""
    parsed = httpx.URL(str(url))
    return f"{parsed.host}{parsed.path}"


_URL_QUERY = re.compile(r"(https?://[^\s?#'\"<>]+)[?#][^\s'\"<>]*")


def _detail(exc: Exception) -> str:
    """Failure text for the card and logs: at most 200 chars, URL query strings dropped."""
    if isinstance(exc, RetryAfter):
        text = exc.detail
    elif isinstance(exc, _Unusable):
        text = str(exc)
    elif isinstance(exc, httpx.RequestError):
        try:
            where = _where(exc.request.url)
        except RuntimeError:  # no request attached
            where = "?"
        text = f"{type(exc).__name__} reaching {where}: {exc}"
    else:
        text = f"{type(exc).__name__}: {exc}"
    return _URL_QUERY.sub(r"\1", text)[:200]


_STATUS_HINT = {403: " (access denied)", 429: " (rate limited)"}


async def fetch(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET ``url``; anything but HTTP 200 is a failure whose detail omits the query string."""
    resp = await client.get(url)
    if resp.status_code != 200:
        raise _Unusable(
            f"HTTP {resp.status_code} from {_where(url)}{_STATUS_HINT.get(resp.status_code, '')}"
        )
    return resp


async def _sync(source: str, events: list[mc.MacroEvent], now_ms: int, url: str) -> int:
    if not events:
        raise _Unusable(f"no events parsed from {_where(url)}")
    await store.sync_events(source, events, now_ms)
    return len(events)


async def poll_bls(client: httpx.AsyncClient, now_ms: int) -> int:
    resp = await fetch(client, BLS_ICS_URL)
    return await _sync("bls", mc.parse_bls_ics(resp.text), now_ms, BLS_ICS_URL)


async def poll_bea(client: httpx.AsyncClient, now_ms: int) -> int:
    resp = await fetch(client, BEA_ICS_URL)
    return await _sync("bea", mc.parse_bea_ics(resp.text), now_ms, BEA_ICS_URL)


async def poll_census(client: httpx.AsyncClient, now_ms: int) -> int:
    resp = await fetch(client, CENSUS_CALENDAR_URL)
    return await _sync("census", mc.parse_census_calendar(resp.text), now_ms, CENSUS_CALENDAR_URL)


def _json(resp: httpx.Response) -> object:
    try:
        return json.loads(resp.content.decode("utf-8-sig"))
    except ValueError as exc:  # UnicodeDecodeError and JSONDecodeError
        raise _Unusable(f"no data (HTTP 200 body from {_where(resp.request.url)} was not JSON)") \
            from exc


async def poll_fed(client: httpx.AsyncClient, now_ms: int) -> int:
    resp = await fetch(client, FED_CALENDAR_URL)
    return await _sync("fed", mc.parse_fed_calendar(_json(resp)), now_ms, FED_CALENDAR_URL)


def _retry_after_s(value: str | None, now_s: float) -> float:
    if not value:
        return 0.0
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return parsedate_to_datetime(value).timestamp() - now_s
    except (TypeError, ValueError, IndexError):
        return 0.0


async def poll_forexfactory(client: httpx.AsyncClient, now_ms: int) -> int:
    """Exactly one request per attempt; a 429 waits max(Retry-After, 5 min)."""
    resp = await client.get(FF_WEEK_URL)
    if resp.status_code == 429:
        wait = max(_retry_after_s(resp.headers.get("retry-after"), now_ms / 1000), FF_MIN_RETRY_S)
        raise RetryAfter(wait, f"rate limited (HTTP 429 from {_where(FF_WEEK_URL)}); "
                               f"next try in {int(wait)}s")
    if resp.status_code != 200:
        raise _Unusable(f"no data (HTTP {resp.status_code} from {_where(FF_WEEK_URL)})")
    events, consensus = mc.parse_ff_week(_json(resp))
    if not events:
        raise _Unusable("no USD events in the response")
    # The pull covers its whole week, so a vanished first or last slot is removed too.
    await store.sync_events("forexfactory", events, now_ms, window=mc.ff_week_window(events))
    await store.record_consensus(consensus, now_ms)
    return len(events)


def default_sources() -> list[MacroSource]:
    return [
        MacroSource(BLS_SCHEDULE, 6 * 3600.0, poll_bls),
        MacroSource(BEA_SCHEDULE, 6 * 3600.0, poll_bea),
        MacroSource(CENSUS_SCHEDULE, 6 * 3600.0, poll_census),
        # Speeches are posted one to two weeks ahead, so the Fed calendar is re-read hourly.
        MacroSource(FED_CALENDAR, 3600.0, poll_fed),
        MacroSource(FF_WEEK, 3600.0, poll_forexfactory),
    ]


class MacroRecorder:
    def __init__(
        self,
        *,
        sources: Sequence[MacroSource] | None = None,
        tick_s: float = DEFAULT_TICK_S,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._sources = list(sources) if sources is not None else default_sources()
        self.tick_s = tick_s
        self._client_factory = client_factory or _default_client
        self._time_fn = time_fn
        self._started_at = time_fn()
        self._status: dict[str, MacroFeedStatus] = {}

    def snapshot(self) -> MacroSnapshot:
        feeds = {
            s.key: self._status.get(s.key)
            or MacroFeedStatus(s.cadence_s, None, None, None, None, None, None)
            for s in self._sources
        }
        return MacroSnapshot(self._time_fn(), self._started_at, self.tick_s, feeds)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Poll due sources every ``tick_s`` until ``stop_event``."""
        self._started_at = self._time_fn()
        async with self._client_factory() as client:
            while not stop_event.is_set():
                await self.record_once(client, int(self._time_fn() * 1000))
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=self.tick_s)

    async def record_once(self, client: httpx.AsyncClient, now_ms: int) -> None:
        """One pass over the due sources. Never raises; failures are kept per source."""
        now = now_ms / 1000
        for source in self._sources:
            status = self._status.get(source.key)
            if status is not None and status.next_attempt_at is not None \
                    and now < status.next_attempt_at:
                continue
            await self._attempt(source, client, now_ms)

    async def _attempt(self, source: MacroSource, client: httpx.AsyncClient, now_ms: int) -> None:
        now = now_ms / 1000
        previous = self._status.get(source.key) or MacroFeedStatus(
            source.cadence_s, None, None, None, None, None, None
        )
        deadline = asyncio.timeout(source.deadline_s)
        try:
            async with deadline:
                rows = int(await source.poll(client, now_ms))
        except RetryAfter as exc:
            self._failed(source, previous, now, _detail(exc), exc.seconds, retry_after=True)
        except Exception as exc:  # noqa: BLE001 — every failure is a feed status
            detail = (f"no complete answer within {source.deadline_s:g}s"
                      if deadline.expired() else _detail(exc))
            self._failed(source, previous, now, detail, source.retry_s, retry_after=False)
        else:
            if previous.ok is False:
                log.info("macro_recorder.feed_recovered", feed=source.key)
            self._status[source.key] = MacroFeedStatus(
                source.cadence_s, True, now, now, now + source.cadence_s, rows, None
            )

    def _failed(
        self,
        source: MacroSource,
        previous: MacroFeedStatus,
        now: float,
        detail: str,
        delay_s: float,
        *,
        retry_after: bool,
    ) -> None:
        if previous.ok is not False:
            log.warning("macro_recorder.feed_down", feed=source.key, error=detail)
        self._status[source.key] = replace(
            previous,
            cadence_s=source.cadence_s,
            ok=False,
            last_attempt_at=now,
            next_attempt_at=now + delay_s,
            detail=detail,
            retry_after=retry_after,
        )


# Process-wide recorder, set by the dashboard lifespan. None outside the app.
_current: MacroRecorder | None = None


def set_current(recorder: MacroRecorder | None) -> None:
    global _current
    _current = recorder


def current() -> MacroRecorder | None:
    return _current
