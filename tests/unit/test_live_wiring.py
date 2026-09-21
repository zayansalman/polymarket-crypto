"""Engine + controller + dashboard wiring tests for live mode (issue #20).

No network, no real ClobClient: the live executor is replaced with mocks.
Verifies that paper mode stays the untouched default, that live entries/exits
route through the executor, that cancel-on-roll happens, and that boot is
refused without the operator gates.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

import config as _config
import db as _db
import polymarket_bot.controller as controller
import polymarket_bot.paper as paper
from polymarket_exec.execution.live import LiveOrderResult


@pytest_asyncio.fixture
async def bot_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_wiring.db")
    await _db.init_db()
    return _db


def _snapshot(side: str | None = "Up", notional: float = 3.0) -> paper.PaperSnapshot:
    return paper.PaperSnapshot(
        created_at="2026-06-10T12:00:00+00:00",
        window_slug="btc-updown-5m-1770000000",
        market_question="Bitcoin Up or Down?",
        remaining_seconds=200,
        spot_price=100000.0,
        reference_price=99950.0,
        sigma_per_second=0.5,
        market_up_price=0.57,
        market_down_price=0.43,
        fair_up_prob=0.65,
        edge=0.08,
        signal_side=side,
        confidence=0.8,
        notional_usd=notional,
        reason="edge above minimum",
        feed_source="test",
        up_token_id="UP_TOKEN",
        down_token_id="DOWN_TOKEN",
    )


def _mock_executor() -> MagicMock:
    executor = MagicMock()
    executor.submit_entry = AsyncMock(
        return_value=LiveOrderResult(
            ok=True, status="SUBMITTED", order_id="0xE1",
            price=0.57, size=5.26, notional_usd=3.0,
        )
    )
    executor.submit_exit = AsyncMock(
        return_value=LiveOrderResult(
            ok=True, status="SUBMITTED", order_id="0xX1",
            price=0.55, size=5.26, notional_usd=2.89,
        )
    )
    executor.cancel_open = AsyncMock(return_value=["0xE1"])
    executor.enforce_kill_switch = AsyncMock(return_value=False)
    executor.record_realized_pnl = AsyncMock()
    executor.resync_flat = AsyncMock(return_value=False)
    return executor


async def _open_positions(bot_db) -> list[dict]:
    async with bot_db.connect() as conn:
        async with conn.execute("SELECT * FROM paper_positions") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Paper mode untouched by default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_entry_does_not_touch_live_executor(bot_db) -> None:
    assert paper._live_executor is None  # default state
    await paper._maybe_open_position(_snapshot())
    rows = await _open_positions(bot_db)
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 0.57
    assert rows[0]["notional_usd"] == 3.0


@pytest.mark.asyncio
async def test_paper_close_does_not_touch_live_executor(bot_db) -> None:
    await paper._maybe_open_position(_snapshot())
    pos = (await _open_positions(bot_db))[0]
    await paper._close_position(pos, _snapshot(), 0.60, "TARGET")
    rows = await _open_positions(bot_db)
    assert rows[0]["state"] == "closed"
    assert rows[0]["exit_reason"] == "TARGET"


# ---------------------------------------------------------------------------
# Live entry routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_entry_routes_through_executor(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)

    await paper._maybe_open_position(_snapshot(side="Up"))

    executor.submit_entry.assert_awaited_once()
    kwargs = executor.submit_entry.await_args.kwargs
    assert kwargs["token_id"] == "UP_TOKEN"
    assert kwargs["side_price"] == 0.57
    assert kwargs["notional_usd"] == 3.0
    # Ledger mirrors the executor's actual fill terms.
    rows = await _open_positions(bot_db)
    assert len(rows) == 1
    assert rows[0]["shares"] == 5.26


@pytest.mark.asyncio
async def test_live_entry_down_uses_down_token(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)

    snapshot = _snapshot(side="Down")
    await paper._maybe_open_position(snapshot)

    assert executor.submit_entry.await_args.kwargs["token_id"] == "DOWN_TOKEN"
    assert executor.submit_entry.await_args.kwargs["side_price"] == 0.43


@pytest.mark.asyncio
async def test_blocked_live_entry_writes_no_position(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    executor.submit_entry = AsyncMock(
        return_value=LiveOrderResult(ok=False, status="BLOCKED", reason="per-trade cap")
    )
    monkeypatch.setattr(paper, "_live_executor", executor)

    await paper._maybe_open_position(_snapshot())

    assert await _open_positions(bot_db) == []


# ---------------------------------------------------------------------------
# Live exit routing + cancel-on-roll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["WINDOW_ROLL", "BAND_REENTRY"])
async def test_live_exit_cancels_resting_entry_on_roll(
    bot_db, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    await paper._maybe_open_position(_snapshot())
    pos = (await _open_positions(bot_db))[0]

    closed = await paper._close_position(pos, _snapshot(), 0.55, reason)

    assert closed is True
    executor.cancel_open.assert_awaited_once()
    assert executor.cancel_open.await_args.kwargs["reason"] == reason
    executor.submit_exit.assert_awaited_once()
    # Realized PnL is recorded INSIDE the executor on confirmed fills, never
    # fed from paper prices by the engine.
    executor.record_realized_pnl.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["TIME", "TARGET", "STOP"])
async def test_live_exit_without_cancel_for_price_time_exits(
    bot_db, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    await paper._maybe_open_position(_snapshot())
    pos = (await _open_positions(bot_db))[0]

    closed = await paper._close_position(pos, _snapshot(), 0.55, reason)

    assert closed is True
    executor.cancel_open.assert_not_awaited()  # submit_exit flattens internally
    executor.submit_exit.assert_awaited_once()
    # The ledger row mirrors the executor's confirmed fill terms.
    rows = await _open_positions(bot_db)
    assert rows[0]["state"] == "closed"
    assert rows[0]["exit_price"] == pytest.approx(0.55)
    assert rows[0]["realized_pnl_usd"] == pytest.approx(5.26 * (0.55 - 0.57))


# ---------------------------------------------------------------------------
# Failed live exits must NEVER close the ledger row (stranded-token guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason",
    [
        ("BLOCKED", "KILL switch active"),
        ("ERROR", "CLOB 503"),
        ("UNFILLED", "exit SELL not filled in time"),
    ],
)
async def test_failed_live_exit_keeps_position_open(
    bot_db, monkeypatch: pytest.MonkeyPatch, status: str, reason: str
) -> None:
    """If the live exit is blocked/fails/does not fill, real tokens are still
    on the exchange — the row must stay OPEN so the next tick retries, and no
    phantom PnL may be recorded."""
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    await paper._maybe_open_position(_snapshot())
    pos = (await _open_positions(bot_db))[0]
    executor.submit_exit = AsyncMock(
        return_value=LiveOrderResult(ok=False, status=status, reason=reason)
    )

    closed = await paper._close_position(pos, _snapshot(), 0.55, "TIME")

    assert closed is False
    rows = await _open_positions(bot_db)
    assert rows[0]["state"] == "open"  # retried on the next tick
    assert rows[0]["realized_pnl_usd"] in (None, 0)
    executor.record_realized_pnl.assert_not_called()


@pytest.mark.asyncio
async def test_skipped_exit_closes_row_with_zero_pnl(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SKIPPED means the executor CONFIRMED the entry never filled — nothing
    real exists, so the row closes with zero live PnL (no fictional
    paper-price PnL)."""
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    await paper._maybe_open_position(_snapshot())
    pos = (await _open_positions(bot_db))[0]
    executor.submit_exit = AsyncMock(
        return_value=LiveOrderResult(
            ok=False, status="SKIPPED", reason="no matched size"
        )
    )

    closed = await paper._close_position(pos, _snapshot(), 0.40, "TIME")

    assert closed is True
    rows = await _open_positions(bot_db)
    assert rows[0]["state"] == "closed"
    assert rows[0]["realized_pnl_usd"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_partial_exit_fill_accrues_into_open_row(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    monkeypatch.setattr(paper, "_live_executor", executor)
    await paper._maybe_open_position(_snapshot())
    pos = (await _open_positions(bot_db))[0]
    executor.submit_exit = AsyncMock(
        return_value=LiveOrderResult(
            ok=False, status="UNFILLED", reason="partial",
            price=0.55, size=2.0,
        )
    )

    closed = await paper._close_position(pos, _snapshot(), 0.55, "TIME")

    assert closed is False
    rows = await _open_positions(bot_db)
    assert rows[0]["state"] == "open"
    assert rows[0]["realized_pnl_usd"] == pytest.approx(2.0 * (0.55 - 0.57))


@pytest.mark.asyncio
async def test_kill_switch_skips_new_entries_in_tick(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = _mock_executor()
    executor.enforce_kill_switch = AsyncMock(return_value=True)
    monkeypatch.setattr(paper, "_live_executor", executor)
    monkeypatch.setattr(paper, "_build_snapshot", AsyncMock(return_value=_snapshot()))

    await paper.paper_tick_once()

    executor.enforce_kill_switch.assert_awaited_once()
    executor.submit_entry.assert_not_awaited()
    assert await _open_positions(bot_db) == []


# ---------------------------------------------------------------------------
# Boot gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_loop_refuses_without_gates(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_config, "BOT_MODE", "live")
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")

    await paper.run_paper_loop(threading.Event())

    assert paper._live_executor is None
    assert await _db.get_config("polymarket_bot.state") == "stopped"
    detail = await _db.get_config("polymarket_bot.detail")
    assert "refused" in detail.lower()
    assert "paper mode" in detail  # explicit "did NOT fall back" message


@pytest.mark.asyncio
async def test_controller_start_refuses_live_without_gates(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_config, "BOT_MODE", "live")
    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    monkeypatch.setattr(controller, "_live_consent", True)  # operator clicked LIVE
    runner = MagicMock()
    monkeypatch.setattr(controller, "_ensure_runner_started", runner)

    status = await controller.request_start()

    runner.assert_not_called()  # nothing starts — no silent paper fallback
    assert status.state == "stopped"
    assert "POLYMARKET_PRIVATE_KEY" in status.detail


@pytest.mark.asyncio
async def test_controller_start_runs_paper_by_default(
    bot_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin paper explicitly so the test is deterministic regardless of an
    # operator .env that opts into live locally.
    monkeypatch.setattr(controller, "BOT_MODE", "paper")
    monkeypatch.setattr(_config, "BOT_MODE", "paper")
    runner = MagicMock()
    monkeypatch.setattr(controller, "_ensure_runner_started", runner)
    monkeypatch.setattr(controller, "_is_runner_alive", lambda: True)

    status = await controller.request_start()

    runner.assert_called_once()
    assert status.state == "running"
    assert status.mode == "paper"


def test_config_mode_choices_reject_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_MODE", "yolo")
    assert _config._env_choice("BOT_MODE", "paper", {"paper", "live"}) == "paper"
    monkeypatch.setenv("BOT_MODE", "live")
    assert _config._env_choice("BOT_MODE", "paper", {"paper", "live"}) == "live"
