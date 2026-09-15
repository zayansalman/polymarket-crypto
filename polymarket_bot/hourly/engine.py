"""Hourly BTC engine: one decision per hour per strategy, per-strategy entries, Binance settlement.

Called by ``polymarket_bot.paper.paper_tick_once`` when the loop was started on the BTC 1h
selection. Strategies never read mode; entries go through the same RiskGate as every other
entry. Each strategy owns one open-position slot (operator decision 2026-09-14).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from db import journal_live_order, notify
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot.hourly import ledger, market, mean_reversion
from polymarket_bot.hourly.market import HOUR_S, HourMarket
from polymarket_exec.execution.gate import EntryRequest
from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("hourly_engine")

TIMEFRAME = "1h"
_ET = ZoneInfo("America/New_York")
_CANDLES = mean_reversion.WINDOW + 2

# (strategy_id, enable knob). Kronos BTC Fine Tune joins in PR 2.
STRATEGIES: tuple[tuple[str, str], ...] = (
    (mean_reversion.STRATEGY_ID, "hourly_mean_reversion_enabled"),
)

# Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
# decision-record actions for an entry attempt. SUBMITTING is written before any order
# goes out; UNCERTAIN ends an hour whose order may have reached the venue.
SUBMITTING = "SUBMITTING"
UNCERTAIN_PREFIX = "UNCERTAIN:"
UNFINISHED_ATTEMPT = f"{UNCERTAIN_PREFIX}entry attempt did not finish"

_market_cache: dict[int, HourMarket] = {}
_open_cache: dict[int, float] = {}


def reset_caches() -> None:
    _market_cache.clear()
    _open_cache.clear()


async def _market_for(client: httpx.AsyncClient, start_ts: int) -> HourMarket:
    if start_ts not in _market_cache:
        found = await market.discover(client, start_ts)
        if found is None:
            raise RuntimeError(f"Could not discover the BTC hourly market {market.slug_for(start_ts)}")
        _market_cache.clear()
        _market_cache[start_ts] = found
    return _market_cache[start_ts]


async def _hour_open(client: httpx.AsyncClient, start_ts: int, now_ms: int) -> float | None:
    if start_ts not in _open_cache:
        candle = await market.fetch_hour_candle(client, start_ts, now_ms)
        if candle is None:
            return None
        _open_cache.clear()
        _open_cache[start_ts] = candle.open
    return _open_cache[start_ts]


async def build_snapshot(client: httpx.AsyncClient, now: int | None = None) -> PaperSnapshot:
    from polymarket_bot import paper as P

    now = P._now() if now is None else now
    start = market.hour_start(now)
    m = await _market_for(client, start)
    up_book = await P._fetch_clob_book(client, m.up_token_id)
    down_book = await P._fetch_clob_book(client, m.down_token_id)
    hour_open = await _hour_open(client, start, now * 1000)
    spot = await market.fetch_spot(client)
    return P.PaperSnapshot(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_slug=m.slug,
        market_question=m.question,
        remaining_seconds=max(0, start + HOUR_S - now),
        spot_price=spot or 0.0,
        reference_price=hour_open or 0.0,
        sigma_per_second=0.0,
        market_up_price=up_book.best_ask,
        market_down_price=down_book.best_ask,
        fair_up_prob=0.5,
        edge=0.0,
        signal_side=None,
        confidence=0.0,
        notional_usd=0.0,
        reason="hourly: waiting",
        feed_source=(
            f"spot={'binance_rest' if spot else 'unavailable'};"
            f"ref={'binance_kline' if hour_open else 'unavailable'};vol=none;quotes=clob"
        ),
        up_token_id=m.up_token_id,
        down_token_id=m.down_token_id,
        up_best_bid=up_book.best_bid,
        up_best_ask=up_book.best_ask,
        up_bid_size=up_book.bid_size,
        up_ask_size=up_book.ask_size,
        down_best_bid=down_book.best_bid,
        down_best_ask=down_book.best_ask,
        down_bid_size=down_book.bid_size,
        down_ask_size=down_book.ask_size,
        quote_source="clob",
        feed_degraded=hour_open is None,
    )


def _factors(start_ts: int) -> dict[str, Any]:
    utc = datetime.fromtimestamp(start_ts, UTC)
    return {
        "us_open_hour": utc.astimezone(_ET).hour == 9,
        "expiry_08utc": utc.hour == 8,
        "weekend": utc.weekday() >= 5,
    }


async def decide_hour(
    client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int
) -> dict[str, dict[str, Any]]:
    """Record this hour's decision for every enabled strategy (once), return the rows."""
    from polymarket_bot import paper as P

    start = market.hour_start(now)
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(snapshot.window_slug, sid)
        if row is None:
            pending.append(sid)
        else:
            rows[sid] = row
    if not pending or snapshot.reference_price <= 0:
        return rows
    now_ms = now * 1000
    spot = await market.fetch_closed_candles(
        client, market="spot", symbol="BTCUSDT", now_ms=now_ms, limit=_CANDLES
    )
    perp = await market.fetch_closed_candles(
        client, market="perp", symbol="BTCUSDT", now_ms=now_ms, limit=_CANDLES
    )
    prev_ms = (start - HOUR_S) * 1000
    if not (
        len(spot) >= mean_reversion.WINDOW and len(perp) >= mean_reversion.WINDOW
        and spot[-1].open_time_ms == prev_ms and perp[-1].open_time_ms == prev_ms
    ):
        log.info("hourly_engine.waiting_for_previous_hour", window_slug=snapshot.window_slug)
        return rows
    late = now - start > _knobs.cached("hourly_entry_deadline_seconds")
    mode = "live" if P._live_executor is not None else "paper"
    for sid in pending:
        if sid == mean_reversion.STRATEGY_ID:
            d = mean_reversion.decide(spot, perp)
        else:  # pragma: no cover - PR 2 adds Kronos
            continue
        await ledger.record_decision(
            strategy_id=sid, window_slug=snapshot.window_slug, window_start_ts=start,
            side=d.side, reason=d.reason, signal=d.signal, factors=_factors(start),
            up_bid=snapshot.up_best_bid, up_ask=snapshot.up_best_ask,
            down_bid=snapshot.down_best_bid, down_ask=snapshot.down_best_ask,
            hour_open=snapshot.reference_price, mode=mode, late=late,
        )
        rows[sid] = await ledger.get_decision(snapshot.window_slug, sid) or {}
    return rows


async def _open_row_for(strategy_id: str, mode: str) -> bool:
    """True while this strategy's slot in this mode still holds an open row."""
    from polymarket_bot import paper as P

    async with P.connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM paper_positions "
            "WHERE state = 'open' AND strategy_id = ? AND mode = ?",
            (strategy_id, mode),
        ) as cur:
            return bool((await cur.fetchone())["n"])


async def _insert_row(
    snapshot: PaperSnapshot, *, strategy_id: str, side: str, price: float, notional: float,
    shares: float, start: int, reason: str | None, mode: str,
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
             strategy_id, TIMEFRAME, start),
        )
        position_id = int(cur.lastrowid or 0)
        await db.commit()
    return position_id


async def _enter(snapshot: PaperSnapshot, strategy_id: str, side: str, start: int) -> None:
    from polymarket_bot import paper as P

    executor = P._live_executor
    mode = "live" if executor is not None else "paper"
    if await _open_row_for(strategy_id, mode):
        return  # this strategy's slot is still held (previous hour settling): retry next tick
    ask = snapshot.up_best_ask if side == "Up" else snapshot.down_best_ask
    top = snapshot.up_ask_size if side == "Up" else snapshot.down_ask_size
    if ask is None or ask <= 0 or (top is not None and top <= 0):
        return  # no executable ask this tick: retry until the deadline
    gate = P._risk_gate
    shares = max(gate.trade_shares if gate is not None else DEFAULT_MIN_ORDER_SIZE,
                 DEFAULT_MIN_ORDER_SIZE)
    if top is not None:
        shares = min(shares, top)
    notional = shares * ask
    token = snapshot.up_token_id if side == "Up" else snapshot.down_token_id
    reason = (await ledger.get_decision(snapshot.window_slug, strategy_id) or {}).get(
        "decision_reason")
    # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
    # record the attempt before any order goes out, in both modes. open_entries only
    # enters from PENDING, so a tick that dies mid-attempt (or a restart within the
    # hour) can never place a second order for this hour.
    await ledger.set_action(snapshot.window_slug, strategy_id, SUBMITTING)
    if executor is not None:
        slot = executor.slot_executor(strategy_id)
        # The ledger says this strategy is flat in live: heal any phantom slot
        # state an interrupted stop left behind (same as the legacy loop, #91).
        await slot.resync_flat()
        # Row first, then the real order (the legacy loop's ordering): a failed
        # submit deletes the row, and a crash after submit leaves a row that boot
        # reconciliation adopts from the journal. RiskGate runs inside submit_entry.
        position_id = await _insert_row(
            snapshot, strategy_id=strategy_id, side=side, price=ask, notional=notional,
            shares=shares, start=start, reason=reason, mode=mode,
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
                await ledger.set_action(
                    snapshot.window_slug, strategy_id, f"BLOCKED:{result.reason}")
                log.warning("hourly_engine.live_entry_not_placed", strategy_id=strategy_id,
                            status=result.status, reason=result.reason)
                return
            # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
            # every other failure happened at or after the post (an exception or timeout,
            # or a response without success and an order id). The venue may still have
            # accepted the order, and a re-post is a new order, so the hour ends here.
            uncertain = f"{UNCERTAIN_PREFIX}{result.status} {result.reason}".strip()
            await ledger.set_action(snapshot.window_slug, strategy_id, uncertain)
            await notify(
                "live_entry_uncertain",
                f"LIVE entry for {strategy_id} may or may not be on Polymarket "
                f"({result.status}: {result.reason}). No retry this hour; check the "
                "account's open orders and trades.",
                {"window_slug": snapshot.window_slug, "strategy_id": strategy_id,
                 "token_id": token},
            )
            log.warning("hourly_engine.live_entry_outcome_unknown", strategy_id=strategy_id,
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
                await ledger.set_action(snapshot.window_slug, strategy_id, f"BLOCKED:{blocked}")
                log.info("hourly_engine.entry_blocked", strategy_id=strategy_id, reason=blocked)
                return
        position_id = await _insert_row(
            snapshot, strategy_id=strategy_id, side=side, price=ask, notional=notional,
            shares=shares, start=start, reason=reason, mode=mode,
        )
        if gate is not None:
            await gate.record_buy_notional(round(ask * shares, 4))
    await ledger.set_action(snapshot.window_slug, strategy_id, "ENTERED", position_id)
    label = "LIVE" if executor is not None else "Paper"
    await notify(
        f"{mode}_entry",
        f"{label} BUY {side} ${notional:.2f} @ {ask:.3f} ({strategy_id})",
        {"window_slug": snapshot.window_slug, "strategy_id": strategy_id},
    )
    log.info("hourly_engine.entered", mode=mode, strategy_id=strategy_id, side=side,
             price=ask, shares=shares, window_slug=snapshot.window_slug)


async def open_entries(snapshot: PaperSnapshot, now: int, *, allow_entries: bool) -> None:
    start = market.hour_start(now)
    deadline = _knobs.cached("hourly_entry_deadline_seconds")
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(snapshot.window_slug, sid)
        if row is not None and row["action"] == SUBMITTING:
            # Claude, 2026-09-15, branch-review finding hourly-ambiguous-post-error-retried:
            # an earlier tick started an entry and never recorded its result (it raised,
            # or the process stopped). Close the hour out; never enter again.
            await ledger.set_action(snapshot.window_slug, sid, UNFINISHED_ATTEMPT,
                                    expected_action=SUBMITTING)
            continue
        if row is None or row["action"] != "PENDING":
            continue
        if now - start > deadline:
            await ledger.set_action(snapshot.window_slug, sid, "MISSED")
            continue
        if allow_entries:
            await _enter(snapshot, sid, str(row["decision_side"]), start)


async def settle_due(client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int) -> None:
    """Settle hourly positions and decision rows whose hour candle has closed on Binance."""
    from polymarket_bot import paper as P

    now_ms = now * 1000
    candles: dict[int, market.HourCandle | None] = {}

    async def closed_candle(start_ts: int) -> market.HourCandle | None:
        if start_ts not in candles:
            try:
                candles[start_ts] = await market.fetch_hour_candle(client, start_ts, now_ms)
            except httpx.HTTPError as exc:
                log.warning("hourly_engine.settle_read_failed", start=start_ts, error=str(exc))
                candles[start_ts] = None
        c = candles[start_ts]
        return c if c is not None and c.closed else None

    async with P.connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' AND market_timeframe = ? "
            "ORDER BY opened_at",
            (TIMEFRAME,),
        ) as cur:
            open_rows = [dict(r) for r in await cur.fetchall()]
    for pos in open_rows:
        start_ts = int(pos["window_start_ts"])
        live_row = pos.get("mode") == "live"
        if start_ts + HOUR_S > now or (live_row and P._live_executor is None):
            continue  # hour still running, or real tokens only the live executor may book
        c = await closed_candle(start_ts)
        if c is None:
            continue
        up = market.up_won(c.open, c.close)
        won = up if pos["side"] == "Up" else not up
        held: float | None = None
        pnl: float | None = None
        if live_row:
            slot = P._live_executor.slot_executor(str(pos["strategy_id"]))
            result = await slot.record_settlement(won, pos["window_slug"])
            if not result.ok and result.status != "SKIPPED":
                continue  # registration failed: keep the row open and retry next tick
            # Book the venue's real held size and fee-true PnL (as the legacy loop does).
            held, pnl = result.size or 0.0, result.notional_usd
        await P._close_position(
            pos, snapshot, 1.0 if won else 0.0, "SETTLED",
            settled=True, settled_held=held, settled_pnl=pnl,
        )
    for start_ts in await ledger.unsettled_windows(now):
        c = await closed_candle(start_ts)
        if c is not None:
            await ledger.settle_window(start_ts, c.open, c.close)


def _reason_line(rows: dict[str, dict[str, Any]]) -> str:
    if not rows:
        return "hourly: waiting for the previous hour to close"
    return " | ".join(f"{sid}: {r.get('action')} ({r.get('decision_reason')})"
                      for sid, r in rows.items())


async def tick(client: httpx.AsyncClient, *, allow_entries: bool = True) -> PaperSnapshot:
    from polymarket_bot import paper as P

    # One clock read per tick: the snapshot's market and every decision, entry and
    # settlement step must agree on the hour, even when a tick straddles H:00.
    now = P._now()
    snapshot = await build_snapshot(client, now)
    await settle_due(client, snapshot, now)
    rows = await decide_hour(client, snapshot, now)
    await open_entries(snapshot, now, allow_entries=allow_entries)
    for sid in list(rows):
        rows[sid] = await ledger.get_decision(snapshot.window_slug, sid) or rows[sid]
    snapshot.reason = _reason_line(rows)
    entered = [r for r in rows.values() if r.get("action") == "ENTERED"]
    if entered:
        snapshot.signal_side = entered[0].get("decision_side")
    await P._log_tick(snapshot)
    return snapshot
