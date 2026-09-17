"""Daily BTC engine: one decision per noon-ET window per strategy, entries, Binance settlement.

Called by polymarket_bot.paper.paper_tick_once when the loop was started on the BTC 1d
selection. Runs Tsinghua-Kronos BTC 24h (name: Zayan, 2026-09-16) on Polymarket's daily BTC
Up/Down market in whichever mode was selected. Entries go through
polymarket_bot.strategy_slot_entry: one order attempt per window, one open position per
strategy, the same RiskGate. Settlement reads the two Binance 1-minute closes the market
resolves on; an exact tie pays 0.50 a share (market rules, read 2026-09-16).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from db import notify
from logging_setup import get_logger
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategy_slot_entry as slot_entry
from polymarket_bot.daily_btc import forecast_input, ledger, market, tsinghua_kronos_btc_24h
from polymarket_bot.hourly.market import fetch_spot
from polymarket_bot.kronos_forecast import client as kronos

if TYPE_CHECKING:
    from polymarket_bot.paper import PaperSnapshot

log = get_logger("daily_btc_engine")

TIMEFRAME = "1d"
# Settle once Binance has certainly published the settlement minute (Claude, 2026-09-16).
SETTLE_GRACE_S = 120
# (strategy_id, enable knob)
STRATEGIES: tuple[tuple[str, str], ...] = (
    (tsinghua_kronos_btc_24h.STRATEGY_ID, "daily_btc_tsinghua_kronos_btc_24h_enabled"),
)
DISPLAY_NAMES = {tsinghua_kronos_btc_24h.STRATEGY_ID: tsinghua_kronos_btc_24h.DISPLAY_NAME}

_market_cache: dict[int, market.DayMarket] = {}


def current_window_start(now: int) -> int:
    """The reference (noon-ET) start of the window ``now`` falls in — the uniform
    per-engine window accessor ``paper.py`` uses to match an open row to its own current
    window (Claude, 2026-09-17, generalizing branch-review finding
    5m-stop-depends-on-hourly-discovery for two engines).
    """
    return market.current_window(now).reference_ts


def reset_caches() -> None:
    _market_cache.clear()


class _DailyDecision:
    """One btc_daily_market_decisions row as a strategy_slot_entry.DecisionHandle."""

    def __init__(self, reference_ts: int, strategy_id: str, mode: str) -> None:
        self.reference_ts, self.strategy_id, self.mode = reference_ts, strategy_id, mode

    async def reason(self) -> str | None:
        row = await ledger.get_decision(self.reference_ts, self.strategy_id, mode=self.mode)
        return (row or {}).get("decision_reason")

    async def set_action(self, action: str, position_id: int | None = None, *,
                         expected_action: str | None = None) -> bool:
        return await ledger.set_action(self.reference_ts, self.strategy_id, action, position_id,
                                       mode=self.mode, expected_action=expected_action)


async def market_for(client: httpx.AsyncClient, window: market.DayWindow) -> market.DayMarket:
    if window.reference_ts not in _market_cache:
        found = await market.discover(client, window)
        if found is None:
            raise RuntimeError(
                f"Could not find the BTC daily market for {window.market_date:%Y-%m-%d}")
        _market_cache.clear()
        _market_cache[window.reference_ts] = found
    return _market_cache[window.reference_ts]


async def _minute_close(client: httpx.AsyncClient, ts: int, now: int) -> float | None:
    try:
        return await market.fetch_minute_close(client, ts, now)
    except httpx.HTTPError as exc:
        log.warning("daily_btc_engine.minute_close_failed", minute_ts=ts, error=str(exc))
        return None


async def build_snapshot(client: httpx.AsyncClient, now: int | None = None) -> PaperSnapshot:
    from polymarket_bot import paper as P

    now = P._now() if now is None else now
    window = market.current_window(now)
    m = await market_for(client, window)
    up_book = await P._fetch_clob_book(client, m.up_token_id)
    down_book = await P._fetch_clob_book(client, m.down_token_id)
    reference = await _minute_close(client, window.reference_ts, now)
    spot = await fetch_spot(client)
    return P.PaperSnapshot(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_slug=m.slug,
        market_question=m.question,
        remaining_seconds=max(0, window.settle_ts - now),
        spot_price=spot or 0.0,
        reference_price=reference or 0.0,
        sigma_per_second=0.0,
        market_up_price=up_book.best_ask,
        market_down_price=down_book.best_ask,
        fair_up_prob=0.5,
        edge=0.0,
        signal_side=None,
        confidence=0.0,
        notional_usd=0.0,
        reason="daily BTC: waiting",
        feed_source=(
            f"spot={'binance_rest' if spot else 'unavailable'};"
            f"ref={'binance_1m' if reference else 'pending'};vol=none;quotes=clob"
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
        feed_degraded=False,
    )


def _book(snapshot: PaperSnapshot) -> dict[str, float | None]:
    return {"up_bid": snapshot.up_best_bid, "up_ask": snapshot.up_best_ask,
            "down_bid": snapshot.down_best_bid, "down_ask": snapshot.down_best_ask}


async def decide_window(
    client: httpx.AsyncClient, snapshot: PaperSnapshot, m: market.DayMarket, now: int, *,
    mode: str,
) -> dict[str, dict[str, Any]]:
    """Record this window's decision for every enabled strategy (once per mode)."""
    from polymarket_bot import paper as P

    window = m.window
    deadline = int(_knobs.cached("daily_btc_entry_deadline_seconds"))
    rows: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        # Keyed by the window's reference time and by mode, as in the hourly engine
        # (Claude, 2026-09-15, branch-review findings dst-fallback-slug-collision and
        # decision-row-shared-across-modes).
        row = await ledger.get_decision(window.reference_ts, sid, mode=mode)
        if row is None:
            pending.append(sid)
        else:
            rows[sid] = row
    if not pending:
        return rows
    elapsed = now - window.reference_ts
    if elapsed > deadline:
        for sid in pending:
            await ledger.record_decision(
                strategy_id=sid, mode=mode, market=m, side=None,
                reason=f"missed: the window opened {elapsed} s ago, past the {deadline} s deadline",
                signal={}, **_book(snapshot), late=True, available=True,
            )
            rows[sid] = await ledger.get_decision(window.reference_ts, sid, mode=mode) or {}
        return rows
    candles = await forecast_input.fetch_forecast_candles(client, window, now)
    if candles is None:
        log.info("daily_btc_engine.waiting_for_noon_candle", window_slug=m.slug)
        return rows
    for sid in pending:
        if sid != tsinghua_kronos_btc_24h.STRATEGY_ID:
            continue
        result = await kronos.run_forecast(
            tsinghua_kronos_btc_24h.request_for(candles, window.reference_ts))
        decision = tsinghua_kronos_btc_24h.decide(
            result, candles=candles, reference_ts=window.reference_ts,
            up_ask=snapshot.up_best_ask, down_ask=snapshot.down_best_ask,
            edge_threshold=float(
                _knobs.cached("daily_btc_tsinghua_kronos_btc_24h_edge_threshold")),
            # 23 or 25 on daylight-saving days; the forecast always covers 24 hours.
            window_hours=(window.settle_ts - window.reference_ts) / 3600,
        )
        # The one clock read after the tick's own: the forecast takes about 9 s and up to
        # the worker timeout, so a forecast that ends past the deadline is recorded as
        # missed instead of entered late (Claude, 2026-09-16).
        late = P._now() - window.reference_ts > deadline
        wrote = await ledger.record_decision(
            strategy_id=sid, mode=mode, market=m, side=decision.side, reason=decision.reason,
            signal=decision.signal, **_book(snapshot), late=late,
            available=decision.available,
        )
        if wrote and not decision.available:
            await notify(
                "daily_btc_forecast_unavailable",
                f"{DISPLAY_NAMES[sid]} could not forecast {m.slug}: {result.error}. "
                "No bet this window.",
                {"window_slug": m.slug, "strategy_id": sid},
            )
        log.info("daily_btc_engine.decided", strategy_id=sid, mode=mode, side=decision.side,
                 available=decision.available, late=late, window_slug=m.slug)
        rows[sid] = await ledger.get_decision(window.reference_ts, sid, mode=mode) or {}
    return rows


async def open_entries(
    snapshot: PaperSnapshot, m: market.DayMarket, now: int, *, allow_entries: bool, mode: str
) -> None:
    deadline = int(_knobs.cached("daily_btc_entry_deadline_seconds"))
    for sid, knob in STRATEGIES:
        if not _knobs.cached(knob):
            continue
        row = await ledger.get_decision(m.window.reference_ts, sid, mode=mode)
        if row is None:
            continue
        await slot_entry.advance(
            snapshot, slot_entry.SlotWindow(sid, TIMEFRAME, m.window.reference_ts),
            action=str(row["action"]), side=row["decision_side"], now=now, deadline_s=deadline,
            allow_entries=allow_entries, mode=mode,
            decision=_DailyDecision(m.window.reference_ts, sid, mode),
        )


async def settle_due(client: httpx.AsyncClient, snapshot: PaperSnapshot, now: int) -> None:
    """Settle daily positions and decision rows whose settlement minute has closed on Binance.

    Reads Binance only, never Gamma, so any timeframe's run can call it.
    """
    from polymarket_bot import paper as P

    # As in the hourly engine (Claude, 2026-09-15, branch-review finding
    # pending-row-never-finalized): every tick, before anything that can skip or fail,
    # close out decisions left open in windows that already ended, in every mode. The
    # current window is handled by open_entries.
    finalized = await ledger.finalize_ended_windows(
        market.current_window(now).reference_ts, slot_entry.UNFINISHED_ATTEMPT)
    if any(finalized.values()):
        log.info("daily_btc_engine.ended_windows_finalized", **finalized)
    if finalized["unfinished"]:
        # Same operator warning as the hourly engine's (branch-review finding
        # hourly-reentry-after-untraced-post): a live order may exist with no ledger row.
        await notify(
            "entry_attempt_unfinished",
            f"{finalized['unfinished']} daily BTC entry attempt(s) in an earlier window did "
            "not finish: the bot stopped or its tick failed mid-order. If the bot was LIVE, an "
            "order may be on Polymarket with no ledger row; check the account's open orders "
            "and trades.",
            finalized,
        )

    closes: dict[int, float | None] = {}

    async def close_at(ts: int) -> float | None:
        if ts not in closes:
            closes[ts] = await _minute_close(client, ts, now)
        return closes[ts]

    async with P.connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' AND market_timeframe = ? "
            "ORDER BY opened_at",
            (TIMEFRAME,),
        ) as cur:
            open_rows = [dict(r) for r in await cur.fetchall()]
    for pos in open_rows:
        live_row = pos.get("mode") == "live"
        if live_row and P._live_executor is None:
            continue  # real tokens: only the live executor may book them
        window = market.current_window(int(pos["window_start_ts"]))
        if now < window.settle_ts + SETTLE_GRACE_S:
            continue
        reference, final = await close_at(window.reference_ts), await close_at(window.settle_ts)
        if reference is None or final is None:
            continue
        result = market.outcome(reference, final)
        pay = market.payout(str(pos["side"]), result)
        held: float | None = None
        pnl: float | None = None
        if live_row:
            slot = P._live_executor.slot_executor(str(pos["strategy_id"]))
            settled = await slot.record_settlement(pay == 1.0, pos["window_slug"], payout=pay)
            if not settled.ok and settled.status != "SKIPPED":
                continue  # registration failed: keep the row open and retry next tick
            # Book the venue's real held size and fee-true PnL (as the hourly engine does).
            held, pnl = settled.size or 0.0, settled.notional_usd
        await P._close_position(pos, snapshot, pay, "SETTLED", settled=True,
                                settled_held=held, settled_pnl=pnl)
    for reference_ts, settle_ts in await ledger.unsettled_windows(now, SETTLE_GRACE_S):
        reference, final = await close_at(reference_ts), await close_at(settle_ts)
        if reference is not None and final is not None:
            await ledger.settle_window(reference_ts, reference, final,
                                       market.outcome(reference, final))


def _reason_line(rows: dict[str, dict[str, Any]]) -> str:
    if not rows:
        return "daily BTC: waiting for the noon-ET candle"
    return " | ".join(
        f"{DISPLAY_NAMES.get(sid, sid)}: {row.get('action')} ({row.get('decision_reason')})"
        for sid, row in rows.items()
    )


async def tick(client: httpx.AsyncClient, *, allow_entries: bool = True) -> PaperSnapshot:
    from polymarket_bot import paper as P

    # One clock read per tick, as in the hourly engine: the snapshot's market and every
    # decision, entry and settlement step agree on the window, even when a tick straddles noon.
    now = P._now()
    # The decision record is per mode; strategies never see it, it only picks the row. Read
    # once so the decision and the entry step use the same row (as in the hourly engine).
    mode = "live" if P._live_executor is not None else "paper"
    snapshot = await build_snapshot(client, now)
    await settle_due(client, snapshot, now)
    m = await market_for(client, market.current_window(now))
    rows = await decide_window(client, snapshot, m, now, mode=mode)
    await open_entries(snapshot, m, now, allow_entries=allow_entries, mode=mode)
    for sid in list(rows):
        rows[sid] = await ledger.get_decision(m.window.reference_ts, sid, mode=mode) or rows[sid]
    snapshot.reason = _reason_line(rows)
    entered = [r for r in rows.values() if r.get("action") == ledger.ENTERED]
    if entered:
        snapshot.signal_side = entered[0].get("decision_side")
    await P._log_tick(snapshot)
    return snapshot
