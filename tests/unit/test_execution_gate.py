"""The risk gate (``ems/execution/gate.py``): one leg per mode, totals from ``risk_events``."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems import runtime_knobs as _knobs
from ems.execution import gate

pytestmark = pytest.mark.asyncio

NOON = 1_790_078_400 + 12 * 3600  # 12:00 UTC on a day


@pytest_asyncio.fixture(autouse=True)
async def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "gate.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    await _db.init_db()
    return tmp_path


async def test_defaults_allow_a_five_dollar_paper_order_and_cap_live_at_three() -> None:
    assert (await gate.RiskGate("paper").check(5.0, now=NOON)).allowed
    assert not (await gate.RiskGate("paper").check(5.01, now=NOON)).allowed
    verdict = await gate.RiskGate("live").check(4.0, now=NOON)
    assert verdict.reason == gate.MAX_TRADE
    assert "$3.00" in verdict.message


async def test_the_kill_switch_comes_first(temp_db) -> None:
    (temp_db / "KILL").touch()
    verdict = await gate.RiskGate("paper").check(1_000_000.0, now=NOON)
    assert verdict.reason == gate.KILL


@pytest.mark.parametrize("notional", [0.0, -1.0, float("nan")])
async def test_an_order_worth_nothing_is_blocked(notional: float) -> None:
    assert (await gate.RiskGate("paper").check(notional, now=NOON)).reason == gate.BAD_NOTIONAL


async def test_the_daily_cap_counts_net_notional_placed_today() -> None:
    await _knobs.set("paper_daily_notional_cap_usd", 10.0)
    leg = gate.RiskGate("paper")
    await leg.commit(strategy="s", order_ref="a", notional_usd=4.0, now=NOON)
    await leg.commit(strategy="s", order_ref="b", notional_usd=4.0, now=NOON)
    assert (await leg.check(3.0, now=NOON)).reason == gate.DAILY_CAP
    await leg.credit(strategy="s", order_ref="b", unfilled_usd=3.0, now=NOON + 60)
    assert (await leg.check(3.0, now=NOON)).allowed
    assert (await leg.day_totals(NOON)).notional_usd == pytest.approx(5.0)
    # Yesterday's orders do not count today.
    assert (await leg.day_totals(NOON + gate.DAY_S)).notional_usd == 0.0


async def test_the_loss_halt_uses_today_settled_pnl() -> None:
    await _knobs.set("live_max_trade_usd", 5.0)
    leg = gate.RiskGate("live")  # halt 10 USD by default
    await leg.realize(strategy="s", order_ref="a", pnl_usd=-6.0, now=NOON - gate.DAY_S)
    await leg.realize(strategy="s", order_ref="b", pnl_usd=-6.0, now=NOON)
    assert (await leg.check(1.0, now=NOON)).allowed
    await leg.realize(strategy="s", order_ref="c", pnl_usd=-4.0, now=NOON)
    verdict = await leg.check(1.0, now=NOON)
    assert verdict.reason == gate.LOSS_HALT and verdict.today.pnl_usd == pytest.approx(-10.0)


async def test_a_zero_halt_is_off() -> None:
    leg = gate.RiskGate("paper")  # paper halt is 0 by default
    await leg.realize(strategy="s", order_ref="a", pnl_usd=-500.0, now=NOON)
    assert (await leg.check(1.0, now=NOON)).allowed


async def test_credit_and_pnl_are_counted_once() -> None:
    leg = gate.RiskGate("paper")
    await leg.commit(strategy="s", order_ref="a", notional_usd=5.0, now=NOON)
    for _ in range(3):
        await leg.credit(strategy="s", order_ref="a", unfilled_usd=2.0, now=NOON)
        await leg.realize(strategy="s", order_ref="a", pnl_usd=1.5, now=NOON)
    totals = await leg.day_totals(NOON)
    assert totals.notional_usd == pytest.approx(3.0) and totals.pnl_usd == pytest.approx(1.5)


async def test_paper_and_live_never_share_a_total() -> None:
    await gate.RiskGate("paper").commit(strategy="s", order_ref="a", notional_usd=5.0, now=NOON)
    await gate.RiskGate("paper").realize(strategy="s", order_ref="a", pnl_usd=-50.0, now=NOON)
    live = await gate.RiskGate("live").day_totals(NOON)
    assert live == gate.DayTotals(notional_usd=0.0, pnl_usd=0.0)


async def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        gate.RiskGate("shadow")
