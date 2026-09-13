"""Settle-style strategy profile tests (issue #28).

One entry per window, hold to resolution, settlement registered with the
live executor without an exit order. No network calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
import polymarket_bot.paper as paper
from polymarket_bot import runtime_knobs as _knobs
from polymarket_exec.execution.live import LiveExecutor


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_settle.db")
    await _db.init_db()
    return _db


def _snapshot(**overrides) -> paper.PaperSnapshot:
    base = dict(
        created_at="2026-06-11T06:00:00+00:00",
        window_slug="btc-updown-5m-1781160000",
        market_question="q",
        remaining_seconds=200,
        spot_price=62000.0,
        reference_price=61950.0,
        sigma_per_second=0.0001,
        market_up_price=0.55,
        market_down_price=0.47,
        fair_up_prob=0.62,
        edge=0.07,
        signal_side="Up",
        confidence=0.70,
        notional_usd=3.0,
        reason="enter Up: executable edge +0.070 @ ask 0.550",
        feed_source="t",
        up_best_bid=0.54,
        up_best_ask=0.55,
        up_bid_size=100.0,
        up_ask_size=100.0,
        down_best_bid=0.45,
        down_best_ask=0.47,
        down_bid_size=100.0,
        down_ask_size=100.0,
    )
    base.update(overrides)
    return paper.PaperSnapshot(**base)


_POS = {
    "position_id": 1,
    "side": "Up",
    "entry_price": 0.50,
    "shares": 6.0,
    "notional_usd": 3.0,
    "window_slug": "btc-updown-5m-1781160000",
    "realized_pnl_usd": None,
}


# ---------------------------------------------------------------------------
# Exit style gating
# ---------------------------------------------------------------------------


def test_settle_style_holds_through_target_and_stop_marks():
    # +20% mark would be TARGET, -20% would be STOP under scalp; settle holds.
    assert _knobs.cached("exit_style") == "settle"  # repo default
    assert paper._exit_reason(_snapshot(), _POS, exit_price=0.60) is None
    assert paper._exit_reason(_snapshot(), _POS, exit_price=0.40) is None


def test_scalp_style_still_scalps(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(_knobs._cache, "exit_style", "scalp")
    assert paper._exit_reason(_snapshot(), _POS, exit_price=0.60) == "TARGET"
    assert paper._exit_reason(_snapshot(), _POS, exit_price=0.40) == "STOP"


# ---------------------------------------------------------------------------
# One entry per window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_entry_per_window(test_db, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(paper, "_live_executor", None)
    snap = _snapshot()
    await paper._maybe_open_position(snap)
    async with paper.connect() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM paper_positions") as cur:
            assert (await cur.fetchone())["n"] == 1
        # Close it; settle style must still refuse a second entry this window.
        await db.execute("UPDATE paper_positions SET state = 'closed'")
        await db.commit()
    await paper._maybe_open_position(snap)
    async with paper.connect() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM paper_positions") as cur:
            assert (await cur.fetchone())["n"] == 1
    # A new window is a fresh signal.
    await paper._maybe_open_position(_snapshot(window_slug="btc-updown-5m-1781160300"))
    async with paper.connect() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM paper_positions") as cur:
            assert (await cur.fetchone())["n"] == 2
        async with db.execute(
            "SELECT strategy_style FROM paper_positions LIMIT 1"
        ) as cur:
            assert (await cur.fetchone())["strategy_style"] == "settle"


# ---------------------------------------------------------------------------
# Live settlement registration
# ---------------------------------------------------------------------------


def _settled_executor(tmp_path: Path) -> LiveExecutor:
    ex = LiveExecutor(
        private_key="0x" + "1" * 64,
        funder="0xFUNDER",
        signature_type=1,
        max_trade_usd=3.0,
        daily_loss_halt_usd=10.0,
        bankroll_cap_usd=30.0,
        max_entry_slippage=0.5,
        exit_fill_timeout_seconds=5.0,
        kill_switch_path=tmp_path / "KILL",
        client=MagicMock(),
    )
    ex._position_open = True
    ex._entry_price = 0.50
    ex._entry_token_id = "tok"
    ex._entry_sold_size = 0.0
    ex._entry_order_id = None
    ex._matched_entry_size = AsyncMock(return_value=6.0)  # type: ignore[method-assign]
    return ex


@pytest.mark.asyncio
async def test_record_settlement_win(test_db, tmp_path: Path):
    ex = _settled_executor(tmp_path)
    result = await ex.record_settlement(True, "btc-updown-5m-1781160000")
    assert result.ok and result.status == "SETTLED"
    assert ex.daily_realized_pnl == pytest.approx(3.0)  # 6 * (1.0 - 0.5)
    assert ex._position_open is False
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT intent, status, price, size FROM live_orders"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    assert rows and rows[-1]["intent"] == "SETTLEMENT" and rows[-1]["price"] == 1.0


@pytest.mark.asyncio
async def test_record_settlement_loss_feeds_daily_halt(test_db, tmp_path: Path):
    ex = _settled_executor(tmp_path)
    result = await ex.record_settlement(False, "btc-updown-5m-1781160000")
    assert result.ok and result.status == "SETTLED"
    assert ex.daily_realized_pnl == pytest.approx(-3.0)  # 6 * (0.0 - 0.5)


@pytest.mark.asyncio
async def test_record_settlement_without_position_skips(test_db, tmp_path: Path):
    ex = _settled_executor(tmp_path)
    ex._position_open = False
    result = await ex.record_settlement(True, "w")
    assert not result.ok and result.status == "SKIPPED"
    assert ex.daily_realized_pnl == 0.0


# ---------------------------------------------------------------------------
# Settled close bypasses the executor exit path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settled_close_places_no_exit_order(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    executor = MagicMock()
    executor.submit_exit = AsyncMock()
    monkeypatch.setattr(paper, "_live_executor", executor)
    snap = _snapshot()
    async with paper.connect() as db:
        await db.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state,"
            " entry_price, notional_usd, shares, quote_source, strategy_style)"
            " VALUES (?, ?, 'Up', 'open', 0.5, 3.0, 6.0, 'clob', 'settle')",
            (snap.created_at, snap.window_slug),
        )
        await db.commit()
    pos = dict(_POS)
    closed = await paper._close_position(pos, snap, 1.0, "WINDOW_ROLL", settled=True)
    assert closed is True
    executor.submit_exit.assert_not_called()
    async with paper.connect() as db:
        async with db.execute(
            "SELECT state, exit_price, realized_pnl_usd FROM paper_positions"
        ) as cur:
            row = dict(await cur.fetchone())
    assert row["state"] == "closed"
    assert row["exit_price"] == 1.0
    assert row["realized_pnl_usd"] == pytest.approx(3.0)  # 6 * (1.0 - 0.5)


async def _insert_settle_pos(snap) -> None:
    async with paper.connect() as db:
        await db.execute(
            "INSERT INTO paper_positions(opened_at, window_slug, side, state,"
            " entry_price, notional_usd, shares, quote_source, strategy_style)"
            " VALUES (?, ?, 'Up', 'open', 0.5, 3.0, 6.0, 'clob', 'settle')",
            (snap.created_at, snap.window_slug),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_settled_close_uses_real_held_size(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """Ledger PnL uses the executor's REAL held size, not the recorded shares.

    Only 4 of the 6 recorded shares actually filled on-venue (#103): the ledger
    must book 4 * (1.0 - 0.5) = 2.0, not the recorded-shares 3.0.
    """
    executor = MagicMock()
    executor.submit_exit = AsyncMock()
    monkeypatch.setattr(paper, "_live_executor", executor)
    snap = _snapshot()
    await _insert_settle_pos(snap)
    await paper._close_position(
        dict(_POS), snap, 1.0, "WINDOW_ROLL", settled=True, settled_held=4.0
    )
    async with paper.connect() as db:
        async with db.execute(
            "SELECT realized_pnl_usd FROM paper_positions"
        ) as cur:
            row = dict(await cur.fetchone())
    assert row["realized_pnl_usd"] == pytest.approx(2.0)  # 4 * (1.0 - 0.5)


@pytest.mark.asyncio
async def test_settled_close_phantom_books_zero(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """An entry that never filled on-venue (held 0) books 0 — no phantom win (#103)."""
    executor = MagicMock()
    executor.submit_exit = AsyncMock()
    monkeypatch.setattr(paper, "_live_executor", executor)
    snap = _snapshot()
    await _insert_settle_pos(snap)
    await paper._close_position(
        dict(_POS), snap, 1.0, "WINDOW_ROLL", settled=True, settled_held=0.0
    )
    async with paper.connect() as db:
        async with db.execute(
            "SELECT realized_pnl_usd FROM paper_positions"
        ) as cur:
            row = dict(await cur.fetchone())
    assert row["realized_pnl_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_settled_live_close_skips_paper_counter(
    test_db, monkeypatch: pytest.MonkeyPatch
):
    """Live settle must NOT re-book to the paper counter (#103).

    record_settlement already booked the real-held PnL to the LIVE leg; the
    close booking is_live=False would double-count it into the paper leg (the
    bug behind paper_pnl mirroring live_pnl).
    """
    executor = MagicMock()
    executor.submit_exit = AsyncMock()
    monkeypatch.setattr(paper, "_live_executor", executor)
    gate = MagicMock()
    gate.record_realized_pnl = AsyncMock()
    monkeypatch.setattr(paper, "_risk_gate", gate)
    snap = _snapshot()
    await _insert_settle_pos(snap)
    await paper._close_position(
        dict(_POS), snap, 1.0, "WINDOW_ROLL", settled=True, settled_held=6.0
    )
    gate.record_realized_pnl.assert_not_called()


# ---------------------------------------------------------------------------
# Anti-adverse-selection entry filters (issue #29)
# ---------------------------------------------------------------------------


def test_edge_above_cap_is_rejected():
    from polymarket_bot.strategy import StrategyParams, signal_from_executable_edges

    params = StrategyParams(
        min_trade_usd=1.0, max_trade_usd=5.0, entry_edge_min=0.045,
        min_confidence=0.50, entry_min_remaining_seconds=60,
        entry_edge_max=0.07, min_entry_price=0.50,
    )
    side, _, _, reason = signal_from_executable_edges(
        edge_up=0.30, edge_down=-0.40, remaining_seconds=120,
        up_ask=0.55, down_ask=0.60, params=params,
    )
    assert side is None and "stale-model guard" in reason


def test_longshot_entry_below_min_price_is_rejected():
    from polymarket_bot.strategy import StrategyParams, signal_from_executable_edges

    params = StrategyParams(
        min_trade_usd=1.0, max_trade_usd=5.0, entry_edge_min=0.045,
        min_confidence=0.50, entry_min_remaining_seconds=60,
        entry_edge_max=0.07, min_entry_price=0.50,
    )
    side, _, _, reason = signal_from_executable_edges(
        edge_up=0.06, edge_down=-0.10, remaining_seconds=120,
        up_ask=0.36, down_ask=0.70, params=params,
    )
    assert side is None and "too extreme" in reason


def test_modest_edge_favorite_passes_filters():
    from polymarket_bot.strategy import StrategyParams, signal_from_executable_edges

    params = StrategyParams(
        min_trade_usd=1.0, max_trade_usd=5.0, entry_edge_min=0.045,
        min_confidence=0.50, entry_min_remaining_seconds=60,
        entry_edge_max=0.07, min_entry_price=0.50,
    )
    side, _, notional, reason = signal_from_executable_edges(
        edge_up=0.055, edge_down=-0.08, remaining_seconds=120,
        up_ask=0.62, down_ask=0.42, params=params,
    )
    assert side == "Up" and notional > 0 and "executable edge" in reason


# ---------------------------------------------------------------------------
# No strategy loaded (v0 archived 2026-09-13)
# ---------------------------------------------------------------------------


def _stub_market_inputs(monkeypatch: pytest.MonkeyPatch, *, spot: float | None) -> None:
    """Healthy book + feed for one window with a modest favourite edge on Up —
    inside the archived v0 edge band, so the old gates would have entered."""
    now = paper._now()
    market = {
        "window_start_ts": now - 120,
        "slug": f"btc-updown-5m-{now - 120}",
        "question": "BTC up?",
        "outcomePrices": "[\"0.40\", \"0.60\"]",
        "clobTokenIds": "[\"up-token\", \"down-token\"]",
    }
    books = {
        "up-token": paper.BookTop(best_bid=0.54, best_ask=0.55, bid_size=50.0, ask_size=50.0),
        "down-token": paper.BookTop(best_bid=0.44, best_ask=0.46, bid_size=50.0, ask_size=50.0),
    }
    monkeypatch.setattr(paper, "_fetch_current_market", AsyncMock(return_value=market))
    monkeypatch.setattr(
        paper, "_fetch_clob_book", AsyncMock(side_effect=lambda _c, token: books[token])
    )
    monkeypatch.setattr(paper, "_chainlink_spot_and_closes", lambda: (spot, []))
    monkeypatch.setattr(paper, "_rest_spot_fallback", AsyncMock(return_value=None))
    monkeypatch.setattr(paper, "_get_window_reference", AsyncMock(return_value=60_000.0))
    monkeypatch.setattr(
        paper, "_sigma_with_fallback", AsyncMock(return_value=(0.00002, "chainlink_ws"))
    )


@pytest.mark.asyncio
async def test_no_strategy_loaded_never_signals_an_entry(test_db, monkeypatch):
    from polymarket_bot.strategy import signal_from_executable_edges

    _stub_market_inputs(monkeypatch, spot=60_004.5)

    snap = await paper._build_snapshot(MagicMock())

    # The archived v0 gates would have entered Up on exactly this tick.
    old_side, _, old_notional, _ = signal_from_executable_edges(
        edge_up=snap.fair_up_prob - 0.55,
        edge_down=(1 - snap.fair_up_prob) - 0.46,
        remaining_seconds=snap.remaining_seconds,
        up_ask=0.55,
        down_ask=0.46,
        params=paper._strategy_params(),
    )
    assert old_side == "Up" and old_notional > 0
    # The loop, with no strategy loaded, does not.
    assert snap.signal_side is None
    assert snap.notional_usd == 0.0
    assert snap.reason == paper.NO_STRATEGY_REASON


@pytest.mark.asyncio
async def test_degraded_feed_reason_still_wins_over_no_strategy(test_db, monkeypatch):
    _stub_market_inputs(monkeypatch, spot=None)

    snap = await paper._build_snapshot(MagicMock())

    assert snap.signal_side is None
    assert snap.reason.startswith("skip: settlement feed degraded")
