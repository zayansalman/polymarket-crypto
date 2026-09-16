"""Hourly engine's decision handle for strategy_slot_entry: expected_action reaches the ledger."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import paper
from polymarket_bot import strategy_slot_entry as slot_entry
from polymarket_bot.hourly import engine, ledger
from polymarket_bot.hourly import market as hm

H = 1_789_326_000  # 3PM ET hour, as in test_hourly_engine.py
SID = "btcusdt_1h_spot_taker_push_perp_unconfirmed_close_extreme_reversal"
SLUG = hm.slug_for(H)


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    monkeypatch.setattr(paper, "_live_executor", None)
    monkeypatch.setattr(paper, "_risk_gate", None)
    return _db


def _snapshot() -> paper.PaperSnapshot:
    return paper.PaperSnapshot(
        created_at="2026-09-13T19:00:30+00:00", window_slug=SLUG, market_question=SLUG,
        remaining_seconds=3570, spot_price=110.0, reference_price=110.0,
        sigma_per_second=0.0, market_up_price=0.52, market_down_price=0.52, fair_up_prob=0.5,
        edge=0.0, signal_side=None, confidence=0.0, notional_usd=0.0, reason="",
        feed_source="quotes=clob", up_token_id=f"up-{H}", down_token_id=f"down-{H}",
        up_best_bid=0.48, up_best_ask=0.52, up_bid_size=300.0, up_ask_size=300.0,
        down_best_bid=0.48, down_best_ask=0.52, down_bid_size=300.0, down_ask_size=300.0,
    )


# Claude, 2026-09-17, review of the strategy_slot_entry extraction: _HourlyDecision.set_action
# passes expected_action to the ledger. A tick read SUBMITTING and, while it told the
# operator, another write recorded the result; its close-out must leave that result alone.
@pytest.mark.asyncio
@pytest.mark.parametrize(("written_meanwhile", "expected"), [
    (False, (slot_entry.UNFINISHED_ATTEMPT, None)),
    (True, ("ENTERED", 7)),
])
async def test_closing_out_an_attempt_never_overwrites_a_result_written_meanwhile(
    test_db, monkeypatch, written_meanwhile, expected
) -> None:
    await ledger.record_decision(
        strategy_id=SID, window_slug=SLUG, window_start_ts=H, side="Down", reason="r",
        signal={}, factors={}, up_bid=0.48, up_ask=0.52, down_bid=0.48, down_ask=0.52,
        hour_open=110.0, mode="paper", late=False,
    )
    await ledger.set_action(H, SID, slot_entry.SUBMITTING, mode="paper")

    async def notify(*_args, **_kwargs) -> None:
        if written_meanwhile:
            await ledger.set_action(H, SID, "ENTERED", 7, mode="paper")

    monkeypatch.setattr(slot_entry, "notify", notify)
    await slot_entry.advance(
        _snapshot(), slot_entry.SlotWindow(SID, engine.TIMEFRAME, H),
        action=slot_entry.SUBMITTING, side="Down", now=H + 30, deadline_s=120,
        allow_entries=True, mode="paper", decision=engine._HourlyDecision(H, SID, "paper"),
    )
    row = await ledger.get_decision(H, SID, mode="paper")
    assert (row["action"], row["position_id"]) == expected
