"""Hourly BTC engine: one decision per hour per strategy, per-strategy entries, Binance settlement.

Called by ``polymarket_bot.paper.paper_tick_once`` when the loop was started on the BTC 1h
selection. Strategies never read mode; entries go through the same RiskGate as every other
entry. Each strategy owns one open-position slot (operator decision 2026-09-14). The entry
step itself is shared with the daily engine in ``polymarket_bot.strategy_slot_entry``.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx

from db import notify
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategy_slot_entry as slot_entry
from polymarket_bot.hourly import btcusdt_1h_spot_taker_push_reversal as spot_taker_push_rule
from polymarket_bot.hourly import book_record, candle_audit, ledger, market
from polymarket_bot.hourly.market import HOUR_S, HourMarket

# The entry-attempt actions moved to strategy_slot_entry (Claude, 2026-09-16); this module
# keeps its old names for them.
from polymarket_bot.strategy_slot_entry import (  # noqa: F401 - old import path
    SUBMITTING,
    UNCERTAIN_PREFIX,
    UNFINISHED_ATTEMPT,
)

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("hourly_engine")

TIMEFRAME = "1h"
_ET = ZoneInfo("America/New_York")
# One closed candle beyond the rule's window, plus Binance's still-forming candle, which
# fetch_closed_candles drops.
_CANDLES = spot_taker_push_rule.WINDOW + 2

# (strategy_id, enable knob). "Kronos BTCUSDT 1h fine-tune (Hugging Face lc2004): next-hour
# Up chance vs Polymarket price" joins in a later change.
STRATEGIES: tuple[tuple[str, str], ...] = (
    (spot_taker_push_rule.STRATEGY_ID, "hourly_btcusdt_1h_spot_taker_push_reversal_enabled"),
)

_market_cache: dict[int, HourMarket] = {}
_open_cache: dict[int, float] = {}


def reset_caches() -> None:
    _market_cache.clear()
    _open_cache.clear()
    book_record.reset_caches()
    candle_audit.reset_state()


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
    client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int, *, mode: str
) -> dict[str, dict[str, Any]]:
    """Record this hour's decision for every enabled strategy (once per mode), return the rows."""
    start = market.hour_start(now)
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        # Keyed by the UTC hour start (Claude, 2026-09-15, branch-review finding
        # dst-fallback-slug-collision): two fall-back-day hours share one ET slug.
        # Keyed by mode too (Claude, 2026-09-15, branch-review finding
        # decision-row-shared-across-modes): a paper row decided earlier in the hour does not
        # stand in for the live run's own decision, gate check and action, or the reverse.
        row = await ledger.get_decision(start, sid, mode=mode)
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
    candles_fetched_at_ms = int(time.time() * 1000)
    prev_ms = (start - HOUR_S) * 1000
    if not (
        len(spot) >= spot_taker_push_rule.WINDOW
        and len(perp) >= spot_taker_push_rule.WINDOW
        and spot[-1].open_time_ms == prev_ms and perp[-1].open_time_ms == prev_ms
    ):
        log.info("hourly_engine.waiting_for_previous_hour", window_slug=snapshot.window_slug)
        return rows
    late = now - start > _knobs.cached("hourly_entry_deadline_seconds")
    for sid in pending:
        if sid == spot_taker_push_rule.STRATEGY_ID:
            d = spot_taker_push_rule.decide(spot, perp)
        else:  # pragma: no cover - PR 2 adds Kronos
            continue
        # When the candles were read and which tick decided, for the candle audit.
        d.signal["candles_fetched_at_ms"] = candles_fetched_at_ms
        d.signal["decided_at_ms"] = now_ms
        await ledger.record_decision(
            strategy_id=sid, window_slug=snapshot.window_slug, window_start_ts=start,
            side=d.side, reason=d.reason, signal=d.signal, factors=_factors(start),
            up_bid=snapshot.up_best_bid, up_ask=snapshot.up_best_ask,
            down_bid=snapshot.down_best_bid, down_ask=snapshot.down_best_ask,
            hour_open=snapshot.reference_price, mode=mode, late=late,
        )
        rows[sid] = await ledger.get_decision(start, sid, mode=mode) or {}
    return rows


class _HourlyDecision:
    """The hourly decision record for one (hour, strategy, mode) as a slot_entry.DecisionHandle."""

    def __init__(self, start: int, strategy_id: str, mode: str) -> None:
        self.start, self.strategy_id, self.mode = start, strategy_id, mode

    async def reason(self) -> str | None:
        row = await ledger.get_decision(self.start, self.strategy_id, mode=self.mode)
        return (row or {}).get("decision_reason")

    async def set_action(
        self, action: str, position_id: int | None = None, *, expected_action: str | None = None
    ) -> bool:
        # ledger.set_action returns None; the write either happened or raised.
        await ledger.set_action(self.start, self.strategy_id, action, position_id,
                                mode=self.mode, expected_action=expected_action)
        return True


async def open_entries(
    snapshot: PaperSnapshot, now: int, *, allow_entries: bool, mode: str, executor: Any = None
) -> None:
    """Advance this mode's entries. ``executor`` is the live executor the tick started with.

    A runner whose mode changed mid-tick (the global executor is no longer the one this tick
    started with) writes nothing (Claude session "Tsinghua base Kronos btc 24h", 2026-09-17).
    """
    from polymarket_bot import paper as P

    start = market.hour_start(now)
    deadline = _knobs.cached("hourly_entry_deadline_seconds")
    if P._live_executor is not executor or (executor is not None) != (mode == "live"):
        log.warning("hourly_engine.entries_skipped_runner_changed", tick_mode=mode)
        return
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        # Only this mode's row (Claude, 2026-09-15, branch-review finding
        # decision-row-shared-across-modes).
        row = await ledger.get_decision(start, sid, mode=mode)
        if row is None:
            continue
        await slot_entry.advance(
            snapshot, slot_entry.SlotWindow(sid, TIMEFRAME, start), action=row["action"],
            side=row["decision_side"], now=now, deadline_s=deadline,
            allow_entries=allow_entries, mode=mode, decision=_HourlyDecision(start, sid, mode),
        )


async def settle_due(client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int) -> None:
    """Settle hourly positions and decision rows whose hour candle has closed on Binance."""
    from polymarket_bot import paper as P

    # Claude, 2026-09-15, branch-review finding pending-row-never-finalized: every tick,
    # before anything that can skip or fail, close out decisions left open in hours that
    # already ended. The current hour is handled by open_entries.
    finalized = await ledger.finalize_ended_hours(market.hour_start(now), UNFINISHED_ATTEMPT)
    if any(finalized.values()):
        log.info("hourly_engine.ended_hours_finalized", **finalized)
    if finalized["unfinished"]:
        # Same operator warning as a current-hour unfinished attempt (branch-review finding
        # hourly-reentry-after-untraced-post): a live order may exist with no ledger row.
        await notify(
            "entry_attempt_unfinished",
            f"{finalized['unfinished']} entry attempt(s) in an earlier hour did not finish: the "
            "bot stopped or its tick failed mid-order. If the bot was LIVE, an order may be on "
            "Polymarket with no ledger row; check the account's open orders and trades.",
            finalized,
        )

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
    start = market.hour_start(now)
    # The decision record is per mode (Claude, 2026-09-15, branch-review finding
    # decision-row-shared-across-modes). Strategies never see it; it only picks the row.
    executor = P._live_executor  # read once: entries use this executor or none at all
    mode = "live" if executor is not None else "paper"
    snapshot = await build_snapshot(client, now)
    # Book record before this tick's entry step (observation only; approved by Zayan
    # (operator), 2026-09-15). Rows taken after an earlier live entry this hour are flagged,
    # since our own order may be in the book. _market_for is cached: no extra Gamma call.
    await book_record.maybe_record(
        client, snapshot=snapshot, market=await _market_for(client, start), now=now)
    await settle_due(client, snapshot, now)
    rows = await decide_hour(client, snapshot, now, mode=mode)
    await open_entries(snapshot, now, allow_entries=allow_entries, mode=mode, executor=executor)
    for sid in list(rows):
        rows[sid] = await ledger.get_decision(start, sid, mode=mode) or rows[sid]
    snapshot.reason = _reason_line(rows)
    # Candle audit of earlier decisions once Binance publishes the day's archive (observation
    # only; approved by Zayan (operator), 2026-09-15). At most one archive day per tick.
    await candle_audit.audit_due(client, now)
    entered = [r for r in rows.values() if r.get("action") == "ENTERED"]
    if entered:
        snapshot.signal_side = entered[0].get("decision_side")
    await P._log_tick(snapshot)
    return snapshot
