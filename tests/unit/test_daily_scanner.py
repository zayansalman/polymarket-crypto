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

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.daily import scanner as _scanner
from polymarket_bot.daily.ledger import record_signal
from polymarket_bot.daily.types import DailyMarketView, DailySignal
from polymarket_bot.fees import net_pnl_per_share


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
    """Answers the 1-minute kline lookup ``fetch_close_at`` makes.

    ``close`` may be a plain price (every symbol settles there), a dict
    keyed by symbol, or an exception instance to raise for that symbol.
    ``None`` stands for "Binance answered, but with no rows" — the shape
    that makes ``fetch_close_at`` return ``None``.
    """

    def __init__(self, close):
        self._close = close
        self.kline_calls: list[dict] = []

    def _for(self, symbol: str):
        if isinstance(self._close, dict):
            return self._close[symbol]
        return self._close

    async def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/klines") and params.get("interval") == "1m":
            self.kline_calls.append(params)
            value = self._for(params["symbol"])
            if isinstance(value, BaseException):
                raise value
            if value is None:
                return _FakeResponse([])
            return _FakeResponse([[0, 0, 0, 0, value]])
        raise AssertionError(f"unexpected request: {url} {params}")


class _RecordingLog:
    """Stands in for the module logger so a test can pin that a failure was
    reported rather than swallowed (AGENTS.md: no silent failures).

    Captures the live exception alongside the event, so a test asserting
    "this failure was reported" can also assert it was the failure it
    staged — otherwise a stub whose signature drifted out of sync with
    production raises ``TypeError``, gets caught by the same
    ``except Exception``, and passes the assertion silently.
    """

    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.kwargs: list[dict] = []
        self.errors: list[BaseException | None] = []

    def _record(self, level):
        def _log(event, **kw):
            self.events.append((level, event))
            self.kwargs.append(kw)
            self.errors.append(sys.exc_info()[1])

        return _log

    def __getattr__(self, level):
        return self._record(level)

    def error_for(self, event: str) -> BaseException | None:
        """The exception in flight when ``event`` was logged."""
        return next(
            err for (_lvl, ev), err in zip(self.events, self.errors, strict=True) if ev == event
        )

    def kwargs_for(self, event: str) -> dict:
        return next(
            kw for (_lvl, ev), kw in zip(self.events, self.kwargs, strict=True) if ev == event
        )


_PAST_SLUG = "dogecoin-up-or-down-on-september-15-2026"


def _past_iso(hours: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _future_iso(hours: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat(timespec="seconds")


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
    the tick its settlement pass.

    Asserts the premise too (``no_markets_found`` was logged, i.e. discovery
    genuinely returned empty rather than blowing up) — without that, a
    discovery that RAISED would satisfy this test just as well, and the pin
    would really only say "settlement runs when entry does nothing".
    """
    await _record_open_past_due(reference_price=0.20)
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    async def _no_markets(_client):
        return {}

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _no_markets)
    client = _FakeBinanceClient(close=0.25)  # settled above reference -> Up

    await _scanner.scan_once(client)

    assert ("warning", "daily_scan.no_markets_found") in recorder.events
    row = await _row(_PAST_SLUG)
    assert row["state"] == "settled"
    assert row["outcome"] == "Up"
    assert row["settlement_price"] == pytest.approx(0.25)
    # Exact, not just >0: a fee-math regression that halved the payout would
    # still be positive (mirrors test_daily_ledger.py's settle assertions).
    expected = 22.0 * net_pnl_per_share(0.45, won=True, fee_rate=0.07)
    assert row["realized_pnl_usd"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_settlement_runs_before_the_entry_pass(test_db, monkeypatch: pytest.MonkeyPatch):
    """Ordering is the fix, not an accident: settling first is what makes a
    due window resolve on a tick whose entry half then finds nothing. Without
    this pin, reordering the two halves keeps every other test green."""
    calls: list[str] = []

    async def _settle(_client):
        calls.append("settle")

    async def _enter(_client):
        calls.append("enter")

    monkeypatch.setattr(_scanner, "_settle_due", _settle)
    monkeypatch.setattr(_scanner, "_enter_best", _enter)

    await _scanner.scan_once(_FakeBinanceClient(close=0.25))

    assert calls == ["settle", "enter"]


@pytest.mark.asyncio
async def test_settlement_failure_does_not_block_entry(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """A broken settlement pass must still leave the tick able to enter."""
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    view, sig = _view(), _signal()
    boom = RuntimeError("settlement feed down")

    async def _one_market(_client):
        return {"sol": {"slug": view.window_slug}}

    async def _scored(_client, _asset, _market):
        return view, sig

    async def _raise(_client):
        raise boom

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _one_market)
    monkeypatch.setattr(_scanner, "_scored_view", _scored)
    monkeypatch.setattr(_scanner, "_settle_due", _raise)

    await _scanner.scan_once(_FakeBinanceClient(close=0.25))

    assert view.window_slug in await _states()
    assert ("exception", "daily_scan.settle_failed") in recorder.events
    # The staged failure, not an incidental TypeError from a stub whose
    # signature drifted — `except Exception` cannot tell those apart.
    assert recorder.error_for("daily_scan.settle_failed") is boom


@pytest.mark.asyncio
async def test_entry_failure_does_not_block_settlement(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """The reverse: a discovery/entry blow-up must not strand a due window."""
    await _record_open_past_due(reference_price=0.20)
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)
    boom = RuntimeError("gamma down")

    async def _raise(_client):
        raise boom

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _raise)

    await _scanner.scan_once(_FakeBinanceClient(close=0.15))

    row = await _row(_PAST_SLUG)
    assert row["state"] == "settled"
    assert row["outcome"] == "Down"  # 0.15 < reference 0.20
    assert ("exception", "daily_scan.entry_pass_failed") in recorder.events
    assert recorder.error_for("daily_scan.entry_pass_failed") is boom


@pytest.mark.asyncio
async def test_a_window_that_has_not_resolved_yet_is_left_alone(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """The guard that keeps a still-trading window out of settlement.

    Untested, this is the expensive direction to get wrong: settling early
    books a fabricated PnL against a mid-window print into the paper record
    the whole strategy is judged on.
    """
    await _record_open_past_due(resolves_at=_future_iso(hours=3))

    async def _no_markets(_client):
        return {}

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _no_markets)
    client = _FakeBinanceClient(close=0.25)

    await _scanner.scan_once(client)

    assert (await _row(_PAST_SLUG))["state"] == "open"
    assert client.kline_calls == []  # not even priced, let alone settled


@pytest.mark.asyncio
async def test_unavailable_settlement_price_is_logged_not_swallowed(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """A due window Binance won't price stays open — but must say so.

    ``fetch_close_at`` turns every HTTP error into ``None``, so without its
    own log this path strands a row exactly like the bug this module's fix
    addresses, through a quieter door (AGENTS.md: no silent failures).
    """
    await _record_open_past_due()
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    async def _no_markets(_client):
        return {}

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _no_markets)

    await _scanner.scan_once(_FakeBinanceClient(close=None))  # Binance: no rows

    assert (await _row(_PAST_SLUG))["state"] == "open"
    assert ("warning", "daily_scan.settle_price_unavailable") in recorder.events
    assert recorder.kwargs_for("daily_scan.settle_price_unavailable")["window_slug"] == _PAST_SLUG


@pytest.mark.asyncio
async def test_one_unsettleable_row_does_not_strand_the_others(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """Per-row isolation, matching what the entry half already does per asset.

    A single poison row must not abort settlement for every other due window
    on every tick — that is the same "sat open for a day" symptom again.
    """
    await _record_open_past_due(window_slug="aaa-up-or-down-on-september-15-2026")
    await _record_open_past_due(window_slug="zzz-up-or-down-on-september-15-2026")
    recorder = _RecordingLog()
    monkeypatch.setattr(_scanner, "log", recorder)

    async def _no_markets(_client):
        return {}

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _no_markets)

    # Both rows carry binance_symbol DOGEUSDT, so poison the call itself:
    # the first row priced raises, the rest answer normally.
    calls = {"n": 0}
    real_fetch = _scanner._market.fetch_close_at

    async def _flaky(client, symbol, ts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IndexError("short kline row")
        return await real_fetch(client, symbol, ts)

    monkeypatch.setattr(_scanner._market, "fetch_close_at", _flaky)

    await _scanner.scan_once(_FakeBinanceClient(close=0.25))

    states = await _states()
    assert sorted(states.values()) == ["open", "settled"]  # one survived the other
    assert ("exception", "daily_scan.settle_row_failed") in recorder.events
    assert "window_slug" in recorder.kwargs_for("daily_scan.settle_row_failed")


@pytest.mark.asyncio
async def test_a_healthy_tick_both_settles_and_enters(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """The happy path: neither half crippled, both do their job."""
    await _record_open_past_due(reference_price=0.20)
    view, sig = _view(), _signal()

    async def _one_market(_client):
        return {"sol": {"slug": view.window_slug}}

    async def _scored(_client, _asset, _market):
        return view, sig

    monkeypatch.setattr(_scanner._market, "discover_daily_markets", _one_market)
    monkeypatch.setattr(_scanner, "_scored_view", _scored)

    await _scanner.scan_once(_FakeBinanceClient(close=0.25))

    states = await _states()
    assert states[_PAST_SLUG] == "settled"
    assert states[view.window_slug] == "open"
