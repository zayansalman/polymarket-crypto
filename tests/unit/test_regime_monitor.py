"""Regime monitor + ledger (polymarket_bot/regime/{monitor,ledger}.py).

End-to-end through a fake HTTP client and a temp SQLite DB: the quality
flags and grade ``build_snapshot`` assigns, the book source choice (loop
ticks for the loop's own market, a direct CLOB read otherwise), the
persisted row and its join keys, and the threshold-version journal.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import market_selection
from polymarket_bot.regime import classify, ledger, monitor, sources
from polymarket_bot.regime.types import Bar, BookState, QualityFlag, VenueMarket

DAILY = ("doge", "sol", "xrp", "bnb", "eth")
# 1790000200 % 300 == 100 → 200s remaining in the window: inside the quotable phase.
NOW_TS = 1_790_000_200
NOW = datetime.fromtimestamp(NOW_TS, tz=UTC)
CREATED = NOW.isoformat(timespec="seconds")


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monitor._slow._items.clear()
    monitor._venue_completed.clear()
    monitor._thresholds_recorded.clear()
    return _db


def _bars(n: int, secs: int, qv: float = 1000.0, t0_ms: int | None = None) -> list[Bar]:
    """Deterministic bars whose per-second vol is the same at every bar length
    (moves scale with sqrt(bar seconds)), so seasonal ratios read ≈ 1."""
    t0 = t0_ms if t0_ms is not None else (NOW_TS - n * secs) * 1000
    scale = (secs / 60.0) ** 0.5
    out = []
    px = 100.0
    for i in range(n):
        o = px
        c = o * (1 + 0.0005 * scale * (1 if i % 3 else -1))
        out.append(Bar(t0 + i * secs * 1000, o, max(o, c) * (1 + 0.0004 * scale),
                       min(o, c) * (1 - 0.0004 * scale), c,
                       1.0, qv * secs / 60, 20 * secs, qv * secs / 120))
        px = c
    return out


def _kline_rows(n: int, secs: int, qv: float = 1000.0) -> list[list[Any]]:
    return [
        [b.open_time_ms, str(b.open), str(b.high), str(b.low), str(b.close), "1",
         b.open_time_ms + secs * 1000 - 1, str(b.quote_volume), b.trades, "0", str(b.taker_buy_quote), "0"]
        for b in _bars(n, secs, qv)
    ]


# --- build_snapshot (pure) ----------------------------------------------------------


def _build(**kw) -> Any:
    base = dict(
        asset="btc", symbol="BTCUSDT", timeframe="5m", created_at=CREATED, created_ts=NOW_TS,
        bars_1m=_bars(60, 60), bars_5m=_bars(288, 300), bars_1h=_bars(672, 3600),
        book=None, venue_current=None, venue_completed=[], edge_gate=0.045, daily_assets=DAILY,
        window_slug="btc-updown-5m-1790000100", run_id="run1", scan_seq=3,
    )
    base.update(kw)
    return monitor.build_snapshot(**base)


def test_build_snapshot_full_grade_when_everything_present() -> None:
    book = BookState(0.012, 0.008, 150.0, 12, 5, 4e-5, "chainlink_ws")
    snap = _build(book=book, venue_current=VenueMarket("s", 10.0, 100.0, "u", "d"), asset="eth", symbol="ETHUSDT")
    assert snap.quality == () and snap.grade == "full" and snap.usable_for_router
    assert "daily_altcoin" in {f.strategy_id for f in snap.fits}
    assert snap.bands["book"] == "cheap" and snap.sources["book"] == "paper_ticks"
    assert snap.sources["vol_1s"] == "chainlink_ws"
    assert snap.sources["estimator.volatility"] == "garman_klass_1m_60"
    assert snap.window_slug == "btc-updown-5m-1790000100" and snap.run_id == "run1" and snap.scan_seq == 3
    assert not snap.headline.startswith("PARTIAL DATA")


def test_build_snapshot_flags_each_missing_input() -> None:
    snap = _build(bars_1m=_bars(10, 60), bars_5m=_bars(100, 300), bars_1h=_bars(10, 3600))
    codes = [q.code for q in snap.quality]
    assert codes == ["bars_1m_short", "bars_5m_short", "bars_1h_short", "book_absent",
                     "venue_market_absent"]
    assert snap.grade == "partial" and not snap.usable_for_router
    assert snap.headline.startswith("PARTIAL DATA")
    assert all(q.code in classify.QUALITY_CODES for q in snap.quality)


def test_build_snapshot_none_grade_without_bars() -> None:
    snap = _build(bars_1m=[], bars_5m=[], bars_1h=[])
    assert snap.grade == "none"
    assert {f.strategy_id: f.fit for f in snap.fits} == {
        "btc_5m_taker": "blocked", "pairarb_maker": "blocked", "daily_altcoin": "blocked",
    }
    assert snap.recommendation == "stand down: no family can be evaluated"


def test_build_snapshot_stale_and_degraded_book_flags() -> None:
    book = BookState(0.012, 0.008, 150.0, 3, 120, None, "floor", feed_degraded=True)
    snap = _build(book=book, asset="eth", venue_current=VenueMarket("s", 1.0, 1.0))
    codes = {q.code for q in snap.quality}
    assert {"book_stale", "loop_feed_degraded", "loop_sigma_unusable"} <= codes
    assert snap.bands["book"] == "stale"


def test_build_snapshot_extra_flags_suppress_duplicates() -> None:
    snap = _build(extra_quality=[QualityFlag("book_out_of_phase", "20s"), QualityFlag("venue_slug_unknown", "x")])
    codes = [q.code for q in snap.quality]
    assert "book_absent" not in codes and "venue_market_absent" not in codes
    assert "book_out_of_phase" in codes and "venue_slug_unknown" in codes


def test_direct_clob_book_source_is_recorded_without_loop_sigma_flag() -> None:
    book = BookState(0.03, 0.03, 40.0, 1, 0, None, None, source="clob_direct")
    snap = _build(book=book, asset="eth", venue_current=VenueMarket("s", 1.0, 1.0))
    assert "loop_sigma_unusable" not in {q.code for q in snap.quality}
    assert snap.sources["book"] == "clob_direct"
    taker = {f.strategy_id: f for f in snap.fits}["btc_5m_taker"]
    assert "m.loop_sigma_unusable" in taker.reasons  # the fit still says the loop sigma is missing


# --- ledger ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_and_read_back_round_trip(test_db) -> None:
    snap = _build(asset="eth", symbol="ETHUSDT")
    row_id = await ledger.record_snapshot(snap)
    assert row_id == 1
    got = await ledger.latest_snapshot()
    assert got is not None
    assert got["asset"] == "eth" and got["created_ts"] == NOW_TS and got["window_slug"] == snap.window_slug
    assert got["run_id"] == "run1" and got["scan_seq"] == 3 and got["grade"] == "partial"
    assert got["usable_for_router"] is False
    assert got["bands"] == snap.bands
    assert got["features"]["vol_1h_gk"] == pytest.approx(snap.features.vol_1h_gk)
    assert [f["strategy_id"] for f in got["fits"]] == ["btc_5m_taker", "pairarb_maker", "daily_altcoin"]
    assert got["quality"][0] == {"code": "book_absent", "detail": "no in-phase book read"}
    assert got["sources"] == snap.sources
    assert got["recommendation"] == snap.recommendation
    assert await ledger.latest_snapshot(asset="btc") is None
    assert (await ledger.latest_snapshot(asset="eth"))["id"] == 1


@pytest.mark.asyncio
async def test_recent_snapshots_oldest_first_and_asset_scoped(test_db) -> None:
    for seq in range(3):
        await ledger.record_snapshot(_build(scan_seq=seq))
    await ledger.record_snapshot(_build(asset="eth", symbol="ETHUSDT", scan_seq=9))
    rows = await ledger.recent_snapshots(limit=2, asset="btc")
    assert [r["scan_seq"] for r in rows] == [1, 2]
    assert [r["scan_seq"] for r in await ledger.recent_snapshots()] == [0, 1, 2, 9]


@pytest.mark.asyncio
async def test_record_thresholds_is_idempotent(test_db) -> None:
    t = classify.DEFAULT_THRESHOLDS
    assert await ledger.record_thresholds(t.version, t.as_dict(), CREATED) is True
    assert await ledger.record_thresholds(t.version, {"changed": 1}, CREATED) is False
    async with test_db.connect() as conn:
        async with conn.execute("SELECT thresholds_json FROM regime_threshold_versions") as cur:
            rows = await cur.fetchall()
    assert len(rows) == 1 and json.loads(rows[0]["thresholds_json"])["vol_1s_low"] == 3e-5


# --- scan_all through a fake client ------------------------------------------------


class _Resp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload, self.status = payload, status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import httpx
            raise httpx.HTTPStatusError("x", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self.payload


class _Client:
    """Routes klines / gamma / clob URLs; records every call."""

    def __init__(self, *, klines: bool = True, gamma: bool = True, clob: bool = True) -> None:
        self.klines, self.gamma, self.clob = klines, gamma, clob
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _Resp:
        params = params or {}
        self.calls.append((url, params))
        if "/api/v3/klines" in url:
            if not self.klines:
                return _Resp(None, 500)
            secs = {"1m": 60, "5m": 300, "1h": 3600}[params["interval"]]
            return _Resp(_kline_rows(int(params["limit"]), secs))
        if "/markets" in url:
            if not self.gamma:
                return _Resp([], 404)
            slug = params["slug"]
            return _Resp([{"slug": slug, "volumeNum": 500.0, "liquidityNum": 120.0,
                           "clobTokenIds": '["up-tok", "down-tok"]', "outcomes": '["Up", "Down"]'}])
        if "/book" in url:
            if not self.clob:
                return _Resp(None, 500)
            return _Resp({"bids": [{"price": "0.48", "size": "100"}],
                          "asks": [{"price": "0.53", "size": "90"}]})
        return _Resp([], 404)


async def _insert_tick(db, *, age_s: int, remaining: int, slug: str = "btc-updown-5m-1790000100") -> None:
    created = datetime.fromtimestamp(NOW_TS - age_s, tz=UTC).isoformat(timespec="seconds")
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_ticks(created_at, window_slug, remaining_seconds, sigma_per_second, "
            "feed_source, reason, up_best_bid, up_best_ask, up_bid_size, up_ask_size, "
            "down_best_bid, down_best_ask, down_bid_size, down_ask_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (created, slug, remaining, 4.5e-5, "spot=chainlink_ws;ref=chainlink_rest;vol=chainlink_ws;quotes=clob",
             "skip: no strategy loaded", 0.49, 0.51, 100.0, 200.0, 0.49, 0.51, 100.0, 300.0),
        )
        await conn.commit()


async def _rows(db, asset: str | None = None) -> list[dict]:
    q = "SELECT asset, scan_seq, run_id, created_ts, timeframe, window_slug, grade FROM regime_snapshots"
    args: tuple = ()
    if asset:
        q += " WHERE asset = ?"
        args = (asset,)
    async with db.connect() as conn:
        async with conn.execute(q + " ORDER BY id", args) as cur:
            return [dict(r) for r in await cur.fetchall()]


def _book_calls(client) -> int:
    return sum(1 for u, _ in client.calls if u.endswith("/book"))


ALL_ASSETS = list(sources.SPOT_SYMBOL)


@pytest.mark.asyncio
async def test_scan_all_covers_all_six_assets_whatever_the_selector_shows(test_db) -> None:
    """The history must not depend on the header selector: sol/1d is selected, yet every cycle
    journals all six assets on the 5m family."""
    await market_selection.set_selection("sol", "1d")
    snaps = await monitor.scan_all(_Client(), now_ts=NOW_TS)  # type: ignore[arg-type]
    assert [s.asset for s in snaps] == ALL_ASSETS == ["btc", "eth", "sol", "xrp", "doge", "bnb"]
    assert {s.timeframe for s in snaps} == {"5m"}
    assert len({s.run_id for s in snaps}) == 1 and len({s.scan_seq for s in snaps}) == 1
    assert {s.created_ts for s in snaps} == {NOW_TS}
    assert {s.asset: s.window_slug for s in snaps} == {a: f"{a}-updown-5m-1790000100" for a in ALL_ASSETS}
    assert all(s.grade == "full" and s.usable_for_router for s in snaps)
    rows = await _rows(test_db)
    assert [r["asset"] for r in rows] == ALL_ASSETS
    for asset in ALL_ASSETS:
        latest = await ledger.latest_snapshot(asset=asset)
        assert latest is not None and latest["symbol"] == sources.SPOT_SYMBOL[asset]


@pytest.mark.asyncio
async def test_btc_reads_loop_ticks_while_every_other_asset_reads_the_book_directly(test_db) -> None:
    """Loop ticks are BTC 5m's only; a fresh BTC tick must never be scored as another asset's book."""
    await _insert_tick(test_db, age_s=3, remaining=150)
    client = _Client()
    snaps = {s.asset: s for s in await monitor.scan_all(client, now_ts=NOW_TS)}  # type: ignore[arg-type]
    assert snaps["btc"].sources["book"] == "paper_ticks" and snaps["btc"].sources["vol_1s"] == "chainlink_ws"
    assert snaps["btc"].features.overround == pytest.approx(0.02) and snaps["btc"].features.vol_1s == 4.5e-5
    assert all(snaps[a].sources["book"] == "clob_direct" for a in ALL_ASSETS if a != "btc")
    assert _book_calls(client) == 2 * 5  # up + down token for each of the five non-BTC assets
    assert snaps["btc"].grade == "full" and snaps["btc"].features.venue_windows_used == 6.0


@pytest.mark.asyncio
async def test_btc_falls_back_to_a_direct_read_when_loop_ticks_are_stale(test_db) -> None:
    await _insert_tick(test_db, age_s=500, remaining=150)
    client = _Client()
    snaps = {s.asset: s for s in await monitor.scan_all(client, now_ts=NOW_TS)}  # type: ignore[arg-type]
    assert snaps["btc"].sources["book"] == "clob_direct"
    assert snaps["btc"].features.overround == pytest.approx(0.06)
    assert snaps["btc"].features.maker_capture == pytest.approx(0.04)
    assert _book_calls(client) == 2 * 6
    assert "loop_sigma_unusable" not in {q.code for q in snaps["btc"].quality}


@pytest.mark.asyncio
async def test_each_cycle_is_one_scan_seq_shared_by_its_rows(test_db) -> None:
    client = _Client()
    first = await monitor.scan_all(client, now_ts=NOW_TS)  # type: ignore[arg-type]
    second = await monitor.scan_all(client, now_ts=NOW_TS + 60)  # type: ignore[arg-type]
    assert {s.scan_seq for s in second} == {first[0].scan_seq + 1}
    for asset in ALL_ASSETS:
        seqs = [r["scan_seq"] for r in await _rows(test_db, asset)]
        assert seqs == [first[0].scan_seq, first[0].scan_seq + 1]  # consecutive per asset


@pytest.mark.asyncio
async def test_one_failing_asset_does_not_stop_the_cycle_and_leaves_its_own_gap(
    test_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the isolation contract: a fetch that blows up for one asset costs only
    that asset's row, and the missing scan_seq in ITS series is what reveals the abort."""
    client = _Client()
    real_fetch = monitor.sources.fetch_bars

    async def flaky(c, symbol, interval, limit, **kw):
        if symbol == "ETHUSDT":
            raise RuntimeError("fetch failed")
        return await real_fetch(c, symbol, interval, limit, **kw)

    monkeypatch.setattr(monitor.sources, "fetch_bars", flaky)
    first = await monitor.scan_all(client, now_ts=NOW_TS)  # type: ignore[arg-type]
    assert [s.asset for s in first] == [a for a in ALL_ASSETS if a != "eth"]
    monkeypatch.setattr(monitor.sources, "fetch_bars", real_fetch)
    second = await monitor.scan_all(client, now_ts=NOW_TS + 60)  # type: ignore[arg-type]
    assert [s.asset for s in second] == ALL_ASSETS
    seq1 = first[0].scan_seq
    assert [r["scan_seq"] for r in await _rows(test_db, "eth")] == [seq1 + 1]      # cycle 1 missing
    assert [r["scan_seq"] for r in await _rows(test_db, "btc")] == [seq1, seq1 + 1]


@pytest.mark.asyncio
async def test_a_record_failure_on_one_row_does_not_drop_the_others(
    test_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_record = monitor.ledger.record_snapshot

    async def flaky_record(snapshot):
        if snapshot.asset == "sol":
            raise sqlite3.OperationalError("database is locked")
        return await real_record(snapshot)

    monkeypatch.setattr(monitor.ledger, "record_snapshot", flaky_record)
    journaled = await monitor.scan_all(_Client(), now_ts=NOW_TS)  # type: ignore[arg-type]
    assert [s.asset for s in journaled] == [a for a in ALL_ASSETS if a != "sol"]
    assert [r["asset"] for r in await _rows(test_db)] == [a for a in ALL_ASSETS if a != "sol"]


@pytest.mark.asyncio
async def test_every_source_failing_still_journals_all_six_rows(test_db) -> None:
    snaps = await monitor.scan_all(_Client(klines=False, gamma=False, clob=False), now_ts=NOW_TS)  # type: ignore[arg-type]
    assert [s.asset for s in snaps] == ALL_ASSETS and {s.grade for s in snaps} == {"none"}
    for s in snaps:
        assert {"bars_unavailable", "book_absent", "venue_market_absent"} <= {q.code for q in s.quality}
    assert len(await _rows(test_db)) == 6


@pytest.mark.asyncio
async def test_out_of_phase_window_records_the_flag_for_every_asset(test_db) -> None:
    snaps = await monitor.scan_all(_Client(), now_ts=NOW_TS - 90)  # type: ignore[arg-type]  # 290s left
    for s in snaps:
        codes = [q.code for q in s.quality]
        assert "book_out_of_phase" in codes and "book_absent" not in codes, s.asset


@pytest.mark.asyncio
async def test_thresholds_are_journaled_once_per_process(test_db) -> None:
    client = _Client()
    await monitor.scan_all(client, now_ts=NOW_TS)  # type: ignore[arg-type]
    await monitor.scan_all(client, now_ts=NOW_TS + 60)  # type: ignore[arg-type]
    async with test_db.connect() as conn:
        async with conn.execute("SELECT version FROM regime_threshold_versions") as cur:
            assert [r["version"] for r in await cur.fetchall()] == [classify.THRESHOLDS_VERSION]


@pytest.mark.asyncio
async def test_slow_bars_are_cached_across_cycles(test_db) -> None:
    client = _Client()
    await monitor.scan_all(client, now_ts=NOW_TS)  # type: ignore[arg-type]
    first = sum(1 for u, _ in client.calls if "klines" in u)
    await monitor.scan_all(client, now_ts=NOW_TS + 60)  # type: ignore[arg-type]
    second = sum(1 for u, _ in client.calls if "klines" in u) - first
    assert first == 6 * 3 and second == 6  # only the six 1m series are refetched


@pytest.mark.asyncio
async def test_assets_are_fetched_concurrently_not_one_after_another(test_db) -> None:
    class _SlowClient(_Client):
        in_flight = 0
        peak = 0

        async def get(self, url, params=None):  # type: ignore[override]
            _SlowClient.in_flight += 1
            _SlowClient.peak = max(_SlowClient.peak, _SlowClient.in_flight)
            await asyncio.sleep(0.01)
            try:
                return await super().get(url, params)
            finally:
                _SlowClient.in_flight -= 1

    await monitor.scan_all(_SlowClient(), now_ts=NOW_TS)  # type: ignore[arg-type]
    assert _SlowClient.peak >= 6  # a sequential scan would never exceed 3 in flight


@pytest.mark.asyncio
async def test_snapshot_asset_builds_without_persisting(test_db) -> None:
    snap = await monitor.snapshot_asset(
        _Client(), "eth", "5m", now_ts=NOW_TS, scan_seq=7, fresh_book_clock=False  # type: ignore[arg-type]
    )
    assert snap is not None and snap.asset == "eth" and snap.scan_seq == 7
    assert await _rows(test_db) == []


@pytest.mark.asyncio
async def test_snapshot_asset_daily_timeframe_uses_daily_slug(test_db) -> None:
    client = _Client()
    snap = await monitor.snapshot_asset(
        client, "sol", "1d", now_ts=NOW_TS, scan_seq=1, fresh_book_clock=False  # type: ignore[arg-type]
    )
    assert snap is not None and snap.window_slug is None
    slugs = [p.get("slug") for _, p in client.calls if "slug" in p]
    assert slugs and all(s.startswith("solana-up-or-down-on-") for s in slugs)
    assert "book_absent" in {q.code for q in snap.quality}  # no clock-derived window to read


@pytest.mark.asyncio
async def test_snapshot_asset_skips_an_unmapped_asset(test_db) -> None:
    assert await monitor.snapshot_asset(
        _Client(), "not-an-asset", "5m", now_ts=NOW_TS, scan_seq=1  # type: ignore[arg-type]
    ) is None


def test_scan_assets_follow_the_symbol_map_and_cover_every_selectable_asset() -> None:
    assert monitor.scan_assets() == tuple(sources.SPOT_SYMBOL)
    assert set(market_selection.ASSETS) <= set(monitor.scan_assets())
    assert monitor.SCAN_TIMEFRAME == "5m"


def test_monitor_knobs_are_registered() -> None:
    from polymarket_bot import runtime_knobs as k

    assert k.KNOBS["regime_scan_interval_seconds"].default == 60.0
    assert k.KNOBS["regime_monitor_enabled"].default is True
    assert k.KNOBS["regime_monitor_enabled"].group == "Regime monitor"


# --- review regressions ------------------------------------------------------------------


def test_the_loops_own_market_is_not_capped_below_full_grade() -> None:
    """Regression: a `daily_family_proxy` quality flag made btc — the default and only
    loop-supported market — permanently `partial`, never usable_for_router."""
    book = BookState(0.012, 0.008, 150.0, 12, 5, 4e-5, "chainlink_ws")
    snap = _build(book=book, venue_current=VenueMarket("s", 1.0, 1.0))
    assert snap.asset == "btc" and snap.grade == "full" and not snap.headline.startswith("PARTIAL")


@pytest.mark.real_regime_monitor
@pytest.mark.asyncio
async def test_run_forever_survives_knob_read_errors(test_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the interval read sat outside the try, so one transient SQLite error
    ('database is locked') ended the always-on monitor with no trace."""
    async def locked(_name: str):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(monitor._knobs, "get", locked)
    slept: list[float] = []
    stop = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 3:
            stop.set()

    monkeypatch.setattr(monitor.asyncio, "sleep", fake_sleep)
    await monitor.run_forever(stop)
    assert slept == [60.0, 60.0, 60.0]  # the registered default, three surviving cycles


@pytest.mark.real_regime_monitor
@pytest.mark.asyncio
async def test_run_forever_keeps_going_after_a_failed_scan(test_db, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def boom(_client, now_ts=None):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(monitor, "scan_all", boom)
    stop = asyncio.Event()

    async def fake_sleep(_s: float) -> None:
        if attempts >= 2:
            stop.set()

    monkeypatch.setattr(monitor.asyncio, "sleep", fake_sleep)
    await monitor.run_forever(stop)
    assert attempts == 2


@pytest.mark.real_regime_monitor
@pytest.mark.asyncio
async def test_run_forever_pauses_when_the_enabled_knob_is_off(test_db, monkeypatch: pytest.MonkeyPatch) -> None:
    from polymarket_bot import runtime_knobs as k

    await k.set("regime_monitor_enabled", False)
    scans = 0

    async def counting(_client, now_ts=None):
        nonlocal scans
        scans += 1

    monkeypatch.setattr(monitor, "scan_all", counting)
    stop = asyncio.Event()
    cycles = 0

    async def fake_sleep(_s: float) -> None:
        nonlocal cycles
        cycles += 1
        if cycles >= 2:
            stop.set()

    monkeypatch.setattr(monitor.asyncio, "sleep", fake_sleep)
    await monitor.run_forever(stop)
    assert scans == 0 and cycles == 2


def test_default_test_env_keeps_the_monitor_idle_but_the_marker_opts_back_in() -> None:
    """The autouse conftest stub stops app-lifespan tests polling live venues / the real DB."""
    assert monitor.run_forever.__name__ == "_idle"


@pytest.mark.real_regime_monitor
def test_real_monitor_marker_restores_the_real_loop() -> None:
    assert monitor.run_forever.__name__ == "run_forever"


@pytest.mark.asyncio
async def test_completed_window_cache_waits_for_a_full_window_to_settle(test_db) -> None:
    """Regression: every completed window was cached on first read, so the window that closed
    seconds ago froze whatever partial Gamma volume existed at that instant."""
    client = _Client()
    await monitor.snapshot_asset(client, "btc", "5m", now_ts=NOW_TS, scan_seq=1, fresh_book_clock=False)  # type: ignore[arg-type]
    current_start = NOW_TS - NOW_TS % 300
    newest_completed = f"btc-updown-5m-{current_start - 300}"
    older_completed = f"btc-updown-5m-{current_start - 600}"
    assert newest_completed not in monitor._venue_completed   # closed 100s ago: not settled
    assert older_completed in monitor._venue_completed        # closed 400s ago: settled
    assert len(monitor._venue_completed) == 5
    before = sum(1 for u, _ in client.calls if "/markets" in u)
    await monitor.snapshot_asset(client, "btc", "5m", now_ts=NOW_TS + 60, scan_seq=2, fresh_book_clock=False)  # type: ignore[arg-type]
    after = sum(1 for u, _ in client.calls if "/markets" in u)
    assert after - before == 2  # only the current window and the not-yet-settled one are refetched


@pytest.mark.asyncio
async def test_book_phase_is_measured_against_the_venue_windows_own_end() -> None:
    """Regression: the phase used a fixed 60-270s band and the scan-start clock. It is now a fraction
    of the selected window and is computed at the book stage against the window's end."""
    venue = VenueMarket("eth-updown-15m-0", 1.0, 1.0, "up", "down")
    client = _Client()
    # window [0, 900): 120s remaining -> closing phase (< 0.2 * 900) -> no book, honest flag.
    book, flag = await monitor._book(client, "eth", "15m", venue, 780, 0)  # type: ignore[arg-type]
    assert book is None and flag is not None and flag.code == "book_out_of_phase"
    # 200s remaining -> quotable for a 15m window -> direct read.
    book, flag = await monitor._book(client, "eth", "15m", venue, 700, 0)  # type: ignore[arg-type]
    assert flag is None and book is not None and book.source == "clob_direct"
    # The window has already ended by the book stage -> negative remaining -> out of phase.
    book, flag = await monitor._book(client, "eth", "15m", venue, 901, 0)  # type: ignore[arg-type]
    assert book is None and flag is not None and flag.code == "book_out_of_phase"
    # No tokens / no window -> book_absent, never a fabricated read.
    _, flag = await monitor._book(client, "eth", "15m", VenueMarket("s", 1.0, 1.0), 700, 0)  # type: ignore[arg-type]
    assert flag is not None and flag.code == "book_absent"
    _, flag = await monitor._book(client, "eth", "15m", venue, 700, None)  # type: ignore[arg-type]
    assert flag is not None and flag.code == "book_absent"


def test_thresholds_version_moves_with_rule_semantics() -> None:
    assert classify.THRESHOLDS_VERSION == "2026-09-29.v1"
