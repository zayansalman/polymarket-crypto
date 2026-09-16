"""Macro recorder: per-source cadence, failure retry delays, RetryAfter, and the default sources."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

import db as _db
from polymarket_exec.ops import macro_recorder as mr
from polymarket_exec.storage import macro_store as store

# Captured before the autouse conftest fixture swaps ``run`` for an offline stub.
_REAL_RUN = mr.MacroRecorder.run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "macro"
T0 = 1_789_400_000.0  # 2026-09-14 ~15:33 UTC
DAY_MS = 86_400_000


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


class _Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    @property
    def ms(self) -> int:
        return int(self.t * 1000)


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event: str, **kw) -> None:
        self.events.append((event, kw))

    info = warning


def _source(key: str, results: list, cadence_s: float = 3600.0, min_retry_s: float | None = None,
            calls: list | None = None) -> mr.MacroSource:
    async def poll(client: httpx.AsyncClient, clock) -> int:
        if calls is not None:
            calls.append(clock())
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, BaseException):
            raise result
        return result

    return mr.MacroSource(key, cadence_s, poll, min_retry_s)


def _client(handler=None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(404))))


def test_default_sources_and_client() -> None:
    sources = {s.key: s for s in mr.default_sources()}
    assert list(sources) == [mr.BLS_SCHEDULE, mr.BEA_SCHEDULE, mr.CENSUS_SCHEDULE,
                             mr.FED_CALENDAR, mr.FF_WEEK]
    assert sources[mr.BLS_SCHEDULE].cadence_s == 6 * 3600
    assert sources[mr.CENSUS_SCHEDULE].cadence_s == 6 * 3600
    assert sources[mr.FED_CALENDAR].cadence_s == 3600
    assert sources[mr.FF_WEEK].cadence_s == 3600
    assert sources[mr.FF_WEEK].retry_s >= 300
    assert {s.deadline_s for s in sources.values()} == {120.0}
    assert mr.MacroSource("x:y", 600.0, sources[mr.FF_WEEK].poll).retry_s == 600.0
    assert mr.MacroSource("x:y", 86_400.0, sources[mr.FF_WEEK].poll).retry_s == 900.0
    client = mr._default_client()
    try:
        ua = client.headers["User-Agent"]
        assert ua == mr.USER_AGENT and "@" not in ua and "python-httpx" not in ua
        assert client.timeout.read == 20.0
    finally:
        asyncio.run(client.aclose())


def _fixture_handler(calls: list[str], *, ff_status: int = 200, ff_headers=None,
                     ff_body: bytes | None = None, bls_status: int = 200):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == mr.BLS_ICS_URL:
            if bls_status != 200:
                return httpx.Response(bls_status, text="<html>Access Denied</html>")
            return httpx.Response(200, content=(FIXTURES / "bls_schedule.ics").read_bytes(),
                                  headers={"content-type": "text/calendar"})
        if url == mr.BEA_ICS_URL:
            return httpx.Response(200, content=(FIXTURES / "bea_schedule.ics").read_bytes(),
                                  headers={"content-type": "text/plain; charset=UTF-8"})
        if url == mr.CENSUS_CALENDAR_URL:
            return httpx.Response(200, content=(FIXTURES / "census_calendar.html").read_bytes(),
                                  headers={"content-type": "text/html;charset=UTF-8"})
        if url == mr.FED_CALENDAR_URL:  # served with a UTF-8 BOM, like the live endpoint
            body = b"\xef\xbb\xbf" + (FIXTURES / "fed_calendar.json").read_bytes()
            return httpx.Response(200, content=body, headers={"content-type": "application/json"})
        if url == mr.FF_WEEK_URL:
            if ff_status != 200 or ff_body is not None:
                return httpx.Response(ff_status, content=ff_body or b"<html>Rate Limited</html>",
                                      headers=ff_headers or {"content-type": "text/html"})
            return httpx.Response(200, content=(FIXTURES / "ff_thisweek.json").read_bytes(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(404)

    return handle


@pytest.mark.asyncio
async def test_default_sources_record_every_calendar(test_db) -> None:
    clock = _Clock()
    calls: list[str] = []
    rec = mr.MacroRecorder(time_fn=clock)
    async with _client(_fixture_handler(calls)) as client:
        await rec.record_once(client, clock.ms)
    feeds = rec.snapshot().feeds
    assert {k: (f.ok, f.rows) for k, f in feeds.items()} == {
        mr.BLS_SCHEDULE: (True, 7), mr.BEA_SCHEDULE: (True, 8), mr.CENSUS_SCHEDULE: (True, 3),
        mr.FED_CALENDAR: (True, 7), mr.FF_WEEK: (True, 8),
    }
    assert sorted(calls) == sorted([mr.BLS_ICS_URL, mr.BEA_ICS_URL, mr.CENSUS_CALENDAR_URL,
                                    mr.FED_CALENDAR_URL, mr.FF_WEEK_URL])
    upcoming = await store.events_between(clock.ms, clock.ms + 40 * DAY_MS,
                                          categories=["fomc_decision", "cpi", "retail_sales"])
    assert {(r["source"], r["title"]) for r in upcoming} >= {
        ("fed", "FOMC Meeting"), ("forexfactory", "Federal Funds Rate"),
        ("bls", "Consumer Price Index"), ("census", "Advance Monthly Sales for Retail and Food Services"),
    }
    claims_at = [r for r in await store.events_between(clock.ms, clock.ms + 7 * DAY_MS,
                                                        categories=["jobless_claims"])][0]
    row = await store.consensus_as_of("forexfactory", "Unemployment Claims",
                                      claims_at["scheduled_at_ms"], clock.ms)
    assert row is not None and row["forecast"] == "209K"


@pytest.mark.asyncio
async def test_sources_poll_on_their_own_cadence() -> None:
    clock = _Clock()
    fast_calls: list[int] = []
    slow_calls: list[int] = []
    rec = mr.MacroRecorder(
        sources=[_source("a:fast", [3], cadence_s=3600, calls=fast_calls),
                 _source("b:slow", [5], cadence_s=6 * 3600, calls=slow_calls)],
        time_fn=clock,
    )
    async with _client() as client:
        await rec.record_once(client, clock.ms)
        clock.t += 3599
        await rec.record_once(client, clock.ms)
        assert (len(fast_calls), len(slow_calls)) == (1, 1)
        clock.t += 1
        await rec.record_once(client, clock.ms)
        assert (len(fast_calls), len(slow_calls)) == (2, 1)
        clock.t = T0 + 6 * 3600
        await rec.record_once(client, clock.ms)
    assert (len(fast_calls), len(slow_calls)) == (3, 2)
    fast = rec.snapshot().feeds["a:fast"]
    assert (fast.ok, fast.rows, fast.last_ok_at, fast.last_attempt_at) == (True, 3, clock.t, clock.t)
    assert fast.next_attempt_at == clock.t + 3600 and fast.cadence_s == 3600
    assert fast.detail is None and fast.retry_after is False


@pytest.mark.asyncio
async def test_each_source_is_stamped_when_its_answer_arrives(test_db) -> None:
    """A slow source earlier in the pass, or the request itself, never back-dates a stamp."""
    clock = _Clock()

    async def slow_failure(client: httpx.AsyncClient, _clock) -> int:
        clock.t += 30  # e.g. a host that takes its whole connect timeout
        raise RuntimeError("slow host")

    fixture = _fixture_handler([])

    def two_second_request(request: httpx.Request) -> httpx.Response:
        clock.t += 2
        return fixture(request)

    ff = [s for s in mr.default_sources() if s.key == mr.FF_WEEK]
    rec = mr.MacroRecorder(sources=[mr.MacroSource("a:slow", 6 * 3600.0, slow_failure), *ff],
                           time_fn=clock)
    async with _client(two_second_request) as c:
        await rec.record_once(c, clock.ms)
    sent, arrived = T0 + 30, T0 + 32
    slow, st = rec.snapshot().feeds["a:slow"], rec.snapshot().feeds[mr.FF_WEEK]
    assert (slow.last_attempt_at, slow.next_attempt_at) == (T0, sent + 900)
    assert (st.ok, st.last_attempt_at, st.last_ok_at, st.next_attempt_at) == (
        True, sent, arrived, arrived + 3600)
    rows = await store.events_between(0, 2**62, include_removed=True)
    assert {r["first_seen_ms"] for r in rows} == {int(arrived * 1000)}
    claims = [r for r in rows if r["title"] == "Unemployment Claims"][0]
    key = ("forexfactory", "Unemployment Claims", claims["scheduled_at_ms"])
    assert await store.consensus_as_of(*key, int(T0 * 1000) + 10_000) is None
    assert await store.consensus_as_of(*key, int(arrived * 1000) - 1) is None
    row = await store.consensus_as_of(*key, int(arrived * 1000))
    assert row is not None and (row["forecast"], row["taken_at_ms"]) == ("209K", arrived * 1000)


@pytest.mark.asyncio
async def test_stamps_never_run_before_the_pass_start(test_db) -> None:
    clock = _Clock()
    seen: list[int] = []
    rec = mr.MacroRecorder(sources=[_source("a:src", [1], calls=seen)], time_fn=clock)
    async with _client() as c:
        await rec.record_once(c, clock.ms + 5000)  # the wall clock stepped back since then
    st = rec.snapshot().feeds["a:src"]
    assert seen == [clock.ms + 5000]
    assert (st.last_attempt_at, st.last_ok_at, st.next_attempt_at) == (T0 + 5, T0 + 5, T0 + 3605)


@pytest.mark.asyncio
async def test_failure_retries_after_the_retry_delay_and_keeps_last_success(monkeypatch) -> None:
    log = _Log()
    monkeypatch.setattr(mr, "log", log)
    clock = _Clock()
    calls: list[int] = []
    results: list = [4, RuntimeError("boom"), RuntimeError("boom again"), 6]
    rec = mr.MacroRecorder(sources=[_source("a:src", results, cadence_s=3600, min_retry_s=600,
                                            calls=calls)], time_fn=clock)
    async with _client() as client:
        await rec.record_once(client, clock.ms)  # ok
        clock.t += 3600
        await rec.record_once(client, clock.ms)  # fails
        st = rec.snapshot().feeds["a:src"]
        assert (st.ok, st.rows, st.last_ok_at, st.detail) == (False, 4, T0, "RuntimeError: boom")
        assert st.next_attempt_at == clock.t + 600 and st.retry_after is False
        clock.t += 599
        await rec.record_once(client, clock.ms)
        assert len(calls) == 2  # still inside the retry delay
        clock.t += 1
        await rec.record_once(client, clock.ms)  # fails again: logged once
        clock.t += 600
        await rec.record_once(client, clock.ms)  # recovers
    assert len(calls) == 4
    st = rec.snapshot().feeds["a:src"]
    assert (st.ok, st.rows, st.detail, st.last_ok_at) == (True, 6, None, clock.t)
    assert [e for e, _kw in log.events] == ["macro_recorder.feed_down",
                                            "macro_recorder.feed_recovered"]


@pytest.mark.asyncio
async def test_retry_after_pushes_the_next_attempt_out() -> None:
    clock = _Clock()
    calls: list[int] = []
    rec = mr.MacroRecorder(
        sources=[_source("a:src", [mr.RetryAfter(1200, "rate limited"), 2], cadence_s=3600,
                         calls=calls)],
        time_fn=clock,
    )
    async with _client() as client:
        await rec.record_once(client, clock.ms)
        st = rec.snapshot().feeds["a:src"]
        assert (st.ok, st.retry_after, st.detail, st.next_attempt_at) == (
            False, True, "rate limited", T0 + 1200)
        clock.t += 1199
        await rec.record_once(client, clock.ms)
        assert len(calls) == 1
        clock.t += 1
        await rec.record_once(client, clock.ms)
    assert len(calls) == 2 and rec.snapshot().feeds["a:src"].retry_after is False


@pytest.mark.asyncio
async def test_a_source_past_its_deadline_fails_and_the_pass_moves_on(test_db) -> None:
    clock = _Clock()
    later: list[int] = []

    async def hangs(client: httpx.AsyncClient, _now) -> int:
        await asyncio.sleep(30)  # e.g. a host dripping bytes just inside the read timeout
        return 1

    async def own_timeout(client: httpx.AsyncClient, _now) -> int:
        raise TimeoutError("upstream busy")

    rec = mr.MacroRecorder(
        sources=[mr.MacroSource("a:hangs", 3600.0, hangs, deadline_s=0.05),
                 mr.MacroSource("b:own", 3600.0, own_timeout, deadline_s=5.0),
                 _source("c:next", [2], calls=later)],
        time_fn=clock,
    )
    async with _client() as c:
        await asyncio.wait_for(rec.record_once(c, clock.ms), timeout=5)
    feeds = rec.snapshot().feeds
    hung = feeds["a:hangs"]
    assert (hung.ok, hung.detail, hung.retry_after) == (
        False, "no complete answer within 0.05s", False)
    assert hung.next_attempt_at == T0 + 900
    assert (feeds["b:own"].ok, feeds["b:own"].detail) == (False, "TimeoutError: upstream busy")
    assert len(later) == 1 and feeds["c:next"].ok is True


@pytest.mark.asyncio
async def test_retry_after_detail_drops_query_strings(test_db, monkeypatch) -> None:
    log = _Log()
    monkeypatch.setattr(mr, "log", log)
    clock = _Clock()

    async def keyed(client: httpx.AsyncClient, _now) -> int:
        raise mr.RetryAfter(60, "HTTP 429 from https://api.example.com/flows?api_key=SECRET123")

    rec = mr.MacroRecorder(sources=[mr.MacroSource("x:keyed", 3600.0, keyed)], time_fn=clock)
    async with _client() as c:
        await rec.record_once(c, clock.ms)
    st = rec.snapshot().feeds["x:keyed"]
    assert (st.ok, st.retry_after) == (False, True)
    assert st.detail == "HTTP 429 from https://api.example.com/flows"
    assert log.events == [("macro_recorder.feed_down", {"feed": "x:keyed", "error": st.detail})]


@pytest.mark.asyncio
async def test_record_once_never_raises_and_other_sources_still_run(test_db) -> None:
    clock = _Clock()
    rec = mr.MacroRecorder(
        sources=[_source("a:bad", [ValueError("bad payload")]),
                 _source("b:weird", ["not a number"]),
                 _source("c:good", [1])],
        time_fn=clock,
    )
    async with _client() as client:
        await rec.record_once(client, clock.ms)
    feeds = rec.snapshot().feeds
    assert (feeds["a:bad"].ok, feeds["a:bad"].detail) == (False, "ValueError: bad payload")
    assert feeds["b:weird"].ok is False
    assert feeds["c:good"].ok is True


@pytest.mark.asyncio
async def test_zero_events_and_http_errors_are_failures_without_query_strings(test_db) -> None:
    clock = _Clock()
    calls: list[str] = []
    rec = mr.MacroRecorder(time_fn=clock)
    empty_ff = json.dumps([{"title": "CPI y/y", "country": "GBP",
                            "date": "2026-09-16T02:00:00-04:00"}]).encode()
    async with _client(_fixture_handler(calls, bls_status=403, ff_body=empty_ff,
                                        ff_headers={"content-type": "application/json"})) as c:
        await rec.record_once(c, clock.ms)
    feeds = rec.snapshot().feeds
    assert feeds[mr.BLS_SCHEDULE].ok is False
    assert feeds[mr.BLS_SCHEDULE].detail.startswith(
        "HTTP 403 from www.bls.gov/schedule/news_release/bls.ics")
    assert (feeds[mr.FF_WEEK].ok, feeds[mr.FF_WEEK].detail) == (False, "no USD events in the response")

    async def secret_poll(client: httpx.AsyncClient, now_ms: int) -> int:
        await mr.fetch(client, "https://api.example.gov/data?registrationkey=SECRET123")
        return 1

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    for handler in (lambda r: httpx.Response(500), refuse):
        rec2 = mr.MacroRecorder(sources=[mr.MacroSource("x:secret", 3600.0, secret_poll)],
                                time_fn=clock)
        async with _client(handler) as c:
            await rec2.record_once(c, clock.ms)
        detail = rec2.snapshot().feeds["x:secret"].detail or ""
        assert "api.example.gov/data" in detail and "SECRET123" not in detail
        assert "registrationkey" not in detail


@pytest.mark.asyncio
async def test_forexfactory_429_waits_at_least_five_minutes_with_one_request(test_db) -> None:
    for header, wait in (("600", 600), ("30", 300), (None, 300)):
        clock = _Clock()
        calls: list[str] = []
        headers = {"content-type": "text/html"}
        if header is not None:
            headers["retry-after"] = header
        rec = mr.MacroRecorder(sources=[s for s in mr.default_sources() if s.key == mr.FF_WEEK],
                               time_fn=clock)
        async with _client(_fixture_handler(calls, ff_status=429, ff_headers=headers)) as c:
            await rec.record_once(c, clock.ms)
            st = rec.snapshot().feeds[mr.FF_WEEK]
            assert (st.ok, st.retry_after, st.next_attempt_at) == (False, True, T0 + wait)
            assert "429" in (st.detail or "")
            clock.t += wait - 1
            await rec.record_once(c, clock.ms)
        assert calls == [mr.FF_WEEK_URL]  # exactly one request per attempt, no early retry


@pytest.mark.asyncio
async def test_forexfactory_edge_rows_of_the_week_can_be_removed(test_db) -> None:
    """The week's first slot moves later and its last slot is dropped: both old slots go."""
    week = json.loads((FIXTURES / "ff_thisweek.json").read_bytes())
    edited = [dict(r, date="2026-09-16T08:45:00-04:00") if r["title"] == "Retail Sales m/m"
              else r for r in week
              if r["title"] not in ("Core Retail Sales m/m", "FOMC Member Bowman Speaks")]
    clock = _Clock()
    ff = [s for s in mr.default_sources() if s.key == mr.FF_WEEK]
    for body in (None, json.dumps(edited).encode()):
        rec = mr.MacroRecorder(sources=ff, time_fn=clock)
        handler = _fixture_handler([], ff_body=body,
                                   ff_headers={"content-type": "application/json"})
        async with _client(handler) as c:
            await rec.record_once(c, clock.ms)
        assert rec.snapshot().feeds[mr.FF_WEEK].ok is True
        clock.t += 3600
    rows = await store.events_between(0, 2**62, include_removed=True)
    status = {(r["title"], r["scheduled_at_ms"]): r["status"] for r in rows}
    wed_1230, wed_1245 = 1_789_561_800_000, 1_789_562_700_000  # 2026-09-16 12:30Z / 12:45Z
    assert status[("Core Retail Sales m/m", wed_1230)] == "removed"
    assert status[("Retail Sales m/m", wed_1230)] == "removed"
    assert status[("Retail Sales m/m", wed_1245)] == "scheduled"
    assert status[("FOMC Member Bowman Speaks", 1_789_738_200_000)] == "removed"  # Fri 13:30Z
    assert status[("Unemployment Claims", 1_789_648_200_000)] == "scheduled"


@pytest.mark.asyncio
async def test_forexfactory_html_body_is_no_data(test_db) -> None:
    clock = _Clock()
    rec = mr.MacroRecorder(sources=[s for s in mr.default_sources() if s.key == mr.FF_WEEK],
                           time_fn=clock)
    async with _client(_fixture_handler([], ff_body=b"<html>Rate Limited</html>")) as c:
        await rec.record_once(c, clock.ms)
    st = rec.snapshot().feeds[mr.FF_WEEK]
    assert st.ok is False and st.retry_after is False and "no data" in (st.detail or "")


def test_snapshot_lists_every_source_before_any_attempt() -> None:
    clock = _Clock()
    rec = mr.MacroRecorder(tick_s=30.0, time_fn=clock)
    snap = rec.snapshot()
    assert (snap.taken_at, snap.started_at, snap.tick_s) == (T0, T0, 30.0)
    assert list(snap.feeds) == [s.key for s in mr.default_sources()]
    assert all(f.ok is None and f.last_attempt_at is None and f.rows is None
               for f in snap.feeds.values())


@pytest.mark.asyncio
async def test_run_records_then_stops_cleanly() -> None:
    calls: list[int] = []
    rec = mr.MacroRecorder(sources=[_source("a:src", [2], calls=calls)], tick_s=3600,
                           client_factory=_client)
    stop = asyncio.Event()
    task = asyncio.create_task(_REAL_RUN(rec, stop))
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert len(calls) == 1 and rec.snapshot().feeds["a:src"].ok is True


def test_current_registry() -> None:
    rec = mr.MacroRecorder()
    mr.set_current(rec)
    try:
        assert mr.current() is rec
    finally:
        mr.set_current(None)
    assert mr.current() is None


def test_dashboard_lifespan_registers_and_clears_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    from fastapi.testclient import TestClient

    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as client:
        rec = mr.current()
        assert isinstance(rec, mr.MacroRecorder)
        page = client.get("/").text
        assert "BLS schedule" in page and "ForexFactory week" in page  # FEEDS card reads it
    assert mr.current() is None
