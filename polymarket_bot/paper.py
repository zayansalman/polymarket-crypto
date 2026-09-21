"""BTC 5-minute trading engine (paper by default, live opt-in).

It discovers the current Polymarket BTC 5m market, prices it against the
SETTLEMENT feed (Polymarket's Chainlink BTC/USD stream — reference open via
the crypto-price REST API, live spot + sigma via the ws-live-data WebSocket,
issue #21), quotes the EXECUTABLE market from the CLOB order book for both
outcome tokens (issue #22), and records entries/exits in SQLite. In paper
mode (the default) no real orders are ever placed. When the operator selects
LIVE in the dashboard AND the live boot gate passes (private key + coherent
wallet), entries/exits are ALSO routed through
:class:`polymarket_exec.execution.live.LiveExecutor`, which places real risk-gated
orders on the Polymarket CLOB.

Data-source discipline (three sources, zero level-mixing):

* Chainlink (REST + WS) — reference open, live spot, sigma. Settlement truth.
* CLOB order book — executable best bid/ask/size per outcome token. The only
  prices the strategy trades against; paper BUYs fill at best ask, SELLs at
  best bid. Gamma ``outcomePrices`` are journaled (``gamma_up_price``) purely
  to quantify their staleness and are never used for pricing.
* Binance — volatility-shape fallback only (returns, never levels).

If the Chainlink feed is unavailable (WS stale AND no usable reference) the
engine opens NO new entries and journals ``skip: settlement feed degraded``;
existing positions still exit on book prices / time / window roll.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from config import (
    BINANCE_API_BASE,
    MARKET_TIMEFRAME_MINUTES,
    POLYMARKET_CLOB_API,
    POLYMARKET_CRYPTO_PRICE_API,
    POLYMARKET_GAMMA_API,
    POLYMARKET_LIVE_DATA_WS,
    CHAINLINK_STALE_SECONDS,
    PRINT_GRANULARITY_USD,
)
import config as _config
from polymarket_bot import market_selection
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from db import connect, get_config, journal_live_order, notify, set_config
from logging_setup import get_logger
from polymarket_exec.connectors.chainlink_settlement import (
    ChainlinkSettlementConnector,
    ChainlinkWsFeed,
)
from polymarket_exec.execution.live import (
    DEFAULT_MIN_ORDER_SIZE,
    LiveExecutor,
    build_live_executor,
)
from polymarket_exec.execution.gate import (
    EntryRequest,
    RiskGate,
    build_gate_from_config,
)
from polymarket_exec.marketdata import hub as _marketdata_hub
from polymarket_bot.strategy import (
    drift_per_second,
    fair_up_probability,
    sigma_per_second,
)

log = get_logger("paper")

# Live executor for the current run loop. None means pure paper mode.
_live_executor: LiveExecutor | None = None

# Shared risk gate for the current run loop (issue #64). In paper mode this
# is a standalone RiskGate; in live mode it is the LiveExecutor's gate (same
# object), so both paths reuse the same daily counters and gate decisions.
_risk_gate: RiskGate | None = None

# Settlement-aligned Chainlink WS feed for the current run loop (issue #21).
# None / stale means NO new entries (the tick journals why).
_chainlink_feed: ChainlinkWsFeed | None = None
# Process-wide feed owned by someone else (see set_shared_chainlink_feed).
_shared_chainlink_feed: ChainlinkWsFeed | None = None

# Reference open print per window_start_ts. The open is immutable once the
# provisional revision settles, so one stabilized REST read per window.
_reference_cache: dict[int, float] = {}

# The loop's decision slot. The v0 strategy (entry gates, auto-pause, param
# tuner, calibration, model picker) was archived 2026-09-13 — see
# docs/archive/v0-strategy.md. Until a new strategy is plugged in here, every
# tick journals this reason and no entry is ever taken, in paper AND live.
NO_STRATEGY_REASON = "skip: no strategy loaded"

# --- Loop heartbeat + generation (#147 watchdog) --------------------------
# The heartbeat is stamped at loop entry and after EVERY iteration (including
# failed ticks); a stalled age while the bot should be running means the loop
# is wedged on an unbounded await. The generation counter lets an abandoned
# (wedged, later un-wedged) loop detect it was superseded so it never clears
# the new loop's globals or writes state=stopped over a running successor.
_heartbeat_monotonic: float | None = None
_loop_generation: int = 0


def _beat() -> None:
    global _heartbeat_monotonic
    _heartbeat_monotonic = time.monotonic()


def heartbeat_age_seconds() -> float:
    """Seconds since the loop last completed an iteration (inf if never)."""
    if _heartbeat_monotonic is None:
        return float("inf")
    return time.monotonic() - _heartbeat_monotonic


def set_shared_chainlink_feed(feed: ChainlinkWsFeed | None) -> None:
    """Hand the loop a process-wide Chainlink WS feed (the dashboard's feed monitor).

    While set, the loop reads that feed instead of opening its own connection,
    so the FEEDS card shows the exact stream the loop trades on. The owner keeps
    it running; the loop never stops it.
    """
    global _shared_chainlink_feed
    _shared_chainlink_feed = feed


def _is_current_generation(my_generation: int) -> bool:
    return _loop_generation == my_generation


# Minimum points in the WS 1s series before it is trusted for sigma;
# below this the engine falls back to Binance return SHAPE (never levels).
_MIN_CHAINLINK_SIGMA_POINTS = 30

BINANCE_API = BINANCE_API_BASE
FIVE_MINUTES = MARKET_TIMEFRAME_MINUTES * 60


@dataclass(frozen=True)
class BookTop:
    """Top of one outcome token's CLOB book (best level = LAST array element)."""

    best_bid: float | None = None
    best_ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def crossed(self) -> bool:
        return (
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > self.best_ask
        )

    @property
    def buyable(self) -> bool:
        """An entry BUY needs an ask, on a book that is not crossed."""
        return self.best_ask is not None and not self.crossed


EMPTY_BOOK = BookTop()


@dataclass
class PaperSnapshot:
    created_at: str
    window_slug: str
    market_question: str
    remaining_seconds: int
    spot_price: float
    reference_price: float
    sigma_per_second: float
    # Executable prices: best ASK per side from the CLOB book (what a BUY
    # actually pays). None when that side has no ask this tick.
    market_up_price: float | None
    market_down_price: float | None
    fair_up_prob: float
    edge: float
    signal_side: str | None
    confidence: float
    notional_usd: float
    reason: str
    feed_source: str
    up_token_id: str = ""
    down_token_id: str = ""
    # CLOB top-of-book for both outcome tokens (issue #22).
    up_best_bid: float | None = None
    up_best_ask: float | None = None
    up_bid_size: float | None = None
    up_ask_size: float | None = None
    down_best_bid: float | None = None
    down_best_ask: float | None = None
    down_bid_size: float | None = None
    down_ask_size: float | None = None
    quote_source: str = "clob"
    # Gamma outcomePrices Up — journaled ONLY to quantify staleness vs the book.
    gamma_up_price: float | None = None
    # True when the Chainlink settlement feed could not price this tick;
    # entries are blocked and pricing-model-based exits are suppressed.
    feed_degraded: bool = False
    # Pre-calibration fair_up_prob — populated when a calibrator is active so
    # the dashboard can show raw vs calibrated and the journal stays auditable.
    # Equals fair_up_prob when the identity calibrator is in use.
    fair_up_prob_raw: float | None = None
    # One-second directional drift (mean 1s log-return) from the settlement
    # series, paired with sigma to form the regime input the adaptive-cushion
    # candidates consume (#94). In-memory only — not journaled to the tick
    # table — and defaults to 0.0 (neutral regime) so historical replays and
    # the many existing constructors are unaffected.
    drift_per_second: float = 0.0

    @property
    def has_executable_quote(self) -> bool:
        return self.up_best_ask is not None or self.down_best_ask is not None


def _loss_halt_stop_detail(gate: Any, mode: str) -> str | None:
    """Stop-detail string when the daily loss halt is breached (#76), else None.

    ``run_paper_loop`` calls this once per tick. A non-None result means: stop
    the bot, flatten, and surface this as LAST DETAIL so the operator knows to
    Reset the tally before pressing Start. Returns None when the gate is absent,
    within the limit, or the operator has the bypass on.

    LIVE mode only (#146): a breached PAPER line never hard-stops the loop —
    its entries are already blocked per tick by the gate's ``block_reason``,
    and stopping would kill settlement too, on a line that risks zero
    capital (which silently froze the paper book on 07-02).
    ``_notify_paper_halt_pause`` surfaces the paused state instead.
    """
    if gate is None or not gate.loss_halt_breached():
        return None
    if mode != "live":
        return None
    pnl = gate.halt_pnl
    limit = gate.effective_daily_loss_halt_usd
    # Trailing high-water-mark floor (#112): peak - limit. Cite it (not a fixed
    # -limit), since the halt can fire at a POSITIVE pnl after a banked peak.
    return (
        f"Daily loss halt: live realized {pnl:+.2f} USD at/below trailing floor "
        f"{gate.loss_halt_floor:+.2f} (peak {gate.halt_peak:+.2f} − {limit:.2f} limit). "
        "Bot stopped & flattened — Reset the halt, then Start to resume."
    )


# One notification per breach episode (#146); re-arms when the halt clears
# (daily roll or operator reset) so the next episode notifies again.
_paper_halt_pause_notified = False


async def _notify_paper_halt_pause(gate: Any, mode: str) -> None:
    """Surface a breached PAPER halt as a pause, not a stop (#146)."""
    global _paper_halt_pause_notified
    breached = mode != "live" and gate is not None and gate.loss_halt_breached()
    if not breached:
        _paper_halt_pause_notified = False
        return
    if _paper_halt_pause_notified:
        return
    _paper_halt_pause_notified = True
    await notify(
        "paper_halt_pause",
        f"Paper loss halt hit (realized {gate.halt_pnl:+.2f} at/below trailing "
        f"floor {gate.loss_halt_floor:+.2f}): paper entries paused until the "
        "daily window rolls or the halt is reset — the loop keeps running "
        "and open positions still settle (#146).",
        {"halt_pnl": gate.halt_pnl, "floor": gate.loss_halt_floor},
    )
    log.warning(
        "paper_loop.loss_halt_pause",
        halt_pnl=gate.halt_pnl,
        floor=gate.loss_halt_floor,
    )


async def run_paper_loop(stop_event: threading.Event, mode: str | None = None) -> None:
    """Run until Stop is pressed or the process exits.

    Mode comes from ``BOT_MODE``: ``paper`` (default) journals simulated
    trades only; ``live`` ALSO routes entries/exits through the risk-gated
    LiveExecutor. Live boot refusal stops the loop — it never silently falls
    back to paper. The controller passes the ``mode`` Start decided on; when
    omitted it is read from the runtime selector (falling back to BOT_MODE).
    """
    global _live_executor, _chainlink_feed, _risk_gate, _loop_generation
    # Watchdog bookkeeping (#147): claim a fresh generation and stamp the
    # heartbeat before any await, so a stall during startup is also visible.
    _loop_generation += 1
    my_generation = _loop_generation
    _beat()
    # Runtime mode selector (dashboard) overrides the env default; live still
    # passes the same boot gate. Falls back to BOT_MODE when unset.
    if mode is None:
        mode = await get_config("polymarket_bot.requested_mode", _config.BOT_MODE) or "paper"
    if mode == "live":
        try:
            executor = build_live_executor()
            await executor.start()
        except Exception as e:  # noqa: BLE001 — includes LiveBootRefused
            error = f"{type(e).__name__}: {e!s}"
            await set_config("polymarket_bot.state", "stopped")
            await set_config("polymarket_bot.mode", "live")
            await _set_detail(
                f"LIVE mode refused to start: {error} "
                "The bot did NOT fall back to paper mode and is not running."
            )
            await notify("live_boot_refused", f"Live mode boot refused: {error}")
            log.error("live_loop.boot_refused", error=error)
            return
        _live_executor = executor
        _risk_gate = executor.gate  # share state: counters, kill flag, cfg
    else:
        # Paper mode: same gate class, same persisted counters, same config.
        # "What paper does is what live would have done." is_live defaults False
        # so the paper gate halts on the paper (study) leg (#76). The loss-halt
        # bypass is re-read each tick via refresh_overrides and applies here too.
        _risk_gate = build_gate_from_config()
        await _risk_gate.load()
        await _risk_gate.refresh_overrides()

    # Settlement-aligned live spot (issue #21). Inside the dashboard the feed
    # monitor already holds the WS connection, so read that one; otherwise the
    # loop owns a feed that lives and dies with it. Until the first print
    # arrives, ticks journal "settlement feed degraded" and open no entries.
    feed_task: asyncio.Task[None] | None = None
    if _shared_chainlink_feed is not None:
        feed = _shared_chainlink_feed
    else:
        feed = ChainlinkWsFeed(
            url=POLYMARKET_LIVE_DATA_WS,
            stale_after_s=CHAINLINK_STALE_SECONDS,
        )
        feed_task = asyncio.create_task(feed.run())
    _chainlink_feed = feed

    await set_config("polymarket_bot.state", "running")
    await set_config("polymarket_bot.mode", mode)
    # Mark this run's start so the ribbon's session P&L covers THIS run only.
    await set_config(
        "polymarket_bot.session_start", datetime.now(UTC).isoformat(timespec="seconds")
    )
    if mode == "live":
        await _set_detail(
            "BTC LIVE loop running — orders are REAL. "
            f"Kill switch: touch {_config.KILL_SWITCH_PATH} to halt and cancel."
        )
        await notify("live_started", "BTC LIVE bot started — orders are real")
    else:
        await _set_detail("BTC paper loop running. No real orders will be placed.")
        await notify("paper_started", "BTC paper bot started")
    log.info("paper_loop.started", mode=mode)

    # Stream the SELECTED market's books while the loop runs, so _fetch_clob_book
    # reads them instead of polling the venue. hot=True also races a REST read
    # against the sockets on this window. No-op without a dashboard hub.
    _selection = await market_selection.get_selection()
    _marketdata_hub.want(_selection.asset, _selection.timeframe, "bot loop", hot=True)

    # Set when the daily loss halt trips (#76): the loop stops the bot and the
    # finally surfaces this as LAST DETAIL instead of the generic stop line.
    stop_detail: str | None = None
    try:
        while not stop_event.is_set():
            try:
                snapshot = await paper_tick_once()
                await _set_detail(_detail_from_snapshot(snapshot))
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e!s}"
                log.warning("paper_loop.tick_failed", error=error)
                await _set_detail(f"BTC {mode} loop tick failed: {error}")
            # Issue #76: a breached daily loss halt STOPS the bot (cancel +
            # flatten via the finally below), not just blocks entries — so the
            # operator resets the tally and restarts to resume trading.
            stop_detail = _loss_halt_stop_detail(_risk_gate, mode)
            if stop_detail is not None:
                log.warning("paper_loop.loss_halt_stop", detail=stop_detail)
                await notify("loss_halt_stop", stop_detail)
                break
            # Paper breach (#146): entries pause, the loop keeps running —
            # notify once per episode.
            await _notify_paper_halt_pause(_risk_gate, mode)
            _beat()  # #147: iteration completed (even a failed tick beats)
            await _sleep_interruptible(stop_event, float(_knobs.cached('paper_tick_seconds')))
    finally:
        if not _is_current_generation(my_generation):
            # A watchdog respawn superseded this loop while it was wedged
            # (#147). Its stop_event is set, so it exits here — but it must
            # NOT clear the successor's globals or write state=stopped over
            # a running loop. Only its own local feed gets torn down.
            log.warning(
                "paper_loop.superseded_exit", generation=my_generation
            )
            if feed_task is not None:
                feed.stop()
                feed_task.cancel()
            return  # the successor holds the hub demand now: leave it alone
        _marketdata_hub.release("bot loop")
        if _live_executor is not None:
            # Flatten BEFORE dropping the executor: this thread owns it, so
            # Stop can never paper-close a live position (which would strand
            # real tokens on the exchange with a ledger that says flat).
            try:
                await _live_executor.cancel_open(reason="LOOP_STOP")
            except Exception as e:  # noqa: BLE001
                log.warning("live_loop.stop_cancel_failed", error=str(e))
            try:
                await force_close_open_positions("STOP_REQUEST")
            except Exception as e:  # noqa: BLE001
                log.warning("live_loop.stop_flatten_failed", error=str(e))
            _live_executor = None
        _risk_gate = None
        _chainlink_feed = None
        if feed_task is not None:
            feed.stop()
            feed_task.cancel()
            try:
                await feed_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await set_config("polymarket_bot.state", "stopped")
        await _set_detail(
            stop_detail
            or f"BTC {mode} loop stopped. No new entries will be opened."
        )
        await notify("paper_stopped", f"BTC {mode} bot stopped")
        log.info("paper_loop.stopped", mode=mode)


def _make_settlement_client() -> httpx.AsyncClient:
    """HTTP client for the Polymarket reference/settlement reads.

    HTTP/2 is REQUIRED (#120): the ``crypto-price`` endpoint is behind Cloudflare
    bot management, which 403s ("Just a moment...") a Chrome-spoofed request sent
    over HTTP/1.1 — real Chrome speaks HTTP/2, so the UA-vs-protocol mismatch
    reads as a bot. Over HTTP/2 the same request passes (measured 0/5 → 5/5).
    Without the reference price the bot SKIPs every window, so this is
    load-bearing for trading at all.
    """
    return httpx.AsyncClient(timeout=10.0, follow_redirects=True, http2=True)


async def paper_tick_once() -> PaperSnapshot:
    """One trading tick. Useful for tests and dashboard-driven smoke checks."""
    kill_active = False
    if _live_executor is not None:
        # Kill switch is checked every tick BEFORE any order can be placed.
        kill_active = await _live_executor.enforce_kill_switch()
    # Re-read paper override toggles so dashboard changes take effect on the
    # very next tick without needing a Stop/Start. No-op in live mode.
    # The runtime per-trade cap (#50) is re-read for BOTH modes — it is a
    # tuning knob the operator sets from the dashboard, not a paper-only
    # safety override.
    if _risk_gate is not None:
        await _risk_gate.refresh_overrides()
        await _risk_gate.refresh_runtime_limits()
    # Dashboard-editable strategy/behavior knobs (#206) — re-read every tick so
    # an operator change applies without a restart, same as the gate above.
    await _knobs.refresh_cache()
    async with _make_settlement_client() as client:
        snapshot = await _build_snapshot(client)
        await _log_tick(snapshot)
        await _close_due_positions(snapshot, client)
        if not kill_active:
            await _maybe_open_position(snapshot)
    return snapshot


async def force_close_open_positions(exit_reason: str = "STOP_REQUEST") -> int:
    """Close all open positions; returns how many actually closed.

    In live mode a position only counts as closed when the executor confirmed
    the flatten — failed live exits keep their ledger rows OPEN so they are
    retried (or escalated to the operator) instead of stranding real tokens.

    Without a live executor, LIVE rows are never touched: a paper close would
    mark them flat at a fictional price while real tokens stay on the exchange.
    """
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' ORDER BY opened_at"
        ) as cur:
            positions = [dict(r) for r in await cur.fetchall()]
    if _live_executor is None:
        live_rows = [p for p in positions if p.get("mode") == "live"]
        if live_rows:
            log.warning(
                "force_close.live_rows_left_open",
                count=len(live_rows),
                reason="no live executor — flatten on Polymarket",
            )
        positions = [p for p in positions if p.get("mode") != "live"]
    if not positions:
        return 0
    async with _make_settlement_client() as client:
        snapshot = await _build_snapshot(client)
    closed = 0
    for pos in positions:
        if await _close_position(
            pos,
            snapshot,
            _current_price_for_side(snapshot, pos["side"]),
            exit_reason,
        ):
            closed += 1
    return closed


async def count_open_positions(mode: str | None = None) -> int:
    """Number of open rows in the position ledger (optionally one mode's)."""
    sql = "SELECT COUNT(*) AS n FROM paper_positions WHERE state = 'open'"
    params: tuple[str, ...] = ()
    if mode is not None:
        sql += " AND mode = ?"
        params = (mode,)
    async with connect() as db:
        async with db.execute(sql, params) as cur:
            return int((await cur.fetchone())["n"])


def _parse_feed_source(raw: Any) -> dict[str, str]:
    """Parse 'spot=...;ref=...;vol=...;quotes=...' into a dict; lenient on shape."""
    if not isinstance(raw, str):
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _now() -> int:
    """Wall clock in unix seconds — module-level so tests can pin time."""
    return int(time.time())


def _share_sized_notional(
    side: str | None,
    notional: float,
    up_ask: float | None,
    down_ask: float | None,
    trade_shares: float | None,
) -> float:
    """Resize a clip to a fixed share count when the operator set one (#89).

    Returns ``trade_shares × the chosen side's ask`` so the order sizes to ≈N
    shares. Falls through to the original dollar ``notional`` when no share
    target is set, no side was chosen, or the side has no usable ask.
    """
    if side is None or notional <= 0 or trade_shares is None:
        return notional
    side_ask = up_ask if side == "Up" else down_ask
    if side_ask and side_ask > 0:
        return trade_shares * side_ask
    return notional


async def _build_snapshot(client: httpx.AsyncClient) -> PaperSnapshot:
    now = _now()
    market = await _fetch_current_market(client, now)
    start_ts = int(market["window_start_ts"])
    slug = str(market["slug"])
    question = str(market.get("question") or slug)
    remaining = max(0, start_ts + FIVE_MINUTES - now)

    # Gamma is used for market DISCOVERY only. Its outcomePrices are
    # journaled to quantify staleness, never used for pricing (issue #22).
    gamma_up_price, _gamma_down_price = _outcome_prices(market)
    up_token_id, down_token_id = _outcome_token_ids(market)

    # Executable quotes: CLOB top-of-book for both outcome tokens.
    up_book = await _fetch_clob_book(client, up_token_id)
    down_book = await _fetch_clob_book(client, down_token_id)

    # Settlement feed: spot from the Chainlink WS series (REST print poll as
    # fallback — same stream, same levels), reference open from the
    # crypto-price REST API (cached per window).
    spot, chainlink_closes = _chainlink_spot_and_closes()
    spot_source = "chainlink_ws" if spot is not None else "unavailable"
    if spot is None:
        rest_print = await _rest_spot_fallback(client, slug)
        if rest_print is not None:
            spot = rest_print[1]
            spot_source = "chainlink_rest_poll"
    reference = await _get_window_reference(client, slug, start_ts)

    sigma, vol_source = await _sigma_with_fallback(client, chainlink_closes)
    # Directional drift for the regime-adaptive candidates (#94), from the SAME
    # settlement series as sigma. Chainlink-only on purpose: a thin series ->
    # 0.0 -> neutral regime (candidates fall back to their fixed cushion), and
    # the Binance shape-fallback is a different instrument's level path, so its
    # drift direction would be misleading even though its return shape is fine
    # for volatility.
    drift = drift_per_second(chainlink_closes)

    degraded_reason: str | None = None
    if spot is None:
        degraded_reason = "chainlink spot unavailable (WS stale and REST poll failed)"
    elif reference is None:
        degraded_reason = "chainlink reference unavailable (REST)"

    if degraded_reason is None:
        fair_up = fair_up_probability(
            spot, reference, sigma, remaining, print_granularity=PRINT_GRANULARITY_USD
        )
    else:
        fair_up = 0.5

    # Edge against the EXECUTABLE price: a BUY of side X pays X's best ask.
    # A degraded feed pins fair_up at 0.5, so any "edge" against a lopsided
    # book would be an artifact — journal no edge at all in that state.
    # Journaled as market observations for the dashboard; nothing on the
    # trading path acts on them.
    if degraded_reason is None:
        edge_up = fair_up - up_book.best_ask if up_book.buyable else None
        edge_down = (1.0 - fair_up) - down_book.best_ask if down_book.buyable else None
    else:
        edge_up = edge_down = None

    # No strategy is loaded (v0 archived 2026-09-13): never enter. A degraded
    # feed still says so, so the operator sees the more urgent reason.
    side, confidence, notional = None, 0.0, 0.0
    if degraded_reason is not None:
        reason = f"skip: settlement feed degraded ({degraded_reason})"
    else:
        reason = NO_STRATEGY_REASON

    # Share-denominated sizing (#89): when the operator sets a target share count,
    # resize the clip to ≈N shares (notional = N × the chosen side's ask). The
    # live executor / paper fill still auto-bumps to the venue minimum (#87).
    if _risk_gate is not None:
        notional = _share_sized_notional(
            side, notional, up_book.best_ask, down_book.best_ask,
            _risk_gate.trade_shares,
        )

    candidate_edges = [e for e in (edge_up, edge_down) if e is not None]
    edge = max(candidate_edges) if candidate_edges else 0.0

    return PaperSnapshot(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        window_slug=slug,
        market_question=question,
        remaining_seconds=remaining,
        spot_price=spot if spot is not None else 0.0,
        reference_price=reference if reference is not None else 0.0,
        sigma_per_second=sigma,
        drift_per_second=drift,
        market_up_price=up_book.best_ask,
        market_down_price=down_book.best_ask,
        fair_up_prob=fair_up,
        fair_up_prob_raw=fair_up,
        edge=edge,
        signal_side=side,
        confidence=confidence,
        notional_usd=notional,
        reason=reason,
        feed_source=(
            f"spot={spot_source};"
            f"ref={'chainlink_rest' if reference is not None else 'unavailable'};"
            f"vol={vol_source};quotes=clob"
        ),
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        up_best_bid=up_book.best_bid,
        up_best_ask=up_book.best_ask,
        up_bid_size=up_book.bid_size,
        up_ask_size=up_book.ask_size,
        down_best_bid=down_book.best_bid,
        down_best_ask=down_book.best_ask,
        down_bid_size=down_book.bid_size,
        down_ask_size=down_book.ask_size,
        quote_source="clob",
        gamma_up_price=gamma_up_price,
        feed_degraded=degraded_reason is not None,
    )


def _chainlink_spot_and_closes() -> tuple[float | None, list[float]]:
    """Latest Chainlink WS print and 1s series; (None, []) when stale/absent."""
    feed = _chainlink_feed
    if feed is None or not feed.is_fresh():
        return None, []
    latest = feed.latest()
    if latest is None:
        return None, []
    return latest[1], feed.recent_closes()


async def _rest_spot_fallback(
    client: httpx.AsyncClient, slug: str
) -> tuple[float, float] | None:
    """Near-live settlement print via REST when the WS feed is stale/down."""
    connector = _make_settlement_connector(client, slug)
    try:
        return await connector.get_recent_print()
    except Exception as e:  # noqa: BLE001
        log.warning("chainlink_spot.rest_fallback_failed", error=str(e))
        return None


async def _sigma_with_fallback(
    client: httpx.AsyncClient, chainlink_closes: list[float]
) -> tuple[float, str]:
    """Sigma from the Chainlink 1s series; Binance return SHAPE as fallback.

    Binance levels are never compared with Chainlink levels (measured basis
    ~ -$50.7, std $3.8) — log-returns are basis-free, so the fallback only
    borrows the volatility shape.
    """
    if len(chainlink_closes) >= _MIN_CHAINLINK_SIGMA_POINTS:
        return sigma_per_second(chainlink_closes), "chainlink_ws"
    try:
        _, closes = await _fetch_spot_and_recent_closes(client)
        return sigma_per_second(closes), "binance_shape_fallback"
    except Exception as e:  # noqa: BLE001
        log.warning("sigma_fallback.binance_failed", error=str(e))
        return sigma_per_second([]), "floor"


def _make_settlement_connector(
    client: httpx.AsyncClient, slug: str
) -> ChainlinkSettlementConnector:
    """Factory for the REST settlement connector — patchable in tests."""
    return ChainlinkSettlementConnector(
        client,
        api_base=POLYMARKET_CRYPTO_PRICE_API,
        referer_slug=slug,
    )


async def _get_window_reference(
    client: httpx.AsyncClient, slug: str, start_ts: int
) -> float | None:
    """Stabilized Chainlink open print for the window, cached per window."""
    cached = _reference_cache.get(start_ts)
    if cached is not None:
        return cached
    connector = _make_settlement_connector(client, slug)
    try:
        reference = await connector.get_reference_price(start_ts)
    except Exception as e:  # noqa: BLE001
        log.warning("chainlink_reference.fetch_failed", window_start_ts=start_ts, error=str(e))
        return None
    _reference_cache[start_ts] = reference
    # Prune anything older than an hour so the cache cannot grow unbounded.
    for ts in [ts for ts in _reference_cache if ts < start_ts - 3600]:
        _reference_cache.pop(ts, None)
    return reference


async def _fetch_clob_book(client: httpx.AsyncClient, token_id: str) -> BookTop:
    """Top-of-book for one outcome token: the market-data hub, else CLOB /book.

    The hub streams this market's book over the CLOB market channel and serves
    it in microseconds; a REST read is ~200 ms old before it even lands. It
    hands back nothing at all rather than a book older than ``BOOK_MAX_STALE_S``
    or one whose sockets are down, and then this falls back to the REST read
    below (also when the process runs without a hub at all).

    CLOB books list levels worst-to-best, so the best level is the LAST
    element of each array (same convention as the live executor's
    ``_book_context``). Returns an empty book on any error — an empty book
    blocks entries, never crashes the tick.
    """
    if not token_id:
        return EMPTY_BOOK
    streamed = _marketdata_hub.book_top(token_id, _marketdata_hub.BOOK_MAX_STALE_S)
    if streamed is not None:
        return BookTop(best_bid=streamed.best_bid, best_ask=streamed.best_ask,
                       bid_size=streamed.bid_size, ask_size=streamed.ask_size)
    try:
        r = await client.get(
            f"{POLYMARKET_CLOB_API}/book", params={"token_id": token_id}
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("clob_book.fetch_failed", token_id=token_id[:16], error=str(e))
        return EMPTY_BOOK
    if not isinstance(data, dict):
        return EMPTY_BOOK
    best_bid, bid_size = _best_level(data.get("bids"))
    best_ask, ask_size = _best_level(data.get("asks"))
    return BookTop(
        best_bid=best_bid, best_ask=best_ask, bid_size=bid_size, ask_size=ask_size
    )


def _best_level(levels: Any) -> tuple[float | None, float | None]:
    """(price, size) of the best level — the LAST element of a CLOB array."""
    if not isinstance(levels, list) or not levels:
        return None, None
    best = levels[-1]
    if not isinstance(best, dict):
        return None, None
    try:
        return float(best["price"]), float(best["size"])
    except (KeyError, TypeError, ValueError):
        return None, None


async def _fetch_current_market(client: httpx.AsyncClient, now: int) -> dict[str, Any]:
    current_start = now - (now % FIVE_MINUTES)
    # Try current first, then next and previous to handle boundary/API timing.
    for start_ts in (current_start, current_start + FIVE_MINUTES, current_start - FIVE_MINUTES):
        slug = f"btc-updown-5m-{start_ts}"
        data = await _gamma_get(client, "markets", {"slug": slug})
        market = _first(data)
        if market is None:
            event_data = await _gamma_get(client, "events", {"slug": slug})
            event = _first(event_data)
            markets = event.get("markets") if event else None
            market = markets[0] if isinstance(markets, list) and markets else None
        if market is None:
            continue
        market["window_start_ts"] = start_ts
        return market
    raise RuntimeError("Could not discover current BTC 5-minute Polymarket market.")


async def _gamma_get(
    client: httpx.AsyncClient, endpoint: str, params: dict[str, Any]
) -> Any:
    r = await client.get(f"{POLYMARKET_GAMMA_API}/{endpoint}", params=params)
    r.raise_for_status()
    return r.json()


async def _fetch_spot_and_recent_closes(client: httpx.AsyncClient) -> tuple[float, list[float]]:
    r = await client.get(
        f"{BINANCE_API}/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1s", "limit": 90},
    )
    r.raise_for_status()
    rows = r.json()
    closes = [float(row[4]) for row in rows if len(row) > 4]
    if not closes:
        raise RuntimeError("Binance returned no BTC closes.")
    return closes[-1], closes


def _outcome_prices(market: dict[str, Any]) -> tuple[float, float]:
    prices = _json_list(market.get("outcomePrices"))
    outcomes = _json_list(market.get("outcomes"))
    if len(prices) != 2:
        raise RuntimeError("BTC market did not expose two outcome prices.")
    up_idx = 0
    if len(outcomes) == 2:
        labels = [str(x).lower() for x in outcomes]
        if "up" in labels:
            up_idx = labels.index("up")
    down_idx = 1 - up_idx
    return float(prices[up_idx]), float(prices[down_idx])


def _outcome_token_ids(market: dict[str, Any]) -> tuple[str, str]:
    """(up_token_id, down_token_id) from the gamma market's clobTokenIds.

    The clobTokenIds list is aligned with the outcomes list. Returns empty
    strings when unavailable — live entries are then blocked, never guessed.
    """
    token_ids = _json_list(market.get("clobTokenIds"))
    if len(token_ids) != 2:
        return "", ""
    outcomes = _json_list(market.get("outcomes"))
    up_idx = 0
    if len(outcomes) == 2:
        labels = [str(x).lower() for x in outcomes]
        if "up" in labels:
            up_idx = labels.index("up")
    return str(token_ids[up_idx]), str(token_ids[1 - up_idx])


def _token_id_for_side(snapshot: PaperSnapshot, side: str) -> str:
    return snapshot.up_token_id if side == "Up" else snapshot.down_token_id


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _first(value: Any) -> dict[str, Any] | None:
    if isinstance(value, list) and value:
        first = value[0]
        return first if isinstance(first, dict) else None
    return None


async def _log_tick(snapshot: PaperSnapshot) -> None:
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO paper_ticks(
              created_at, window_slug, market_question, remaining_seconds,
              spot_price, reference_price, sigma_per_second, market_up_price,
              market_down_price, fair_up_prob, edge, signal_side, confidence,
              notional_usd, feed_source, reason,
              up_best_bid, up_best_ask, up_bid_size, up_ask_size,
              down_best_bid, down_best_ask, down_bid_size, down_ask_size,
              quote_source, gamma_up_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.created_at,
                snapshot.window_slug,
                snapshot.market_question,
                snapshot.remaining_seconds,
                snapshot.spot_price,
                snapshot.reference_price,
                snapshot.sigma_per_second,
                snapshot.market_up_price,
                snapshot.market_down_price,
                snapshot.fair_up_prob,
                snapshot.edge,
                snapshot.signal_side,
                snapshot.confidence,
                snapshot.notional_usd,
                snapshot.feed_source,
                snapshot.reason,
                snapshot.up_best_bid,
                snapshot.up_best_ask,
                snapshot.up_bid_size,
                snapshot.up_ask_size,
                snapshot.down_best_bid,
                snapshot.down_best_ask,
                snapshot.down_bid_size,
                snapshot.down_ask_size,
                snapshot.quote_source,
                snapshot.gamma_up_price,
            ),
        )
        await db.commit()


async def _maybe_open_position(snapshot: PaperSnapshot) -> None:
    # Operator switch (STRATEGIES card): off gates ENTRIES only. Start/Stop
    # still owns the loop itself, and every exit/settlement path below runs
    # regardless, so switching off never strands an open position.
    if not await _strategies.enabled("btc_updown"):
        return
    if not snapshot.signal_side or snapshot.notional_usd <= 0:
        return
    async with connect() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM paper_positions WHERE state = 'open'"
        ) as cur:
            if (await cur.fetchone())["n"]:
                return
        if _knobs.cached('exit_style') == "settle":
            # One entry per window, ever (issue #28): re-entering the same
            # window after an exit pays the spread again for the same signal
            # — the churn that lost the scalp-style soak.
            async with db.execute(
                "SELECT COUNT(*) AS n FROM paper_positions WHERE window_slug = ?",
                (snapshot.window_slug,),
            ) as cur:
                if (await cur.fetchone())["n"]:
                    return
    # Honest paper fill (issue #22): a BUY fills at the side's best ASK,
    # capped by the top-of-book size. No ask means no executable entry.
    entry_price = (
        snapshot.market_up_price
        if snapshot.signal_side == "Up"
        else snapshot.market_down_price
    )
    if entry_price is None or entry_price <= 0:
        log.info(
            "paper_entry.skipped_no_ask",
            window_slug=snapshot.window_slug,
            side=snapshot.signal_side,
        )
        return
    top_size = (
        snapshot.up_ask_size if snapshot.signal_side == "Up" else snapshot.down_ask_size
    )
    notional = snapshot.notional_usd
    shares = notional / entry_price
    # Parity with live auto-bump (#87): round a sub-minimum clip up to the venue
    # share minimum so paper previews the same fill live would place.
    if shares < DEFAULT_MIN_ORDER_SIZE:
        shares = DEFAULT_MIN_ORDER_SIZE
        notional = shares * entry_price
    if top_size is not None:
        if top_size <= 0:
            log.info(
                "paper_entry.skipped_empty_top_of_book",
                window_slug=snapshot.window_slug,
                side=snapshot.signal_side,
            )
            return
        if shares > top_size:
            shares = top_size
            notional = shares * entry_price
            log.info(
                "paper_entry.size_capped_to_top_of_book",
                window_slug=snapshot.window_slug,
                side=snapshot.signal_side,
                shares=round(shares, 4),
                notional=round(notional, 4),
            )

    executor = _live_executor
    if executor is not None:
        # The ledger is authoritatively flat here (the open-row check above
        # returned none). A live ledger row closes only after a confirmed
        # venue flatten, so heal any phantom in-memory open-state the executor
        # may still hold (e.g. left by an interrupted stop/restart) — otherwise
        # its singleton gate blocks every entry with "max 1" (issue #91).
        await executor.resync_flat()
        # Live mode: the ledger row is written BEFORE the real order goes
        # out. Every failure mode of this ordering is benign: a failed
        # submit deletes the row (or, if even that write fails, the row is
        # later closed as a confirmed zero-fill), whereas submitting first
        # could leave a REAL position with no ledger row — unmanaged by
        # every exit path. The shared RiskGate runs inside submit_entry.
        position_id = await _insert_position_row(
            snapshot, entry_price, notional, shares
        )
        result = await executor.submit_entry(
            token_id=_token_id_for_side(snapshot, snapshot.signal_side),
            side_price=entry_price,
            notional_usd=notional,
            window_slug=snapshot.window_slug,
        )
        if not result.ok:
            # Blocked/error — journaled in live_orders. Remove the
            # provisional row so the ledger mirrors live intent.
            await _delete_position_row(position_id)
            return
        entry_price = result.price or entry_price
        notional = result.notional_usd or notional
        shares = result.size or (notional / entry_price)
        await _update_position_terms(position_id, entry_price, notional, shares)
    else:
        # Paper mode: route the entry through the SAME RiskGate live uses
        # (issue #64). A paper trade that paper opens is one live would have
        # opened too; a paper trade live would have blocked is recorded in
        # live_orders with mode='paper' instead.
        gate = _risk_gate
        if gate is not None:
            blocked = gate.block_reason(
                EntryRequest(
                    notional_usd=notional,
                    position_open=False,  # already checked the ledger above
                    entry_order_resting=False,  # paper has no resting orders
                    # Same snapshot for both → slippage delta is 0; honest
                    # live-grade slippage parity requires re-quoting the book
                    # at this point (follow-up after issue #64).
                    side_price=entry_price,
                    best_ask=entry_price,
                )
            )
            if blocked is not None:
                await journal_live_order(
                    intent="ENTRY", side="BUY", status="BLOCKED",
                    window_slug=snapshot.window_slug,
                    token_id=_token_id_for_side(snapshot, snapshot.signal_side),
                    price=entry_price, size=shares, notional_usd=notional,
                    error=blocked, mode="paper",
                )
                log.info(
                    "paper_entry.blocked_by_gate",
                    window_slug=snapshot.window_slug,
                    side=snapshot.signal_side,
                    reason=blocked,
                )
                return
        await _insert_position_row(snapshot, entry_price, notional, shares)
        if gate is not None:
            await gate.record_buy_notional(round(entry_price * shares, 4))
    label = "LIVE" if executor is not None else "Paper"
    await notify(
        "live_entry" if executor is not None else "paper_entry",
        f"{label} BUY {snapshot.signal_side} ${notional:.2f} @ {entry_price:.3f}",
        {"window_slug": snapshot.window_slug, "confidence": snapshot.confidence},
    )
    log.info(
        "paper_position.opened",
        mode="live" if executor is not None else "paper",
        window_slug=snapshot.window_slug,
        side=snapshot.signal_side,
        notional=notional,
        entry_price=entry_price,
    )


async def _insert_position_row(
    snapshot: PaperSnapshot, entry_price: float, notional: float, shares: float
) -> int:
    mode = "live" if _live_executor is not None else "paper"
    async with connect() as db:
        cur = await db.execute(
            """
            INSERT INTO paper_positions(
              opened_at, window_slug, market_question, side, state, entry_price,
              notional_usd, shares, opened_spot, confidence, edge, entry_reason,
              feed_source, quote_source, strategy_style, mode
            ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.created_at,
                snapshot.window_slug,
                snapshot.market_question,
                snapshot.signal_side,
                entry_price,
                notional,
                shares,
                snapshot.spot_price,
                snapshot.confidence,
                snapshot.edge,
                snapshot.reason,
                snapshot.feed_source,
                snapshot.quote_source,
                _knobs.cached('exit_style'),
                mode,
            ),
        )
        position_id = int(cur.lastrowid or 0)
        await db.commit()
    return position_id


async def _delete_position_row(position_id: int) -> None:
    async with connect() as db:
        await db.execute(
            "DELETE FROM paper_positions WHERE position_id = ?", (position_id,)
        )
        await db.commit()


async def _update_position_terms(
    position_id: int, entry_price: float, notional: float, shares: float
) -> None:
    """Overwrite a provisional row with the terms the executor actually used."""
    async with connect() as db:
        await db.execute(
            "UPDATE paper_positions SET entry_price = ?, notional_usd = ?, "
            "shares = ? WHERE position_id = ?",
            (entry_price, notional, shares, position_id),
        )
        await db.commit()


async def _close_due_positions(
    snapshot: PaperSnapshot, client: httpx.AsyncClient
) -> None:
    async with connect() as db:
        async with db.execute(
            "SELECT * FROM paper_positions WHERE state = 'open' ORDER BY opened_at"
        ) as cur:
            positions = [dict(r) for r in await cur.fetchall()]

    for pos in positions:
        if pos["window_slug"] != snapshot.window_slug:
            await _close_rolled_position(pos, snapshot, client)
            continue
        # Honest paper exit (issue #22): a SELL receives the side's best BID.
        # No bid means no executable exit — hold and retry next tick.
        exit_price = _current_price_for_side(snapshot, pos["side"])
        if exit_price is None:
            log.info(
                "paper_exit.held_no_bid",
                position_id=pos["position_id"],
                window_slug=pos["window_slug"],
                side=pos["side"],
            )
            continue
        reason = _exit_reason(snapshot, pos, exit_price)
        if reason is None:
            continue
        await _close_position(pos, snapshot, exit_price, reason)


async def _close_rolled_position(
    pos: dict[str, Any], snapshot: PaperSnapshot, client: httpx.AsyncClient
) -> bool:
    """Close a position whose 5-minute window has rolled.

    Live mode keeps the established path: the executor cancels any resting
    entry and flattens at the REAL book; the price passed here is advisory.

    Paper mode: the old window has resolved (or is resolving), so the only
    honest exit value is the SETTLEMENT payout — 1.0 when the held side won
    (Chainlink close >= open resolves Up), else 0.0 — read from the same
    crypto-price endpoint that defines resolution. The pre-fix behavior
    (pricing the OLD position off the NEW window's quote) was fiction.
    While settlement is not yet readable the row is held and retried.
    """
    if _live_executor is not None and _knobs.cached('exit_style') != "settle":
        advisory = _current_price_for_side(snapshot, pos["side"])
        if advisory is None:
            advisory = float(pos["entry_price"])
        return await _close_position(pos, snapshot, advisory, "WINDOW_ROLL")
    won = await _settle_position_outcome(pos, client)
    if won is None:
        log.info(
            "paper_exit.window_roll_awaiting_settlement",
            position_id=pos["position_id"],
            window_slug=pos["window_slug"],
        )
        return False
    settled_held: float | None = None
    settled_pnl: float | None = None
    if _live_executor is not None:
        # Settle-style live: no exit order — register the resolution with the
        # executor (PnL into the daily halt, slot freed; winning tokens await
        # operator redemption per the runbook). A failed registration keeps
        # the row open and retries next tick.
        result = await _live_executor.record_settlement(won, pos["window_slug"])
        if not result.ok and result.status != "SKIPPED":
            return False
        # Real held size the venue actually settled (0 when the entry never
        # filled) — the ledger books on THIS, not the recorded shares (#103) —
        # and the executor's fee-true realized PnL (#133), so the ledger row
        # matches the journal and the daily halt to the cent.
        settled_held = result.size or 0.0
        settled_pnl = result.notional_usd
    return await _close_position(
        pos, snapshot, 1.0 if won else 0.0, "WINDOW_ROLL",
        settled=True, settled_held=settled_held, settled_pnl=settled_pnl,
    )


async def _settle_position_outcome(
    pos: dict[str, Any], client: httpx.AsyncClient
) -> bool | None:
    """True/False when the held side won/lost; None while not yet settleable."""
    start_ts = _window_start_from_slug(pos["window_slug"])
    if start_ts is None:
        log.warning(
            "paper_exit.unparseable_window_slug", window_slug=pos["window_slug"]
        )
        return None
    connector = _make_settlement_connector(client, pos["window_slug"])
    try:
        up_won = await connector.settle_window(start_ts)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "paper_exit.settlement_read_failed",
            window_slug=pos["window_slug"],
            error=str(e),
        )
        return None
    if up_won is None:
        return None
    return up_won if pos["side"] == "Up" else (not up_won)


def _window_start_from_slug(slug: str) -> int | None:
    """Unix start ts from a btc-updown-5m-<ts> slug, or None."""
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


async def _close_position(
    pos: dict[str, Any],
    snapshot: PaperSnapshot,
    exit_price: float,
    reason: str,
    settled: bool = False,
    settled_held: float | None = None,
    settled_pnl: float | None = None,
) -> bool:
    """Close one position; returns True when the ledger row was closed.

    Live mode only closes the row when the executor CONFIRMED the flatten
    (or confirmed the entry never filled). A blocked/failed/unfilled live
    exit keeps the row open so it is retried on the next tick — the ledger
    must never claim flat while real tokens remain on the exchange. Realized
    PnL for the daily loss halt is recorded inside the executor on confirmed
    fills, never here from paper prices.

    ``settled=True`` means the market resolved and the executor (if any)
    already registered the outcome via ``record_settlement`` — no exit order
    is placed and the row closes at the settlement payout.
    """
    executor = _live_executor
    entry_price = float(pos["entry_price"])
    prior_pnl = float(pos["realized_pnl_usd"] or 0.0)
    if executor is not None and not settled:
        # Window roll / band re-entry must first cancel any unfilled resting
        # entry order before flattening whatever did fill.
        if reason in ("WINDOW_ROLL", "BAND_REENTRY"):
            await executor.cancel_open(reason=reason)
        exit_result = await executor.submit_exit(
            side_price=exit_price,
            size=float(pos["shares"]),
            window_slug=pos["window_slug"],
        )
        if not exit_result.ok and exit_result.status != "SKIPPED":
            # BLOCKED / ERROR / UNFILLED: real tokens may still be held.
            # Record any partial fill into the row, keep it OPEN, and retry.
            tranche_size = exit_result.size or 0.0
            if tranche_size > 0:
                tranche_price = (
                    exit_result.price if exit_result.price is not None else exit_price
                )
                prior_pnl += tranche_size * (tranche_price - entry_price)
                async with connect() as db:
                    await db.execute(
                        "UPDATE paper_positions SET realized_pnl_usd = ? "
                        "WHERE position_id = ?",
                        (prior_pnl, pos["position_id"]),
                    )
                    await db.commit()
            log.warning(
                "live_exit.failed_position_kept_open",
                position_id=pos["position_id"],
                window_slug=pos["window_slug"],
                status=exit_result.status,
                reason=exit_result.reason,
            )
            return False
        if exit_result.status == "SKIPPED":
            # Confirmed: the entry never filled, so nothing real existed.
            pnl = prior_pnl
        else:
            sold = exit_result.size or 0.0
            if exit_result.price is not None:
                exit_price = exit_result.price
            pnl = prior_pnl + sold * (exit_price - entry_price)
    elif executor is not None:
        # Live settled (#103). record_settlement already booked the REAL held-size
        # PnL to the LIVE counter, so the ledger row must use that same real held
        # — NOT the recorded shares, which can include an entry that never filled
        # (a phantom that would otherwise book a fictional win). And we must NOT
        # re-book here: a second record_realized_pnl(is_live=False) would
        # double-count this live PnL into the PAPER leg (the bug behind
        # paper_pnl mirroring live_pnl).
        held = settled_held if settled_held is not None else float(pos["shares"])
        # Prefer the executor's realized number — it is net of the entry taker
        # fee (#133); the price×size fallback covers older callers only.
        realized = (
            settled_pnl
            if settled_pnl is not None
            else held * (exit_price - entry_price)
        )
        pnl = prior_pnl + realized
    else:
        pnl = float(pos["shares"]) * (exit_price - entry_price)
        # Paper closes feed the SAME daily-loss-halt counter live closes do
        # (issue #64). Without this, paper losses don't advance the halt and
        # paper diverges from what live would have done next.
        gate = _risk_gate
        if gate is not None:
            await gate.record_realized_pnl(round(pnl - prior_pnl, 4), is_live=False)
    async with connect() as db:
        await db.execute(
            """
            UPDATE paper_positions
            SET state = 'closed', closed_at = ?, exit_price = ?,
                closed_spot = ?, exit_reason = ?, realized_pnl_usd = ?
            WHERE position_id = ?
            """,
            (
                snapshot.created_at,
                exit_price,
                snapshot.spot_price,
                reason,
                pnl,
                pos["position_id"],
            ),
        )
        await db.commit()
    await notify(
        "live_exit" if executor is not None else "paper_exit",
        f"{'LIVE' if executor is not None else 'Paper'} EXIT {pos['side']} ${pnl:+.2f} ({reason})",
        {"window_slug": pos["window_slug"], "position_id": pos["position_id"]},
    )
    log.info(
        "paper_position.closed",
        mode="live" if executor is not None else "paper",
        position_id=pos["position_id"],
        window_slug=pos["window_slug"],
        side=pos["side"],
        pnl=round(pnl, 4),
        exit_reason=reason,
    )
    return True


def _current_price_for_side(snapshot: PaperSnapshot, side: str) -> float | None:
    """EXECUTABLE exit price: the held side's best BID (what a SELL receives).

    Selling at the bid (after buying at the ask) is what makes the paper
    fill model pay the spread like a real taker. None when the book shows
    no bid this tick.
    """
    return snapshot.up_best_bid if side == "Up" else snapshot.down_best_bid


def _exit_reason(snapshot: PaperSnapshot, pos: dict[str, Any], exit_price: float) -> str | None:
    if _knobs.cached('exit_style') == "settle":
        # Hold to resolution: the only exits are WINDOW_ROLL settlement
        # (handled in _close_rolled_position) and operator stop. Intra-window
        # marks against the bid are noise, not realized outcomes.
        return None
    if pos["window_slug"] != snapshot.window_slug:
        return "WINDOW_ROLL"
    entry_price = float(pos["entry_price"])
    notional = float(pos["notional_usd"])
    shares = float(pos["shares"])
    pnl = shares * (exit_price - entry_price)
    if snapshot.remaining_seconds <= _knobs.cached('paper_time_exit_seconds'):
        return "TIME"
    if pnl >= notional * _knobs.cached('paper_target_return'):
        return "TARGET"
    if pnl <= notional * _knobs.cached('paper_stop_return'):
        return "STOP"
    # Pricing-model-based exit only when the pricing model is trustworthy this tick:
    # a degraded settlement feed or an unquotable book pins edge near zero,
    # which must not masquerade as "the edge genuinely collapsed".
    if (
        not snapshot.feed_degraded
        and snapshot.has_executable_quote
        and abs(snapshot.edge) < _config.PAPER_ENTRY_EDGE_MIN / 2
    ):
        return "BAND_REENTRY"
    return None


async def _set_detail(detail: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    await set_config("polymarket_bot.updated_at", now)
    await set_config("polymarket_bot.detail", detail)


def _detail_from_snapshot(snapshot: PaperSnapshot) -> str:
    side = snapshot.signal_side or "SKIP"
    if _live_executor is not None:
        header = (
            "BTC LIVE loop running — orders are REAL. "
            f"Kill switch: touch {_config.KILL_SWITCH_PATH}.\n\n"
        )
    else:
        header = "BTC paper loop running. No real orders are placed.\n\n"
    return header + (
        f"Window: {snapshot.window_slug} ({snapshot.remaining_seconds}s left)\n"
        f"Spot: ${snapshot.spot_price:,.2f} vs ref ${snapshot.reference_price:,.2f}\n"
        f"Polymarket Up: {snapshot.market_up_price:.3f}; fair Up: {snapshot.fair_up_prob:.3f}; "
        f"edge: {snapshot.edge:+.3f}\n"
        f"Signal: {side}; confidence {snapshot.confidence:.2f}; notional ${snapshot.notional_usd:.0f}\n"
        f"Gate: {_gate_preview_line(snapshot)}\n"
        f"{_feed_label(snapshot.feed_source)}"
    )


# Human labels for the per-component feed_source tokens (issue #151). The old
# hardcoded "Binance public fallback" line misstated the settlement story: spot
# and reference resolve on Chainlink — Polymarket's own settlement feed (#21) —
# and only the volatility SHAPE ever falls back to Binance.
_FEED_SOURCE_LABELS = {
    "chainlink_ws": "Chainlink WS",
    "chainlink_rest_poll": "Chainlink REST-poll",
    "chainlink_rest": "Chainlink REST",
    "binance_shape_fallback": "Binance (vol shape)",
    "binance_public": "Binance public",
    "binance": "Binance",
    "clob": "CLOB",
    "unavailable": "unavailable",
}


def _feed_label(feed_source: str) -> str:
    """Human-readable feed line derived from the actual per-component sources.

    ``feed_source`` is ``spot=…;ref=…;vol=…;quotes=…`` (see
    :func:`_parse_feed_source`). Rendering the real sources — rather than a
    fixed string — lets the status panel tell the truth about the
    settlement-critical spot/reference feeds (Chainlink) versus the
    volatility-shape input that may fall back to Binance (issue #151).

    A trailing qualifier states settlement alignment plainly: Polymarket
    resolves each window on its Chainlink BTC/USD print, so what matters is
    that spot and reference are Chainlink; a Binance vol-shape fallback is
    cosmetic to settlement, whereas spot leaving Chainlink is a real warning.
    """
    parts = _parse_feed_source(feed_source)
    if not parts:
        return "Feed: source unavailable"

    def lbl(token: str | None) -> str:
        if not token:
            return "—"
        return _FEED_SOURCE_LABELS.get(token, token)

    segments = [
        f"spot {lbl(parts.get('spot'))}",
        f"ref {lbl(parts.get('ref'))}",
        f"vol {lbl(parts.get('vol'))}",
    ]
    if parts.get("quotes"):
        segments.append(f"quotes {lbl(parts.get('quotes'))}")
    line = "Feed: " + " · ".join(segments)

    spot_src = parts.get("spot", "") or ""
    ref_src = parts.get("ref", "") or ""
    if spot_src.startswith("chainlink") and ref_src.startswith("chainlink"):
        line += " (settlement-aligned)"
    elif not spot_src.startswith("chainlink"):
        line += " (⚠ spot off Chainlink — settlement risk)"
    return line


def _gate_preview_line(snapshot: PaperSnapshot) -> str:
    """Live-equivalent verdict for the next entry, surfaced on every tick.

    Same RiskGate decides paper and live, so this line is the operator's
    single source of truth: paper-side BLOCKED rows in live_orders carry
    the same reason, and live-mode behaviour is identical.
    """
    gate = _risk_gate
    if gate is None or not snapshot.signal_side or snapshot.notional_usd <= 0:
        return "—"
    entry_price = (
        snapshot.market_up_price
        if snapshot.signal_side == "Up"
        else snapshot.market_down_price
    )
    side_price = entry_price if entry_price and entry_price > 0 else None
    reason = gate.block_reason(
        EntryRequest(
            notional_usd=snapshot.notional_usd,
            position_open=False,  # ledger check is separate; preview only
            entry_order_resting=False,
            side_price=side_price,
            best_ask=side_price,
        )
    )
    if reason is None:
        return "OK"
    return f"BLOCK — {reason}"


async def _sleep_interruptible(stop_event: threading.Event, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if stop_event.is_set():
            return
        await asyncio.sleep(min(0.25, deadline - time.monotonic()))
