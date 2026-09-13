"""Paper execution manager — explicit order lifecycle with SQLite persistence.

The :class:`PaperExecutionManager` implements the full order state machine:
PENDING → ACKNOWLEDGED → FILLED (or PARTIAL_FILL, CANCELLED, REJECTED).
Every transition is persisted to an audit table.
"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from polymarket_exec.core.interfaces import AbstractExecutionManager
from polymarket_exec.core.types import (
    ExitReason,
    MarketWindow,
    OrderState,
    PaperOrder,
    PaperPosition,
    Side,
    Signal,
    SignalAction,
    Tick,
)

# Default risk / execution constants (mirrors config.py)
_DEFAULT_TARGET_RETURN = 0.10
_DEFAULT_STOP_RETURN = -0.08
_DEFAULT_TIME_EXIT_SECONDS = 45
_DEFAULT_ENTRY_EDGE_MIN = 0.045


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    window_slug     TEXT    NOT NULL,
    side            TEXT    NOT NULL,
    state           TEXT    NOT NULL,
    requested_notional REAL NOT NULL,
    filled_notional REAL    NOT NULL DEFAULT 0.0,
    entry_price     REAL    NOT NULL,
    confidence      REAL    NOT NULL,
    edge            REAL    NOT NULL,
    feed_source     TEXT    NOT NULL DEFAULT 'unknown'
);
CREATE INDEX IF NOT EXISTS idx_orders_window ON paper_orders(window_slug);
CREATE INDEX IF NOT EXISTS idx_orders_state   ON paper_orders(state);

CREATE TABLE IF NOT EXISTS paper_positions (
    position_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES paper_orders(order_id),
    opened_at       TEXT    NOT NULL,
    closed_at       TEXT,
    state           TEXT    NOT NULL DEFAULT 'open',
    exit_price      REAL,
    exit_reason     TEXT,
    realized_pnl_usd REAL
);
CREATE INDEX IF NOT EXISTS idx_positions_state ON paper_positions(state);
CREATE INDEX IF NOT EXISTS idx_positions_order ON paper_positions(order_id);

CREATE TABLE IF NOT EXISTS order_state_transitions (
    transition_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES paper_orders(order_id),
    from_state      TEXT    NOT NULL,
    to_state        TEXT    NOT NULL,
    transitioned_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_order ON order_state_transitions(order_id);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _price_for_side(window: MarketWindow, side: Side) -> float:
    return window.up_price if side is Side.UP else window.down_price


def _current_price_for_tick(tick: Tick, side: Side) -> float:
    return tick.window.up_price if side is Side.UP else tick.window.down_price


def _pnl_for_position(position: PaperPosition, exit_price: float) -> float:
    """Unrealised PnL = shares * (exit_price - entry_price)."""
    entry_price = position.order.entry_price
    notional = position.order.filled_notional
    if entry_price <= 0 or notional <= 0:
        return 0.0
    shares = notional / entry_price
    return shares * (exit_price - entry_price)


# ---------------------------------------------------------------------------
# PaperExecutionManager
# ---------------------------------------------------------------------------

class PaperExecutionManager(AbstractExecutionManager):
    """Manages paper orders through an explicit lifecycle state machine.

    Parameters
    ----------
    db_pool_or_path:
        Either a ``Path`` to a SQLite file, or an existing
        ``aiosqlite.Connection`` pool.
    max_open_positions:
        Maximum number of concurrent open positions (default 1).
    latency_sim_ms:
        Simulated latency in milliseconds between order submission
        and acknowledgement (default 0.0 = no latency).
    rng:
        Random source for fill simulation (partial-fill / reject rolls).
        Pass a seeded or deterministic ``random.Random`` for reproducible
        fills in tests; defaults to a fresh unseeded ``random.Random()``.
    """

    def __init__(
        self,
        db_pool_or_path: Path | str | aiosqlite.Connection,
        max_open_positions: int = 1,
        latency_sim_ms: float = 0.0,
        target_return: float = _DEFAULT_TARGET_RETURN,
        stop_return: float = _DEFAULT_STOP_RETURN,
        time_exit_seconds: int = _DEFAULT_TIME_EXIT_SECONDS,
        entry_edge_min: float = _DEFAULT_ENTRY_EDGE_MIN,
        rng: random.Random | None = None,
    ) -> None:
        self._db_path: Optional[str] = None
        self._db: Optional[aiosqlite.Connection] = None
        self._owns_db = True

        if isinstance(db_pool_or_path, (Path, str)):
            self._db_path = str(db_pool_or_path)
        elif isinstance(db_pool_or_path, aiosqlite.Connection):
            self._db = db_pool_or_path
            self._owns_db = False

        self._rng = rng if rng is not None else random.Random()
        self.max_open_positions = max_open_positions
        self.latency_sim_ms = latency_sim_ms
        self.target_return = target_return
        self.stop_return = stop_return
        self.time_exit_seconds = time_exit_seconds
        self.entry_edge_min = entry_edge_min

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def init(self) -> None:
        """Create tables and indices. Safe to call multiple times."""
        db = await self._ensure_db()
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(_SCHEMA)
        await db.commit()

    async def close(self) -> None:
        """Close the underlying DB connection if we own it."""
        if self._owns_db and self._db is not None:
            await self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db
        if self._db_path is None:
            raise RuntimeError("No database configured")
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        return self._db

    async def _persist_transition(
        self,
        db: aiosqlite.Connection,
        order_id: int,
        from_state: OrderState,
        to_state: OrderState,
    ) -> None:
        await db.execute(
            """
            INSERT INTO order_state_transitions
                (order_id, from_state, to_state, transitioned_at)
            VALUES (?, ?, ?, ?)
            """,
            (order_id, from_state.name, to_state.name, _utc_now_iso()),
        )

    async def _update_order_state(
        self,
        db: aiosqlite.Connection,
        order_id: int,
        new_state: OrderState,
        filled_notional: Optional[float] = None,
    ) -> None:
        if filled_notional is not None:
            await db.execute(
                """
                UPDATE paper_orders
                SET state = ?, filled_notional = ?
                WHERE order_id = ?
                """,
                (new_state.name, filled_notional, order_id),
            )
        else:
            await db.execute(
                "UPDATE paper_orders SET state = ? WHERE order_id = ?",
                (new_state.name, order_id),
            )

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit_order(self, signal: Signal, window: MarketWindow) -> PaperOrder:
        """Submit an order derived from *signal* for *window*.

        Full lifecycle: PENDING → ACKNOWLEDGED → FILLED
        (or PARTIAL_FILL, or CANCELLED for SKIP signals).

        Returns:
            A :class:`PaperOrder` in its final state.
        """
        db = await self._ensure_db()

        # If signal is SKIP, create a CANCELLED order immediately
        if signal.action is SignalAction.SKIP:
            return await self._create_cancelled_order(db, signal, window)

        side = signal.side
        assert side is not None, "ENTER_UP/ENTER_DOWN must have a side"

        entry_price = _price_for_side(window, side)
        created_at = _utc_now()
        created_at_iso = created_at.isoformat(timespec="seconds")

        # 1. Create order in PENDING state
        cursor = await db.execute(
            """
            INSERT INTO paper_orders
                (created_at, window_slug, side, state, requested_notional,
                 filled_notional, entry_price, confidence, edge, feed_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at_iso,
                window.slug,
                side.value,
                OrderState.PENDING.name,
                signal.notional_usd,
                0.0,
                entry_price,
                signal.confidence,
                signal.edge,
                "binance",  # default feed source
            ),
        )
        order_id = cursor.lastrowid
        assert order_id is not None

        order = PaperOrder(
            order_id=order_id,
            created_at=created_at,
            window_slug=window.slug,
            side=side,
            state=OrderState.PENDING,
            requested_notional=signal.notional_usd,
            filled_notional=0.0,
            entry_price=entry_price,
            confidence=signal.confidence,
            edge=signal.edge,
            feed_source="binance",
        )

        # Persist PENDING transition
        await self._persist_transition(db, order_id, OrderState.PENDING, OrderState.PENDING)

        # 2. (Optional) Simulate latency
        if self.latency_sim_ms > 0:
            await asyncio.sleep(self.latency_sim_ms / 1000.0)

        # 3. Transition to ACKNOWLEDGED
        await self._update_order_state(db, order_id, OrderState.ACKNOWLEDGED)
        await self._persist_transition(
            db, order_id, OrderState.PENDING, OrderState.ACKNOWLEDGED
        )
        order.state = OrderState.ACKNOWLEDGED

        # 4. Determine fill: full fill (default), partial fill (rare), or no-fill
        fill_state, filled_notional = self._determine_fill(signal)

        # 5. Transition to final fill state
        await self._update_order_state(db, order_id, fill_state, filled_notional)
        await self._persist_transition(
            db, order_id, OrderState.ACKNOWLEDGED, fill_state
        )
        order.state = fill_state
        order.filled_notional = filled_notional

        await db.commit()

        # 6. If FILLED, create a PaperPosition
        if fill_state is OrderState.FILLED:
            await self._create_position(db, order)

        return order

    async def _create_cancelled_order(
        self, db: aiosqlite.Connection, signal: Signal, window: MarketWindow
    ) -> PaperOrder:
        """Create a CANCELLED order for a SKIP signal."""
        created_at = _utc_now()
        created_at_iso = created_at.isoformat(timespec="seconds")

        side = signal.side if signal.side is not None else Side.UP
        entry_price = _price_for_side(window, side)

        cursor = await db.execute(
            """
            INSERT INTO paper_orders
                (created_at, window_slug, side, state, requested_notional,
                 filled_notional, entry_price, confidence, edge, feed_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at_iso,
                window.slug,
                side.value,
                OrderState.CANCELLED.name,
                signal.notional_usd,
                0.0,
                entry_price,
                signal.confidence,
                signal.edge,
                "binance",
            ),
        )
        order_id = cursor.lastrowid
        assert order_id is not None

        await self._persist_transition(db, order_id, OrderState.CANCELLED, OrderState.CANCELLED)
        await db.commit()

        return PaperOrder(
            order_id=order_id,
            created_at=created_at,
            window_slug=window.slug,
            side=side,
            state=OrderState.CANCELLED,
            requested_notional=signal.notional_usd,
            filled_notional=0.0,
            entry_price=entry_price,
            confidence=signal.confidence,
            edge=signal.edge,
            feed_source="binance",
        )

    def _determine_fill(self, signal: Signal) -> tuple[OrderState, float]:
        """Determine fill outcome: full fill, partial fill, or no-fill.

        Returns:
            (final_state, filled_notional)
        """
        # Default: full fill
        # Rare (1%): partial fill
        # Very rare (0.1%): no-fill (rejected)
        roll = self._rng.random()
        if roll < 0.001:
            return OrderState.REJECTED, 0.0
        if roll < 0.01:
            partial = round(signal.notional_usd * self._rng.uniform(0.1, 0.5), 2)
            return OrderState.PARTIAL_FILL, partial
        return OrderState.FILLED, signal.notional_usd

    async def _create_position(
        self, db: aiosqlite.Connection, order: PaperOrder
    ) -> PaperPosition:
        """Create an open PaperPosition linked to a FILLED order."""
        opened_at = _utc_now()
        opened_at_iso = opened_at.isoformat(timespec="seconds")

        cursor = await db.execute(
            """
            INSERT INTO paper_positions
                (order_id, opened_at, state)
            VALUES (?, ?, ?)
            """,
            (order.order_id, opened_at_iso, "open"),
        )
        position_id = cursor.lastrowid
        assert position_id is not None

        await db.commit()

        position = PaperPosition(
            position_id=position_id,
            order=order,
            opened_at=opened_at,
        )
        return position

    # ------------------------------------------------------------------
    # Exit checking
    # ------------------------------------------------------------------

    async def check_exits(
        self, position: PaperPosition, tick: Tick
    ) -> Optional[ExitReason]:
        """Evaluate whether *position* should be exited given the latest *tick*.

        Checks are evaluated in priority order:
        1. WINDOW_ROLL — position window != tick window
        2. TIME — remaining seconds <= time_exit_seconds
        3. TARGET — pnl >= notional * target_return
        4. STOP — pnl <= notional * stop_return
        5. BAND_REENTRY — |edge| < entry_edge_min / 2

        Returns:
            The :class:`ExitReason` if the position should close, else ``None``.
        """
        # 1. WINDOW_ROLL: position from a different window
        if position.order.window_slug != tick.window.slug:
            return ExitReason.WINDOW_ROLL

        # Compute remaining seconds from window end
        remaining_seconds = int(tick.window.end_ts - tick.ts.timestamp())

        # Compute current PnL
        exit_price = _current_price_for_tick(tick, position.order.side)
        pnl = _pnl_for_position(position, exit_price)
        notional = position.order.filled_notional

        # 2. TIME: too little time remaining
        if remaining_seconds <= self.time_exit_seconds:
            return ExitReason.TIME

        # 3. TARGET: profit target hit
        if pnl >= notional * self.target_return:
            return ExitReason.TARGET

        # 4. STOP: stop loss hit
        if pnl <= notional * self.stop_return:
            return ExitReason.STOP

        # 5. BAND_REENTRY: edge has decayed below half the entry threshold
        if abs(tick.signal.edge) < self.entry_edge_min / 2:
            return ExitReason.BAND_REENTRY

        return None

    # ------------------------------------------------------------------
    # Force close
    # ------------------------------------------------------------------

    async def force_close_all(self, reason: ExitReason) -> list[PaperPosition]:
        """Close ALL open positions immediately at their entry price
        (marking PnL as 0 for forced closes).

        Returns:
            The list of positions that were closed.
        """
        db = await self._ensure_db()
        positions = await self._fetch_open_positions(db)
        closed: list[PaperPosition] = []

        now = _utc_now()
        now_iso = now.isoformat(timespec="seconds")

        for pos in positions:
            await db.execute(
                """
                UPDATE paper_positions
                SET state = 'closed', closed_at = ?,
                    exit_price = ?, exit_reason = ?, realized_pnl_usd = ?
                WHERE position_id = ?
                """,
                (now_iso, pos.order.entry_price, reason.value, 0.0, pos.position_id),
            )
            pos.closed_at = now
            pos.exit_price = pos.order.entry_price
            pos.exit_reason = reason
            pos.realized_pnl_usd = 0.0
            closed.append(pos)

        await db.commit()
        return closed

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_open_positions(self) -> list[PaperPosition]:
        """Return all positions whose state is ``open``."""
        db = await self._ensure_db()
        return await self._fetch_open_positions(db)

    async def get_position(self, position_id: int) -> Optional[PaperPosition]:
        """Fetch a single position by ID, or ``None`` if not found."""
        db = await self._ensure_db()
        return await self._fetch_position_by_id(db, position_id)

    async def get_closed_positions(self, limit: int = 50) -> list[PaperPosition]:
        """Return the most recently closed positions."""
        db = await self._ensure_db()
        cursor = await db.execute(
            """
            SELECT p.*, o.created_at AS o_created_at, o.window_slug, o.side,
                   o.state AS o_state, o.requested_notional, o.filled_notional,
                   o.entry_price, o.confidence, o.edge, o.feed_source
            FROM paper_positions p
            JOIN paper_orders o ON p.order_id = o.order_id
            WHERE p.state = 'closed'
            ORDER BY p.closed_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_position(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal queries
    # ------------------------------------------------------------------

    async def _fetch_open_positions(
        self, db: aiosqlite.Connection
    ) -> list[PaperPosition]:
        cursor = await db.execute(
            """
            SELECT p.*, o.created_at AS o_created_at, o.window_slug, o.side,
                   o.state AS o_state, o.requested_notional, o.filled_notional,
                   o.entry_price, o.confidence, o.edge, o.feed_source
            FROM paper_positions p
            JOIN paper_orders o ON p.order_id = o.order_id
            WHERE p.state = 'open'
            ORDER BY p.opened_at ASC
            """
        )
        rows = await cursor.fetchall()
        return [self._row_to_position(row) for row in rows]

    async def _fetch_position_by_id(
        self, db: aiosqlite.Connection, position_id: int
    ) -> Optional[PaperPosition]:
        cursor = await db.execute(
            """
            SELECT p.*, o.created_at AS o_created_at, o.window_slug, o.side,
                   o.state AS o_state, o.requested_notional, o.filled_notional,
                   o.entry_price, o.confidence, o.edge, o.feed_source
            FROM paper_positions p
            JOIN paper_orders o ON p.order_id = o.order_id
            WHERE p.position_id = ?
            """,
            (position_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_position(row) if row else None

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    def _row_to_position(self, row: aiosqlite.Row) -> PaperPosition:
        """Reconstruct a :class:`PaperPosition` from a joined query row."""
        # Parse order created_at
        o_created = row["o_created_at"]
        if isinstance(o_created, str):
            o_created_dt = datetime.fromisoformat(o_created)
        else:
            o_created_dt = datetime.fromtimestamp(float(o_created), tz=UTC)

        # Parse position opened_at
        opened = row["opened_at"]
        if isinstance(opened, str):
            opened_dt = datetime.fromisoformat(opened)
        else:
            opened_dt = datetime.fromtimestamp(float(opened), tz=UTC)

        # Parse closed_at
        closed_at: Optional[datetime] = None
        closed_raw = row["closed_at"]
        if closed_raw is not None:
            if isinstance(closed_raw, str):
                closed_at = datetime.fromisoformat(closed_raw)
            else:
                closed_at = datetime.fromtimestamp(float(closed_raw), tz=UTC)

        # Parse exit_reason
        exit_reason: Optional[ExitReason] = None
        er_raw = row["exit_reason"]
        if er_raw is not None:
            try:
                exit_reason = ExitReason(er_raw)
            except ValueError:
                pass

        # Parse order state
        order_state = OrderState[row["o_state"]]

        order = PaperOrder(
            order_id=row["order_id"],
            created_at=o_created_dt,
            window_slug=row["window_slug"],
            side=Side(row["side"]),
            state=order_state,
            requested_notional=row["requested_notional"],
            filled_notional=row["filled_notional"],
            entry_price=row["entry_price"],
            confidence=row["confidence"],
            edge=row["edge"],
            feed_source=row["feed_source"],
        )

        return PaperPosition(
            position_id=row["position_id"],
            order=order,
            opened_at=opened_dt,
            closed_at=closed_at,
            exit_price=row["exit_price"],
            exit_reason=exit_reason,
            realized_pnl_usd=row["realized_pnl_usd"],
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def get_order_transition_history(self, order_id: int) -> list[dict]:
        """Return the full state-transition audit log for an order."""
        db = await self._ensure_db()
        cursor = await db.execute(
            """
            SELECT transition_id, order_id, from_state, to_state, transitioned_at
            FROM order_state_transitions
            WHERE order_id = ?
            ORDER BY transition_id ASC
            """,
            (order_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_order_count(self) -> int:
        """Total number of orders."""
        db = await self._ensure_db()
        cursor = await db.execute("SELECT COUNT(*) FROM paper_orders")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_position_count(self) -> int:
        """Total number of positions."""
        db = await self._ensure_db()
        cursor = await db.execute("SELECT COUNT(*) FROM paper_positions")
        row = await cursor.fetchone()
        return row[0] if row else 0
