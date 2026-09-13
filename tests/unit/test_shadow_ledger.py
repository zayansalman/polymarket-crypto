"""Shadow forward-tester ledger tests (record + settle, net of taker fee)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot.shadow.fees import net_pnl_per_share
from polymarket_bot.shadow.ledger import record_shadow_signal, settle_open_shadow


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


async def _record(
    db,
    *,
    model_id: str,
    side: str,
    entry_price: float = 0.55,
    shares: float = 10.0,
    window_slug: str = "btc-updown-5m-1700000000",
) -> None:
    await record_shadow_signal(
        created_at="2026-06-18T00:00:00+00:00",
        window_slug=window_slug,
        model_id=model_id,
        side=side,
        entry_price=entry_price,
        fair_prob=0.60,
        edge=0.05,
        confidence=0.70,
        reason=f"enter {side}",
        notional_usd=entry_price * shares,
        shares=shares,
        quote_source="clob",
        feed_source="binance",
    )


async def _fetch_all(db) -> list:
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM model_shadow_positions ORDER BY model_id"
        ) as cur:
            return await cur.fetchall()


@pytest.mark.asyncio
async def test_record_two_models_same_window(test_db):
    """Two distinct models on one window produce two open rows."""
    await _record(test_db, model_id="alpha", side="Up")
    await _record(test_db, model_id="beta", side="Down")

    rows = await _fetch_all(test_db)
    assert len(rows) == 2
    assert {r["model_id"] for r in rows} == {"alpha", "beta"}
    assert all(r["state"] == "open" for r in rows)
    assert all(r["realized_pnl_usd"] is None for r in rows)


@pytest.mark.asyncio
async def test_record_is_idempotent_per_window_model(test_db):
    """Re-inserting the same (window, model) is ignored — first write wins."""
    await _record(test_db, model_id="alpha", side="Up", entry_price=0.55)
    # Same window+model, different fields — must NOT overwrite or duplicate.
    await _record(test_db, model_id="alpha", side="Down", entry_price=0.80)

    rows = await _fetch_all(test_db)
    assert len(rows) == 1
    assert rows[0]["side"] == "Up"
    assert rows[0]["entry_price"] == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_settle_resolves_both_with_net_of_fee_pnl(test_db):
    """Settling a window resolves every open row with after-fee PnL signs.

    Up wins when outcome_side='Up'; the Down model loses. Both PnLs equal
    shares * net_pnl_per_share, i.e. net of the 7% taker fee charged on entry.
    """
    await _record(test_db, model_id="up_model", side="Up", entry_price=0.55, shares=10.0)
    await _record(test_db, model_id="down_model", side="Down", entry_price=0.40, shares=10.0)

    settled = await settle_open_shadow(
        window_slug="btc-updown-5m-1700000000",
        outcome_side="Up",
        settlement_price=1.0,
        resolved_at="2026-06-18T00:05:00+00:00",
        fee_rate=0.07,
    )
    assert settled == 2

    rows = {r["model_id"]: r for r in await _fetch_all(test_db)}

    # Both rows are now settled and stamped with the outcome.
    assert all(r["state"] == "settled" for r in rows.values())
    assert all(r["outcome"] == "Up" for r in rows.values())
    assert all(r["settlement_price"] == pytest.approx(1.0) for r in rows.values())
    assert all(r["resolved_at"] == "2026-06-18T00:05:00+00:00" for r in rows.values())

    # The Up row WON: gross (1 - 0.55) less the entry fee, times 10 shares > 0.
    up = rows["up_model"]
    expected_up = 10.0 * net_pnl_per_share(0.55, won=True, fee_rate=0.07)
    assert up["realized_pnl_usd"] == pytest.approx(expected_up)
    assert up["realized_pnl_usd"] > 0

    # The Down row LOST: -entry less the fee, times 10 shares < 0.
    down = rows["down_model"]
    expected_down = 10.0 * net_pnl_per_share(0.40, won=False, fee_rate=0.07)
    assert down["realized_pnl_usd"] == pytest.approx(expected_down)
    assert down["realized_pnl_usd"] < 0


@pytest.mark.asyncio
async def test_regime_columns_migrated_and_default_null(test_db):
    """#122: the migration adds the vol/basis columns; omitting them → NULL."""
    async with test_db.connect() as conn:
        cols = {
            r["name"]
            for r in await (
                await conn.execute("PRAGMA table_info(model_shadow_positions)")
            ).fetchall()
        }
    assert {
        "spot_at_decision",
        "reference_at_decision",
        "sigma_per_second",
        "drift_per_second",
    } <= cols

    # A caller that does not supply them (the default) records NULLs.
    await _record(test_db, model_id="legacy", side="Up")
    row = (await _fetch_all(test_db))[0]
    assert row["spot_at_decision"] is None
    assert row["sigma_per_second"] is None


@pytest.mark.asyncio
async def test_regime_columns_persist_when_supplied(test_db):
    """#122: decision-time market state is stored verbatim for regime axes."""
    await record_shadow_signal(
        created_at="2026-07-09T12:00:00+00:00",
        window_slug="btc-updown-5m-1783600000",
        model_id="v0",
        side="Up",
        entry_price=0.55,
        fair_prob=0.60,
        edge=0.05,
        confidence=0.70,
        reason="enter Up",
        notional_usd=2.75,
        shares=5.0,
        quote_source="clob",
        feed_source="chainlink_ws",
        spot_at_decision=62_800.0,
        reference_at_decision=62_750.0,
        sigma_per_second=4.2e-05,
        drift_per_second=-1.1e-06,
    )
    row = (await _fetch_all(test_db))[0]
    assert row["spot_at_decision"] == pytest.approx(62_800.0)
    assert row["reference_at_decision"] == pytest.approx(62_750.0)
    assert row["sigma_per_second"] == pytest.approx(4.2e-05)
    assert row["drift_per_second"] == pytest.approx(-1.1e-06)


@pytest.mark.asyncio
async def test_settle_only_touches_open_rows_of_the_window(test_db):
    """Settle is scoped to the window and ignores already-settled rows."""
    await _record(test_db, model_id="alpha", side="Up", window_slug="win-A")
    await _record(test_db, model_id="alpha", side="Up", window_slug="win-B")

    first = await settle_open_shadow(
        window_slug="win-A",
        outcome_side="Up",
        settlement_price=1.0,
        resolved_at="2026-06-18T00:05:00+00:00",
    )
    assert first == 1

    # Re-settling win-A finds no open rows now (idempotent settle → 0).
    again = await settle_open_shadow(
        window_slug="win-A",
        outcome_side="Up",
        settlement_price=1.0,
        resolved_at="2026-06-18T00:06:00+00:00",
    )
    assert again == 0

    rows = {r["window_slug"]: r for r in await _fetch_all(test_db)}
    assert rows["win-A"]["state"] == "settled"
    assert rows["win-B"]["state"] == "open"
    assert rows["win-B"]["realized_pnl_usd"] is None
