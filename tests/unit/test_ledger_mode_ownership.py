"""Ledger ownership: a loop only ever closes rows of its own mode.

'live' when a LiveExecutor is attached, 'paper' otherwise — for tick exits,
window-roll/settlement closes and force close alike. The other mode's open
rows are left open and surfaced (log + one notify per change + a dashboard
detail line). A paper close of a LIVE row would book it flat at a fictional
price while real tokens stay on Polymarket; a live executor acting on a PAPER
row would place a real order for simulated shares.

Each test runs against its own throwaway SQLite so the real journal is untouched.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import db as _db
import polymarket_bot.paper as paper
from polymarket_exec.execution.live import LiveOrderResult

CURRENT = "btc-updown-5m-1770000300"
ROLLED = "btc-updown-5m-1770000000"


@pytest_asyncio.fixture
async def ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "ownership.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    monkeypatch.setattr(paper, "_foreign_open", None)
    return _db


def _snapshot(**overrides) -> paper.PaperSnapshot:
    base = dict(
        created_at="2026-09-13T12:00:00+00:00",
        window_slug=CURRENT,
        market_question="Bitcoin Up or Down?",
        remaining_seconds=200,
        spot_price=100000.0,
        reference_price=99950.0,
        sigma_per_second=0.5,
        market_up_price=0.57,
        market_down_price=0.45,
        fair_up_prob=0.65,
        edge=0.08,
        signal_side="Up",
        confidence=0.8,
        notional_usd=3.0,
        reason="edge above minimum",
        feed_source="test",
        up_token_id="UP_TOKEN",
        down_token_id="DOWN_TOKEN",
        up_best_bid=0.55,
        down_best_bid=0.43,
    )
    base.update(overrides)
    return paper.PaperSnapshot(**base)


async def _insert(mode: str | None, window_slug: str = CURRENT, side: str = "Up") -> int:
    async with _db.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions (opened_at, window_slug, side, state, "
            "entry_price, notional_usd, shares, mode) VALUES (?, ?, ?, 'open', 0.5, 2, 4, ?)",
            ("2026-09-13T11:59:00+00:00", window_slug, side, mode),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def _states() -> dict[int, str]:
    async with _db.connect() as conn:
        async with conn.execute("SELECT position_id, state FROM paper_positions") as cur:
            return {int(r["position_id"]): r["state"] for r in await cur.fetchall()}


async def _row(position_id: int) -> dict:
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM paper_positions WHERE position_id = ?", (position_id,)
        ) as cur:
            return dict(await cur.fetchone())


async def _notifications(event_type: str) -> int:
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT COUNT(*) AS n FROM notification_feed WHERE event_type = ?", (event_type,)
        ) as cur:
            return int((await cur.fetchone())["n"])


def _executor() -> MagicMock:
    executor = MagicMock()
    executor.cancel_open = AsyncMock(return_value=[])
    executor.resync_flat = AsyncMock(return_value=False)
    executor.submit_entry = AsyncMock(
        return_value=LiveOrderResult(
            ok=True, status="SUBMITTED", order_id="0xE", price=0.57, size=5.26, notional_usd=3.0
        )
    )
    executor.submit_exit = AsyncMock(
        return_value=LiveOrderResult(
            ok=True, status="SUBMITTED", order_id="0xX", price=0.55, size=4.0, notional_usd=2.2
        )
    )
    executor.record_settlement = AsyncMock(
        return_value=LiveOrderResult(
            ok=True, status="SETTLED", order_id=None, price=1.0, size=4.0, notional_usd=2.0
        )
    )
    return executor


def _gate() -> MagicMock:
    gate = MagicMock()
    gate.record_realized_pnl = AsyncMock()
    return gate


# ---------------------------------------------------------------------------
# Per-tick exits: intra-window exit + window-roll settlement close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_tick_never_closes_live_rows(ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _gate()
    monkeypatch.setattr(paper, "_risk_gate", gate)
    monkeypatch.setattr(paper, "_exit_reason", lambda *_a: "TARGET")
    monkeypatch.setattr(paper, "_settle_position_outcome", AsyncMock(return_value=True))
    paper_now, paper_rolled = await _insert("paper"), await _insert("paper", ROLLED)
    live_now, live_rolled = await _insert("live"), await _insert("live", ROLLED)

    await paper._close_due_positions(_snapshot(), client=None)

    states = await _states()
    assert states[paper_now] == "closed" and states[paper_rolled] == "closed"
    assert states[live_now] == "open" and states[live_rolled] == "open"
    live_row = await _row(live_rolled)
    assert live_row["exit_price"] is None and live_row["realized_pnl_usd"] is None
    # Only the two PAPER closes reach the paper halt counter.
    assert gate.record_realized_pnl.await_count == 2


@pytest.mark.asyncio
async def test_live_tick_never_acts_on_paper_rows(ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    monkeypatch.setattr(paper, "_exit_reason", lambda *_a: "TARGET")
    monkeypatch.setattr(paper, "_settle_position_outcome", AsyncMock(return_value=True))
    paper_now, paper_rolled = await _insert("paper"), await _insert("paper", ROLLED)
    live_now, live_rolled = await _insert("live"), await _insert("live", ROLLED)

    await paper._close_due_positions(_snapshot(), client=None)

    states = await _states()
    assert states[live_now] == "closed" and states[live_rolled] == "closed"
    assert states[paper_now] == "open" and states[paper_rolled] == "open"
    executor.submit_exit.assert_awaited_once()  # the live intra-window exit only
    executor.record_settlement.assert_awaited_once_with(True, ROLLED)


@pytest.mark.asyncio
async def test_close_chokepoints_refuse_other_mode_rows(
    ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: a caller that skips the owned-row selection is still refused."""
    gate = _gate()
    monkeypatch.setattr(paper, "_risk_gate", gate)
    live_id = await _insert("live", ROLLED)
    live_pos = await _row(live_id)
    assert await paper._close_position(live_pos, _snapshot(), 0.55, "STOP_REQUEST") is False
    gate.record_realized_pnl.assert_not_awaited()

    executor = _executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    monkeypatch.setattr(paper, "_settle_position_outcome", AsyncMock(return_value=True))
    paper_pos = await _row(await _insert("paper", ROLLED))
    null_pos = await _row(await _insert(None, ROLLED))  # pre-migration row reads as paper
    for pos in (paper_pos, null_pos):
        assert await paper._close_position(pos, _snapshot(), 0.55, "TIME") is False
        assert await paper._close_rolled_position(pos, _snapshot(), client=None) is False
    executor.submit_exit.assert_not_awaited()
    executor.record_settlement.assert_not_awaited()
    assert set((await _states()).values()) == {"open"}


# ---------------------------------------------------------------------------
# Surfacing: log + notify once per change, dashboard detail while present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_live_rows_notify_once_and_show_in_detail(
    ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paper, "_exit_reason", lambda *_a: None)  # hold everything
    live_id = await _insert("live")
    snap = _snapshot()

    for _ in range(3):
        await paper._close_due_positions(snap, client=None)

    assert await _notifications("live_positions_left_open") == 1
    assert "1 LIVE position(s) OPEN" in paper._detail_from_snapshot(snap)

    # Operator flattens + closes the row: warning clears, no new notify.
    async with _db.connect() as conn:
        await conn.execute(
            "UPDATE paper_positions SET state = 'closed' WHERE position_id = ?", (live_id,)
        )
        await conn.commit()
    await paper._close_due_positions(snap, client=None)
    assert "WARNING" not in paper._detail_from_snapshot(snap)
    assert await _notifications("live_positions_left_open") == 1

    # A new stranded row is a new episode → notified again.
    await _insert("live")
    await paper._close_due_positions(snap, client=None)
    assert await _notifications("live_positions_left_open") == 2


@pytest.mark.asyncio
async def test_live_loop_surfaces_stray_paper_rows(ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paper, "_live_executor", _executor())
    monkeypatch.setattr(paper, "_exit_reason", lambda *_a: None)
    await _insert("paper")
    snap = _snapshot()

    await paper._close_due_positions(snap, client=None)
    await paper._close_due_positions(snap, client=None)

    assert await _notifications("paper_positions_left_open") == 1
    assert "1 PAPER position(s) OPEN" in paper._detail_from_snapshot(snap)


# ---------------------------------------------------------------------------
# Force close: ownership + a missing bid never aborts the rest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_close_missing_bid_holds_row_and_closes_the_rest(
    ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = _snapshot(up_best_bid=None, down_best_bid=0.43)

    @asynccontextmanager
    async def _client():
        yield None

    monkeypatch.setattr(paper, "_make_settlement_client", _client)
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=snap))
    no_bid = await _insert("paper", side="Up")
    with_bid = await _insert("paper", side="Down")
    live_id = await _insert("live", side="Down")

    assert await paper.force_close_open_positions() == 1

    states = await _states()
    assert states == {no_bid: "open", with_bid: "closed", live_id: "open"}
    assert (await _row(with_bid))["exit_price"] == pytest.approx(0.43)


# ---------------------------------------------------------------------------
# Entry gating: the one-position limit counts only this loop's own rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_live_row_does_not_block_paper_entry(ledger) -> None:
    await _insert("live", ROLLED)
    await paper._maybe_open_position(_snapshot())
    assert await paper.count_open_positions(mode="paper") == 1
    assert await paper.count_open_positions(mode="live") == 1

    # ...but paper's own open row still enforces one-at-a-time.
    await paper._maybe_open_position(_snapshot(window_slug="btc-updown-5m-1770000600"))
    assert await paper.count_open_positions(mode="paper") == 1


@pytest.mark.asyncio
async def test_open_paper_row_does_not_block_live_entry(
    ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    await _insert("paper", ROLLED)

    await paper._maybe_open_position(_snapshot())

    executor.submit_entry.assert_awaited_once()
    assert await paper.count_open_positions(mode="live") == 1


@pytest.mark.asyncio
async def test_count_open_positions_reads_null_mode_as_paper(ledger) -> None:
    await _insert(None)
    assert await paper.count_open_positions(mode="paper") == 1
    assert await paper.count_open_positions(mode="live") == 0
    assert await paper.count_open_positions() == 1
