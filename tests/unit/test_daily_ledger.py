"""Daily altcoin scanner ledger tests (record + settle, incl. the 50-50 tie
rule this market family uses instead of the BTC family's tie-credits-Up)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.daily.ledger import (
    open_settlement_candidates,
    record_signal,
    settle,
)
from polymarket_bot.shadow.fees import net_pnl_per_share


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _record(
    db,
    *,
    window_slug: str = "solana-up-or-down-on-august-30-2026",
    asset: str = "sol",
    side: str = "Up",
    entry_price: float = 0.55,
    shares: float = 10.0,
    reference_price: float = 100.0,
    resolves_at: str = "2026-08-30T16:00:00+00:00",
    binance_symbol: str = "SOLUSDT",
) -> bool:
    return await record_signal(
        created_at="2026-08-30T00:00:00+00:00",
        window_slug=window_slug,
        asset=asset,
        side=side,
        entry_price=entry_price,
        fair_prob=0.60,
        edge=0.05,
        confidence=0.70,
        reason=f"enter {side}",
        notional_usd=entry_price * shares,
        shares=shares,
        reference_price=reference_price,
        resolves_at=resolves_at,
        binance_symbol=binance_symbol,
    )


async def _fetch_all(db) -> list:
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM daily_shadow_positions ORDER BY window_slug"
        ) as cur:
            return await cur.fetchall()


@pytest.mark.asyncio
async def test_record_returns_true_on_first_insert(test_db):
    inserted = await _record(test_db)
    assert inserted is True
    rows = await _fetch_all(test_db)
    assert len(rows) == 1
    assert rows[0]["state"] == "open"
    assert rows[0]["reference_price"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_record_is_idempotent_per_window(test_db):
    """A second scan tick for the SAME window is ignored, not duplicated."""
    first = await _record(test_db, side="Up", entry_price=0.55)
    second = await _record(test_db, side="Down", entry_price=0.80)  # different fields
    assert first is True
    assert second is False

    rows = await _fetch_all(test_db)
    assert len(rows) == 1
    assert rows[0]["side"] == "Up"
    assert rows[0]["entry_price"] == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_two_different_windows_both_record(test_db):
    await _record(test_db, window_slug="solana-up-or-down-on-august-30-2026", asset="sol")
    await _record(test_db, window_slug="dogecoin-up-or-down-on-august-30-2026", asset="doge")
    rows = await _fetch_all(test_db)
    assert len(rows) == 2
    assert {r["asset"] for r in rows} == {"sol", "doge"}


@pytest.mark.asyncio
async def test_settle_win_net_of_fee(test_db):
    await _record(test_db, side="Up", entry_price=0.55, shares=10.0)
    n = await settle(
        window_slug="solana-up-or-down-on-august-30-2026",
        outcome_side="Up",
        settlement_price=105.0,
        resolved_at="2026-08-30T16:00:05+00:00",
    )
    assert n == 1
    row = (await _fetch_all(test_db))[0]
    assert row["state"] == "settled"
    assert row["outcome"] == "Up"
    expected = 10.0 * net_pnl_per_share(0.55, won=True, fee_rate=0.07)
    assert row["realized_pnl_usd"] == pytest.approx(expected)
    assert expected > 0


@pytest.mark.asyncio
async def test_settle_loss_net_of_fee(test_db):
    await _record(test_db, side="Down", entry_price=0.40, shares=10.0)
    await settle(
        window_slug="solana-up-or-down-on-august-30-2026",
        outcome_side="Up",
        settlement_price=105.0,
        resolved_at="2026-08-30T16:00:05+00:00",
    )
    row = (await _fetch_all(test_db))[0]
    expected = 10.0 * net_pnl_per_share(0.40, won=False, fee_rate=0.07)
    assert row["realized_pnl_usd"] == pytest.approx(expected)
    assert expected < 0


@pytest.mark.asyncio
async def test_settle_exact_tie_pays_fifty_fifty(test_db):
    """This family's own resolution rule (confirmed live): an exact tie
    resolves 50-50, unlike the BTC 5m family's tie-credits-Up. Every share —
    Up or Down — pays exactly $0.50, net of the entry fee."""
    await _record(test_db, side="Up", entry_price=0.30, shares=10.0)
    n = await settle(
        window_slug="solana-up-or-down-on-august-30-2026",
        outcome_side=None,
        settlement_price=100.0,  # equals reference_price=100.0 -> tie
        resolved_at="2026-08-30T16:00:05+00:00",
    )
    assert n == 1
    row = (await _fetch_all(test_db))[0]
    assert row["outcome"] == "tie"
    fee = 0.30 * (1 - 0.30) * 0.07
    expected = 10.0 * (0.50 - 0.30 - fee)
    assert row["realized_pnl_usd"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_settle_only_touches_named_window(test_db):
    await _record(test_db, window_slug="solana-up-or-down-on-august-30-2026", asset="sol")
    await _record(test_db, window_slug="dogecoin-up-or-down-on-august-30-2026", asset="doge")
    await settle(
        window_slug="solana-up-or-down-on-august-30-2026",
        outcome_side="Up",
        settlement_price=105.0,
        resolved_at="2026-08-30T16:00:05+00:00",
    )
    rows = {r["asset"]: r for r in await _fetch_all(test_db)}
    assert rows["sol"]["state"] == "settled"
    assert rows["doge"]["state"] == "open"


@pytest.mark.asyncio
async def test_open_settlement_candidates_is_self_contained(test_db):
    await _record(
        test_db,
        window_slug="ethereum-up-or-down-on-august-30-2026",
        asset="eth",
        reference_price=2450.0,
        resolves_at="2026-08-30T16:00:00Z",
        binance_symbol="ETHUSDT",
    )
    candidates = await open_settlement_candidates()
    assert len(candidates) == 1
    c = candidates[0]
    assert c["window_slug"] == "ethereum-up-or-down-on-august-30-2026"
    assert c["reference_price"] == pytest.approx(2450.0)
    assert c["resolves_at"] == "2026-08-30T16:00:00Z"
    assert c["binance_symbol"] == "ETHUSDT"


@pytest.mark.asyncio
async def test_settled_window_is_not_a_settlement_candidate(test_db):
    await _record(test_db)
    await settle(
        window_slug="solana-up-or-down-on-august-30-2026",
        outcome_side="Up",
        settlement_price=105.0,
        resolved_at="2026-08-30T16:00:05+00:00",
    )
    assert await open_settlement_candidates() == []
