"""Operator loss-halt controls (#76).

Covers the four moving parts of issue #76:
  * the loop's auto-stop decision (`_loss_halt_stop_detail`),
  * the `/api/loss_halt/bypass` and `/api/loss_halt/reset` endpoints,
  * the one-shot stale-bypass migration, and
  * the LOSS HALT panel (STATUS pill → bypass button, Reset button).

Each test runs against its own throwaway SQLite so the real journal is untouched.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import db as _db
from polymarket_exec.execution.gate import GateConfig, RiskGate, get_loss_halt_bypass
from polymarket_exec.ops.dashboard.panels import guardrails
from polymarket_bot.paper import _loss_halt_stop_detail


def _cfg(*, daily_loss_halt_usd: float = 10.0) -> GateConfig:
    return GateConfig(
        max_trade_usd=5.0,
        daily_loss_halt_usd=daily_loss_halt_usd,
        bankroll_cap_usd=None,
        max_entry_slippage=0.02,
        kill_switch_path=Path("/does/not/exist"),
    )


@pytest_asyncio.fixture
async def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Throwaway SQLite for the async (gate/migration) tests."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_lh.db")
    await _db.init_db()
    yield


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient on an isolated DB. The lifespan runs init_db + the #76
    migration, so the bypass starts OFF (halt ON)."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_lh_ep.db")
    from polymarket_exec.ops.dashboard.app import app

    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Loop auto-stop decision
# ---------------------------------------------------------------------------


class TestLossHaltStopDetail:
    @pytest.mark.asyncio
    async def test_stop_detail_on_live_breach(self, isolated_db) -> None:
        g = RiskGate(_cfg(), is_live=True)
        await g.record_realized_pnl(-12.4, is_live=True)
        detail = _loss_halt_stop_detail(g, "live")
        assert detail is not None
        assert "loss halt" in detail.lower()
        assert "live" in detail.lower()
        assert "Reset" in detail

    @pytest.mark.asyncio
    async def test_stop_detail_cites_trailing_floor(self, isolated_db) -> None:
        # With a banked peak the halt fires at a POSITIVE pnl (#112); the operator
        # message must cite the trailing floor, not the nonsensical fixed "-limit".
        g = RiskGate(_cfg(), is_live=True)
        await g.record_realized_pnl(30.0, is_live=True)   # peak 30
        await g.record_realized_pnl(-12.0, is_live=True)  # +18 <= floor +20 → halt
        detail = _loss_halt_stop_detail(g, "live")
        assert detail is not None
        assert "peak" in detail.lower()
        assert "+20.00" in detail  # the trailing floor = peak 30 − limit 10

    @pytest.mark.asyncio
    async def test_none_within_limit(self, isolated_db) -> None:
        g = RiskGate(_cfg(), is_live=True)
        await g.record_realized_pnl(-5.0, is_live=True)
        assert _loss_halt_stop_detail(g, "live") is None

    @pytest.mark.asyncio
    async def test_paper_breach_never_stops_the_loop(self, isolated_db) -> None:
        """#146: a halted PAPER line must not kill the shadow-race collector.
        Entries stay blocked by the per-entry gate; the loop keeps ticking so
        shadow logging and settlement continue on a zero-capital line."""
        g = RiskGate(_cfg(), is_live=False)
        await g.record_realized_pnl(-12.4, is_live=False)
        assert g.loss_halt_breached()  # the halt IS breached...
        assert _loss_halt_stop_detail(g, "paper") is None  # ...but no hard stop


class TestPaperHaltPauseNotify:
    @pytest.mark.asyncio
    async def test_notifies_once_per_episode_and_rearms(self, isolated_db) -> None:
        import polymarket_bot.paper as paper

        paper._paper_halt_pause_notified = False
        g = RiskGate(_cfg(), is_live=False)
        await g.record_realized_pnl(-12.4, is_live=False)

        await paper._notify_paper_halt_pause(g, "paper")
        await paper._notify_paper_halt_pause(g, "paper")  # same episode: no dup

        async with _db.connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM notification_feed"
                " WHERE event_type = 'paper_halt_pause'"
            ) as cur:
                assert (await cur.fetchone())["n"] == 1

        # Episode ends (e.g. daily roll / reset) -> the notifier re-arms.
        g2 = RiskGate(_cfg(), is_live=False)
        await paper._notify_paper_halt_pause(g2, "paper")
        assert paper._paper_halt_pause_notified is False

    @pytest.mark.asyncio
    async def test_never_fires_in_live_mode(self, isolated_db) -> None:
        import polymarket_bot.paper as paper

        paper._paper_halt_pause_notified = False
        g = RiskGate(_cfg(), is_live=True)
        await g.record_realized_pnl(-12.4, is_live=True)
        await paper._notify_paper_halt_pause(g, "live")
        async with _db.connect() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS n FROM notification_feed"
                " WHERE event_type = 'paper_halt_pause'"
            ) as cur:
                assert (await cur.fetchone())["n"] == 0

    @pytest.mark.asyncio
    async def test_none_when_bypassed(self, isolated_db) -> None:
        from polymarket_exec.execution.gate import set_loss_halt_bypass

        await set_loss_halt_bypass(True)
        g = RiskGate(_cfg(), is_live=True)
        await g.record_realized_pnl(-50.0, is_live=True)
        await g.refresh_overrides()
        assert _loss_halt_stop_detail(g, "live") is None

    @pytest.mark.asyncio
    async def test_live_ignores_paper_leg(self, isolated_db) -> None:
        g = RiskGate(_cfg(), is_live=True)
        await g.record_realized_pnl(-50.0, is_live=False)  # paper only
        assert _loss_halt_stop_detail(g, "live") is None

    def test_none_when_gate_missing(self) -> None:
        assert _loss_halt_stop_detail(None, "live") is None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class TestLossHaltEndpoints:
    def test_bypass_enable_then_disable(self, client: TestClient) -> None:
        r = client.post("/api/loss_halt/bypass", json={"enabled": True})
        assert r.status_code == 200
        assert r.json()["bypass_loss_halt"] is True
        assert asyncio.run(get_loss_halt_bypass()) is True

        r = client.post("/api/loss_halt/bypass", json={"enabled": False})
        assert r.json()["bypass_loss_halt"] is False
        assert asyncio.run(get_loss_halt_bypass()) is False

    def test_reset_running_keeps_tally(self, client: TestClient) -> None:
        # While running, Reset leaves the loss-halt tally to the loop (it owns
        # it in memory).
        asyncio.run(_db.set_config("polymarket_bot.state", "running"))
        asyncio.run(_db.set_config("risk.live_realized_pnl", "-8.0"))
        r = client.post("/api/loss_halt/reset")
        body = r.json()
        assert body["status"] == "ok"
        assert body["halt_reset"] is False
        assert asyncio.run(_db.get_config("risk.live_realized_pnl")) == "-8.0"

    def test_reset_zeroes_when_stopped(self, client: TestClient) -> None:
        asyncio.run(_db.set_config("polymarket_bot.state", "stopped"))
        asyncio.run(_db.set_config("risk.live_realized_pnl", "-8.0"))
        asyncio.run(_db.set_config("risk.paper_realized_pnl", "-3.0"))
        r = client.post("/api/loss_halt/reset")
        assert r.json()["status"] == "ok"
        assert float(asyncio.run(_db.get_config("risk.live_realized_pnl"))) == 0.0
        assert float(asyncio.run(_db.get_config("risk.paper_realized_pnl"))) == 0.0

    def test_reset_clears_trailing_halt_after_banked_peak(
        self, client: TestClient
    ) -> None:
        """The #112 trailing halt fires at ``peak - limit``, so zeroing PnL alone
        leaves a banked peak holding the floor above 0 and the operator stays
        halted. Reset must also clear the peaks. Proven through a freshly loaded
        gate (what the loop sees on the next Start), not just the raw keys."""
        asyncio.run(_db.set_config("polymarket_bot.state", "stopped"))
        asyncio.run(_db.set_config("risk.date", RiskGate._today()))
        # Banked +$30 peak, bled back to +$20 → floor +20, 20 <= 20 → HALTED.
        asyncio.run(_db.set_config("risk.live_realized_pnl", "20.0"))
        asyncio.run(_db.set_config("risk.live_peak_pnl", "30.0"))

        before = RiskGate(_cfg(), is_live=True)
        asyncio.run(before.load())
        assert before.loss_halt_breached() is True  # precondition: halted

        r = client.post("/api/loss_halt/reset")
        assert r.json()["status"] == "ok"

        after = RiskGate(_cfg(), is_live=True)
        asyncio.run(after.load())
        assert after.halt_peak == 0.0
        assert after.loss_halt_breached() is False


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


class TestBypassMigration:
    @pytest.mark.asyncio
    async def test_clears_stale_flag_once(self, isolated_db) -> None:
        from polymarket_exec.execution.gate import (
            migrate_clear_stale_bypass_v76,
            set_loss_halt_bypass,
        )

        await set_loss_halt_bypass(True)  # stale paper-era flag
        await migrate_clear_stale_bypass_v76()
        assert await get_loss_halt_bypass() is False  # cleared → halt ON

    @pytest.mark.asyncio
    async def test_does_not_wipe_later_deliberate_bypass(self, isolated_db) -> None:
        from polymarket_exec.execution.gate import (
            migrate_clear_stale_bypass_v76,
            set_loss_halt_bypass,
        )

        await migrate_clear_stale_bypass_v76()  # first run sets the sentinel
        await set_loss_halt_bypass(True)  # operator deliberately enables later
        await migrate_clear_stale_bypass_v76()  # must be a no-op now
        assert await get_loss_halt_bypass() is True


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


def _render(**over) -> str:
    args = dict(
        day_spend=0.0,
        bankroll_cap=30.0,
        submitted_count=0,
        submitted_notional=0.0,
        day_pnl=0.0,
        live_pnl=0.0,
        paper_pnl=0.0,
        loss_halt_usd=10.0,
        state="stopped",
        bot_detail="",
        session_start=None,
        blocked=[],
        mode="paper",
        bypass_loss_halt=False,
    )
    args.update(over)
    return guardrails.render(**args)


class TestGuardrailsPanel:
    def test_status_is_a_bypass_button(self) -> None:
        html = _render(mode="live", bypass_loss_halt=True)
        assert "/api/loss_halt/bypass" in html
        assert ">BYPASS<" in html
        assert "enabled:false" in html  # clicking BYPASS re-enables the halt

    def test_status_ok_click_enables_bypass(self) -> None:
        html = _render(mode="live", live_pnl=-5.0)
        assert ">OK<" in html
        assert "enabled:true" in html  # clicking OK disables the halt

    def test_status_halted_when_live_leg_breached(self) -> None:
        html = _render(mode="live", live_pnl=-12.0)
        assert ">HALTED<" in html

    def test_paper_losses_do_not_halt_live_panel(self) -> None:
        # Live leg fine (-5), paper leg deep (-30): live mode shows OK, not HALTED.
        html = _render(mode="live", live_pnl=-5.0, paper_pnl=-30.0)
        assert ">OK<" in html
        assert ">HALTED<" not in html

    def test_headroom_uses_live_leg_in_live(self) -> None:
        html = _render(mode="live", live_pnl=-4.0, paper_pnl=-30.0)
        assert "Headroom (live)" in html
        assert "$6.00" in html  # 10 - 4, paper -30 ignored

    def test_reset_button_present_and_enabled_when_stopped(self) -> None:
        html = _render(mode="live", state="stopped")
        assert "/api/loss_halt/reset" in html
        assert "Reset halt" in html

    def test_reset_disabled_when_running(self) -> None:
        html = _render(mode="live", state="running")
        assert "Stop the bot to reset" in html

    def test_no_cannot_disable_text(self) -> None:
        html = _render(mode="live")
        assert "cannot disable" not in html


class TestGatePanelParity:
    """The LOSS HALT panel verdict MUST match RiskGate enforcement for the same
    inputs (#112). A divergence — a different comparison or peak derivation —
    would tell the operator they're OK while the loop has actually halted (or
    vice versa) on real money. This guards the two formulas against drift."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "seq",
        [
            [],              # 0/0 → full headroom, not halted
            [30.0, -10.0],   # pnl 20, peak 30, floor 20 → halted (boundary, <=)
            [30.0, -9.0],    # pnl 21, peak 30, floor 20 → not halted
            [30.0, -11.0],   # pnl 19, peak 30, floor 20 → halted
            [-10.0],         # never profitable → halted (== old fixed floor)
            [-9.99],         # never profitable → not halted
            [2.0, -2.0],     # pnl 0, peak 2, floor -8 → not halted
        ],
    )
    async def test_panel_matches_gate(self, isolated_db, seq) -> None:
        gate = RiskGate(_cfg(), is_live=True)
        for pnl in seq:
            await gate.record_realized_pnl(pnl, is_live=True)
        html = _render(mode="live", live_pnl=gate.halt_pnl, live_peak=gate.halt_peak)
        assert (">HALTED<" in html) == gate.loss_halt_breached()
