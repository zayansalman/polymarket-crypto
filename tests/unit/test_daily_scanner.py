"""Daily altcoin scanner tick tests.

Pins the one thing a live run caught: settlement and entry are INDEPENDENT
halves of a tick. Position id 257 (dogecoin, resolved 2026-09-15T16:00Z) sat
``state='open'`` for over a day because ``scan_once`` returned early whenever
discovery came back empty — a Gamma outage, or the UTC-date lookup bug fixed
in #239, which left discovery empty from noon ET to UTC midnight. Settlement
needs nothing from discovery (it settles off the ``reference_price`` /
``resolves_at`` / ``binance_symbol`` stamped on each open row), so it must
run on every tick regardless, and neither half may swallow the other.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.daily import scanner as _scanner
from polymarket_bot.daily.ledger import record_signal
from polymarket_bot.daily.types import DailyMarketView, DailySignal


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeBinanceClient:
    """Answers the 1-minute kline lookup ``fetch_close_at`` makes."""

    def __init__(self, close: float):
        self._close = close
        self.kline_calls: list[dict] = []

    async def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/klines") and params.get("interval") == "1m":
            self.kline_calls.append(params)
            return _FakeResponse([[0, 0, 0, 0, self._close]])
        raise AssertionError(f"unexpected request: {url} {params}")


class _RecordingLog:
    """Stands in for the module logger so a test can pin that a failure was
    reported rather than swallowed (AGENTS.md: no silent failures)."""

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def _record(self, level):
        def _log(event, **_kw):
            self.events.append((level, event))

        return _log

    def __getattr__(self, level):
        return self._record(level)


_PAST_SLUG = "dogecoin-up-or-down-on-september-15-2026"


def _past_iso(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


async def _record_open_past_due(
    *,
    window_slug: str = _PAST_SLUG,
    reference_price: float = 0.20,
    resolves_at: str | None = None,
) -> None:
    await record_signal(
        created_at=_past_iso(hours=26),
        window_slug=window_slug,
        asset="doge",
        side="Up",
        entry_price=0.45,
        fair_prob=0.55,
        edge=0.10,
        confidence=0.6,
        reason="enter Up",
        notional_usd=10.0,
        shares=22.0,
        reference_price=reference_price,
        resolves_at=resolves_at or _past_iso(hours=2),
        binance_symbol="DOGEUSDT",
    )


async def _states() -> dict[str, str]:
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT window_slug, state, outcome FROM daily_shadow_positions"
        ) as cur:
            return {r["window_slug"]: r["state"] for r in await cur.fetchall()}


async def _row(window_slug: str) -> dict:
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM daily_shadow_positions WHERE window_slug = ?", (window_slug,)
        ) as cur:
            return dict(await cur.fetchone())


def _view() -> DailyMarketView:
    return DailyMarketView(
        asset="sol",
        window_slug="solana-up-or-down-on-september-16-2026",
        condition_id="0xabc",
        up_token="1",
        down_token="2",
        binance_symbol="SOLUSDT",
        resolves_at="2026-09-16T16:00:00Z",
        remaining_seconds=7200,
        spot=105.0,
        reference=100.0,
        up_ask=0.50,
        down_ask=0.52,
        market_up_price=0.50,
        fair_up=0.60,
        sigma_per_second=1e-5,
        drift_per_second=0.0,
        liquidity_usd=5000.0,
        order_min_size=5.0,
    )


def _signal() -> DailySignal:
    return DailySignal(
        asset="sol",
        side="Up",
        entry_price=0.50,
        fair_prob=0.60,
        edge=0.10,
        confidence=0.7,
        reason="enter Up",
    )


@pytest.mark.asyncio
async def test_settles_due_position_when_discovery_finds_no_markets(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """The regression pin for id 257: an empty discovery result must not cost
    the tick its settlement pass."""
    await _record_open_past_due(reference_price=0.20)

    async def _no_markets(_client):
        return {}

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _no_markets)
    client = _FakeBinanceClient(close=0.25)  # settled above reference -> Up

    await _scanner.scan_once(client)

    row = await _row(_PAST_SLUG)
    assert row["state"] == "settled"
    assert row["outcome"] == "Up"
    assert row["settlement_price"] == pytest.approx(0.25)
    assert row["realized_pnl_usd"] > 0  # side Up, outcome Up


@pytest.mark.asyncio
async def test_empty_discovery_is_still_logged(test_db, monkeypatch: pytest.MonkeyPatch):
    """Settling on an empty tick must not quietly drop the 'no markets'
    warning — that log is how an operator sees a discovery outage."""
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    async def _no_markets(_client):
        return {}

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _no_markets)

    await _scanner.scan_once(_FakeBinanceClient(close=0.25))

    assert ("warning", "daily_scan.no_markets_found") in recorder.events


@pytest.mark.asyncio
async def test_settlement_failure_does_not_block_entry(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """A broken settlement pass must still leave the tick able to enter."""
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    view, sig = _view(), _signal()

    async def _one_market(_client):
        return {"sol": {"slug": view.window_slug}}

    async def _scored(_client, _asset, _market):
        return view, sig

    async def _boom(_client):
        raise RuntimeError("settlement feed down")

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _one_market)
    monkeypatch.setattr(_scanner, "_scored_view", _scored)
    monkeypatch.setattr(_scanner, "_settle_due", _boom)

    await _scanner.scan_once(_FakeBinanceClient(close=0.25))

    assert view.window_slug in await _states()
    assert ("exception", "daily_scan.settle_failed") in recorder.events


@pytest.mark.asyncio
async def test_entry_failure_does_not_block_settlement(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """The reverse: a discovery/entry blow-up must not strand a due window."""
    await _record_open_past_due(reference_price=0.20)
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    async def _boom(_client):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _boom)

    await _scanner.scan_once(_FakeBinanceClient(close=0.15))

    row = await _row(_PAST_SLUG)
    assert row["state"] == "settled"
    assert row["outcome"] == "Down"  # 0.15 < reference 0.20
    assert ("exception", "daily_scan.entry_pass_failed") in recorder.events
