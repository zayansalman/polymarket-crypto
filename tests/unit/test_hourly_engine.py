"""Hourly engine: one decision per hour, per-strategy slots, deadline, gate, Binance settlement."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

import config as _config
import db as _db
from polymarket_bot import paper
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.hourly import engine, ledger
from polymarket_bot.hourly import market as hm
from polymarket_exec.execution.live import LiveOrderResult

H = 1_789_326_000  # 3PM ET hour
SLUG = hm.slug_for(H)
NEXT_SLUG = hm.slug_for(H + 3600)


def _kline(open_s: int, o: float, c: float, hi: float, lo: float, tb_share: float) -> list:
    return [open_s * 1000, str(o), str(hi), str(lo), str(c), "10", open_s * 1000 + 3_599_999,
            "1000", 5, str(10 * tb_share), "0", "0"]


def _history(end_hour: int, last_tb: float, *, last_close: float = 110.0) -> list[list]:
    """170 rows: 169 closed hours ending at end_hour-1 (last one pushed up), plus forming end_hour."""
    rows = []
    for k in range(169, 1, -1):
        share = 0.55 if k % 2 else 0.45
        rows.append(_kline(end_hour - k * 3600, 100.0, 100.5, 101.0, 99.0, share))
    rows.append(_kline(end_hour - 3600, 100.0, last_close, 110.0, 99.0, last_tb))
    rows.append(_kline(end_hour, last_close, last_close, last_close, last_close, 0.5))
    return rows


class _Venue:
    """httpx handler for Gamma, CLOB books, Binance spot/perp klines and ticker."""

    def __init__(self, *, hour_close: float = 109.0) -> None:
        self.hour_close = hour_close

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, p = str(request.url), request.url.params
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/markets"):
            slug = p["slug"]
            start = H if slug == SLUG else H + 3600
            iso = hm.datetime.fromtimestamp(start, hm.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(200, json=[{
                "slug": slug, "question": slug, "eventStartTime": iso,
                "outcomes": json.dumps(["Up", "Down"]),
                "clobTokenIds": json.dumps([f"up-{start}", f"down-{start}"]),
            }])
        if url.startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            return httpx.Response(200, json={
                "bids": [{"price": "0.48", "size": "300"}],
                "asks": [{"price": "0.52", "size": "300"}],
            })
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/ticker/price"):
            return httpx.Response(200, json={"price": "110.0"})
        if "startTime" in p:  # hour candle for open / settlement
            start = int(p["startTime"]) // 1000
            rows = [_kline(start, 110.0, self.hour_close, 111.0, 108.0, 0.5)]
            if start + 3600 <= self.end_hour:  # Binance returns the next candle once it began
                c = self.hour_close
                rows.append(_kline(start + 3600, c, c, c, c, 0.5))
            return httpx.Response(200, json=rows)
        if url.startswith(f"{_config.BINANCE_API_BASE}/api/v3/klines"):
            return httpx.Response(200, json=_history(self.end_hour, 0.9))
        if url.startswith(f"{hm.BINANCE_FAPI}/fapi/v1/klines"):
            return httpx.Response(200, json=_history(self.end_hour, 0.5))
        return httpx.Response(404)

    end_hour = H


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    await _knobs.refresh_cache()
    engine.reset_caches()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    return _db


async def _tick(monkeypatch, now: int, venue: _Venue, *, allow: bool = True):
    monkeypatch.setattr(paper, "_now", lambda: now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(venue)) as client:
        return await engine.tick(client, allow_entries=allow)


async def _positions() -> list[dict]:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM paper_positions ORDER BY position_id")
        return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_signal_enters_once_on_its_own_slot_and_settles_net_of_fee(test_db, monkeypatch):
    venue = _Venue(hour_close=109.0)  # hour H closes below its 110 open -> Down wins
    snap = await _tick(monkeypatch, H + 30, venue)
    assert snap.window_slug == SLUG and snap.reference_price == 110.0
    await _tick(monkeypatch, H + 35, venue)  # same hour: no second entry
    pos = await _positions()
    assert len(pos) == 1
    p = pos[0]
    assert (p["side"], p["strategy_id"], p["market_timeframe"], p["window_start_ts"]) == (
        "Down", "hourly_mean_reversion", "1h", H)
    assert p["entry_price"] == 0.52 and p["shares"] == 5.0 and p["mode"] == "paper"
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "ENTERED" and row["position_id"] == p["position_id"]

    venue.end_hour = H + 3600
    await _tick(monkeypatch, H + 3600 + 20, venue)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["exit_price"] == 1.0
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (1 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    settled = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert settled["outcome_side"] == "Down" and settled["hour_close"] == 109.0


@pytest.mark.asyncio
async def test_other_strategy_slot_does_not_block(test_db, monkeypatch):
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Up', 'open', 0.5, 2.5, 5, 'kronos_btc_finetune', '1h', ?, 'paper')",
            (SLUG, H),
        )
        await conn.commit()
    await _tick(monkeypatch, H + 30, _Venue())
    sids = sorted(p["strategy_id"] for p in await _positions() if p["state"] == "open")
    assert sids == ["hourly_mean_reversion", "kronos_btc_finetune"]


@pytest.mark.asyncio
async def test_hour_candle_not_closed_on_binance_keeps_position_and_record_open(test_db, monkeypatch):
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue)
    await _tick(monkeypatch, H + 3600 + 5, venue)  # local clock past H+1; Binance has no H+1 candle yet
    assert (await _positions())[0]["state"] == "open"
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["settled_at"] is None


@pytest.mark.asyncio
async def test_after_deadline_signal_is_missed_and_no_entry(test_db, monkeypatch):
    await _tick(monkeypatch, H + 121, _Venue())
    assert await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_gate_block_is_recorded_once_and_journaled(test_db, monkeypatch):
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(return_value="daily loss halt: test")
    monkeypatch.setattr(paper, "_risk_gate", gate)
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert await _positions() == []
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "BLOCKED:daily loss halt: test"
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM live_orders WHERE status='BLOCKED'")
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_disabled_strategy_records_nothing_and_kill_holds_entries(test_db, monkeypatch):
    await _knobs.set("hourly_mean_reversion_enabled", False)
    await _tick(monkeypatch, H + 30, _Venue())
    assert await ledger.get_decision(SLUG, "hourly_mean_reversion") is None
    await _knobs.set("hourly_mean_reversion_enabled", True)
    await _tick(monkeypatch, H + 31, _Venue(), allow=False)
    assert await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "PENDING"


@pytest.mark.asyncio
async def test_tick_straddling_the_hour_uses_one_clock_read(test_db, monkeypatch):
    clock = iter([H + 3599, H + 3601, H + 3602, H + 3603])
    monkeypatch.setattr(paper, "_now", lambda: next(clock))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_Venue())) as client:
        snap = await engine.tick(client)
    assert snap.window_slug == SLUG
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT window_slug, window_start_ts FROM hourly_strategy_context")
        rows = [dict(r) for r in await cur.fetchall()]
    assert all(hm.slug_for(r["window_start_ts"]) == r["window_slug"] for r in rows)


@pytest.mark.asyncio
async def test_waits_until_previous_hour_is_closed(test_db, monkeypatch):
    venue = _Venue()
    venue.end_hour = H - 3600  # Binance hasn't produced a closed H-1 candle yet
    await _tick(monkeypatch, H + 2, venue)
    assert await ledger.get_decision(SLUG, "hourly_mean_reversion") is None


# --- Live mode: the same decision, routed through the strategy's live slot ---------


def _live_account(entry: LiveOrderResult | None = None) -> MagicMock:
    """Account executor fake: slot_executor(strategy_id) returns one mock slot per strategy."""
    slots: dict[str, MagicMock] = {}

    def slot_executor(strategy_id: str) -> MagicMock:
        if strategy_id not in slots:
            slot = MagicMock()
            slot.resync_flat = AsyncMock(return_value=False)
            slot.submit_entry = AsyncMock(return_value=entry or LiveOrderResult(
                ok=True, status="SUBMITTED", order_id="0xE", price=0.52, size=5.0,
                notional_usd=2.6))
            slot.record_settlement = AsyncMock(return_value=LiveOrderResult(
                ok=True, status="SETTLED", price=1.0, size=5.0, notional_usd=2.2))
            slots[strategy_id] = slot
        return slots[strategy_id]

    account = MagicMock()
    account.slot_executor = MagicMock(side_effect=slot_executor)
    account.slots = slots
    return account


def _live_gate() -> MagicMock:
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(side_effect=AssertionError("live entries gate inside submit_entry"))
    gate.record_realized_pnl = AsyncMock()
    gate.record_buy_notional = AsyncMock()
    return gate


async def _go_live(monkeypatch, entry: LiveOrderResult | None = None) -> MagicMock:
    account = _live_account(entry)
    monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", _live_gate())
    return account


async def _insert_hourly(start: int, mode: str, side: str = "Down") -> None:
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, ?, 'open', 0.5, 2.5, 5, 'hourly_mean_reversion', '1h', ?, ?)",
            (hm.slug_for(start), side, start, mode),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_own_open_row_blocks_a_second_entry_in_paper_and_live(test_db, monkeypatch):
    await _insert_hourly(H, "paper")
    await _tick(monkeypatch, H + 30, _Venue())
    assert len(await _positions()) == 1
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "PENDING"
    await _insert_hourly(H, "live")
    account = await _go_live(monkeypatch)
    await _tick(monkeypatch, H + 40, _Venue())
    slot = account.slots.get("hourly_mean_reversion")
    assert slot is None or (slot.submit_entry.await_count == 0 and slot.resync_flat.await_count == 0)
    assert len(await _positions()) == 2


@pytest.mark.asyncio
async def test_live_entry_uses_the_strategy_slot_and_settles_through_it(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue)
    slot = account.slots["hourly_mean_reversion"]
    slot.resync_flat.assert_awaited_once()
    slot.submit_entry.assert_awaited_once_with(
        token_id=f"down-{H}", side_price=0.52, notional_usd=pytest.approx(2.6), window_slug=SLUG)
    p = (await _positions())[0]
    assert (p["mode"], p["strategy_id"], p["entry_price"], p["shares"]) == (
        "live", "hourly_mean_reversion", 0.52, 5.0)
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["mode"] == "live"

    venue.end_hour = H + 3600
    await _tick(monkeypatch, H + 3600 + 20, venue)
    slot.record_settlement.assert_awaited_once_with(True, SLUG)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["realized_pnl_usd"] == pytest.approx(2.2)


@pytest.mark.asyncio
async def test_live_blocked_entry_is_recorded_once_and_leaves_no_row(test_db, monkeypatch):
    account = await _go_live(monkeypatch, LiveOrderResult(
        ok=False, status="BLOCKED", reason="daily loss halt: test"))
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert await _positions() == []
    assert account.slots["hourly_mean_reversion"].submit_entry.await_count == 1
    row = await ledger.get_decision(SLUG, "hourly_mean_reversion")
    assert row["action"] == "BLOCKED:daily loss halt: test"


# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried: a failed
# post may still have reached the book, so it ends the hour instead of retrying.
@pytest.mark.asyncio
async def test_live_order_error_ends_the_hour_as_uncertain_without_a_second_post(
    test_db, monkeypatch
):
    account = await _go_live(monkeypatch, LiveOrderResult(ok=False, status="ERROR", reason="venue"))
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    submit = account.slots["hourly_mean_reversion"].submit_entry
    assert submit.await_count == 1 and await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "UNCERTAIN:ERROR venue"
    await _tick(monkeypatch, H + 121, _Venue())
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "UNCERTAIN:ERROR venue"
    assert submit.await_count == 1


def _clob_client_whose_post_times_out() -> MagicMock:
    """CLOB client fake: books answer, every order post raises what py_clob_client_v2 raises
    when the HTTP request fails (for example a read timeout after the body was sent)."""
    from types import SimpleNamespace

    from py_clob_client_v2.exceptions import PolyApiException

    client = MagicMock()
    client.get_order_book.return_value = SimpleNamespace(
        asks=[SimpleNamespace(price="0.99", size="10"), SimpleNamespace(price="0.52", size="300")],
        bids=[SimpleNamespace(price="0.48", size="300")], tick_size="0.01", min_order_size="5")
    client.create_and_post_order.side_effect = PolyApiException(error_msg="Request exception!")
    return client


@pytest.mark.asyncio
async def test_live_post_exception_is_posted_once_per_hour_and_notified(
    test_db, monkeypatch, tmp_path
):
    from polymarket_exec.execution.live import LiveExecutor

    client = _clob_client_whose_post_times_out()
    executor = LiveExecutor(
        private_key="0x" + "1" * 64, funder="0xFUNDER", signature_type=2, max_trade_usd=3.0,
        daily_loss_halt_usd=10.0, bankroll_cap_usd=30.0, max_entry_slippage=0.5,
        exit_fill_timeout_seconds=1.0, kill_switch_path=tmp_path / "KILL", client=client,
    )
    monkeypatch.setattr(paper, "_live_executor", executor)
    monkeypatch.setattr(paper, "_risk_gate", executor.gate)
    for offset in (30, 35, 40, 45, 121):
        await _tick(monkeypatch, H + offset, _Venue())
    assert client.create_and_post_order.call_count == 1
    assert await _positions() == []
    action = (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"]
    assert action.startswith("UNCERTAIN:ERROR ") and "Request exception!" in action
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT status FROM live_orders WHERE intent = 'ENTRY'")
        assert [r["status"] for r in await cur.fetchall()] == ["ERROR"]
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM notification_feed WHERE event_type = 'live_entry_uncertain'")
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_submitting_is_recorded_before_the_live_post(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    seen: list[str] = []
    slot = account.slot_executor("hourly_mean_reversion")
    entered = slot.submit_entry.return_value

    async def submit_entry(**_kwargs):
        seen.append((await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"])
        return entered

    slot.submit_entry.side_effect = submit_entry
    await _tick(monkeypatch, H + 30, _Venue())
    assert seen == ["SUBMITTING"]
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "ENTERED"


@pytest.mark.asyncio
async def test_live_attempt_that_raised_is_not_retried_and_ends_uncertain(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    slot = account.slot_executor("hourly_mean_reversion")
    slot.submit_entry.side_effect = RuntimeError("journal write failed")
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == "SUBMITTING"
    await _tick(monkeypatch, H + 35, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert slot.submit_entry.await_count == 1
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == (
        "UNCERTAIN:entry attempt did not finish")


@pytest.mark.asyncio
async def test_paper_attempt_that_raised_is_not_retried_and_ends_uncertain(test_db, monkeypatch):
    real_insert = engine._insert_row
    calls = {"n": 0}

    async def insert_row(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(engine, "_insert_row", insert_row)
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 35, _Venue())
    assert calls["n"] == 1 and await _positions() == []
    assert (await ledger.get_decision(SLUG, "hourly_mean_reversion"))["action"] == (
        "UNCERTAIN:entry attempt did not finish")


@pytest.mark.asyncio
async def test_paper_mode_leaves_live_rows_to_the_live_executor(test_db, monkeypatch):
    await _insert_hourly(H - 3600, "live")
    await _tick(monkeypatch, H + 30, _Venue())
    rows = await _positions()
    assert [r["state"] for r in rows if r["mode"] == "live"] == ["open"]
    assert len([r for r in rows if r["mode"] == "paper"]) == 1  # slots are per mode


@pytest.mark.asyncio
async def test_live_mode_settles_a_paper_row_paper_style(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    await _insert_hourly(H - 3600, "paper")
    await _tick(monkeypatch, H + 30, _Venue(hour_close=109.0))  # Down won H-1
    row = [r for r in await _positions() if r["mode"] == "paper"][0]
    assert row["state"] == "closed"
    assert row["realized_pnl_usd"] == pytest.approx(5 * 0.5 - 5 * 0.07 * 0.5 * 0.5)
    account.slots["hourly_mean_reversion"].record_settlement.assert_not_awaited()
