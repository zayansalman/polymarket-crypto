"""Daily BTC engine: noon-ET decision, one entry per window, Binance 1-minute settlement, live slot."""
from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import paper
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategy_slot_entry as slot_entry
from polymarket_bot.daily_btc import engine, ledger
from polymarket_bot.daily_btc import market as dbm
from polymarket_bot.kronos_forecast import client as kronos
from polymarket_exec.execution.live import LiveOrderResult

WINDOW = dbm.window_for(date(2026, 9, 17))  # 2026-09-16 16:00 UTC -> 2026-09-17 16:00 UTC
S, E = WINDOW.reference_ts, WINDOW.settle_ts
SID = "tsinghua_kronos_btc_24h"
SLUG = "bitcoin-up-or-down-on-september-17-2026"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]


def _kline(open_s: int, close: float = 100.5, *, seconds: int = 3600) -> list:
    return [open_s * 1000, "100", "101", "99", str(close), "10", open_s * 1000 + seconds * 1000 - 1,
            "1000", 5, "5", "0", "0"]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Venue:
    """httpx handler: Gamma daily markets, CLOB books, Binance ticker, 1h and 1m klines."""

    def __init__(self) -> None:
        self.now = S + 10
        self.minute_closes: dict[int, float] = {S: 100.0, E: 101.0}
        self.noon_candle_ready = True

    def _market_row(self, slug: str) -> dict:
        # Named by the window's end day, as market.discover asks for it.
        m = re.search(r"-on-([a-z]+)-(\d+)-(\d{4})$", slug)
        day = date(int(m.group(3)), _MONTHS.index(m.group(1)) + 1, int(m.group(2)))
        w = dbm.window_for(day)
        return {"slug": slug, "question": f"Bitcoin Up or Down on {day:%B} {day.day}?",
                "eventStartTime": _iso(w.reference_ts), "endDate": _iso(w.settle_ts),
                "outcomes": json.dumps(["Up", "Down"]),
                "clobTokenIds": json.dumps([f"up-{w.reference_ts}", f"down-{w.reference_ts}"]),
                "feesEnabled": True, "feeSchedule": {"rate": 0.07, "exponent": 1}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, p = str(request.url), request.url.params
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/markets"):
            return httpx.Response(200, json=[self._market_row(p["slug"])])
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/events"):
            return httpx.Response(200, json=[])
        if url.startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            return httpx.Response(200, json={"bids": [{"price": "0.48", "size": "300"}],
                                             "asks": [{"price": "0.52", "size": "300"}]})
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/ticker/price"):
            return httpx.Response(200, json={"price": "100.7"})
        if p.get("interval") == "1m":
            ts = int(p["startTime"]) // 1000
            if ts in self.minute_closes and self.now >= ts + 60:
                return httpx.Response(200, json=[_kline(ts, self.minute_closes[ts], seconds=60)])
            return httpx.Response(200, json=[])
        if p.get("interval") == "1h":
            forming = self.now - self.now % 3600
            if not self.noon_candle_ready and forming == S:
                forming = S - 3600  # Binance has not opened the noon candle yet
            rows = [_kline(forming - k * 3600) for k in range(int(p["limit"]) - 1, -1, -1)]
            return httpx.Response(200, json=rows)
        return httpx.Response(404)


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    # A knob a test sets stays in the process-wide cache; give each test its own copy.
    monkeypatch.setattr(_knobs, "_cache", dict(_knobs._cache))
    await _knobs.refresh_cache()
    engine.reset_caches()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(engine, "notify", AsyncMock())
    return _db


def _forecast(monkeypatch: pytest.MonkeyPatch, p: float = 0.8, ok: bool = True) -> AsyncMock:
    result = (kronos.ForecastResult(ok=True, upside_prob=p, last_close=100.5,
                                    final_closes=(101.0,) * 30, seconds=8.8)
              if ok else kronos.ForecastResult(ok=False, error="Kronos worker timed out after 90 s"))
    fake = AsyncMock(return_value=result)
    monkeypatch.setattr(kronos, "run_forecast", fake)
    return fake


async def _tick(monkeypatch: pytest.MonkeyPatch, venue: _Venue, now: int, *, allow: bool = True):
    venue.now = now
    monkeypatch.setattr(paper, "_now", lambda: now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
        return await engine.tick(client, allow_entries=allow)


async def _positions() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM paper_positions ORDER BY position_id")
        return [dict(r) for r in await cur.fetchall()]


async def _insert_daily_row(mode: str) -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Up', 'open', 0.52, 2.6, 5, ?, '1d', ?, ?)",
            (SLUG, SID, S, mode),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def _record_past_window_decision() -> dbm.DayWindow:
    old = dbm.window_for(date(2026, 9, 16))
    old_market = dbm.DayMarket("bitcoin-up-or-down-on-september-16-2026", "q", old, "u", "d",
                               0.07, 1.0)
    await ledger.record_decision(strategy_id=SID, mode="paper", market=old_market, side="Up",
                                 reason="enter Up", signal={}, up_bid=None, up_ask=None,
                                 down_bid=None, down_ask=None, late=False, available=True)
    return old


@pytest.mark.asyncio
async def test_noon_decision_enters_once_and_settles_net_of_the_fee(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch, p=0.8)
    snap = await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, S + 20)
    assert fake.await_count == 1
    request = fake.await_args.args[0]
    assert request.seed == S // 3600 and request.candles[-1][0] == (S - 3600) * 1000
    assert snap.window_slug == SLUG
    (pos,) = await _positions()
    assert (pos["side"], pos["entry_price"], pos["shares"], pos["market_timeframe"],
            pos["window_start_ts"], pos["strategy_id"], pos["mode"]) == (
        "Up", 0.52, 5.0, "1d", S, SID, "paper")
    row = await ledger.get_decision(S, SID, mode="paper")
    assert row["action"] == "ENTERED" and row["position_id"] == pos["position_id"]

    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["exit_price"] == 1.0
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (1 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    settled = await ledger.get_decision(S, SID, mode="paper")
    assert (settled["reference_close"], settled["settle_close"], settled["outcome"]) == (
        100.0, 101.0, "Up")


@pytest.mark.asyncio
async def test_exact_tie_settles_at_half(test_db, monkeypatch):
    venue = _Venue()
    venue.minute_closes = {S: 100.0, E: 100.0}
    _forecast(monkeypatch, p=0.8)
    await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    closed = (await _positions())[0]
    assert closed["exit_price"] == 0.5
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (0.5 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    assert (await ledger.get_decision(S, SID, mode="paper"))["outcome"] == "tie"


@pytest.mark.asyncio
async def test_unavailable_forecast_is_recorded_and_notified_once(test_db, monkeypatch):
    venue, _ = _Venue(), _forecast(monkeypatch, ok=False)
    await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, S + 20)
    assert await _positions() == []
    row = await ledger.get_decision(S, SID, mode="paper")
    assert row["action"] == "UNAVAILABLE" and "timed out" in row["decision_reason"]
    assert engine.notify.await_count == 1


@pytest.mark.asyncio
async def test_late_start_is_missed_without_running_the_model(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch)
    await _tick(monkeypatch, venue, S + 301)
    fake.assert_not_awaited()
    assert (await ledger.get_decision(S, SID, mode="paper"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_forecast_that_ends_past_the_deadline_is_missed_not_entered(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch, p=0.8)
    finished = fake.return_value

    async def forecast_taking_60_s(request):
        monkeypatch.setattr(paper, "_now", lambda: S + 310)
        return finished

    fake.side_effect = forecast_taking_60_s
    await _tick(monkeypatch, venue, S + 250)
    fake.assert_awaited_once()
    assert await _positions() == []
    assert (await ledger.get_decision(S, SID, mode="paper"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_waits_until_binance_has_closed_the_hour_before_noon(test_db, monkeypatch):
    venue, fake = _Venue(), _forecast(monkeypatch)
    venue.noon_candle_ready = False
    await _tick(monkeypatch, venue, S + 2)
    fake.assert_not_awaited()
    assert await ledger.get_decision(S, SID, mode="paper") is None


@pytest.mark.asyncio
async def test_disabled_strategy_records_nothing(test_db, monkeypatch):
    await _knobs.set("daily_btc_tsinghua_kronos_btc_24h_enabled", False)
    venue, fake = _Venue(), _forecast(monkeypatch)
    await _tick(monkeypatch, venue, S + 10)
    fake.assert_not_awaited()
    assert await ledger.get_decision(S, SID, mode="paper") is None


@pytest.mark.asyncio
async def test_pending_decision_from_a_past_window_becomes_missed(test_db, monkeypatch):
    old = await _record_past_window_decision()
    venue, _ = _Venue(), _forecast(monkeypatch, p=0.5)  # no bet today
    await _tick(monkeypatch, venue, S + 10)
    assert (await ledger.get_decision(old.reference_ts, SID, mode="paper"))["action"] == "MISSED"


# Same rule and notice as the hourly engine (Claude, 2026-09-15, branch-review findings
# pending-row-never-finalized and hourly-reentry-after-untraced-post).
@pytest.mark.asyncio
async def test_attempt_left_submitting_in_a_past_window_is_uncertain_and_notified_once(
    test_db, monkeypatch
):
    old = await _record_past_window_decision()
    await ledger.set_action(old.reference_ts, SID, slot_entry.SUBMITTING, mode="paper")
    venue, _ = _Venue(), _forecast(monkeypatch, p=0.5)  # no bet today
    await _tick(monkeypatch, venue, S + 10)
    await _tick(monkeypatch, venue, S + 20)
    row = await ledger.get_decision(old.reference_ts, SID, mode="paper")
    assert row["action"] == slot_entry.UNFINISHED_ATTEMPT
    engine.notify.assert_awaited_once()
    assert engine.notify.await_args.args[0] == "entry_attempt_unfinished"


@pytest.mark.asyncio
async def test_paper_mode_leaves_live_rows_to_the_live_executor(test_db, monkeypatch):
    live_id, paper_id = await _insert_daily_row("live"), await _insert_daily_row("paper")
    venue, _ = _Venue(), _forecast(monkeypatch, p=0.5)  # no bet in the next window
    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    states = {r["position_id"]: r["state"] for r in await _positions()}
    assert states == {live_id: "open", paper_id: "closed"}


def _live_account() -> MagicMock:
    slot = MagicMock()
    slot.resync_flat = AsyncMock(return_value=False)
    slot.submit_entry = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SUBMITTED", order_id="0xE", price=0.52, size=5.0, notional_usd=2.6))
    slot.record_settlement = AsyncMock(return_value=LiveOrderResult(
        ok=True, status="SETTLED", price=1.0, size=5.0, notional_usd=2.23))
    account = MagicMock()
    account.slot_executor = MagicMock(return_value=slot)
    account.slot = slot
    return account


@pytest.mark.asyncio
async def test_live_entry_and_settlement_go_through_the_strategy_slot(test_db, monkeypatch):
    account = _live_account()
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(side_effect=AssertionError("live gates inside submit_entry"))
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", gate)
    venue, _ = _Venue(), _forecast(monkeypatch, p=0.8)
    await _tick(monkeypatch, venue, S + 10)
    account.slot_executor.assert_called_with(SID)
    account.slot.submit_entry.assert_awaited_once_with(
        token_id=f"up-{S}", side_price=0.52, notional_usd=pytest.approx(2.6), window_slug=SLUG)
    assert (await _positions())[0]["mode"] == "live"
    await _tick(monkeypatch, venue, E + engine.SETTLE_GRACE_S + 10)
    account.slot.record_settlement.assert_awaited_once_with(True, SLUG, payout=1.0)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["realized_pnl_usd"] == pytest.approx(2.23)
