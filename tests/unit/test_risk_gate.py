"""Paper/live gate parity tests (issue #64).

Both paper and live consult the SAME :class:`RiskGate`. These tests pin the
canonical decision for a fixed table of scenarios so a future change to the
gate trips a failure in both modes — never just one. Drift is the bug the
unified gate exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio

import db as _db
from polymarket_exec.execution.gate import EntryRequest, GateConfig, RiskGate


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every gate test gets its own throwaway SQLite so the real journal is untouched."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_gate.db")
    await _db.init_db()
    yield


def _cfg(
    *,
    max_trade_usd: float = 5.0,
    daily_loss_halt_usd: float = 10.0,
    bankroll_cap_usd: Optional[float] = None,
    max_entry_slippage: float = 0.02,
    kill_switch_path: Path | None = None,
) -> GateConfig:
    return GateConfig(
        max_trade_usd=max_trade_usd,
        daily_loss_halt_usd=daily_loss_halt_usd,
        bankroll_cap_usd=bankroll_cap_usd,
        max_entry_slippage=max_entry_slippage,
        kill_switch_path=kill_switch_path or Path("/does/not/exist"),
    )


def _req(
    *,
    notional_usd: float = 3.0,
    position_open: bool = False,
    entry_order_resting: bool = False,
    side_price: float | None = 0.55,
    best_ask: float | None = 0.55,
) -> EntryRequest:
    return EntryRequest(
        notional_usd=notional_usd,
        position_open=position_open,
        entry_order_resting=entry_order_resting,
        side_price=side_price,
        best_ask=best_ask,
    )


class TestGateDecisionTable:
    """Table-driven scenarios: each row must give the SAME verdict in both modes.

    The test instantiates two independent gates (one we treat as "paper",
    one as "live") with the same config and inputs, applies the same
    pre-conditions to each, and asserts identical verdicts. Identical config
    + identical inputs MUST yield identical decisions.
    """

    def test_all_pass(self) -> None:
        gate = RiskGate(_cfg())
        assert gate.block_reason(_req()) is None

    def test_kill_switch_blocks(self, tmp_path: Path) -> None:
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        gate_paper = RiskGate(_cfg(kill_switch_path=kill))
        gate_live = RiskGate(_cfg(kill_switch_path=kill))
        assert gate_paper.block_reason(_req()) == gate_live.block_reason(_req())
        assert "KILL switch active" in (gate_live.block_reason(_req()) or "")

    @pytest.mark.asyncio
    async def test_daily_loss_halt_blocks_each_mode_on_its_leg(self) -> None:
        # Issue #76: each mode halts on its OWN realized-loss leg.
        gate_paper = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=False)
        gate_live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate_paper.record_realized_pnl(-10.5, is_live=False)
        await gate_live.record_realized_pnl(-10.5, is_live=True)
        assert "daily loss halt" in (gate_paper.block_reason(_req()) or "")
        assert "daily loss halt" in (gate_live.block_reason(_req()) or "")

    def test_position_open_blocks(self) -> None:
        gate = RiskGate(_cfg())
        msg = gate.block_reason(_req(position_open=True))
        assert msg == "an open position/order already exists (max 1)"

    def test_resting_entry_blocks(self) -> None:
        gate = RiskGate(_cfg())
        msg = gate.block_reason(_req(entry_order_resting=True))
        assert msg == "an open position/order already exists (max 1)"

    def test_per_trade_cap_blocks(self) -> None:
        gate = RiskGate(_cfg(max_trade_usd=3.0))
        msg = gate.block_reason(_req(notional_usd=5.0))
        assert msg is not None and "per-trade cap" in msg

    def test_negative_notional_blocks(self) -> None:
        gate = RiskGate(_cfg())
        assert gate.block_reason(_req(notional_usd=0.0)) == "notional must be positive"

    @pytest.mark.asyncio
    async def test_bankroll_cap_blocks_when_set(self) -> None:
        gate = RiskGate(_cfg(bankroll_cap_usd=10.0))
        await gate.record_buy_notional(8.0)
        msg = gate.block_reason(_req(notional_usd=5.0))
        assert msg is not None and "daily bankroll cap" in msg

    @pytest.mark.asyncio
    async def test_bankroll_cap_disabled_when_none(self) -> None:
        gate = RiskGate(_cfg(bankroll_cap_usd=None))
        await gate.record_buy_notional(50.0)
        assert gate.block_reason(_req(notional_usd=5.0)) is None

    def test_slippage_blocks_when_book_moved_away(self) -> None:
        gate = RiskGate(_cfg(max_entry_slippage=0.02))
        # Book ask is 0.60 but the signal was generated at 0.55 → 5c slippage.
        msg = gate.block_reason(_req(side_price=0.55, best_ask=0.60))
        assert msg is not None and "slippage guard" in msg

    def test_slippage_passes_when_book_steady(self) -> None:
        gate = RiskGate(_cfg(max_entry_slippage=0.02))
        # Within tolerance.
        assert gate.block_reason(_req(side_price=0.55, best_ask=0.56)) is None

    def test_slippage_skipped_when_book_unavailable(self) -> None:
        gate = RiskGate(_cfg(max_entry_slippage=0.02))
        # Book unavailable: other gates still run but slippage does not block.
        assert gate.block_reason(_req(side_price=0.55, best_ask=None)) is None


class TestPaperLiveCounterParity:
    """Realized PnL and buy notional advance the SAME persisted counters."""

    @pytest.mark.asyncio
    async def test_paper_pnl_advances_halt(self) -> None:
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0))
        await gate.record_realized_pnl(-4.0, is_live=False)
        assert gate.block_reason(_req()) is None  # not yet at halt
        await gate.record_realized_pnl(-6.5, is_live=False)  # cumulative -10.5
        msg = gate.block_reason(_req())
        assert msg is not None and "daily loss halt" in msg

    @pytest.mark.asyncio
    async def test_buy_notional_credit_back_releases_cap(self) -> None:
        gate = RiskGate(_cfg(bankroll_cap_usd=10.0))
        await gate.record_buy_notional(8.0)
        # 8 + 5 > 10 → blocked.
        assert "bankroll cap" in (gate.block_reason(_req(notional_usd=5.0)) or "")
        # Credit back $4 (e.g. partial cancel).
        await gate.record_buy_notional(-4.0)
        # 4 + 5 ≤ 10 → unblocked.
        assert gate.block_reason(_req(notional_usd=5.0)) is None


class TestPnlSplit:
    """Issue #67/#76: live and paper PnL track separately; the halt uses the
    running mode's OWN leg, not the sum."""

    @pytest.mark.asyncio
    async def test_live_paper_separate_buckets(self) -> None:
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0))
        await gate.record_realized_pnl(5.0, is_live=True)
        await gate.record_realized_pnl(-2.0, is_live=False)
        assert gate.live_pnl == pytest.approx(5.0)
        assert gate.paper_pnl == pytest.approx(-2.0)
        assert gate.daily_realized_pnl == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_paper_losses_do_not_halt_live(self) -> None:
        # Issue #76: paper-study losses no longer halt live trading.
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-3.0, is_live=True)   # live within limit
        await live.record_realized_pnl(-20.0, is_live=False)  # huge paper loss
        assert live.block_reason(_req()) is None  # live leg -3 > -10 → not halted

    @pytest.mark.asyncio
    async def test_live_halts_on_live_leg_only(self) -> None:
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-11.0, is_live=True)
        msg = live.block_reason(_req())
        assert msg is not None and "daily loss halt" in msg and "live" in msg


class TestLossHaltBypassBothModes:
    """Issue #76: the loss-halt bypass is an operator runtime knob that applies
    in BOTH paper and live (the old live-lock invariant was removed)."""

    @pytest.mark.asyncio
    async def test_live_gate_respects_bypass_flag(self) -> None:
        from polymarket_exec.execution.gate import set_loss_halt_bypass

        await set_loss_halt_bypass(True)  # operator hits the toggle
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-15.0, is_live=True)
        await live.refresh_overrides()
        # Live now HONOURS the bypass — keeps trading past the limit.
        assert live.bypass_loss_halt is True
        assert live.loss_halt_breached() is False
        assert live.block_reason(_req()) is None
        # Other gates STILL run (per-trade cap, slippage, singleton).
        msg = live.block_reason(_req(notional_usd=999.0))
        assert msg is not None and "per-trade cap" in msg

    @pytest.mark.asyncio
    async def test_paper_gate_respects_bypass_flag(self) -> None:
        from polymarket_exec.execution.gate import set_loss_halt_bypass

        await set_loss_halt_bypass(True)
        paper = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=False)
        await paper.record_realized_pnl(-15.0, is_live=False)
        await paper.refresh_overrides()
        assert paper.block_reason(_req()) is None
        assert paper.bypass_loss_halt is True

    @pytest.mark.asyncio
    async def test_bypass_off_halts_live(self) -> None:
        from polymarket_exec.execution.gate import set_loss_halt_bypass

        await set_loss_halt_bypass(False)
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-15.0, is_live=True)
        await live.refresh_overrides()
        assert "daily loss halt" in (live.block_reason(_req()) or "")


class TestLossHaltBreached:
    """``loss_halt_breached()`` — the predicate the loop uses to auto-stop (#76)."""

    @pytest.mark.asyncio
    async def test_true_on_live_leg_past_limit(self) -> None:
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-10.0, is_live=True)
        assert live.loss_halt_breached() is True

    @pytest.mark.asyncio
    async def test_false_within_limit(self) -> None:
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-9.99, is_live=True)
        assert live.loss_halt_breached() is False

    @pytest.mark.asyncio
    async def test_false_when_bypassed(self) -> None:
        from polymarket_exec.execution.gate import set_loss_halt_bypass

        await set_loss_halt_bypass(True)
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-50.0, is_live=True)
        await live.refresh_overrides()
        assert live.loss_halt_breached() is False

    @pytest.mark.asyncio
    async def test_paper_leg_ignored_in_live(self) -> None:
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(-50.0, is_live=False)  # paper only
        assert live.loss_halt_breached() is False


class TestTrailingHighWaterMarkHalt:
    """Issue #112: the loss halt is a TRAILING drawdown from the session
    high-water mark, not a fixed cumulative floor.

    floor = peak - LIMIT (peak ratchets up only, starts at 0 each UTC day).
    A never-profitable session keeps peak=0 ⇒ behaves exactly like the old
    fixed -LIMIT floor (fail-safe). After locking gains you can only give back
    LIMIT from the peak.
    """

    @pytest.mark.asyncio
    async def test_never_profitable_matches_fixed_floor(self) -> None:
        # peak never leaves 0 ⇒ identical to the pre-#112 fixed -$10 floor.
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.record_realized_pnl(-9.5, is_live=True)
        assert gate.loss_halt_breached() is False
        await gate.record_realized_pnl(-0.5, is_live=True)  # -10.0
        assert gate.loss_halt_breached() is True

    @pytest.mark.asyncio
    async def test_operator_headroom_example(self) -> None:
        # The operator's worked example (LIMIT=10): start 10; +2→10; -2→8; +2→10.
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        assert gate.loss_halt_headroom == pytest.approx(10.0)
        await gate.record_realized_pnl(2.0, is_live=True)
        assert gate.loss_halt_headroom == pytest.approx(10.0)
        assert gate.halt_peak == pytest.approx(2.0)
        await gate.record_realized_pnl(-2.0, is_live=True)  # back to 0
        assert gate.loss_halt_headroom == pytest.approx(8.0)
        assert gate.loss_halt_breached() is False
        await gate.record_realized_pnl(2.0, is_live=True)  # back to peak
        assert gate.loss_halt_headroom == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_trailing_stop_locks_gains(self) -> None:
        # After a $30 run, can only give back $10 (halt at +20), not 30+10.
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.record_realized_pnl(30.0, is_live=True)
        assert gate.halt_peak == pytest.approx(30.0)
        assert gate.loss_halt_breached() is False
        await gate.record_realized_pnl(-9.0, is_live=True)  # +21, gave back 9
        assert gate.loss_halt_breached() is False
        await gate.record_realized_pnl(-1.5, is_live=True)  # +19.5, gave back 10.5
        assert gate.loss_halt_breached() is True

    @pytest.mark.asyncio
    async def test_peak_persists_across_reload(self) -> None:
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.record_realized_pnl(20.0, is_live=True)  # peak 20
        await gate.record_realized_pnl(-5.0, is_live=True)  # +15
        gate2 = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate2.load()
        assert gate2.halt_peak == pytest.approx(20.0)
        assert gate2.loss_halt_breached() is False  # +15 > floor +10
        await gate2.record_realized_pnl(-5.0, is_live=True)  # +10 == floor
        assert gate2.loss_halt_breached() is True

    @pytest.mark.asyncio
    async def test_load_without_peak_key_derives_from_pnl(self) -> None:
        # Pre-#112 session: pnl key set, peak key absent → peak = max(0, pnl).
        from datetime import UTC, datetime

        today = datetime.now(UTC).date().isoformat()
        await _db.set_config("risk.date", today)
        await _db.set_config("risk.live_realized_pnl", repr(8.0))
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.load()
        assert gate.halt_peak == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_rollover_resets_peak(self) -> None:
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.record_realized_pnl(30.0, is_live=True)
        assert gate.halt_peak == pytest.approx(30.0)
        gate._date = "2000-01-01"  # force a stale UTC day
        gate._roll_daily_window()
        assert gate.halt_peak == 0.0
        assert gate.halt_pnl == 0.0

    @pytest.mark.asyncio
    async def test_bypass_overrides_trailing_halt(self) -> None:
        from polymarket_exec.execution.gate import set_loss_halt_bypass

        await set_loss_halt_bypass(True)
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.record_realized_pnl(30.0, is_live=True)
        await gate.record_realized_pnl(-20.0, is_live=True)  # +10, well past floor +20
        await gate.refresh_overrides()
        assert gate.loss_halt_breached() is False

    @pytest.mark.asyncio
    async def test_peak_is_per_leg(self) -> None:
        # The live peak ratchets only on live PnL; paper PnL never moves it.
        live = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await live.record_realized_pnl(25.0, is_live=False)  # paper only
        assert live.halt_peak == 0.0  # live leg untouched
        await live.record_realized_pnl(5.0, is_live=True)
        assert live.halt_peak == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_block_reason_cites_trailing_floor(self) -> None:
        gate = RiskGate(_cfg(daily_loss_halt_usd=10.0), is_live=True)
        await gate.record_realized_pnl(30.0, is_live=True)
        await gate.record_realized_pnl(-12.0, is_live=True)  # +18 <= floor +20
        msg = gate.block_reason(_req())
        assert msg is not None and "daily loss halt" in msg
        assert "peak" in msg  # the message explains the trailing nature


class TestRuntimeMaxTradeOverride:
    """Operator runtime per-trade cap (#50): applies in BOTH modes, no restart."""

    def test_effective_defaults_to_cfg(self) -> None:
        gate = RiskGate(_cfg(max_trade_usd=5.0))
        assert gate.runtime_max_trade_usd is None
        assert gate.effective_max_trade_usd == 5.0

    @pytest.mark.asyncio
    async def test_override_lowers_cap(self) -> None:
        from polymarket_exec.execution.gate import set_runtime_max_trade_usd

        gate = RiskGate(_cfg(max_trade_usd=5.0))
        await set_runtime_max_trade_usd(2.0)
        await gate.refresh_runtime_limits()
        assert gate.effective_max_trade_usd == 2.0
        msg = gate.block_reason(_req(notional_usd=3.0))
        assert msg is not None and "per-trade cap" in msg and "2.00" in msg

    @pytest.mark.asyncio
    async def test_override_raises_cap_for_both_modes(self) -> None:
        from polymarket_exec.execution.gate import set_runtime_max_trade_usd

        # The runtime cap applies in both modes (paper and live).
        paper = RiskGate(_cfg(max_trade_usd=3.0), is_live=False)
        live = RiskGate(_cfg(max_trade_usd=3.0), is_live=True)
        # Without override, $5 trips the $3 env cap in both.
        assert paper.block_reason(_req(notional_usd=5.0)) is not None
        assert live.block_reason(_req(notional_usd=5.0)) is not None
        await set_runtime_max_trade_usd(8.0)
        await paper.refresh_runtime_limits()
        await live.refresh_runtime_limits()
        assert paper.effective_max_trade_usd == 8.0
        assert live.effective_max_trade_usd == 8.0
        assert paper.block_reason(_req(notional_usd=5.0)) is None
        assert live.block_reason(_req(notional_usd=5.0)) is None

    @pytest.mark.asyncio
    async def test_override_cleared_falls_back_to_env(self) -> None:
        from polymarket_exec.execution.gate import set_runtime_max_trade_usd

        gate = RiskGate(_cfg(max_trade_usd=5.0))
        await set_runtime_max_trade_usd(2.0)
        await gate.refresh_runtime_limits()
        assert gate.effective_max_trade_usd == 2.0
        await set_runtime_max_trade_usd(None)  # clear
        await gate.refresh_runtime_limits()
        assert gate.runtime_max_trade_usd is None
        assert gate.effective_max_trade_usd == 5.0

    @pytest.mark.asyncio
    async def test_set_get_round_trip(self) -> None:
        from polymarket_exec.execution.gate import (
            get_runtime_max_trade_usd,
            set_runtime_max_trade_usd,
        )

        assert await get_runtime_max_trade_usd() is None
        await set_runtime_max_trade_usd(4.5)
        assert await get_runtime_max_trade_usd() == 4.5
        await set_runtime_max_trade_usd(0)  # ≤0 clears
        assert await get_runtime_max_trade_usd() is None

    @pytest.mark.asyncio
    async def test_invalid_persisted_value_ignored(self) -> None:
        import db as _db

        gate = RiskGate(_cfg(max_trade_usd=5.0))
        await _db.set_config("runtime.max_trade_usd", "not-a-number")
        await gate.refresh_runtime_limits()
        assert gate.runtime_max_trade_usd is None
        assert gate.effective_max_trade_usd == 5.0


class TestRuntimeTradeShares:
    """Operator runtime trade size in shares (#89): precedence + cap derivation."""

    @pytest.mark.asyncio
    async def test_set_get_round_trip(self) -> None:
        from polymarket_exec.execution.gate import (
            get_runtime_trade_shares,
            set_runtime_trade_shares,
        )

        assert await get_runtime_trade_shares() is None
        await set_runtime_trade_shares(8.0)
        assert await get_runtime_trade_shares() == 8.0
        await set_runtime_trade_shares(0)  # ≤0 clears
        assert await get_runtime_trade_shares() is None

    @pytest.mark.asyncio
    async def test_refresh_derives_effective_cap(self) -> None:
        from polymarket_exec.execution.gate import set_runtime_trade_shares

        gate = RiskGate(_cfg(max_trade_usd=5.0))
        await set_runtime_trade_shares(8.0)
        await gate.refresh_runtime_limits()
        assert gate.runtime_trade_shares == 8.0
        # N shares cost ≤ ~$N (price < 1), so the dollar cap derives as N and an
        # 8-share clip (notional ≤ 8 × 0.99 < 8) never trips its own per-trade cap.
        assert gate.effective_max_trade_usd == 8.0
        assert gate.block_reason(_req(notional_usd=7.5)) is None

    @pytest.mark.asyncio
    async def test_shares_take_precedence_over_dollar_override(self) -> None:
        from polymarket_exec.execution.gate import (
            set_runtime_max_trade_usd,
            set_runtime_trade_shares,
        )

        gate = RiskGate(_cfg(max_trade_usd=5.0))
        await set_runtime_max_trade_usd(3.0)
        await set_runtime_trade_shares(10.0)
        await gate.refresh_runtime_limits()
        assert gate.effective_max_trade_usd == 10.0  # shares win

    @pytest.mark.asyncio
    async def test_cleared_shares_fall_back_to_dollar_override(self) -> None:
        from polymarket_exec.execution.gate import (
            set_runtime_max_trade_usd,
            set_runtime_trade_shares,
        )

        gate = RiskGate(_cfg(max_trade_usd=5.0))
        await set_runtime_max_trade_usd(3.0)
        await set_runtime_trade_shares(10.0)
        await gate.refresh_runtime_limits()
        assert gate.effective_max_trade_usd == 10.0
        await set_runtime_trade_shares(None)  # clear shares
        await gate.refresh_runtime_limits()
        assert gate.runtime_trade_shares is None
        assert gate.effective_max_trade_usd == 3.0  # falls back to the dollar override
