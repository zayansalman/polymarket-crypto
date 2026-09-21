"""Unit tests for polymarket_exec.storage.repositories.positions.

Runs against the real schema via db.init_db() (not a hand-simplified one) —
the whole point of this repository is to be a faithful stand-in for the raw
SQL execution/live.py already runs, so its tests need to prove that against
the actual table shape, not an idealized one.
"""

from __future__ import annotations

from pathlib import Path

import db as _db
import pytest
import pytest_asyncio

from polymarket_exec.core.model import Side
from polymarket_exec.storage.repositories import positions as repo


@pytest_asyncio.fixture
async def positions_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point db.py's connection at a throwaway file with the real schema."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_positions.db")
    await _db.init_db()
    return _db


async def _insert_row(
    db_module,
    *,
    window_slug: str = "btc-updown-15m-1789934400",
    side: str = "Up",
    state: str = "open",
    entry_price: float = 0.55,
    shares: float = 10.0,
    notional_usd: float = 5.5,
    opened_at: str = "2026-09-21T00:00:00+00:00",
    closed_at: str | None = None,
    exit_price: float | None = None,
    exit_reason: str | None = None,
    realized_pnl_usd: float | None = None,
    mode: str | None = "paper",
) -> int:
    """Insert one row the way polymarket_bot/paper.py's own INSERT does —
    same columns, same table — and return its position_id."""
    async with db_module.connect() as conn:
        cur = await conn.execute(
            "INSERT INTO paper_positions ("
            "opened_at, closed_at, window_slug, side, state, entry_price, "
            "exit_price, notional_usd, shares, exit_reason, realized_pnl_usd, mode"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                opened_at, closed_at, window_slug, side, state, entry_price,
                exit_price, notional_usd, shares, exit_reason, realized_pnl_usd, mode,
            ),
        )
        await conn.commit()
        return cur.lastrowid


# ============================================================================
# list_open
# ============================================================================


class TestListOpen:
    @pytest.mark.asyncio
    async def test_empty_table_returns_empty_list(self, positions_db) -> None:
        assert await repo.list_open() == []

    @pytest.mark.asyncio
    async def test_returns_only_open_rows(self, positions_db) -> None:
        await _insert_row(positions_db, window_slug="a", state="open")
        await _insert_row(positions_db, window_slug="b", state="closed", closed_at="x")
        positions = await repo.list_open()
        assert [p.market_slug for p in positions] == ["a"]

    @pytest.mark.asyncio
    async def test_orders_oldest_first(self, positions_db) -> None:
        """Matches execution/live.py's own ORDER BY opened_at exactly — the
        boot-reconciliation path assumes the oldest open row is the one
        (and only one) it should adopt."""
        await _insert_row(positions_db, window_slug="second", opened_at="2026-09-21T00:05:00+00:00")
        await _insert_row(positions_db, window_slug="first", opened_at="2026-09-21T00:00:00+00:00")
        positions = await repo.list_open()
        assert [p.market_slug for p in positions] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_maps_every_field_the_live_path_reads(self, positions_db) -> None:
        """execution/live.py's reconciliation reads position_id, window_slug,
        side, entry_price, and shares off the raw row — each must survive
        the trip into Position under its typed name."""
        pid = await _insert_row(
            positions_db, window_slug="btc-updown-15m-1", side="Down",
            entry_price=0.42, shares=12.0, notional_usd=5.04,
        )
        [p] = await repo.list_open()
        assert p.id == pid
        assert p.market_slug == "btc-updown-15m-1"
        assert p.side == Side.DOWN
        assert p.entry_price_usd == 0.42
        assert p.size_shares == 12.0
        assert p.notional_usd == pytest.approx(5.04)
        assert p.mode == "paper"

    @pytest.mark.asyncio
    async def test_null_exit_fields_map_to_none_not_zero(self, positions_db) -> None:
        """A NULL realized_pnl_usd on an open row must stay None — coercing
        it to 0.0 would make an unsettled position look settled at a loss."""
        await _insert_row(positions_db)
        [p] = await repo.list_open()
        assert p.exit_price_usd is None
        assert p.realized_pnl_usd is None
        assert p.closed_at is None


# ============================================================================
# close
# ============================================================================


class TestClose:
    @pytest.mark.asyncio
    async def test_closes_by_id(self, positions_db) -> None:
        pid = await _insert_row(positions_db)
        await repo.close(pid, closed_at="2026-09-21T01:00:00+00:00", exit_reason="MANUAL")
        assert await repo.list_open() == []

    @pytest.mark.asyncio
    async def test_records_closed_at_and_exit_reason(self, positions_db) -> None:
        pid = await _insert_row(positions_db)
        await repo.close(pid, closed_at="2026-09-21T01:00:00+00:00", exit_reason="RECONCILED_UNFILLED")
        async with positions_db.connect() as conn:
            async with conn.execute(
                "SELECT state, closed_at, exit_reason, realized_pnl_usd "
                "FROM paper_positions WHERE position_id = ?",
                (pid,),
            ) as cur:
                row = await cur.fetchone()
        assert row["state"] == "closed"
        assert row["closed_at"] == "2026-09-21T01:00:00+00:00"
        assert row["exit_reason"] == "RECONCILED_UNFILLED"

    @pytest.mark.asyncio
    async def test_defaults_realized_pnl_to_zero_when_never_set(self, positions_db) -> None:
        """Mirrors LiveExecutor._close_ledger_row: a row that settlement
        never touched closes at $0, not NULL — a NULL PnL would silently
        drop out of every KPI sum instead of counting as a scratch."""
        pid = await _insert_row(positions_db, realized_pnl_usd=None)
        await repo.close(pid, closed_at="x", exit_reason="RECONCILED_NO_LIVE_TRACE")
        async with positions_db.connect() as conn:
            async with conn.execute(
                "SELECT realized_pnl_usd FROM paper_positions WHERE position_id = ?",
                (pid,),
            ) as cur:
                row = await cur.fetchone()
        assert row["realized_pnl_usd"] == 0

    @pytest.mark.asyncio
    async def test_never_overwrites_an_already_recorded_pnl(self, positions_db) -> None:
        """A row settlement already priced (e.g. a won/lost outcome) keeps
        that value — COALESCE, not a plain assignment — even if close() is
        called again on it (e.g. a retried boot reconciliation)."""
        pid = await _insert_row(positions_db, realized_pnl_usd=None)
        await repo.close(pid, closed_at="x", exit_reason="SETTLED")
        # Simulate a settlement having since priced this row for real.
        async with positions_db.connect() as conn:
            await conn.execute(
                "UPDATE paper_positions SET realized_pnl_usd = ? WHERE position_id = ?",
                (4.5, pid),
            )
            await conn.commit()
        await repo.close(pid, closed_at="y", exit_reason="RETRIED")
        async with positions_db.connect() as conn:
            async with conn.execute(
                "SELECT realized_pnl_usd FROM paper_positions WHERE position_id = ?",
                (pid,),
            ) as cur:
                row = await cur.fetchone()
        assert row["realized_pnl_usd"] == 4.5

    @pytest.mark.asyncio
    async def test_closing_an_unknown_id_is_a_silent_no_op(self, positions_db) -> None:
        """Matches sqlite's own UPDATE semantics (0 rows matched, no error) —
        a repository should not invent an exception the raw SQL never
        raised, since that would change execution/live.py's error handling
        the moment it is wired onto this function."""
        await repo.close(999, closed_at="x", exit_reason="MANUAL")  # must not raise
