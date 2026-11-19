"""Persistent simulated cash accounting for paper positions."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import paper_wallet


@pytest_asyncio.fixture
async def wallet_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "paper_wallet.db")
    await _db.init_db()
    return _db


@pytest.mark.asyncio
async def test_wallet_initializes_once_and_survives_database_reinitialization(
    wallet_db,
) -> None:
    assert await paper_wallet.cash_balance() == pytest.approx(100.0)

    async with wallet_db.connect() as conn:
        await conn.execute("UPDATE paper_wallet SET cash_usd = 42.25 WHERE wallet_id = 1")
        await conn.commit()
    await wallet_db.init_db()

    assert await paper_wallet.cash_balance() == pytest.approx(42.25)
    async with wallet_db.connect() as conn:
        async with conn.execute(
            "SELECT initial_cash_usd FROM paper_wallet WHERE wallet_id = 1"
        ) as cur:
            row = await cur.fetchone()
    assert row["initial_cash_usd"] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_wallet_round_trip_debits_buy_and_credits_sell(wallet_db) -> None:
    async with wallet_db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        after_buy = await paper_wallet.apply_cash_flow(
            conn,
            position_id=17,
            window_slug="btc-updown-5m-test",
            transaction_type="BUY",
            amount_usd=12.34567,
            details={"entry_price": 0.5},
        )
        await conn.commit()
    assert after_buy == pytest.approx(87.6543)

    async with wallet_db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        after_sell = await paper_wallet.apply_cash_flow(
            conn,
            position_id=17,
            window_slug="btc-updown-5m-test",
            transaction_type="SELL",
            amount_usd=15.0,
            details={"exit_price": 0.6},
        )
        await conn.commit()

    assert after_sell == pytest.approx(102.6543)
    assert await paper_wallet.cash_balance() == pytest.approx(102.6543)


@pytest.mark.asyncio
async def test_wallet_rejects_overdraft_and_unfunded_exit(wallet_db) -> None:
    async with wallet_db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        result = await paper_wallet.apply_cash_flow(
            conn,
            position_id=20,
            window_slug="btc-updown-5m-test",
            transaction_type="BUY",
            amount_usd=100.01,
        )
        await conn.rollback()
    assert result is None

    async with wallet_db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        result = await paper_wallet.apply_cash_flow(
            conn,
            position_id=21,
            window_slug="btc-updown-5m-test",
            transaction_type="SELL",
            amount_usd=5.0,
        )
        await conn.rollback()
    assert result is None
    assert await paper_wallet.cash_balance() == pytest.approx(100.0)
