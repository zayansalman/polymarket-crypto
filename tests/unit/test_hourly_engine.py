"""Hourly engine: one decision per hour, per-strategy slots, deadline, gate, Binance settlement."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
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
from polymarket_exec.execution.gate import build_gate_from_config
from polymarket_exec.execution.live import LiveExecutor, LiveOrderResult

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
        "Down", "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", "1h", H)
    assert p["entry_price"] == 0.52 and p["shares"] == 5.0 and p["mode"] == "paper"
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert row["action"] == "ENTERED" and row["position_id"] == p["position_id"]

    venue.end_hour = H + 3600
    await _tick(monkeypatch, H + 3600 + 20, venue)
    closed = (await _positions())[0]
    assert closed["state"] == "closed" and closed["exit_price"] == 1.0
    assert closed["realized_pnl_usd"] == pytest.approx(5 * (1 - 0.52) - 5 * 0.07 * 0.52 * 0.48)
    settled = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert settled["outcome_side"] == "Down" and settled["hour_close"] == 109.0


@pytest.mark.asyncio
async def test_other_strategy_slot_does_not_block(test_db, monkeypatch):
    async with _db.connect() as conn:
        await conn.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state, entry_price,"
            " notional_usd, shares, strategy_id, market_timeframe, window_start_ts, mode)"
            " VALUES ('x', ?, 'Up', 'open', 0.5, 2.5, 5, 'kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price', '1h', ?, 'paper')",
            (SLUG, H),
        )
        await conn.commit()
    await _tick(monkeypatch, H + 30, _Venue())
    sids = sorted(p["strategy_id"] for p in await _positions() if p["state"] == "open")
    assert sids == ["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", "kronos_lc2004_btcusdt_1h_finetune_up_chance_vs_polymarket_price"]


@pytest.mark.asyncio
async def test_hour_candle_not_closed_on_binance_keeps_position_and_record_open(test_db, monkeypatch):
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue)
    await _tick(monkeypatch, H + 3600 + 5, venue)  # local clock past H+1; Binance has no H+1 candle yet
    assert (await _positions())[0]["state"] == "open"
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["settled_at"] is None


@pytest.mark.asyncio
async def test_after_deadline_signal_is_missed_and_no_entry(test_db, monkeypatch):
    await _tick(monkeypatch, H + 121, _Venue())
    assert await _positions() == []
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["action"] == "MISSED"


@pytest.mark.asyncio
async def test_gate_block_is_recorded_once_and_journaled(test_db, monkeypatch):
    gate = MagicMock()
    gate.trade_shares = 5.0
    gate.block_reason = MagicMock(return_value="daily loss halt: test")
    monkeypatch.setattr(paper, "_risk_gate", gate)
    await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert await _positions() == []
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert row["action"] == "BLOCKED:daily loss halt: test"
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM live_orders WHERE status='BLOCKED'")
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_disabled_strategy_records_nothing_and_kill_holds_entries(test_db, monkeypatch):
    await _knobs.set("hourly_btcusdt_1h_spot_taker_push_reversal_enabled", False)
    await _tick(monkeypatch, H + 30, _Venue())
    assert await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper") is None
    await _knobs.set("hourly_btcusdt_1h_spot_taker_push_reversal_enabled", True)
    await _tick(monkeypatch, H + 31, _Venue(), allow=False)
    assert await _positions() == []
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["action"] == "PENDING"


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
    assert await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper") is None


# --- Live mode: the same decision, routed through the strategy's live slot ---------


def _live_account(entry: LiveOrderResult | None = None) -> MagicMock:
    """Account executor fake: slot_executor(strategy_id) returns one mock slot per strategy."""
    slots: dict[str, MagicMock] = {}

    def slot_executor(strategy_id: str) -> MagicMock:
        if strategy_id not in slots:
            slot = MagicMock()
            slot.tracks_position = False  # a test sets True when the slot holds a filled entry
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
            " VALUES ('x', ?, ?, 'open', 0.5, 2.5, 5, 'btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal', '1h', ?, ?)",
            (hm.slug_for(start), side, start, mode),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_own_open_row_blocks_a_second_entry_in_paper_and_live(test_db, monkeypatch):
    # The same-hour paper row is this hour's entry: the record links it (Claude, 2026-09-15,
    # branch-review finding crash-after-entry-marks-missed) and nothing enters again.
    await _insert_hourly(H, "paper")
    await _tick(monkeypatch, H + 30, _Venue())
    assert len(await _positions()) == 1
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert (row["action"], row["position_id"]) == ("ENTERED", 1)
    await _insert_hourly(H, "live")
    account = await _go_live(monkeypatch)
    await _tick(monkeypatch, H + 40, _Venue())
    slot = account.slots.get("btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal")
    assert slot is None or (slot.submit_entry.await_count == 0 and slot.resync_flat.await_count == 0)
    assert len(await _positions()) == 2


@pytest.mark.asyncio
async def test_previous_hour_open_row_holds_the_slot_and_the_hour_stays_pending(
    test_db, monkeypatch
):
    monkeypatch.setattr(engine, "settle_due", AsyncMock())  # previous hour not settled yet
    await _insert_hourly(H - 3600, "paper")
    await _tick(monkeypatch, H + 30, _Venue())
    assert len(await _positions()) == 1
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert (row["action"], row["position_id"]) == ("PENDING", None)


def _fail_the_first_entered_write(monkeypatch, exc: BaseException | None = None) -> None:
    real = ledger.set_action
    state = {"failed": False}

    async def set_action(window_start_ts, strategy_id, action, position_id=None, **kwargs):
        if action == "ENTERED" and not state["failed"]:
            state["failed"] = True
            raise exc or RuntimeError("database is locked")
        return await real(window_start_ts, strategy_id, action, position_id, **kwargs)

    monkeypatch.setattr(ledger, "set_action", set_action)


@pytest.mark.asyncio
@pytest.mark.parametrize("next_tick", [H + 40, H + 130])  # before and after the deadline
async def test_paper_entry_whose_entered_write_failed_is_linked_not_missed(
    test_db, monkeypatch, next_tick
):
    _fail_the_first_entered_write(monkeypatch)
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, next_tick, _Venue())
    await _tick(monkeypatch, H + 150, _Venue())
    positions = await _positions()
    assert len(positions) == 1
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert (row["action"], row["position_id"]) == ("ENTERED", positions[0]["position_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("next_tick", [H + 40, H + 130])
async def test_live_entry_whose_entered_write_failed_is_linked_not_missed(
    test_db, monkeypatch, next_tick
):
    account = await _go_live(monkeypatch)
    _fail_the_first_entered_write(monkeypatch)
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    slot = account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"]
    slot.tracks_position = True  # the slot holds the filled entry
    await _tick(monkeypatch, next_tick, _Venue())
    positions = await _positions()
    assert len(positions) == 1 and positions[0]["mode"] == "live"
    assert slot.submit_entry.await_count == 1
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live")
    assert (row["action"], row["position_id"]) == ("ENTERED", positions[0]["position_id"])


async def _close_hourly_rows_as_stopped() -> None:
    async with _db.connect() as conn:
        await conn.execute("UPDATE paper_positions SET state = 'closed', exit_reason = "
                           "'STOP_REQUEST' WHERE state = 'open'")
        await conn.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_entry_sold_at_stop_before_its_record_was_written_ends_the_same_way_in_both_modes(
    test_db, monkeypatch, mode
):
    """Claude session polymarket-crypto-95 review, 2026-09-16: after Stop sells the position
    and the bot restarts within the hour, paper and live record the same action."""
    if mode == "live":
        account = await _go_live(monkeypatch)
    _fail_the_first_entered_write(monkeypatch)
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    await _close_hourly_rows_as_stopped()  # Stop sold the current-hour position
    if mode == "live":
        account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].tracks_position = False  # flat after the sale
    await _tick(monkeypatch, H + 60, _Venue())
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode)
    assert row["action"] == engine.UNFINISHED_ATTEMPT
    assert len(await _positions()) == 1  # nothing entered again


@pytest.mark.asyncio
async def test_live_row_the_slot_does_not_track_stays_an_unfinished_attempt(
    test_db, monkeypatch
):
    account = await _go_live(monkeypatch)
    _fail_the_first_entered_write(monkeypatch)
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].tracks_position = False  # outcome unknown
    await _tick(monkeypatch, H + 40, _Venue())
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live")
    assert row["action"] == engine.UNFINISHED_ATTEMPT
    assert account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].submit_entry.await_count == 1


@pytest.mark.asyncio
async def test_live_entry_uses_the_strategy_slot_and_settles_through_it(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue)
    slot = account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"]
    slot.resync_flat.assert_awaited_once()
    slot.submit_entry.assert_awaited_once_with(
        token_id=f"down-{H}", side_price=0.52, notional_usd=pytest.approx(2.6), window_slug=SLUG)
    p = (await _positions())[0]
    assert (p["mode"], p["strategy_id"], p["entry_price"], p["shares"]) == (
        "live", "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", 0.52, 5.0)
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["mode"] == "live"

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
    assert account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].submit_entry.await_count == 1
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live")
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
    submit = account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].submit_entry
    assert submit.await_count == 1 and await _positions() == []
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == "UNCERTAIN:ERROR venue"
    await _tick(monkeypatch, H + 121, _Venue())
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == "UNCERTAIN:ERROR venue"
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
    action = (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"]
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
    slot = account.slot_executor("btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal")
    entered = slot.submit_entry.return_value

    async def submit_entry(**_kwargs):
        seen.append((await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"])
        return entered

    slot.submit_entry.side_effect = submit_entry
    await _tick(monkeypatch, H + 30, _Venue())
    assert seen == ["SUBMITTING"]
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == "ENTERED"


@pytest.mark.asyncio
async def test_live_attempt_that_raised_is_not_retried_and_ends_uncertain(test_db, monkeypatch):
    account = await _go_live(monkeypatch)
    slot = account.slot_executor("btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal")
    slot.submit_entry.side_effect = RuntimeError("journal write failed")
    with pytest.raises(RuntimeError):
        await _tick(monkeypatch, H + 30, _Venue())
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == "SUBMITTING"
    await _tick(monkeypatch, H + 35, _Venue())
    await _tick(monkeypatch, H + 40, _Venue())
    assert slot.submit_entry.await_count == 1
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == (
        "UNCERTAIN:entry attempt did not finish")


# Claude, 2026-09-15, branch-review finding hourly-reentry-after-untraced-post: a live post
# can reach Polymarket right before its journal write fails (the process dies, or sqlite
# raises). Boot reconciliation then finds no trace and closes the row. The hour's SUBMITTING
# record must still stop a second post, and the operator is told to check the account.


class _ProcessDied(BaseException):
    """Stands in for the process dying mid-tick: no ``except Exception`` catches it."""


def _clob_client_whose_post_fills() -> MagicMock:
    """CLOB client fake for a full executor boot: books answer, every post matches 5 shares."""
    from types import SimpleNamespace

    client = MagicMock()
    client.get_order_book.return_value = SimpleNamespace(
        asks=[SimpleNamespace(price="0.99", size="10"), SimpleNamespace(price="0.52", size="300")],
        bids=[SimpleNamespace(price="0.48", size="300")], tick_size="0.01", min_order_size="5")
    client.create_and_post_order.return_value = {
        "success": True, "orderID": "0xE1", "status": "matched",
        "makingAmount": "2.6", "takingAmount": "5"}
    client.cancel_all.return_value = {"canceled": [], "not_canceled": {}}
    return client


async def _boot_live_executor(monkeypatch, client: MagicMock, tmp_path: Path):
    """Build and start a real LiveExecutor (start runs boot reconciliation), then trade live."""
    from polymarket_exec.execution.live import LiveExecutor

    executor = LiveExecutor(
        private_key="0x" + "1" * 64, funder="0xFUNDER", signature_type=2, max_trade_usd=3.0,
        daily_loss_halt_usd=10.0, bankroll_cap_usd=30.0, max_entry_slippage=0.5,
        exit_fill_timeout_seconds=1.0, kill_switch_path=tmp_path / "KILL", client=client,
    )
    await executor.start()
    monkeypatch.setattr(paper, "_live_executor", executor)
    monkeypatch.setattr(paper, "_risk_gate", executor.gate)
    return executor


def _fail_the_submitted_entry_journal_write(monkeypatch, exc: BaseException) -> None:
    """Raise ``exc`` from the first SUBMITTED entry journal write, which runs after the post."""
    from polymarket_exec.execution import live as live_module

    real = live_module.journal_live_order
    failed: list[dict] = []

    async def journal_live_order(**fields):
        if not failed and (fields.get("intent"), fields.get("status")) == ("ENTRY", "SUBMITTED"):
            failed.append(fields)
            raise exc
        return await real(**fields)

    monkeypatch.setattr(live_module, "journal_live_order", journal_live_order)


async def _unfinished_attempt_notifications() -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM notification_feed "
            "WHERE event_type = 'entry_attempt_unfinished'")
        return int((await cur.fetchone())["n"])


@pytest.mark.asyncio
async def test_untraced_live_post_then_restart_is_not_posted_again_and_is_notified(
    test_db, monkeypatch, tmp_path
):
    client = _clob_client_whose_post_fills()
    await _boot_live_executor(monkeypatch, client, tmp_path)
    _fail_the_submitted_entry_journal_write(monkeypatch, _ProcessDied())
    with pytest.raises(_ProcessDied):
        await _tick(monkeypatch, H + 30, _Venue())
    assert client.create_and_post_order.call_count == 1

    engine.reset_caches()  # restart: a new process boots a new executor, which reconciles
    await _boot_live_executor(monkeypatch, client, tmp_path)
    closed = [(r["state"], r["exit_reason"]) for r in await _positions()]
    assert closed == [("closed", "RECONCILED_NO_LIVE_TRACE")]
    for offset in (60, 90):  # both inside the 120 s entry deadline
        await _tick(monkeypatch, H + offset, _Venue())
    assert client.create_and_post_order.call_count == 1
    assert [(r["state"], r["exit_reason"]) for r in await _positions()] == closed
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == (
        engine.UNFINISHED_ATTEMPT)
    assert await _unfinished_attempt_notifications() == 1


@pytest.mark.asyncio
async def test_untraced_live_post_in_a_running_loop_is_not_posted_again_after_stop_and_start(
    test_db, monkeypatch, tmp_path
):
    import sqlite3

    client = _clob_client_whose_post_fills()
    executor = await _boot_live_executor(monkeypatch, client, tmp_path)
    _fail_the_submitted_entry_journal_write(
        monkeypatch, sqlite3.OperationalError("database is locked"))
    with pytest.raises(sqlite3.OperationalError):  # the loop logs the failed tick, keeps going
        await _tick(monkeypatch, H + 30, _Venue())
    await _tick(monkeypatch, H + 35, _Venue())
    assert await _unfinished_attempt_notifications() == 1

    # Stop the way the loop's shutdown does: cancel, then flatten. Nothing tracks the
    # untraced fill, so its row stays open until the next boot reconciliation closes it.
    monkeypatch.setattr(paper, "_make_settlement_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_Venue())))
    await executor.cancel_open_all(reason="LOOP_STOP")
    await paper.force_close_open_positions("STOP_REQUEST")
    await _boot_live_executor(monkeypatch, client, tmp_path)  # Start
    await _tick(monkeypatch, H + 50, _Venue())
    assert client.create_and_post_order.call_count == 1
    assert [(r["state"], r["exit_reason"]) for r in await _positions()] == [
        ("closed", "RECONCILED_NO_LIVE_TRACE")]
    assert await _unfinished_attempt_notifications() == 1


@pytest.mark.asyncio
async def test_deleted_row_whose_uncertain_record_was_never_written_is_not_posted_again(
    test_db, monkeypatch
):
    account = await _go_live(monkeypatch, LiveOrderResult(ok=False, status="ERROR", reason="timeout"))
    real_set_action = ledger.set_action

    async def set_action(window_start_ts, strategy_id, action, *args, **kwargs):
        if action.startswith(f"{engine.UNCERTAIN_PREFIX}ERROR"):
            raise _ProcessDied()
        return await real_set_action(window_start_ts, strategy_id, action, *args, **kwargs)

    monkeypatch.setattr(ledger, "set_action", set_action)
    with pytest.raises(_ProcessDied):
        await _tick(monkeypatch, H + 30, _Venue())
    assert await _positions() == []  # the failed submit deleted its row before the crash
    await _tick(monkeypatch, H + 40, _Venue())
    await _tick(monkeypatch, H + 50, _Venue())
    assert account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].submit_entry.await_count == 1
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live"))["action"] == (
        engine.UNFINISHED_ATTEMPT)
    assert await _unfinished_attempt_notifications() == 1


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
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["action"] == (
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
    account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].record_settlement.assert_not_awaited()


@pytest.mark.asyncio
async def test_filled_live_entry_whose_record_was_lost_is_linked_after_restart(
    test_db, monkeypatch, tmp_path
):
    """Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed: the order
    filled and was journaled, then the process died before ENTERED was written."""
    client = _clob_client_whose_post_fills()
    client.get_order.return_value = {"size_matched": "5", "price": "0.52"}
    await _boot_live_executor(monkeypatch, client, tmp_path)
    _fail_the_first_entered_write(monkeypatch, _ProcessDied())
    with pytest.raises(_ProcessDied):
        await _tick(monkeypatch, H + 30, _Venue())
    assert client.create_and_post_order.call_count == 1

    engine.reset_caches()  # restart: boot reconciliation adopts the filled entry
    executor = await _boot_live_executor(monkeypatch, client, tmp_path)
    assert executor.slot_executor("btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal").tracks_position is True
    await _tick(monkeypatch, H + 150, _Venue())  # past the 120 s entry deadline
    positions = await _positions()
    assert [(p["state"], p["mode"]) for p in positions] == [("open", "live")]
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="live")
    assert (row["action"], row["position_id"]) == ("ENTERED", positions[0]["position_id"])
    assert client.create_and_post_order.call_count == 1


# --- US fall-back day: two UTC hours share one ET-labelled slug -------------------------
# Claude, 2026-09-15, branch-review finding dst-fallback-slug-collision.
# On 2026-11-01, 05:00Z is 1am EDT and 06:00Z is 1am EST, so slug_for gives both hours
# bitcoin-up-or-down-november-1-2026-1am-et. Gamma slugs are unique, so at most one of the two
# markets can carry that slug. Polymarket listed neither 1am ET hour on 2025-11-02, so the real
# slug of a second 1am market is unknown; this stand-in only has to differ from the first.
FIRST_1AM_ET = 1_793_509_200
SECOND_1AM_ET = 1_793_512_800
SECOND_1AM_ET_SLUG = "stand-in-slug-for-the-06-00z-hour-on-2026-11-01"


def _gamma_market(slug: str, start: int) -> dict:
    iso = hm.datetime.fromtimestamp(start, hm.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = hm.datetime.fromtimestamp(start + 3600, hm.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"slug": slug, "question": slug, "eventStartTime": iso, "endDate": end,
            "outcomes": json.dumps(["Up", "Down"]),
            "clobTokenIds": json.dumps([f"up-{start}", f"down-{start}"])}


class _FallBackDayVenue(_Venue):
    """_Venue whose Gamma lists both 1am ET hours of 2026-11-01; the ET slug holds 05:00Z."""

    MARKETS = ((hm.slug_for(FIRST_1AM_ET), FIRST_1AM_ET), (SECOND_1AM_ET_SLUG, SECOND_1AM_ET))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, p = str(request.url), request.url.params
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/markets"):
            return httpx.Response(200, json=[
                _gamma_market(s, t) for s, t in self.MARKETS if s == p["slug"]])
        if url.startswith(f"{_config.POLYMARKET_GAMMA_API}/events"):
            lo, hi = (int(hm.datetime.fromisoformat(p[k].replace("Z", "+00:00")).timestamp())
                      for k in ("end_date_min", "end_date_max"))
            return httpx.Response(200, json=[
                {"slug": s, "markets": [_gamma_market(s, t)]}
                for s, t in self.MARKETS if lo <= t + 3600 <= hi])
        return super().__call__(request)


@pytest.mark.asyncio
async def test_second_1am_et_hour_on_fall_back_day_trades_and_settles_the_first(
    test_db, monkeypatch
):
    venue = _FallBackDayVenue(hour_close=109.0)  # both hours close below their open: Down wins
    venue.end_hour = FIRST_1AM_ET
    await _tick(monkeypatch, FIRST_1AM_ET + 30, venue)
    assert [p["window_start_ts"] for p in await _positions()] == [FIRST_1AM_ET]

    venue.end_hour = SECOND_1AM_ET
    snap = await _tick(monkeypatch, SECOND_1AM_ET + 20, venue)

    assert snap.window_slug == SECOND_1AM_ET_SLUG
    first, second = await _positions()
    assert (first["window_start_ts"], first["state"], first["exit_price"]) == (
        FIRST_1AM_ET, "closed", 1.0)
    assert (second["window_start_ts"], second["window_slug"], second["state"]) == (
        SECOND_1AM_ET, SECOND_1AM_ET_SLUG, "open")
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT * FROM hourly_strategy_context ORDER BY window_start_ts")
        rows = [dict(r) for r in await cur.fetchall()]
    assert [(r["window_start_ts"], r["action"], r["position_id"]) for r in rows] == [
        (FIRST_1AM_ET, "ENTERED", first["position_id"]),
        (SECOND_1AM_ET, "ENTERED", second["position_id"]),
    ]
    assert rows[0]["outcome_side"] == "Down" and rows[1]["settled_at"] is None


# --- Paper and live in the same hour: each mode keeps its own decision row -------------
# Claude, 2026-09-15, branch-review finding decision-row-shared-across-modes.
# The operator can stop a paper run, click LIVE and start again inside the entry deadline.
# The live run must evaluate its own gate and record its own action, not inherit paper's.


async def _decision_rows_by_mode(start: int) -> dict[str, dict]:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT * FROM hourly_strategy_context WHERE window_start_ts = ? AND strategy_id = ?",
            (start, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"),
        )
        return {r["mode"]: dict(r) for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_paper_gate_block_does_not_stop_a_live_entry_in_the_same_hour(test_db, monkeypatch):
    paper_gate = MagicMock()
    paper_gate.trade_shares = 5.0
    paper_gate.block_reason = MagicMock(return_value="daily loss halt: paper realized -50")
    monkeypatch.setattr(paper, "_risk_gate", paper_gate)
    await _tick(monkeypatch, H + 20, _Venue())

    account = await _go_live(monkeypatch)
    await _tick(monkeypatch, H + 60, _Venue())

    assert account.slot_executor("btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal").submit_entry.await_count == 1
    live_positions = [p for p in await _positions() if p["mode"] == "live"]
    assert len(live_positions) == 1
    rows = await _decision_rows_by_mode(H)
    assert (rows["paper"]["action"], rows["paper"]["position_id"]) == (
        "BLOCKED:daily loss halt: paper realized -50", None)
    assert (rows["live"]["action"], rows["live"]["position_id"]) == (
        "ENTERED", live_positions[0]["position_id"])


@pytest.mark.asyncio
async def test_live_entry_after_a_pending_paper_decision_is_recorded_on_a_live_row(
    test_db, monkeypatch
):
    await _tick(monkeypatch, H + 20, _Venue(), allow=False)  # paper decides, entries held
    await _go_live(monkeypatch)
    await _tick(monkeypatch, H + 60, _Venue())

    live_positions = [p for p in await _positions() if p["mode"] == "live"]
    rows = await _decision_rows_by_mode(H)
    assert set(rows) == {"paper", "live"}
    assert (rows["paper"]["action"], rows["paper"]["position_id"]) == ("PENDING", None)
    assert (rows["live"]["action"], rows["live"]["position_id"]) == (
        "ENTERED", live_positions[0]["position_id"])


# --- Kill switch: paper and live keep the same record through the real loop tick ------
# Claude, 2026-09-15, branch-review finding kill-switch-paper-blocked-live-pending


async def _loop_on_1h_with_kill_file(monkeypatch, tmp_path: Path, mode: str) -> tuple[Path, MagicMock | None]:
    """Wire paper_tick_once for 1h with a real RiskGate whose kill file lives in tmp_path."""
    kill = tmp_path / "KILL"
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", kill)
    gate = build_gate_from_config(is_live=mode == "live")
    await gate.load()
    account = None
    if mode == "live":
        account = _live_account()
        account.gate = gate
        account.cancel_open_all = AsyncMock(return_value=[])
        account.enforce_kill_switch = partial(LiveExecutor.enforce_kill_switch, account)
        monkeypatch.setattr(paper, "_live_executor", account)
    monkeypatch.setattr(paper, "_risk_gate", gate)
    monkeypatch.setattr(paper, "_timeframe", engine.TIMEFRAME)
    monkeypatch.setattr(paper, "_make_settlement_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_Venue())))
    return kill, account


async def _loop_tick(monkeypatch, now: int) -> None:
    monkeypatch.setattr(paper, "_now", lambda: now)
    await paper.paper_tick_once()


async def _blocked_journal_rows() -> int:
    async with _db.connect() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM live_orders WHERE status='BLOCKED'")
        return int((await cur.fetchone())["n"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_kill_file_holds_the_hour_pending_then_enters_after_removal(
    test_db, monkeypatch, tmp_path, mode
):
    kill, account = await _loop_on_1h_with_kill_file(monkeypatch, tmp_path, mode)
    kill.touch()
    await _loop_tick(monkeypatch, H + 30)
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode))["action"] == "PENDING"
    assert await _positions() == [] and await _blocked_journal_rows() == 0

    kill.unlink()
    await _loop_tick(monkeypatch, H + 40)
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode))["action"] == "ENTERED"
    assert [p["mode"] for p in await _positions()] == [mode]
    if account is not None:
        assert account.slots["btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"].submit_entry.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_kill_file_kept_past_the_deadline_records_missed_and_no_journal_row(
    test_db, monkeypatch, tmp_path, mode
):
    kill, _ = await _loop_on_1h_with_kill_file(monkeypatch, tmp_path, mode)
    kill.touch()
    await _loop_tick(monkeypatch, H + 30)
    await _loop_tick(monkeypatch, H + 121)
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode))["action"] == "MISSED"
    assert await _positions() == [] and await _blocked_journal_rows() == 0


# --- Thin top-of-book ask: paper and live size, gate and book the same clip ----------
# Claude, 2026-09-15, branch-review finding thin-top-sizing-paper-vs-live


class _ThinTopVenue(_Venue):
    """Same venue, but the best ask level holds only 3 shares (below the 5-share minimum)."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(f"{_config.POLYMARKET_CLOB_API}/book"):
            return httpx.Response(200, json={
                "bids": [{"price": "0.48", "size": "300"}],
                "asks": [{"price": "0.52", "size": "3"}],
            })
        return super().__call__(request)


def _thin_top_clob_client() -> MagicMock:
    client = MagicMock()
    # py-clob-client lists levels worst -> best (best is last).
    client.get_order_book.return_value = SimpleNamespace(
        asks=[SimpleNamespace(price="0.99", size="100"), SimpleNamespace(price="0.52", size="3")],
        bids=[SimpleNamespace(price="0.48", size="300")],
        tick_size="0.01", min_order_size="5",
    )
    client.create_and_post_order.return_value = {
        "success": True, "errorMsg": "", "orderID": "0xTHIN", "status": "live"}
    return client


async def _thin_top_setup(monkeypatch, tmp_path, mode: str, max_trade_usd: float):
    """Real account executor + RiskGate on a mocked CLOB client; records every gate notional."""
    client = _thin_top_clob_client()
    account = LiveExecutor(
        private_key="0x" + "1" * 64, funder="0xFUNDER", signature_type=2,
        max_trade_usd=max_trade_usd, daily_loss_halt_usd=10.0, bankroll_cap_usd=30.0,
        max_entry_slippage=0.5, exit_fill_timeout_seconds=5.0,
        kill_switch_path=tmp_path / "KILL", client=client,
    )
    gate = account.gate
    gate_notionals: list[float] = []
    real_block_reason = gate.block_reason

    def recording_block_reason(req):
        gate_notionals.append(req.notional_usd)
        return real_block_reason(req)

    monkeypatch.setattr(gate, "block_reason", recording_block_reason)
    monkeypatch.setattr(paper, "_risk_gate", gate)
    monkeypatch.setattr(paper, "_live_executor", account if mode == "live" else None)
    return client, gate, gate_notionals


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_thin_top_ask_books_the_venue_minimum_and_gates_on_its_notional(
    test_db, monkeypatch, tmp_path, mode,
):
    client, gate, gate_notionals = await _thin_top_setup(monkeypatch, tmp_path, mode, 3.0)
    await _tick(monkeypatch, H + 30, _ThinTopVenue())
    rows = await _positions()
    assert [(r["mode"], r["shares"]) for r in rows] == [(mode, 5.0)]
    assert rows[0]["notional_usd"] == pytest.approx(2.6)
    assert gate_notionals and all(n == pytest.approx(2.6) for n in gate_notionals)
    assert gate.daily_buy_notional == pytest.approx(2.6)
    if mode == "live":
        assert client.create_and_post_order.call_args.args[0].size == 5.0
    else:
        client.create_and_post_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_thin_top_ask_venue_minimum_over_the_per_trade_cap_blocks_in_both_modes(
    test_db, monkeypatch, tmp_path, mode,
):
    client, gate, gate_notionals = await _thin_top_setup(monkeypatch, tmp_path, mode, 2.0)
    await _tick(monkeypatch, H + 30, _ThinTopVenue())
    assert await _positions() == []
    client.create_and_post_order.assert_not_called()
    assert gate.daily_buy_notional == 0.0
    assert gate_notionals and all(n == pytest.approx(2.6) for n in gate_notionals)
    action = (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode))["action"]
    assert action.startswith("BLOCKED:per-trade cap: 2.60 USD exceeds 2.00 USD")


# Claude, 2026-09-15, branch-review finding pending-row-never-finalized
@pytest.mark.asyncio
async def test_pending_decision_from_a_stopped_hour_is_missed_on_a_later_tick(test_db, monkeypatch):
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue, allow=False)  # entries held: the row stays PENDING
    assert (await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper"))["action"] == "PENDING"
    # Loop stopped before H's deadline; the next tick runs in hour H+1.
    venue.end_hour = H + 3600
    await _tick(monkeypatch, H + 3600 + 20, venue)
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert row["settled_at"] is not None and row["outcome_side"] == "Down"
    assert row["action"] == "MISSED"
    assert [p["window_slug"] for p in await _positions()] == [NEXT_SLUG]  # nothing chased in H


# Claude, 2026-09-15, branch-review finding pending-row-never-finalized
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paper", "live"])
async def test_open_decision_with_a_position_for_its_ended_hour_is_entered(
    test_db, monkeypatch, mode
):
    venue = _Venue(hour_close=109.0)
    if mode == "live":
        await _go_live(monkeypatch)
    await _tick(monkeypatch, H + 30, venue, allow=False)
    # A crash after the live submit but before the ENTERED write leaves the position row.
    await _insert_hourly(H, mode)
    position_id = (await _positions())[0]["position_id"]
    venue.end_hour = H + 3600
    monkeypatch.setattr(engine, "open_entries", AsyncMock())  # only the ended hour matters
    await _tick(monkeypatch, H + 3600 + 20, venue)
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode=mode)
    assert (row["action"], row["position_id"]) == ("ENTERED", position_id)


# Claude, 2026-09-15, branch-review finding pending-row-never-finalized
@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", ["RECONCILED_NO_LIVE_TRACE", "RECONCILED_UNFILLED"])
async def test_ended_hour_is_missed_when_boot_reconciliation_closed_its_row_as_no_bet(
    test_db, monkeypatch, exit_reason
):
    venue = _Venue()
    await _tick(monkeypatch, H + 30, venue, allow=False)
    # Crash between the row insert and a filled order: the next start's boot reconciliation
    # closes the row because no order was placed, or none filled.
    await _insert_hourly(H, "paper")
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE paper_positions SET state = 'closed', exit_reason = ?", (exit_reason,))
        await conn.commit()
    venue.end_hour = H + 3600
    monkeypatch.setattr(engine, "open_entries", AsyncMock())
    await _tick(monkeypatch, H + 3600 + 20, venue)
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert (row["action"], row["position_id"]) == ("MISSED", None)


@pytest.mark.asyncio
async def test_attempt_left_submitting_in_an_ended_hour_is_uncertain_and_the_operator_is_told(
    test_db, monkeypatch
):
    """Claude, 2026-09-16: merging pending-row-never-finalized with the attempt marker from
    hourly-ambiguous-post-error-retried; an unfinished attempt is never recorded as MISSED."""
    venue = _Venue(hour_close=109.0)
    await _tick(monkeypatch, H + 30, venue, allow=False)
    await ledger.set_action(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", engine.SUBMITTING, mode="paper")
    venue.end_hour = H + 3600
    monkeypatch.setattr(engine, "open_entries", AsyncMock())
    await _tick(monkeypatch, H + 3600 + 20, venue)
    row = await ledger.get_decision(H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    assert row["action"] == engine.UNFINISHED_ATTEMPT
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM notification_feed "
            "WHERE event_type = 'entry_attempt_unfinished'")
        assert (await cur.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_tick_records_the_opening_book_once_per_offset(test_db, monkeypatch):
    """Book record wired into the tick (approved by Zayan (operator), 2026-09-15)."""
    await _tick(monkeypatch, H + 2, _Venue())
    await _tick(monkeypatch, H + 5, _Venue())
    async with _db.connect() as conn:
        cur = await conn.execute(
            "SELECT window_start_ts, offset_s, up_best_ask, down_best_ask FROM hourly_book_snapshots")
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [(H, 0, 0.52, 0.52)]


@pytest.mark.asyncio
async def test_decision_saves_the_candles_and_times_the_audit_needs(test_db, monkeypatch):
    """Candle audit inputs (approved by Zayan (operator), 2026-09-15)."""
    await _tick(monkeypatch, H + 30, _Venue())
    row = await ledger.get_decision(
        H, "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal", mode="paper")
    signal = json.loads(row["signal_json"])
    assert signal["decided_at_ms"] == (H + 30) * 1000
    assert signal["candles_fetched_at_ms"] > 0
    assert signal["spot_h1"]["open_time_ms"] == (H - 3600) * 1000
    assert signal["perp_h1"]["open_time_ms"] == (H - 3600) * 1000
    assert row["candle_audit"] is None  # the archive for that day is not due yet
