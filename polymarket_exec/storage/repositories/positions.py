"""The only place ``paper_positions`` is read or written — issue #266 step 2.

Today two call sites issue raw SQL against this table directly:
``polymarket_exec/execution/live.py`` (boot reconciliation: list every open
position, close one by id) and ``polymarket_bot/paper.py`` (the BTC loop's own
insert/close/settle path, which owns the table and is out of scope for this
step — see the issue). This module gives the live executor a typed
alternative to its own raw SQL, closing the finding from the 2026-09-21
cleanup's verification pass: the executor was reading and mutating a table a
different module also writes, from a different thread, with no shared
ownership.

Wiring ``execution/live.py``'s two call sites onto this module is a separate,
follow-up change — reviewed on its own, since it touches the live money path
directly. This module is additive: it changes no existing behavior on its
own.

Every function here mirrors an existing call site's exact SQL, not a
redesign — same table, same WHERE clause, same COALESCE. The point of a
repository at this stage is one typed door, not new business logic.
"""

from __future__ import annotations

from typing import Any

from db import connect  # type: ignore[import-untyped]

from polymarket_exec.core.model import Position, Side


def _row_to_position(row: Any) -> Position:
    r = dict(row)
    return Position(
        id=r["position_id"],
        market_slug=r["window_slug"],
        side=Side(r["side"]),
        entry_price_usd=float(r["entry_price"]),
        size_shares=float(r["shares"]),
        opened_at=r["opened_at"],
        state=r["state"],
        exit_price_usd=(None if r["exit_price"] is None else float(r["exit_price"])),
        closed_at=r["closed_at"],
        realized_pnl_usd=(
            None if r["realized_pnl_usd"] is None else float(r["realized_pnl_usd"])
        ),
        exit_reason=r["exit_reason"],
        mode=r["mode"],
    )


async def list_open() -> list[Position]:
    """Every currently-open position, oldest first.

    Mirrors ``execution/live.py``'s boot-reconciliation query exactly —
    ``SELECT * FROM paper_positions WHERE state = 'open' ORDER BY
    opened_at`` — so swapping that call site onto this function changes
    which rows it sees not at all.
    """
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' ORDER BY opened_at"
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_position(r) for r in rows]


async def close(position_id: int, *, closed_at: str, exit_reason: str) -> None:
    """Close one position by id.

    Mirrors ``LiveExecutor._close_ledger_row`` exactly: ``realized_pnl_usd``
    is set to 0 only if it was never recorded (``COALESCE``), never
    overwritten — a position whose PnL a settlement already booked keeps
    that value no matter how many times ``close`` is called on it.
    """
    async with connect() as db:
        await db.execute(
            "UPDATE paper_positions SET state = 'closed', closed_at = ?, "
            "exit_reason = ?, realized_pnl_usd = COALESCE(realized_pnl_usd, 0) "
            "WHERE position_id = ?",
            (closed_at, exit_reason, position_id),
        )
        await db.commit()
