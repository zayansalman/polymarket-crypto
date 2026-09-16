"""Shared entry step for strategy position slots (hourly and daily BTC strategies), paper and live.

Moved from polymarket_bot/hourly/engine.py (Claude, 2026-09-16, agreed with the hourly
session) so every strategy engine uses one implementation. The bodies are the hourly ones;
only the window and the decision record are now arguments. Behaviour sources:
- one open position per strategy: Zayan (operator), 2026-09-14;
- one order attempt per window, the SUBMITTING / UNCERTAIN records and the untraced-post
  notice: Claude, 2026-09-15, branch-review findings hourly-ambiguous-post-error-retried and
  hourly-reentry-after-untraced-post;
- a still-open same-window position is linked as ENTERED, in live only while the strategy's
  slot tracks the entry: Claude, 2026-09-15, branch-review finding
  crash-after-entry-marks-missed; open rows only, in both modes: review by Claude session
  polymarket-crypto-95, 2026-09-16;
- sizing on a thin top-of-book ask: Claude, 2026-09-15, branch-review finding
  thin-top-sizing-paper-vs-live.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from db import journal_live_order, notify
from logging_setup import get_logger
from polymarket_exec.execution.gate import EntryRequest
from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("strategy_slot_entry")

# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
# decision-record actions for an entry attempt. SUBMITTING is written before any order
# goes out; UNCERTAIN ends a window whose order may have reached the venue.
SUBMITTING = "SUBMITTING"
UNCERTAIN_PREFIX = "UNCERTAIN:"
UNFINISHED_ATTEMPT = f"{UNCERTAIN_PREFIX}entry attempt did not finish"
# The other decision-record actions this step reads or writes (same words in every ledger).
PENDING = "PENDING"
ENTERED = "ENTERED"
MISSED = "MISSED"


@dataclass(frozen=True)
class SlotWindow:
    """The market window a strategy's entry is for, as paper_positions records it."""

    strategy_id: str
    timeframe: str  # paper_positions.market_timeframe, e.g. "1h" or "1d"
    window_start_ts: int  # paper_positions.window_start_ts: the window's start, UTC seconds


class DecisionHandle(Protocol):
    """One strategy's decision record for one window and mode (each engine adapts its ledger)."""

    async def reason(self) -> str | None: ...

    async def set_action(
        self, action: str, position_id: int | None = None, *, expected_action: str | None = None
    ) -> bool: ...


async def open_row_for(strategy_id: str, mode: str) -> dict[str, Any] | None:
    """This strategy's open row in this mode while its slot is held, else None."""
    from polymarket_bot import paper as P

    async with P.connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions "
            "WHERE state = 'open' AND strategy_id = ? AND mode = ? "
            "ORDER BY position_id DESC LIMIT 1",
            (strategy_id, mode),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _same_window_open_position_id(window: SlotWindow, mode: str) -> int | None:
    """This strategy's still-open position for this window and mode, if the entry went through.

    Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed. Only open rows
    count, in both modes (review by Claude session polymarket-crypto-95, 2026-09-16): a row
    already closed (sold at Stop, or closed by boot reconciliation) can't show whether a live
    order filled, so paper and live both leave that window as an unfinished attempt.
    """
    from polymarket_bot import paper as P

    async with P.connect() as db:
        async with db.execute(
            "SELECT position_id FROM paper_positions "
            "WHERE state = 'open' AND strategy_id = ? AND market_timeframe = ? "
            "AND window_start_ts = ? AND mode = ? "
            "ORDER BY position_id DESC LIMIT 1",
            (window.strategy_id, window.timeframe, window.window_start_ts, mode),
        ) as cur:
            row = await cur.fetchone()
    return int(row["position_id"]) if row else None


async def insert_row(
    snapshot: PaperSnapshot, window: SlotWindow, *, side: str, price: float, notional: float,
    shares: float, reason: str | None, mode: str,
) -> int:
    from polymarket_bot import paper as P

    async with P.connect() as db:
        cur = await db.execute(
            """
            INSERT INTO paper_positions(
              opened_at, window_slug, market_question, side, state, entry_price,
              notional_usd, shares, opened_spot, confidence, edge, entry_reason,
              feed_source, quote_source, strategy_style, mode, strategy_id,
              market_timeframe, window_start_ts
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, 0, 0, ?, ?, 'clob', 'settle', ?, ?, ?, ?)
            """,
            (snapshot.created_at, snapshot.window_slug, snapshot.market_question, side, price,
             notional, shares, snapshot.spot_price, reason, snapshot.feed_source, mode,
             window.strategy_id, window.timeframe, window.window_start_ts),
        )
        position_id = int(cur.lastrowid or 0)
        await db.commit()
    return position_id


async def enter(
    snapshot: PaperSnapshot, window: SlotWindow, side: str, decision: DecisionHandle
) -> None:
    from polymarket_bot import paper as P

    strategy_id = window.strategy_id
    executor = P._live_executor
    mode = "live" if executor is not None else "paper"
    if await open_row_for(strategy_id, mode) is not None:
        return  # this strategy's slot is still held (previous window settling): retry next tick
    ask = snapshot.up_best_ask if side == "Up" else snapshot.down_best_ask
    top = snapshot.up_ask_size if side == "Up" else snapshot.down_ask_size
    if ask is None or ask <= 0 or (top is not None and top <= 0):
        return  # no executable ask this tick: retry until the deadline
    gate = P._risk_gate
    shares = gate.trade_shares if gate is not None else DEFAULT_MIN_ORDER_SIZE
    # Cap to the top-of-book ask size first, then raise to the venue minimum, so a
    # thin top level (e.g. 3 shares) still sizes to the 5 shares live will post:
    # paper and live gate on, book and count the same notional.
    # Claude, 2026-09-15, branch-review finding thin-top-sizing-paper-vs-live
    if top is not None:
        shares = min(shares, top)
    shares = max(shares, DEFAULT_MIN_ORDER_SIZE)
    notional = shares * ask
    token = snapshot.up_token_id if side == "Up" else snapshot.down_token_id
    reason = await decision.reason()
    # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
    # record the attempt before any order goes out, in both modes. advance only
    # enters from PENDING, so a tick that dies mid-attempt (or a restart within the
    # window) can never place a second order for this window.
    await decision.set_action(SUBMITTING)
    if executor is not None:
        slot = executor.slot_executor(strategy_id)
        # The ledger says this strategy is flat in live: heal any phantom slot
        # state an interrupted stop left behind (same as the legacy loop, #91).
        await slot.resync_flat()
        # Row first, then the real order (the legacy loop's ordering): a failed
        # submit deletes the row, and a crash after submit leaves a row that boot
        # reconciliation adopts from the journal. RiskGate runs inside submit_entry.
        # Claude, 2026-09-15, branch-review finding hourly-reentry-after-untraced-post:
        # a crash between the post and its journal write leaves no journal entry, so
        # reconciliation closes the row instead; the SUBMITTING record above is what
        # stops a second post for this window.
        position_id = await insert_row(
            snapshot, window, side=side, price=ask, notional=notional,
            shares=shares, reason=reason, mode=mode,
        )
        result = await slot.submit_entry(
            token_id=token, side_price=ask, notional_usd=notional,
            window_slug=snapshot.window_slug,
        )
        if not result.ok:
            await P._delete_position_row(position_id)
            if result.status == "BLOCKED":
                # Refused before any post (gate, no token id, venue minimum, kill
                # switch): nothing reached the venue.
                await decision.set_action(f"BLOCKED:{result.reason}")
                log.warning("strategy_slot_entry.live_entry_not_placed", strategy_id=strategy_id,
                            timeframe=window.timeframe, status=result.status,
                            reason=result.reason)
                return
            # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
            # every other failure happened at or after the post (an exception or timeout,
            # or a response without success and an order id). The venue may still have
            # accepted the order, and a re-post is a new order, so the window ends here.
            uncertain = f"{UNCERTAIN_PREFIX}{result.status} {result.reason}".strip()
            await decision.set_action(uncertain)
            await notify(
                "live_entry_uncertain",
                f"LIVE entry for {strategy_id} may or may not be on Polymarket "
                f"({result.status}: {result.reason}). No retry for this market; check the "
                "account's open orders and trades.",
                {"window_slug": snapshot.window_slug, "strategy_id": strategy_id,
                 "token_id": token},
            )
            log.warning("strategy_slot_entry.live_entry_outcome_unknown",
                        strategy_id=strategy_id, timeframe=window.timeframe,
                        status=result.status, reason=result.reason)
            return
        ask = result.price or ask
        notional = result.notional_usd or notional
        shares = result.size or notional / ask
        await P._update_position_terms(position_id, ask, notional, shares)
    else:
        if gate is not None:
            blocked = gate.block_reason(EntryRequest(
                notional_usd=notional, position_open=False, entry_order_resting=False,
                side_price=ask, best_ask=ask,
            ))
            if blocked is not None:
                await journal_live_order(
                    intent="ENTRY", side="BUY", status="BLOCKED",
                    window_slug=snapshot.window_slug, token_id=token, price=ask, size=shares,
                    notional_usd=notional, error=blocked, mode="paper",
                    strategy_id=strategy_id,
                )
                await decision.set_action(f"BLOCKED:{blocked}")
                log.info("strategy_slot_entry.entry_blocked", strategy_id=strategy_id,
                         timeframe=window.timeframe, reason=blocked)
                return
        position_id = await insert_row(
            snapshot, window, side=side, price=ask, notional=notional,
            shares=shares, reason=reason, mode=mode,
        )
        if gate is not None:
            await gate.record_buy_notional(round(ask * shares, 4))
    await decision.set_action(ENTERED, position_id)
    label = "LIVE" if executor is not None else "Paper"
    await notify(
        f"{mode}_entry",
        f"{label} BUY {side} ${notional:.2f} @ {ask:.3f} ({strategy_id})",
        {"window_slug": snapshot.window_slug, "strategy_id": strategy_id},
    )
    log.info("strategy_slot_entry.entered", mode=mode, strategy_id=strategy_id,
             timeframe=window.timeframe, side=side, price=ask, shares=shares,
             window_slug=snapshot.window_slug)


async def advance(
    snapshot: PaperSnapshot, window: SlotWindow, *, action: str, side: str | None, now: int,
    deadline_s: int, allow_entries: bool, mode: str, decision: DecisionHandle,
) -> None:
    """One entry step for a strategy's recorded decision in this window and mode.

    The checks run in the hourly engine's order after the branch-review fixes:
    1. PENDING or SUBMITTING, and this window's position is still open: record ENTERED with
       it (in live, only while the strategy's slot tracks the entry);
    2. SUBMITTING: tell the operator, then record the unfinished attempt;
    3. any action other than PENDING: nothing to do;
    4. past the entry deadline: MISSED;
    5. entries held (kill switch): nothing, the decision stays PENDING;
    6. otherwise: enter.
    """
    from polymarket_bot import paper as P

    strategy_id = window.strategy_id
    executor = P._live_executor
    if action in (PENDING, SUBMITTING):
        # Claude, 2026-09-15, branch-review finding crash-after-entry-marks-missed: the
        # entry went through but the tick stopped before recording ENTERED (the ledger
        # write failed, or the bot restarted and boot reconciliation adopted the fill).
        # Link the window to that position, before the deadline check, instead of
        # recording MISSED or an unfinished attempt. Live also needs the strategy's
        # slot to track the entry, so an order whose outcome is unknown still ends
        # the window as an unfinished attempt below.
        position_id = await _same_window_open_position_id(window, mode)
        if position_id is not None and (
            executor is None or executor.slot_executor(strategy_id).tracks_position
        ):
            await decision.set_action(ENTERED, position_id, expected_action=action)
            log.warning("strategy_slot_entry.entry_linked_after_interrupted_tick",
                        strategy_id=strategy_id, timeframe=window.timeframe,
                        position_id=position_id, previous_action=action,
                        window_slug=snapshot.window_slug)
            return
    if action == SUBMITTING:
        # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
        # an earlier tick started an entry and never recorded its result (it raised,
        # or the process stopped). Close the window out; never enter again.
        # Claude, 2026-09-15, branch-review finding hourly-reentry-after-untraced-post:
        # a live post can land right before its journal write fails. Boot reconciliation
        # then closes that row as RECONCILED_NO_LIVE_TRACE and nothing tracks the
        # tokens, so tell the operator before closing the window (notify first: if the
        # record write fails, the next tick still finds SUBMITTING and repeats both).
        await notify(
            "entry_attempt_unfinished",
            f"Entry attempt for {strategy_id} ({snapshot.window_slug}) did not finish: the tick "
            "failed or the bot stopped mid-order. No retry for this market. If the bot was "
            "LIVE, an order may be on Polymarket with no ledger row; check the "
            "account's open orders and trades.",
            {"window_slug": snapshot.window_slug, "strategy_id": strategy_id},
        )
        await decision.set_action(UNFINISHED_ATTEMPT, expected_action=SUBMITTING)
        return
    if action != PENDING:
        return
    if now - window.window_start_ts > deadline_s:
        await decision.set_action(MISSED)
        return
    if allow_entries:
        await enter(snapshot, window, str(side), decision)
