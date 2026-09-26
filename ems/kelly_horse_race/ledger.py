"""The ledger for Kelly horse-race: the only code that reads or writes its two tables.

- ``kelly_horse_race_decisions``: one row per BTC 15m window decided. Every input the maths
  saw, both draws, the book, what was sent (or why nothing was), and the window's result.
- ``kelly_horse_race_orders``: one row per order per mode (paper, live) for a decision,
  including the ones the risk gate blocked or the venue refused, each with its reason. The
  row mirrors what the venue reports: state, shares filled, when it closed, whether it can
  still fill (``final``), and whether its unfilled notional was given back to the gate.

The tables are created by ``db.init_db`` (``ems/db.py`` SCHEMA), so they exist before the
runner starts and the dashboard never meets a missing table. Timestamps are integer epoch
seconds.

Settlement: once every order of a window is final and the venue has called the window, each
order that filled is settled at its own price with no fee (a passive order never takes):
``payout = filled x 1[won]`` and ``pnl = filled x (1[won] - price)``. A window with no orders,
or none that filled, still gets its result, so the record shows how P(Up) did every window.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ems import db as _db  # type: ignore[import-untyped]
from ems.execution.resting import OrderView

MODES = ("paper", "live")
OUTCOME_INDEX = {"Up": 0, "Down": 1}
OPEN_STATE = "resting"

DECISION_FIELDS = (
    "window_slug", "condition_id", "up_token", "down_token", "window_start", "window_end", "ts",
    "k_price", "k_source", "x_price", "x_obs_ts", "r60", "sigma_h", "tau_h", "z", "p_up", "u1",
    "side", "u2", "token_id", "best_bid", "best_ask", "bid_size", "tick_size", "min_order_size",
    "max_notional_usd", "price", "shares", "notional_usd", "reason",
)


class AlreadyDecided(RuntimeError):
    """The window already has its decision: one per window, never two."""


@dataclass(frozen=True)
class SettledOrder:
    order_id: int
    mode: str
    filled_size: float
    won: bool
    pnl_usd: float


@dataclass(frozen=True)
class SettledWindow:
    decision_id: int
    window_slug: str
    outcome: str
    orders: tuple[SettledOrder, ...]


async def _rows(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    async with _db.connect() as conn:
        async with conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def decision_for(window_slug: str) -> dict[str, Any] | None:
    rows = await _rows("SELECT * FROM kelly_horse_race_decisions WHERE window_slug = ?",
                       (window_slug,))
    return rows[0] if rows else None


async def record_decision(fields: Mapping[str, Any]) -> int:
    """Write a window's decision. Raises ``AlreadyDecided`` if it already has one."""
    unknown = set(fields) - set(DECISION_FIELDS)
    if unknown:
        raise ValueError(f"unknown decision fields: {sorted(unknown)}")
    names = [n for n in DECISION_FIELDS if n in fields]
    async with _db.connect() as conn:
        cur = await conn.execute(
            f"INSERT OR IGNORE INTO kelly_horse_race_decisions ({', '.join(names)}) "
            f"VALUES ({', '.join('?' * len(names))})",
            [fields[n] for n in names],
        )
        await conn.commit()
        if not cur.rowcount:
            raise AlreadyDecided(f"{fields.get('window_slug')} already has its decision")
        return int(cur.lastrowid)


async def record_order(*, decision_id: int, window_slug: str, mode: str, token_id: str,
                       outcome: str, price: float, size: float, queue_ahead: float,
                       window_end_ts: int, state: str, reason: str | None = None,
                       venue_order_id: str | None = None,
                       placed_ts: int | None = None) -> int:
    """Write one order for one mode: placed (``resting``), ``blocked`` or ``rejected``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    final = 0 if state == OPEN_STATE else 1
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO kelly_horse_race_orders (
              decision_id, window_slug, mode, venue_order_id, token_id, outcome, outcome_index,
              price, size, notional_usd, queue_ahead, placed_ts, window_end_ts, state, reason,
              final, credited, closed_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(decision_id), window_slug, mode, venue_order_id, token_id, outcome,
                OUTCOME_INDEX[outcome], float(price), float(size), round(price * size, 6),
                float(queue_ahead), placed_ts, int(window_end_ts), state, reason, final,
                final, None if final == 0 else placed_ts,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def open_orders(mode: str | None = None) -> list[dict[str, Any]]:
    """Orders a venue may still fill (not final), oldest first."""
    if mode is None:
        return await _rows("SELECT * FROM kelly_horse_race_orders WHERE final = 0 ORDER BY id")
    return await _rows(
        "SELECT * FROM kelly_horse_race_orders WHERE final = 0 AND mode = ? ORDER BY id", (mode,)
    )


async def resting_orders() -> list[dict[str, Any]]:
    return await _rows(
        "SELECT * FROM kelly_horse_race_orders WHERE state = 'resting' ORDER BY id"
    )


async def apply_view(order_id: int, view: OrderView) -> float:
    """Mirror what the venue reports. Returns the shares newly filled."""
    async with _db.connect() as conn:
        async with conn.execute(
            "SELECT filled_size FROM kelly_horse_race_orders WHERE id = ?", (int(order_id),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return 0.0
        before = float(row["filled_size"] or 0.0)
        filled = max(before, float(view.filled_size))
        await conn.execute(
            """
            UPDATE kelly_horse_race_orders
            SET state = ?, filled_size = ?, closed_ts = ?, final = ?, forced = ?
            WHERE id = ?
            """,
            (view.state, filled, view.closed_ts, 1 if view.final else 0,
             1 if view.forced else 0, int(order_id)),
        )
        await conn.commit()
    return max(0.0, filled - before)


async def mark_credited(order_id: int) -> None:
    async with _db.connect() as conn:
        await conn.execute("UPDATE kelly_horse_race_orders SET credited = 1 WHERE id = ?",
                           (int(order_id),))
        await conn.commit()


async def to_credit() -> list[dict[str, Any]]:
    """Final orders whose unfilled notional has not been given back to the gate yet."""
    return await _rows(
        """
        SELECT * FROM kelly_horse_race_orders
        WHERE final = 1 AND credited = 0 AND venue_order_id IS NOT NULL ORDER BY id
        """
    )


async def settlement_due(now: float) -> list[dict[str, Any]]:
    """Ended, unsettled windows whose orders can all fill no more."""
    return await _rows(
        """
        SELECT d.* FROM kelly_horse_race_decisions d
        WHERE d.outcome IS NULL AND d.window_end <= ? AND d.condition_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM kelly_horse_race_orders o WHERE o.decision_id = d.id AND o.final = 0
          )
        ORDER BY d.window_end
        """,
        (int(math.floor(now)),),
    )


async def settle(decision_id: int, *, outcome: str, ts: float) -> SettledWindow | None:
    """Record a window's result and settle every order of it that filled. None if it was
    already settled."""
    if outcome not in OUTCOME_INDEX:
        raise ValueError(f"outcome must be Up or Down, got {outcome!r}")
    stamp = int(math.floor(ts))
    settled: list[SettledOrder] = []
    async with _db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await conn.execute(
                "UPDATE kelly_horse_race_decisions SET outcome = ?, settled_ts = ? "
                "WHERE id = ? AND outcome IS NULL",
                (outcome, stamp, int(decision_id)),
            )
            if not cur.rowcount:
                await conn.rollback()
                return None
            async with conn.execute(
                "SELECT * FROM kelly_horse_race_decisions WHERE id = ?", (int(decision_id),)
            ) as c:
                decision = dict(await c.fetchone())
            async with conn.execute(
                "SELECT * FROM kelly_horse_race_orders WHERE decision_id = ? "
                "AND venue_order_id IS NOT NULL ORDER BY id",
                (int(decision_id),),
            ) as c:
                orders = [dict(r) for r in await c.fetchall()]
            for order in orders:
                filled = float(order["filled_size"] or 0.0)
                won = order["outcome"] == outcome
                payout = round(filled if won else 0.0, 6)
                pnl = round(filled * ((1.0 if won else 0.0) - float(order["price"])), 6)
                await conn.execute(
                    "UPDATE kelly_horse_race_orders SET won = ?, payout_usd = ?, pnl_usd = ?, "
                    "settled_ts = ? WHERE id = ?",
                    (1 if won else 0, payout, pnl, stamp, int(order["id"])),
                )
                settled.append(SettledOrder(order_id=int(order["id"]), mode=order["mode"],
                                            filled_size=filled, won=won, pnl_usd=pnl))
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    return SettledWindow(decision_id=int(decision_id), window_slug=decision["window_slug"],
                         outcome=outcome, orders=tuple(settled))


async def summary() -> dict[str, dict[str, Any]]:
    """Per mode: windows settled with a fill, their P&L, the share of them won, orders placed
    and filled, and notional placed."""
    rows = await _rows(
        """
        SELECT mode,
          SUM(CASE WHEN venue_order_id IS NOT NULL THEN 1 ELSE 0 END) AS placed,
          SUM(CASE WHEN state = 'blocked' THEN 1 ELSE 0 END) AS blocked,
          SUM(CASE WHEN state = 'rejected' THEN 1 ELSE 0 END) AS rejected,
          SUM(CASE WHEN filled_size > 0 THEN 1 ELSE 0 END) AS filled,
          SUM(CASE WHEN venue_order_id IS NOT NULL THEN notional_usd ELSE 0 END) AS notional,
          SUM(CASE WHEN settled_ts IS NOT NULL AND filled_size > 0 THEN 1 ELSE 0 END) AS settled,
          SUM(CASE WHEN settled_ts IS NOT NULL AND filled_size > 0 AND won = 1 THEN 1 ELSE 0
              END) AS won,
          COALESCE(SUM(CASE WHEN settled_ts IS NOT NULL THEN pnl_usd END), 0) AS pnl
        FROM kelly_horse_race_orders GROUP BY mode
        """
    )
    out: dict[str, dict[str, Any]] = {
        mode: {"placed": 0, "blocked": 0, "rejected": 0, "filled": 0, "notional_usd": 0.0,
               "settled": 0, "won": 0, "pnl_usd": 0.0, "win_rate": None}
        for mode in MODES
    }
    for row in rows:
        entry = out[row["mode"]]
        entry.update(placed=int(row["placed"] or 0), blocked=int(row["blocked"] or 0),
                     rejected=int(row["rejected"] or 0), filled=int(row["filled"] or 0),
                     notional_usd=float(row["notional"] or 0.0),
                     settled=int(row["settled"] or 0), won=int(row["won"] or 0),
                     pnl_usd=float(row["pnl"] or 0.0))
        entry["win_rate"] = entry["won"] / entry["settled"] if entry["settled"] else None
    decided = await _rows(
        """
        SELECT COUNT(*) AS n,
          SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS settled,
          SUM(CASE WHEN outcome IS NOT NULL AND side = outcome THEN 1 ELSE 0 END) AS side_won
        FROM kelly_horse_race_decisions WHERE side IS NOT NULL
        """
    )
    out["windows"] = {
        "decided": int(decided[0]["n"] or 0),
        "settled": int(decided[0]["settled"] or 0),
        "side_won": int(decided[0]["side_won"] or 0),
    }
    return out


async def recent(limit: int = 12) -> list[dict[str, Any]]:
    """The latest decisions, newest first, each with its orders by mode under ``orders``."""
    decisions = await _rows(
        "SELECT * FROM kelly_horse_race_decisions ORDER BY window_start DESC, id DESC LIMIT ?",
        (int(limit),),
    )
    if not decisions:
        return []
    ids = [d["id"] for d in decisions]
    orders = await _rows(
        f"SELECT * FROM kelly_horse_race_orders WHERE decision_id IN "
        f"({','.join('?' * len(ids))}) ORDER BY id",
        ids,
    )
    by_decision: dict[int, dict[str, dict[str, Any]]] = {}
    for order in orders:
        by_decision.setdefault(int(order["decision_id"]), {})[order["mode"]] = order
    for decision in decisions:
        decision["orders"] = by_decision.get(int(decision["id"]), {})
    return decisions
