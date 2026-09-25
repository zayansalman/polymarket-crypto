"""Paper ledger for Fade 1h Momentum on 15m.

The only code that reads or writes the strategy's four tables. They are created by
``db.init_db`` (db.py SCHEMA), so they exist before any runner starts and the dashboard never
meets a missing table. Every call goes through ``db.connect()``, so a test that points
``db.DB_PATH`` at a temp file gets a temp ledger.

- ``fade_windows``: one row per coin per 15m window seen, traded or not. The learner needs the
  outcome of every window, so every window gets settled, not only the traded ones.
- ``fade_decisions``: one row per coin per pass. It holds every input the maths saw, so p can
  be recomputed under new dials, plus what the maths said, and why nothing was placed when
  nothing was.
- ``fade_orders``: one row per placement of a paper resting bid. A requote is a cancel plus a
  new row, so each row keeps its own queue position (``depth_ahead``) and its own stretch of
  the trade tape.
- ``fade_dials``: versioned parameter sets. Version 0 is the prior.

Order life: resting -> partial -> filled. Or the order stops resting as cancelled (requote,
restart, stop) or expired (window end), and keeps whatever it filled. Settlement is a separate
step that sets won, pnl and settled_ts on every order in the window. Every fill is a resting
fill at our own price, so there is no fee: pnl = filled_shares x (1 if its side won, else 0)
minus what the shares cost.

The trade tape lags, so a fill can come to light after its order stopped resting. A fill is
accepted for any unsettled order as long as the trade happened while the order was resting.
A window is not settled while any of its orders still has tape left to read.

Each multi-row change is one connection and one transaction. A pass cancelled at any await
leaves either the old state or the new one, never half of each.

Timestamps are integer epoch seconds, the same unit as the window bounds.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import time
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import aiosqlite

import db as _db  # type: ignore[import-untyped]

SIDES = ("Up", "Down")
KINDS = ("entry", "hedge")
RESTING_STATES = ("resting", "partial")
PAPER = "paper"
RECENT_DECISIONS_MAX = 500
_SHARES_EPS = 1e-9

# The prior: dials version 0, used until the learner has settled windows to learn from.
# - w_M = 1, w_S = 0 is the market anchor of the sizing spec (tasks/2026-09-22-fade-1h-sizing-
#   hedging.md section 1): p = Phi(w_M Phi^-1(m) + w_S Phi^-1(p_model)) = m, the market's own
#   price. A bid at or under the market's price then never pays, so nothing is placed until
#   the learner finds the model adds information beyond the price.
# - theta = 0 and kappa0 = 0 reduce the model to the quarter's own move, Phi(y / (sigma sqrt h)).
#   lam, alpha and c are the research fit's starting values; they change nothing while
#   kappa0 = 0.
# - rho, the coin correlation for joint Kelly: 0.76 is where a one-factor Gaussian copula
#   gives the Sep 17-20 tape's rate of all four coins settling the same way (59% of windows).
PRIOR_DIALS: Mapping[str, float] = MappingProxyType({
    "w_M": 1.0,
    "w_S": 0.0,
    "theta": 0.0,
    "kappa0": 0.0,
    "lam": 1.0,
    "alpha": 1.0,
    "c": 0.003,
    "rho": 0.76,
})
PRIOR_SOURCE = "prior"
PRIOR_NOTE = (
    "Starting values before any live learning. p follows the market (w_M = 1, w_S = 0), so no "
    "bid pays until the learner finds the model adds information beyond the price. "
    "theta = 0 and kappa0 = 0 leave only the quarter's own move in the model. rho = 0.76 "
    "matches the Sep 17-20 tape, where all four coins settled the same way in 59% of windows."
)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewOrder:
    """One paper resting bid to place. ``side`` is the outcome the token pays on."""

    window_slug: str
    token_id: str
    side: str
    kind: str
    price: float
    shares: float
    rung: int = 0
    depth_ahead: float = 0.0
    decision_id: int | None = None

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {self.side!r}")
        if self.kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {self.kind!r}")
        if not (_finite(self.price) and 0.0 < self.price < 1.0):
            raise ValueError(f"price must be between 0 and 1, got {self.price!r}")
        if not (_finite(self.shares) and self.shares > 0.0):
            raise ValueError(f"shares must be positive, got {self.shares!r}")
        if not (_finite(self.depth_ahead) and self.depth_ahead >= 0.0):
            raise ValueError(f"depth_ahead must be zero or more, got {self.depth_ahead!r}")
        if self.rung < 0:
            raise ValueError(f"rung must be zero or more, got {self.rung!r}")
        if not self.window_slug or not self.token_id:
            raise ValueError("window_slug and token_id are required")


@dataclass(frozen=True)
class FlowUpdate:
    """What the trade tape said about one order from its cursor up to ``cursor_ts``.

    The stretch read is [cursor, cursor_ts): trades at ``cursor_ts`` itself belong to the
    next read. ``crossed`` is the total volume seen crossing to the order's price since it was
    placed.
    ``add_shares`` are newly filled shares allocated to this order, and ``fill_ts`` is the
    time of the trade that filled them.
    """

    order_id: int
    cursor_ts: int
    crossed: float = 0.0
    add_shares: float = 0.0
    fill_ts: int | None = None


@dataclass(frozen=True)
class FlowResult:
    updated: int  # orders whose cursor moved
    gained: int  # orders that gained filled shares
    stale: int  # updates for tape already read (a replay), or for settled orders


@dataclass(frozen=True)
class Settlement:
    window_slug: str
    outcome: str
    net_pnl: float
    orders: int
    filled_shares: float
    staked_usd: float
    forced_pending: int  # orders settled with tape still unread (force=True only)


class PendingFlow(RuntimeError):
    """A window cannot settle yet: some of its orders still have trade tape to read."""

    def __init__(self, window_slug: str, pending: int) -> None:
        super().__init__(
            f"{window_slug}: {pending} order(s) still have trade tape to read before settling"
        )
        self.window_slug = window_slug
        self.pending = pending


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(
        value
    )


def _num(value: Any) -> float | None:
    """A finite float, or None. NaN and infinity are stored as unknown, not as numbers."""
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ts(value: Any) -> int:
    return int(math.floor(float(value)))


def _clean(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _clean(dataclasses.asdict(value))
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_clean(value), sort_keys=True, allow_nan=False, default=str)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


@contextlib.asynccontextmanager
async def _transaction() -> AsyncIterator[aiosqlite.Connection]:
    """One connection, one transaction: commit on success, roll back on any error or cancel."""
    async with _db.connect() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            await conn.commit()
        except BaseException:
            with contextlib.suppress(Exception):
                await conn.rollback()
            raise


async def _rows(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> list[dict]:
    async with conn.execute(sql, tuple(params)) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _one(conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> dict | None:
    async with conn.execute(sql, tuple(params)) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def _read(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    async with _db.connect() as conn:
        return await _rows(conn, sql, params)


# An order still has trade tape to read while its cursor (which starts at placed_ts) is short
# of the moment it stopped resting: its cancel or expiry, or else its window end. A fully
# filled order has nothing left to read. Needs fade_orders as ``o`` and its window as ``w``.
_NEEDS_FLOW = (
    "o.settled_ts IS NULL AND o.state != 'filled'"
    " AND COALESCE(o.flow_cursor_ts, o.placed_ts) < COALESCE(o.cancelled_ts, w.window_end)"
)

_WINDOW_COLS_FOR_ORDERS = (
    "w.asset, w.condition_id, w.window_start, w.window_end, w.up_token, w.down_token"
)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


async def upsert_window(
    *,
    window_slug: str,
    asset: str,
    window_start: float,
    window_end: float,
    ts: float,
    hour_start: float | None = None,
    condition_id: str | None = None,
    up_token: str | None = None,
    down_token: str | None = None,
    start_ref_price: float | None = None,
    start_ref_source: str | None = None,
    hour_slug: str | None = None,
    hour_condition_id: str | None = None,
) -> None:
    """Record a window, or fill in what was unknown the first time it was seen.

    The first known value of each field wins: the start print is fixed once it is recorded,
    and a later pass can only add what was missing.
    """
    start, end = _ts(window_start), _ts(window_end)
    if end <= start:
        raise ValueError(f"{window_slug}: window_end must be after window_start")
    hour = _ts(hour_start) if hour_start is not None else start - start % 3600
    if not hour <= start < hour + 3600:
        raise ValueError(f"{window_slug}: window does not start inside its hour")
    async with _transaction() as conn:
        await conn.execute(
            """
            INSERT INTO fade_windows (
              window_slug, asset, window_start, window_end, hour_start, condition_id,
              up_token, down_token, start_ref_price, start_ref_source, hour_slug,
              hour_condition_id, created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_slug) DO UPDATE SET
              condition_id = COALESCE(fade_windows.condition_id, excluded.condition_id),
              up_token = COALESCE(fade_windows.up_token, excluded.up_token),
              down_token = COALESCE(fade_windows.down_token, excluded.down_token),
              start_ref_price = COALESCE(fade_windows.start_ref_price, excluded.start_ref_price),
              start_ref_source = COALESCE(fade_windows.start_ref_source,
                                          excluded.start_ref_source),
              hour_slug = COALESCE(fade_windows.hour_slug, excluded.hour_slug),
              hour_condition_id = COALESCE(fade_windows.hour_condition_id,
                                           excluded.hour_condition_id)
            """,
            (
                window_slug, asset, start, end, hour, condition_id, up_token, down_token,
                _num(start_ref_price), start_ref_source, hour_slug, hour_condition_id, _ts(ts),
            ),
        )


async def get_window(window_slug: str) -> dict | None:
    async with _db.connect() as conn:
        return await _one(
            conn, "SELECT * FROM fade_windows WHERE window_slug = ?", (window_slug,)
        )


async def settlement_due(now: float) -> list[dict]:
    """Ended windows with no outcome yet, oldest first.

    ``pending_flow`` counts the window's orders that still have trade tape to read;
    ``settle_window`` refuses while it is above zero.
    """
    return await _read(
        f"""
        SELECT w.*,
               (SELECT COUNT(*) FROM fade_orders o
                 WHERE o.window_slug = w.window_slug AND {_NEEDS_FLOW}) AS pending_flow
          FROM fade_windows w
         WHERE w.outcome IS NULL AND w.window_end <= ?
         ORDER BY w.window_end, w.window_slug
        """,
        (_ts(now),),
    )


async def settle_window(
    window_slug: str, *, outcome: str, ts: float, force: bool = False
) -> Settlement | None:
    """Settle a resolved window and every order in it, in one transaction.

    Orders still resting are expired at the window end first. Returns None if the window was
    already settled. Raises ``PendingFlow`` while any order still has trade tape to read,
    unless ``force`` is set (for tape that can no longer be read); forced orders keep the
    cursor they reached, so the record shows how much tape was actually read.
    """
    if outcome not in SIDES:
        raise ValueError(f"outcome must be one of {SIDES}, got {outcome!r}")
    now = _ts(ts)
    async with _transaction() as conn:
        window = await _one(
            conn, "SELECT * FROM fade_windows WHERE window_slug = ?", (window_slug,)
        )
        if window is None:
            raise ValueError(f"{window_slug}: unknown window")
        if window["outcome"] is not None:
            return None
        if now < window["window_end"]:
            raise ValueError(f"{window_slug}: window has not ended")
        await conn.execute(
            "UPDATE fade_orders SET state = 'expired', cancelled_ts = ?, "
            "cancel_reason = 'window_end' "
            "WHERE window_slug = ? AND state IN ('resting', 'partial')",
            (window["window_end"], window_slug),
        )
        pending_row = await _one(
            conn,
            f"SELECT COUNT(*) AS n FROM fade_orders o "
            f"JOIN fade_windows w ON w.window_slug = o.window_slug "
            f"WHERE o.window_slug = ? AND {_NEEDS_FLOW}",
            (window_slug,),
        )
        pending = int(pending_row["n"]) if pending_row else 0
        if pending and not force:
            raise PendingFlow(window_slug, pending)

        orders = await _rows(
            conn, "SELECT * FROM fade_orders WHERE window_slug = ? AND settled_ts IS NULL",
            (window_slug,),
        )
        net = shares = staked = 0.0
        for order in orders:
            won = order["side"] == outcome
            filled = float(order["filled_shares"] or 0.0)
            pnl = filled * ((1.0 if won else 0.0) - float(order["price"]))
            net += pnl
            shares += filled
            staked += filled * float(order["price"])
            await conn.execute(
                "UPDATE fade_orders SET won = ?, pnl = ?, settled_ts = ? WHERE id = ?",
                (1 if won else 0, pnl, now, order["id"]),
            )
        await conn.execute(
            "UPDATE fade_windows SET outcome = ?, settled_ts = ?, net_pnl = ? "
            "WHERE window_slug = ?",
            (outcome, now, net, window_slug),
        )
    return Settlement(
        window_slug=window_slug,
        outcome=outcome,
        net_pnl=net,
        orders=len(orders),
        filled_shares=shares,
        staked_usd=staked,
        forced_pending=pending if force else 0,
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


async def record_decision(
    *,
    ts: float,
    asset: str,
    inputs: Mapping[str, Any],
    action: str,
    window_slug: str | None = None,
    mode: str | None = None,
    dials_version: int | None = None,
    p_model: float | None = None,
    p: float | None = None,
    side: str | None = None,
    kelly_f: float | None = None,
    stake_usd: float | None = None,
    ladder: Sequence[Mapping[str, Any]] | None = None,
    hedge: Mapping[str, Any] | None = None,
    factors: Mapping[str, Any] | None = None,
    reason: str | None = None,
) -> int:
    """Record one coin's pass: every input, what the maths said, and what was done.

    ``ladder`` is the rungs as ``{"rung", "price", "shares"}`` mappings and ``hedge`` the
    hedge bid, if any. ``reason`` says in plain English why nothing was placed when nothing
    was. Non-finite numbers are stored as unknown.
    """
    if not isinstance(inputs, Mapping):
        raise TypeError("inputs must be a mapping of input name to value")
    if side is not None and side not in SIDES:
        raise ValueError(f"side must be one of {SIDES} or None, got {side!r}")
    if not action:
        raise ValueError("action is required")
    async with _db.connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO fade_decisions (
              ts, asset, window_slug, mode, dials_version, inputs_json, p_model, p, side,
              kelly_f, stake_usd, ladder_json, hedge_json, factors_json, action, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _ts(ts), asset, window_slug, mode, dials_version, _dumps(inputs),
                _num(p_model), _num(p), side, _num(kelly_f), _num(stake_usd),
                _dumps(list(ladder) if ladder is not None else None), _dumps(hedge),
                _dumps(factors), action, reason,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)


def _decision(row: dict) -> dict:
    out = {k: v for k, v in row.items() if not k.endswith("_json")}
    out["inputs"] = _loads(row.get("inputs_json")) or {}
    out["ladder"] = _loads(row.get("ladder_json")) or []
    out["hedge"] = _loads(row.get("hedge_json"))
    out["factors"] = _loads(row.get("factors_json"))
    return out


async def latest_decisions() -> list[dict]:
    """The newest decision for each coin, by coin name.

    Walks the (asset, id) index one coin at a time, so the cost does not grow with the
    thousands of rows a day the table gains.
    """
    rows = await _read(
        """
        WITH RECURSIVE assets(asset) AS (
          SELECT MIN(asset) FROM fade_decisions
          UNION ALL
          SELECT (SELECT MIN(asset) FROM fade_decisions WHERE asset > assets.asset)
            FROM assets WHERE assets.asset IS NOT NULL
        )
        SELECT d.* FROM assets
          JOIN fade_decisions d
            ON d.id = (SELECT id FROM fade_decisions
                        WHERE asset = assets.asset ORDER BY id DESC LIMIT 1)
         ORDER BY d.asset
        """
    )
    return [_decision(r) for r in rows]


async def recent_decisions(n: int = 20, *, asset: str | None = None) -> list[dict]:
    """The newest ``n`` decisions (capped at RECENT_DECISIONS_MAX), newest first."""
    limit = max(1, min(int(n), RECENT_DECISIONS_MAX))
    if asset is None:
        rows = await _read("SELECT * FROM fade_decisions ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = await _read(
            "SELECT * FROM fade_decisions WHERE asset = ? ORDER BY id DESC LIMIT ?",
            (asset, limit),
        )
    return [_decision(r) for r in rows]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------


async def _cancel(
    conn: aiosqlite.Connection, order_ids: Iterable[int], *, ts: int, reason: str
) -> int:
    """Stop resting orders. An order never rests past its window end: one asked to stop at
    or after the end is recorded as expired at the end."""
    ids = sorted({int(i) for i in order_ids})
    if not ids:
        return 0
    marks = ", ".join("?" for _ in ids)
    rows = await _rows(
        conn,
        f"SELECT o.id, w.window_end FROM fade_orders o "
        f"JOIN fade_windows w ON w.window_slug = o.window_slug "
        f"WHERE o.id IN ({marks}) AND o.state IN ('resting', 'partial')",
        ids,
    )
    for row in rows:
        end = int(row["window_end"])
        if ts >= end:
            state, stop_ts, why = "expired", end, "window_end"
        else:
            state, stop_ts, why = "cancelled", ts, reason
        await conn.execute(
            "UPDATE fade_orders SET state = ?, cancelled_ts = ?, cancel_reason = ? "
            "WHERE id = ? AND state IN ('resting', 'partial')",
            (state, stop_ts, why, row["id"]),
        )
    return len(rows)


async def place_orders(
    orders: Sequence[NewOrder],
    *,
    ts: float,
    cancel_ids: Iterable[int] = (),
    cancel_reason: str = "requote",
) -> list[int | None]:
    """Cancel ``cancel_ids`` and place ``orders``, all in one transaction (a requote).

    Returns the new row ids in input order; None marks a placement already recorded (same
    window, token, kind, rung and second). Raises ValueError, writing nothing, if an order's
    window is unknown or over, or its token is not the window's token for its side.
    """
    now = _ts(ts)
    ids: list[int | None] = []
    async with _transaction() as conn:
        await _cancel(conn, cancel_ids, ts=now, reason=cancel_reason)
        windows: dict[str, dict | None] = {}
        for order in orders:
            if order.window_slug not in windows:
                windows[order.window_slug] = await _one(
                    conn, "SELECT * FROM fade_windows WHERE window_slug = ?",
                    (order.window_slug,),
                )
            window = windows[order.window_slug]
            if window is None:
                raise ValueError(f"{order.window_slug}: unknown window, record it first")
            if now >= window["window_end"]:
                raise ValueError(f"{order.window_slug}: window is over, cannot place")
            expected = window["up_token"] if order.side == "Up" else window["down_token"]
            if expected is not None and expected != order.token_id:
                raise ValueError(
                    f"{order.window_slug}: token {order.token_id} is not the {order.side} token"
                )
            cur = await conn.execute(
                """
                INSERT OR IGNORE INTO fade_orders (
                  window_slug, token_id, side, kind, rung, price, shares, depth_ahead,
                  state, placed_ts, decision_id, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'resting', ?, ?, ?)
                """,
                (
                    order.window_slug, order.token_id, order.side, order.kind, order.rung,
                    float(order.price), float(order.shares), float(order.depth_ahead), now,
                    order.decision_id, PAPER,
                ),
            )
            ids.append(int(cur.lastrowid) if cur.rowcount > 0 else None)
    return ids


async def cancel_orders(order_ids: Iterable[int], *, ts: float, reason: str) -> int:
    """Stop the given orders resting. Returns how many were still resting."""
    async with _transaction() as conn:
        return await _cancel(conn, order_ids, ts=_ts(ts), reason=reason)


async def cancel_all_open(*, ts: float, reason: str) -> int:
    """Stop every resting order (a restart, the strategy switched off, or LIVE requested)."""
    async with _transaction() as conn:
        rows = await _rows(
            conn, "SELECT id FROM fade_orders WHERE state IN ('resting', 'partial')"
        )
        return await _cancel(conn, (r["id"] for r in rows), ts=_ts(ts), reason=reason)


async def expire_due(now: float) -> int:
    """Expire, at their window end, all orders still resting in windows that have ended.

    Driven by the ledger's own window_end, never by what market discovery still lists.
    """
    async with _transaction() as conn:
        cur = await conn.execute(
            """
            UPDATE fade_orders
               SET state = 'expired', cancel_reason = 'window_end',
                   cancelled_ts = (SELECT w.window_end FROM fade_windows w
                                    WHERE w.window_slug = fade_orders.window_slug)
             WHERE state IN ('resting', 'partial')
               AND window_slug IN (SELECT window_slug FROM fade_windows WHERE window_end <= ?)
            """,
            (_ts(now),),
        )
        return int(cur.rowcount or 0)


async def orders_needing_flow() -> list[dict]:
    """Orders that still have trade tape to read, with their window's market details.

    ``flow_from`` to ``flow_until`` is the stretch of tape still to read for each order:
    from its cursor (or its placement) to when it stopped resting (or the window end).
    Within a token, the highest bid comes first, the order fills are allocated in.
    """
    return await _read(
        f"""
        SELECT o.*, {_WINDOW_COLS_FOR_ORDERS},
               COALESCE(o.flow_cursor_ts, o.placed_ts) AS flow_from,
               COALESCE(o.cancelled_ts, w.window_end) AS flow_until
          FROM fade_orders o
          JOIN fade_windows w ON w.window_slug = o.window_slug
         WHERE {_NEEDS_FLOW}
         ORDER BY o.window_slug, o.token_id, o.price DESC, o.placed_ts, o.id
        """
    )


async def record_flow(updates: Sequence[FlowUpdate]) -> FlowResult:
    """Apply what the tape said about each order, all in one transaction.

    Each update covers the tape from the order's cursor (its placement, the first time) up to
    ``cursor_ts``. A fill must come from that stretch and from while the order was resting.
    An update for tape already read is a replay and is skipped, so applying the same batch
    twice changes nothing. A fill that breaks these rules raises ValueError and nothing in
    the batch is written.
    """
    updated = gained = stale = 0
    async with _transaction() as conn:
        for u in updates:
            row = await _one(
                conn,
                "SELECT o.*, w.window_end FROM fade_orders o "
                "JOIN fade_windows w ON w.window_slug = o.window_slug WHERE o.id = ?",
                (int(u.order_id),),
            )
            if row is None:
                raise ValueError(f"order {u.order_id} does not exist")
            if row["settled_ts"] is not None:
                stale += 1
                continue
            cursor_to = _ts(u.cursor_ts)
            placed = int(row["placed_ts"])
            if cursor_to < placed:
                raise ValueError(f"order {u.order_id}: tape cursor is before the placement")
            stored = row["flow_cursor_ts"]
            cursor_from = int(stored) if stored is not None else placed
            if stored is not None and cursor_to <= cursor_from:
                stale += 1
                continue
            resting_until = (
                int(row["cancelled_ts"]) if row["cancelled_ts"] is not None
                else int(row["window_end"])
            )
            add = float(u.add_shares or 0.0)
            if not (math.isfinite(add) and add >= 0.0):
                raise ValueError(f"order {u.order_id}: add_shares must be zero or more")
            shares = float(row["shares"])
            filled = float(row["filled_shares"] or 0.0)
            filled_ts = row["filled_ts"]
            if add > 0.0:
                if u.fill_ts is None:
                    raise ValueError(f"order {u.order_id}: a fill needs the trade's time")
                fill_ts = _ts(u.fill_ts)
                if not cursor_from <= fill_ts < min(cursor_to, resting_until):
                    raise ValueError(
                        f"order {u.order_id}: fill at {fill_ts} is outside the tape read "
                        f"({cursor_from} up to {cursor_to}) or after the order stopped resting"
                    )
                before = filled
                filled = min(shares, filled + add)
                if filled > before:
                    gained += 1
                    filled_ts = filled_ts if filled_ts is not None else fill_ts
            state = row["state"]
            if state in RESTING_STATES:
                if filled >= shares - _SHARES_EPS:
                    state = "filled"
                elif filled > _SHARES_EPS:
                    state = "partial"
            await conn.execute(
                """
                UPDATE fade_orders
                   SET flow_cursor_ts = ?, crossed = MAX(crossed, ?), filled_shares = ?,
                       filled_ts = ?, fill_price = ?, state = ?
                 WHERE id = ?
                """,
                (
                    min(cursor_to, resting_until), float(_num(u.crossed) or 0.0), filled,
                    filled_ts, float(row["price"]) if filled > 0.0 else None, state,
                    row["id"],
                ),
            )
            updated += 1
    return FlowResult(updated=updated, gained=gained, stale=stale)


async def open_orders() -> list[dict]:
    """Bids resting now, soonest window first, highest rung first."""
    return await _read(
        f"""
        SELECT o.*, {_WINDOW_COLS_FOR_ORDERS}
          FROM fade_orders o
          JOIN fade_windows w ON w.window_slug = o.window_slug
         WHERE o.state IN ('resting', 'partial')
         ORDER BY w.window_end, o.window_slug, o.kind, o.price DESC, o.id
        """
    )


async def open_positions() -> list[dict]:
    """Filled shares not yet settled, per window and side: what the hedge maths holds."""
    rows = await _read(
        """
        SELECT o.window_slug, w.asset, w.window_end, o.side,
               SUM(o.filled_shares) AS shares,
               SUM(o.filled_shares * o.price) AS cost_usd,
               SUM(CASE WHEN o.kind = 'hedge' THEN o.filled_shares ELSE 0 END) AS hedge_shares
          FROM fade_orders o
          JOIN fade_windows w ON w.window_slug = o.window_slug
         WHERE o.settled_ts IS NULL AND o.filled_shares > 0
         GROUP BY o.window_slug, o.side
         ORDER BY w.window_end, o.window_slug, o.side
        """
    )
    for r in rows:
        r["avg_price"] = r["cost_usd"] / r["shares"] if r["shares"] else None
    return rows


# ---------------------------------------------------------------------------
# Record (profit first)
# ---------------------------------------------------------------------------


async def summary() -> dict:
    """The strategy's record, profit first. Each figure covers only the rows it applies to.

    - ``net_pnl_usd``: settled P&L after fees (resting fills pay none).
    - ``cents_per_share``: net over settled filled shares.
    - ``return_on_staked``: net over what the settled shares cost.
    - ``max_drawdown_usd``: the largest fall from a running peak of cumulative P&L, window by
      window in window-end order (a hedged pair is one bet). Starts from zero, so an opening
      loss counts.
    - ``settled_windows``: windows with at least one filled order that have settled.
    - ``open_exposure_usd``: what the filled but unsettled shares cost (money at risk now).
    - ``resting_usd``: what the unfilled parts of resting bids would cost if they filled.
    - ``hedge_stake_share``: the share of settled stake that went into hedges.

    Ratios are None when nothing has settled, rather than a misleading zero.
    """
    async with _db.connect() as conn:
        settled = await _one(
            conn,
            """
            SELECT COUNT(*) AS orders, COUNT(DISTINCT window_slug) AS windows,
                   COALESCE(SUM(filled_shares), 0) AS shares,
                   COALESCE(SUM(filled_shares * price), 0) AS staked,
                   COALESCE(SUM(pnl), 0) AS pnl,
                   COALESCE(SUM(CASE WHEN kind = 'hedge' THEN filled_shares * price END), 0)
                     AS hedge_staked
              FROM fade_orders
             WHERE settled_ts IS NOT NULL AND filled_shares > 0
            """,
        ) or {}
        by_window = await _rows(
            conn,
            """
            SELECT o.window_slug, SUM(o.pnl) AS pnl
              FROM fade_orders o
              JOIN fade_windows w ON w.window_slug = o.window_slug
             WHERE o.settled_ts IS NOT NULL AND o.filled_shares > 0
             GROUP BY o.window_slug
             ORDER BY MAX(w.window_end), o.window_slug
            """,
        )
        exposure = await _one(
            conn,
            """
            SELECT COALESCE(SUM(filled_shares * price), 0) AS cost,
                   COUNT(DISTINCT window_slug) AS windows
              FROM fade_orders
             WHERE settled_ts IS NULL AND filled_shares > 0
            """,
        ) or {}
        resting = await _one(
            conn,
            """
            SELECT COUNT(*) AS n, COALESCE(SUM((shares - filled_shares) * price), 0) AS usd
              FROM fade_orders WHERE state IN ('resting', 'partial')
            """,
        ) or {}
        counts = await _one(
            conn,
            """
            SELECT COUNT(*) AS placed,
                   COALESCE(SUM(CASE WHEN filled_shares > 0 THEN 1 ELSE 0 END), 0) AS filled
              FROM fade_orders
            """,
        ) or {}
        observed = await _one(
            conn, "SELECT COUNT(*) AS n FROM fade_windows WHERE outcome IS NOT NULL"
        ) or {}

    net = float(settled.get("pnl") or 0.0)
    shares = float(settled.get("shares") or 0.0)
    staked = float(settled.get("staked") or 0.0)
    return {
        "net_pnl_usd": net,
        "cents_per_share": 100.0 * net / shares if shares > 0 else None,
        "return_on_staked": net / staked if staked > 0 else None,
        "max_drawdown_usd": _max_drawdown(float(r["pnl"] or 0.0) for r in by_window),
        "settled_windows": int(settled.get("windows") or 0),
        "settled_orders": int(settled.get("orders") or 0),
        "settled_shares": shares,
        "staked_usd": staked,
        "open_exposure_usd": float(exposure.get("cost") or 0.0),
        "open_windows": int(exposure.get("windows") or 0),
        "resting_usd": float(resting.get("usd") or 0.0),
        "resting_orders": int(resting.get("n") or 0),
        "hedge_stake_share": (
            float(settled.get("hedge_staked") or 0.0) / staked if staked > 0 else None
        ),
        "orders_placed": int(counts.get("placed") or 0),
        "orders_filled": int(counts.get("filled") or 0),
        "windows_observed": int(observed.get("n") or 0),
    }


def _max_drawdown(pnls: Iterable[float]) -> float:
    equity = peak = drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


async def free_cash_usd(start_usd: float) -> float:
    """Bankroll free to bid with: the start, plus settled P&L, minus what open fills cost,
    minus what resting bids would cost if they filled."""
    s = await summary()
    return float(start_usd) + s["net_pnl_usd"] - s["open_exposure_usd"] - s["resting_usd"]


# ---------------------------------------------------------------------------
# Dials
# ---------------------------------------------------------------------------


def _dials(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = {k: v for k, v in row.items() if k != "params_json"}
    out["params"] = _loads(row["params_json"]) or {}
    return out


async def dials() -> dict | None:
    """The newest dials: version, ts, source, params, n_windows, loglik, note. None if unseeded."""
    async with _db.connect() as conn:
        return _dials(await _one(conn, "SELECT * FROM fade_dials ORDER BY version DESC LIMIT 1"))


async def dial_history(n: int = 20) -> list[dict]:
    """The newest ``n`` dial versions, newest first."""
    limit = max(1, min(int(n), RECENT_DECISIONS_MAX))
    rows = await _read("SELECT * FROM fade_dials ORDER BY version DESC LIMIT ?", (limit,))
    return [d for d in (_dials(r) for r in rows) if d is not None]


async def save_dials(
    params: Mapping[str, Any],
    *,
    source: str,
    ts: float,
    n_windows: int = 0,
    loglik: float | None = None,
    note: str | None = None,
) -> int:
    """Store a new dials version (one above the newest) and return it."""
    if not isinstance(params, Mapping) or not params:
        raise ValueError("params must be a non-empty mapping")
    if not source:
        raise ValueError("source is required")
    async with _transaction() as conn:
        row = await _one(conn, "SELECT COALESCE(MAX(version), -1) + 1 AS v FROM fade_dials")
        version = int(row["v"]) if row else 0
        await conn.execute(
            "INSERT INTO fade_dials (version, ts, source, params_json, n_windows, loglik, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (version, _ts(ts), source, _dumps(dict(params)), int(n_windows), _num(loglik), note),
        )
    return version


async def seed_dials(*, ts: float | None = None) -> dict:
    """Store the prior as version 0 if no dials exist yet; return the newest dials."""
    async with _transaction() as conn:
        await conn.execute(
            """
            INSERT INTO fade_dials (version, ts, source, params_json, n_windows, loglik, note)
            SELECT 0, ?, ?, ?, 0, NULL, ?
             WHERE NOT EXISTS (SELECT 1 FROM fade_dials)
            """,
            (
                _ts(time.time() if ts is None else ts), PRIOR_SOURCE,
                _dumps(dict(PRIOR_DIALS)), PRIOR_NOTE,
            ),
        )
    current = await dials()
    if current is None:
        raise RuntimeError("fade_dials is still empty after seeding the prior")
    return current
