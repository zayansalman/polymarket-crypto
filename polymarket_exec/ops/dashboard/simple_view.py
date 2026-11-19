"""Data for the simple paper-trading screen (``GET /`` and ``GET /api/simple``).

The simple screen is the everyday view of the BTC paper bot: practice balance,
Start/Stop, the current Up/Down window and what the bot is doing about it in
plain words, the open trades, a balance chart, and the trade history. Every
money figure is after trading fees.

Read-only by design. Everything here SELECTs from the SQLite ledger the paper
loop already writes (``paper_ticks``, ``paper_positions``, ``paper_wallet``,
``paper_wallet_transactions``, paper ``BLOCKED`` rows in ``live_orders``, and
``notification_feed``). Nothing here starts or stops the loop, moves virtual
cash, or touches the live executor. The page that consumes it is rendered
WITHOUT the per-process dashboard token, so it can never select or start LIVE.

``build_payload`` is a pure function of the raw rows, so the wording and the
arithmetic are unit-testable without a running loop or network.
"""
from __future__ import annotations

import bisect
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from db import connect
from polymarket_bot import learning as _learning
from polymarket_bot import paper_wallet as _wallet
from polymarket_bot.shadow.fees import DEFAULT_TAKER_FEE_RATE, taker_fee_per_share

WINDOW_SECONDS: dict[str, int] = {"5m": 300, "15m": 900}
TRADE_ROWS = 30
CURVE_POINTS_MAX = 400
ACTIVITY_ROWS = 8
# Strategy comparison: a verdict needs this many settled trades, and "beyond
# luck" uses a strict interval because ~11 strategies are compared at once
# (z = 2.85 is roughly a 95% band after a Bonferroni-style correction).
STRATEGY_MIN_TRADES = 50
STRATEGY_Z = 2.85
# Every row of the comparison is counted at the shadows' size (5 shares a
# trade, shadow/runner.py SHADOW_SHARES), so this bot's bigger trades are
# scaled down to match instead of looking 2x better or worse.
STRATEGY_SHARES = 5.0
# Display order and plain-English names; ids are model_shadow_positions.model_id
# ("main" is this bot's own paper trades since learning started).
STRATEGIES: tuple[tuple[str, str, str], ...] = (
    ("main", "This bot", "Your settings with learning"),
    ("bot_raw_twin_v1", "Same bot without learning",
     "Your settings with its raw math, so the gap shows what learning adds"),
    ("learned_late_v1", "Learning, late entries only", "Only buys with 1 to 3 minutes left"),
    ("learned_mid_v1", "Learning, 60¢ to 90¢ shares only", "Skips cheap and very expensive shares"),
    ("raw_wide_v1", "No edge limit",
     "Raw math with the fee check, any edge of 3¢ or more, no upper limit"),
    ("learned_wide_v1", "Learning, no edge limit",
     "Learned chances, any edge of 3¢ or more, no upper limit"),
    ("pricing_v0", "Original rules",
     "The bot's rules before the fee check and learning, with no daily spending limit"),
    ("cushion_favorite_v2", "Clear favorite only",
     "Only when Bitcoin is clearly on one side of the price to beat"),
    ("cushion_fresh_v7", "Clear favorite, first minute",
     "Clear favorite, only in a window's first 60 seconds"),
    ("cushion_fresh_v7_f45", "Clear favorite, first 45 seconds",
     "Clear favorite, only in a window's first 45 seconds"),
    ("pricing_fresh_v8", "First minute only", "Original rules, only in a window's first 60 seconds"),
)

# Venue taker fee per share for a fill at price ``p``: rate*p*(1-p)
# (polymarket_bot/shadow/fees.py, the same formula the live ledger books).
# Since 2026-11-18 the paper wallet PAYS it on every taker fill (``fee_usd`` on
# each wallet transaction; earlier trades were charged once, see
# paper_wallet.book_past_fees), so every profit, loss and balance here is
# after fees.
TAKER_FEE_RATE = DEFAULT_TAKER_FEE_RATE

# Calibration buckets for "How accurate is the bot?" (the bot's own chance of
# winning for the side it bought, at entry).
ACCURACY_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.70, "Under 70%"),
    (0.70, 0.85, "70% to 85%"),
    (0.85, 0.95, "85% to 95%"),
    (0.95, 1.01, "95% or more"),
)

_SLUG_RE = re.compile(r"^(?P<asset>[a-z]+)-updown-(?P<tf>\d+m)-(?P<start>\d{9,11})$")

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def parse_window_slug(slug: str | None) -> dict[str, Any] | None:
    """``btc-updown-5m-1700000000`` -> asset/timeframe/start/end, or None."""
    if not slug:
        return None
    match = _SLUG_RE.match(str(slug))
    if match is None:
        return None
    timeframe = match.group("tf")
    seconds = WINDOW_SECONDS.get(timeframe)
    if seconds is None:
        return None
    start = int(match.group("start"))
    return {
        "asset": match.group("asset"),
        "timeframe": timeframe,
        "start_ts": start,
        "end_ts": start + seconds,
    }


def iso_to_ts(value: Any) -> float | None:
    """ISO-8601 (``Z`` or offset, naive = UTC) -> unix seconds, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _num(value: Any) -> float | None:
    """Finite float or None (journal columns are nullable)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _price(value: Any) -> float | None:
    """A share price in [0, 1], or None."""
    out = _num(value)
    return out if out is not None and 0.0 <= out <= 1.0 else None


def estimated_fee(shares: float | None, price: float | None) -> float:
    """Estimated taker fee in USD for ``shares`` filled at ``price``."""
    if not shares or price is None or not 0.0 < price < 1.0:
        return 0.0
    return round(float(shares) * taker_fee_per_share(price, TAKER_FEE_RATE), 4)


def _cents(value: float) -> str:
    """0.045 -> '4.5¢' (one decimal, trailing .0 dropped)."""
    text = f"{value * 100:.1f}".rstrip("0").rstrip(".")
    return f"{text}¢"


def _clock(ts: float | None) -> str:
    """Unix seconds -> 'HH:MM UTC' for server-side sentences (the page shows local)."""
    if ts is None:
        return "the next window"
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M UTC")


class _Running:
    """Running total of ``(wallet txn id, amount)`` events as of a given id."""

    def __init__(self, events: list[tuple[int, float]]) -> None:
        pts = sorted(events)
        self._ids = [i for i, _ in pts]
        self._sums: list[float] = []
        run = 0.0
        for _, amount in pts:
            run += amount
            self._sums.append(run)

    def at(self, txn_id: Any) -> float:
        try:
            tid = int(txn_id)
        except (TypeError, ValueError):
            return 0.0
        k = bisect.bisect_right(self._ids, tid)
        return self._sums[k - 1] if k else 0.0


def downsample(points: list[dict[str, Any]], limit: int = CURVE_POINTS_MAX) -> list[dict[str, Any]]:
    """Keep at most ``limit`` points, always keeping the first and the last."""
    if len(points) <= limit or limit < 2:
        return points
    step = (len(points) - 1) / (limit - 1)
    picked = [points[round(i * step)] for i in range(limit - 1)]
    picked.append(points[-1])
    return picked


# ---------------------------------------------------------------------------
# Raw rows (read-only SQL)
# ---------------------------------------------------------------------------

_TRADES_SQL = """
SELECT p.position_id, p.opened_at, p.closed_at, p.window_slug, p.side, p.state,
       p.entry_price, p.exit_price, p.shares, p.exit_reason,
       -b.amount_usd AS cost_usd, b.fee_usd AS buy_fee_usd,
       x.transaction_type AS exit_type, x.amount_usd AS exit_usd, x.created_at AS exit_at,
       x.id AS exit_id, x.fee_usd AS exit_fee_usd
  FROM paper_positions p
  JOIN paper_wallet_transactions b
    ON b.position_id = p.position_id AND b.transaction_type = 'BUY'
  LEFT JOIN paper_wallet_transactions x
    ON x.position_id = p.position_id AND x.transaction_type IN ('SELL', 'SETTLEMENT')
 ORDER BY p.opened_at DESC, p.position_id DESC
 LIMIT ?
"""

# Every finished wallet trade, for the totals (not just the rows on screen).
# pnl_usd is after the fees both fills paid.
_CLOSED_SQL = """
SELECT p.position_id, p.shares, p.entry_price, p.exit_price, p.edge,
       b.created_at AS opened_at, b.id AS buy_id, b.fee_usd AS buy_fee_usd,
       -b.amount_usd AS cost_usd,
       x.transaction_type AS exit_type, x.id AS exit_id, x.fee_usd AS exit_fee_usd,
       x.amount_usd AS payout_usd,
       b.fee_usd + x.fee_usd AS fee_usd,
       x.amount_usd + b.amount_usd - b.fee_usd - x.fee_usd AS pnl_usd
  FROM paper_wallet_transactions b
  JOIN paper_wallet_transactions x
    ON x.position_id = b.position_id AND x.transaction_type IN ('SELL', 'SETTLEMENT')
  JOIN paper_positions p ON p.position_id = b.position_id
 WHERE b.transaction_type = 'BUY'
"""

_OPEN_SQL = """
SELECT p.position_id, p.opened_at, p.window_slug, p.side, p.entry_price, p.shares,
       -b.amount_usd AS cost_usd, b.fee_usd AS fee_usd, b.id AS buy_id
  FROM paper_positions p
  JOIN paper_wallet_transactions b
    ON b.position_id = p.position_id AND b.transaction_type = 'BUY'
 WHERE p.state = 'open'
 ORDER BY p.opened_at DESC, p.position_id DESC
"""


_SHADOW_SQL = """
SELECT model_id,
       SUM(CASE WHEN state = 'settled' AND outcome IN ('Up', 'Down')
                 AND realized_pnl_usd IS NOT NULL THEN 1 ELSE 0 END) AS trades,
       SUM(CASE WHEN state = 'settled' AND outcome = side THEN 1 ELSE 0 END) AS wins,
       AVG(CASE WHEN state = 'settled' AND outcome IN ('Up', 'Down') THEN entry_price END) AS avg_price,
       SUM(CASE WHEN state = 'settled' AND outcome IN ('Up', 'Down') THEN realized_pnl_usd END) AS pnl_usd,
       SUM(CASE WHEN state = 'settled' AND outcome IN ('Up', 'Down')
                THEN realized_pnl_usd * realized_pnl_usd END) AS pnl_sq,
       MIN(created_at) AS first_at
  FROM model_shadow_positions
 GROUP BY model_id
"""


async def _one(db: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row is not None else None


async def _all(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with db.execute(sql, params) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def load_raw() -> dict[str, Any]:
    """Read everything the simple screen needs in one short connection."""
    async with connect() as db:
        tick = await _one(
            db, "SELECT * FROM paper_ticks ORDER BY created_at DESC, id DESC LIMIT 1"
        )
        wallet = await _one(
            db,
            "SELECT initial_cash_usd, cash_usd, initialized_at "
            "FROM paper_wallet WHERE wallet_id = 1",
        )
        trades = await _all(db, _TRADES_SQL, (TRADE_ROWS,))
        closed = await _all(db, _CLOSED_SQL)
        open_positions = await _all(db, _OPEN_SQL)
        any_open = await _one(
            db, "SELECT COUNT(*) AS n FROM paper_positions WHERE state = 'open'"
        )
        curve = await _all(
            db,
            "SELECT id, created_at, cash_after_usd FROM paper_wallet_transactions "
            "WHERE transaction_type IN ('SELL', 'SETTLEMENT') ORDER BY created_at, id",
        )
        activity = await _all(
            db,
            "SELECT created_at, event_type, message FROM notification_feed "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (ACTIVITY_ROWS,),
        )
        shadows = await _all(db, _SHADOW_SQL)
        learning_row = await _one(
            db, "SELECT value FROM config WHERE key = ?", (_learning.CONFIG_KEY,)
        )
        learning_start = await _one(
            db, "SELECT value AS t FROM config WHERE key = ?", (_learning.STARTED_KEY,)
        )
        rescales = await _one(
            db, "SELECT value FROM config WHERE key = ?", (_wallet.RESCALE_KEY,)
        )
        fees_from = await _one(
            db, "SELECT value FROM config WHERE key = ?", (_wallet.FEES_KEY,)
        )
        # Entries the RiskGate refused in the last hour: a setting that blocks
        # every trade (2026-11-18: $20-$100 sizing vs the $3 per-trade cap) must
        # show on the screen, not only in the current window's verdict.
        since = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
        blocks_hour = await _one(
            db,
            "SELECT COUNT(*) AS n FROM live_orders "
            "WHERE mode = 'paper' AND status = 'BLOCKED' AND created_at >= ?",
            (since,),
        )
        last_block = await _one(
            db,
            "SELECT created_at, error FROM live_orders "
            "WHERE mode = 'paper' AND status = 'BLOCKED' AND created_at >= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (since,),
        )
        window_trades = 0
        blocked = None
        slug = tick.get("window_slug") if tick else None
        if slug:
            # Trades in this window: in hold-to-settle style the loop makes at
            # most "Most trades open at once" of them (one in live mode).
            row = await _one(
                db,
                "SELECT COUNT(*) AS n FROM paper_positions WHERE window_slug = ?",
                (slug,),
            )
            window_trades = int(row["n"]) if row else 0
            blocked = await _one(
                db,
                "SELECT created_at, error FROM live_orders "
                "WHERE mode = 'paper' AND status = 'BLOCKED' AND window_slug = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (slug,),
            )
    return {
        "tick": tick,
        "wallet": wallet,
        "trades": trades,
        "closed": closed,
        "open_positions": open_positions,
        "any_open": int(any_open["n"]) if any_open else 0,
        "curve": curve,
        "activity": activity,
        "window_trades": window_trades,
        "blocked": blocked,
        "blocks_hour": int((blocks_hour or {}).get("n") or 0),
        "last_block": last_block,
        "shadows": shadows,
        "learning_raw": learning_row["value"] if learning_row else None,
        "learning_started": learning_start["t"] if learning_start else None,
        "rescales_raw": rescales["value"] if rescales else None,
        "fees_from_raw": fees_from["value"] if fees_from else None,
    }


# ---------------------------------------------------------------------------
# Pure assembly
# ---------------------------------------------------------------------------


def _market(
    tick: dict[str, Any] | None, now: float, stale_after: float, fee_aware: bool = False
) -> dict[str, Any] | None:
    if not tick:
        return None
    window = parse_window_slug(tick.get("window_slug")) or {}
    tick_ts = iso_to_ts(tick.get("created_at"))
    age = None if tick_ts is None else max(0.0, now - tick_ts)
    reason = str(tick.get("reason") or "")
    degraded = reason.startswith("skip: settlement feed degraded")
    spot = _num(tick.get("spot_price"))
    ref = _num(tick.get("reference_price"))
    # The loop journals 0.0 when the settlement feed could not price a tick.
    spot = spot if spot and spot > 0 else None
    ref = ref if ref and ref > 0 else None
    up_cost = _price(tick.get("market_up_price"))
    down_cost = _price(tick.get("market_down_price"))
    raw_fair = None if degraded else _price(tick.get("fair_up_prob"))
    # The learned chance is what the paper decision used when learning was on.
    learned = None if degraded else _price(tick.get("learned_up_prob"))
    fair = learned if learned is not None else raw_fair
    edge_up = None if fair is None or up_cost is None else fair - up_cost
    edge_down = None if fair is None or down_cost is None else (1.0 - fair) - down_cost

    def _net(edge: float | None, cost: float | None) -> float | None:
        if edge is None or cost is None:
            return None
        return edge - (taker_fee_per_share(cost, TAKER_FEE_RATE) if fee_aware else 0.0)

    # Mirrors strategy.signal_from_executable_edges: pick by the edge that the
    # minimum-edge rule actually tests (after fees when that setting is on).
    candidates = [
        (side, net)
        for side, net in (("Up", _net(edge_up, up_cost)), ("Down", _net(edge_down, down_cost)))
        if net is not None
    ]
    best_side, best_edge = max(candidates, key=lambda c: c[1]) if candidates else (None, None)
    return {
        "window_slug": tick.get("window_slug"),
        "question": tick.get("market_question") or tick.get("window_slug"),
        "timeframe": window.get("timeframe"),
        "start_ts": window.get("start_ts"),
        "end_ts": window.get("end_ts"),
        "tick_ts": tick_ts,
        "tick_age_s": None if age is None else round(age, 1),
        "fresh": age is not None and age <= stale_after,
        "btc_price": spot,
        "price_to_beat": ref,
        "difference": None if spot is None or ref is None else round(spot - ref, 2),
        "up_cost": up_cost,
        "down_cost": down_cost,
        "up_bid": _price(tick.get("up_best_bid")),
        "down_bid": _price(tick.get("down_best_bid")),
        "chance_up": fair,
        "chance_up_raw": raw_fair if learned is not None else None,
        "edge_up": None if edge_up is None else round(edge_up, 4),
        "edge_down": None if edge_down is None else round(edge_down, 4),
        "best_side": best_side,
        "best_edge": None if best_edge is None else round(best_edge, 4),
        "edge_after_fees": fee_aware,
        "degraded": degraded,
        "reason": reason,
    }


def _trades_phrase(rows: list[dict[str, Any]]) -> str:
    """'an Up trade', '2 Up trades', '3 trades (2 Up, 1 Down)'."""
    ups = sum(1 for r in rows if r.get("side") == "Up")
    downs = len(rows) - ups
    if len(rows) == 1:
        return "an Up trade" if ups else "a Down trade"
    if not downs:
        return f"{ups} Up trades"
    if not ups:
        return f"{downs} Down trades"
    return f"{len(rows)} trades ({ups} Up, {downs} Down)"


def _trade_limit(rules: dict[str, Any], mode: str) -> int:
    """How many trades the loop holds at once (one in real-money mode)."""
    if mode != "paper":
        return 1
    try:
        return max(1, int(rules.get("max_open") or 1))
    except (TypeError, ValueError):
        return 1


def explain(
    *,
    bot_state: str,
    market: dict[str, Any] | None,
    position: dict[str, Any] | None,
    any_open: int,
    window_trades: int,
    blocked: dict[str, Any] | None,
    rules: dict[str, Any],
    mode: str = "paper",
    positions: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """One plain-English sentence (+ detail) for what the bot is doing now.

    ``tone`` drives the styling only: idle | wait | watch | trade | warn.
    ``positions`` is every open trade (newest first); ``position`` alone (the
    newest) is enough for older callers.
    """
    if positions is None:
        positions = [position] if position is not None else []
    if bot_state != "running":
        if mode != "paper":
            return {
                "tone": "idle",
                "headline": "The bot is stopped.",
                "detail": "Real-money mode is selected in the advanced console. "
                "Switch back to paper to start practice trading here.",
            }
        if position is not None:
            return {
                "tone": "idle",
                "headline": "The bot is stopped.",
                "detail": "Press Start to keep practice trading.",
            }
        return {
            "tone": "idle",
            "headline": "The bot is stopped.",
            "detail": "Press Start and it will watch the current Bitcoin window "
            "and place practice trades when the price is right.",
        }
    if market is None:
        return {
            "tone": "wait",
            "headline": "Getting the first market update.",
            "detail": "This usually takes a few seconds after Start.",
        }
    if not market["fresh"]:
        age = market["tick_age_s"]
        ago = f" The last one arrived {int(age)} seconds ago." if age is not None else ""
        return {
            "tone": "warn",
            "headline": "Waiting for fresh market data.",
            "detail": "The bot keeps retrying on its own." + ago,
        }
    end_ts = market.get("end_ts")
    limit = _trade_limit(rules, mode)
    here = [p for p in positions if p.get("window_slug") == market.get("window_slug")]
    total_open = max(int(any_open or 0), len(positions))
    if here and total_open >= limit:
        if len(here) == 1:
            side = here[0]["side"]
            pays = "at or above" if side == "Up" else "below"
            detail = (f"Each share pays $1 if Bitcoin finishes {pays} "
                      "the price to beat, and nothing otherwise.")
        else:
            detail = ("Each share pays $1 if it wins and nothing otherwise. Trades in the "
                      "same window win or lose together.")
        if limit > 1:
            detail += f" {limit} is the most it holds at once (you can change that in Settings)."
        return {
            "tone": "trade",
            "headline": f"Holding {_trades_phrase(here)} until this window settles.",
            "detail": detail,
        }
    if total_open >= limit:
        last = "the last trade" if total_open == 1 else "the last trades"
        return {
            "tone": "wait",
            "headline": f"Waiting for {last} to settle.",
            "detail": "The bot holds one trade at a time." if limit == 1
            else f"The bot holds up to {limit} trades at once.",
        }
    if window_trades >= limit and rules.get("exit_style", "settle") == "settle":
        per = "one trade" if limit == 1 else f"{limit} trades"
        return {
            "tone": "watch",
            "headline": "Done with this window.",
            "detail": f"The bot makes at most {per} per window. "
            f"It looks again when the next one starts at {_clock(end_ts)}.",
        }
    # Room for another trade. Say what's already held so the line below reads
    # as "and here's why it isn't adding one".
    holding = ""
    if here:
        room = limit - total_open
        more = "1 more" if room == 1 else f"{room} more"
        holding = f"It holds {_trades_phrase(here)} in this window and can add {more}. "
    spacing = int(rules.get("spacing_seconds") or 0)
    newest = max((p.get("opened_ts") or 0.0 for p in here), default=None)
    tick_ts = market.get("tick_ts")
    if here and spacing > 0 and newest and tick_ts is not None:
        wait = int(math.ceil(spacing - (tick_ts - newest)))
        if wait > 0:
            return {
                "tone": "trade",
                "headline": f"Holding {_trades_phrase(here)}, waiting before another.",
                "detail": f"After each trade it waits {spacing} seconds before buying again in "
                f"the same window (about {wait} seconds to go), so it doesn't buy one price "
                f"twice. It can hold up to {limit} at once.",
            }
    if blocked is not None and blocked.get("error"):
        return {
            "tone": "warn",
            "headline": "A safety limit blocked a trade in this window.",
            "detail": holding + "It refused because " + block_reason_plain(blocked["error"]),
        }
    reason = market.get("reason") or ""
    best_side = market.get("best_side")
    best_edge = market.get("best_edge")
    if reason.startswith("enter "):
        side = best_side or reason.split()[1].rstrip(":")
        return {
            "tone": "trade",
            "headline": f"Buying {side} now.",
            "detail": holding + "The bot's estimate beats the asking price by enough to trade.",
        }
    out = _market_explain(market, rules, end_ts, reason, best_side, best_edge)
    if holding:
        out = {**out, "detail": holding + out["detail"]}
        if out["tone"] == "watch":
            out["tone"] = "trade"
    return out


def _market_explain(
    market: dict[str, Any],
    rules: dict[str, Any],
    end_ts: Any,
    reason: str,
    best_side: str | None,
    best_edge: float | None,
) -> dict[str, str]:
    """Why the current tick isn't a buy, from the loop's own skip reason."""
    if market.get("degraded"):
        return {
            "tone": "warn",
            "headline": "Bitcoin's settlement price feed isn't responding.",
            "detail": "The bot pauses new trades until it's back. It reconnects on its own.",
        }
    if reason.startswith("skip: no executable quote"):
        return {
            "tone": "watch",
            "headline": "Nobody is selling shares right now.",
            "detail": "The bot only buys at a real asking price, so it's waiting.",
        }
    if reason.startswith("skip: too close to window end"):
        return {
            "tone": "watch",
            "headline": "Too late to trade this window.",
            "detail": f"The bot doesn't start trades in the last {rules['min_seconds_left']} "
            f"seconds. The next window starts at {_clock(end_ts)}.",
        }
    if reason.startswith("skip: edge/confidence below threshold"):
        after = " after fees" if rules.get("fee_aware") else ""
        if best_side is not None and best_edge is not None:
            detail = (
                f"The best price right now is {best_side}, and the bot's estimate beats "
                f"it by {_cents(best_edge)}{after}. It needs at least "
                f"{_cents(rules['edge_min'])}{after}."
            )
        else:
            detail = f"It needs an edge of at least {_cents(rules['edge_min'])}."
        return {
            "tone": "watch",
            "headline": "Sitting out: the edge is too small.",
            "detail": detail,
        }
    if reason.startswith("skip: edge above cap"):
        return {
            "tone": "watch",
            "headline": "Sitting out: the edge looks too good to be true.",
            "detail": f"An edge above {_cents(rules['edge_max'])} usually means the bot's data "
            "is a step behind the market, so it skips these on purpose.",
        }
    if reason.startswith("skip: entry price too extreme"):
        return {
            "tone": "watch",
            "headline": "Sitting out: the price is outside the bot's range.",
            "detail": f"It only buys shares priced between {_cents(rules['price_min'])} "
            f"and {_cents(rules['price_max'])}.",
        }
    if reason.startswith("skip: no strategy loaded"):
        return {
            "tone": "watch",
            "headline": "No strategy is running in this mode.",
            "detail": "Switch back to paper mode to use the practice strategy.",
        }
    return {"tone": "watch", "headline": "Watching the market.", "detail": reason}


def _positions(
    open_rows: list[dict[str, Any]], tick: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], float]:
    """(every open wallet trade marked to market, newest first; their total value).

    ``unrealized_usd`` is after the entry fee, which the wallet already paid.
    """
    current_slug = (tick or {}).get("window_slug")
    total_value = 0.0
    out: list[dict[str, Any]] = []
    for row in open_rows:
        shares = _num(row.get("shares")) or 0.0
        entry = _price(row.get("entry_price")) or 0.0
        side = "Up" if str(row.get("side", "")).lower() == "up" else "Down"
        bid = _price((tick or {}).get(f"{side.lower()}_best_bid"))
        marked_live = row.get("window_slug") == current_slug and bid is not None
        mark = bid if marked_live else entry
        value = shares * mark
        total_value += value
        cost = _num(row.get("cost_usd")) or shares * entry
        fee = _num(row.get("fee_usd")) or 0.0
        window = parse_window_slug(row.get("window_slug")) or {}
        out.append({
            "position_id": row.get("position_id"),
            "window_slug": row.get("window_slug"),
            "side": side,
            "shares": round(shares, 4),
            "entry_price": entry,
            "cost_usd": round(cost, 4),
            "fee_usd": round(fee, 4),
            "mark_price": mark,
            "marked_live": marked_live,
            "value_usd": round(value, 4),
            "unrealized_usd": round(value - cost - fee, 4),
            "opened_ts": iso_to_ts(row.get("opened_at")),
            "start_ts": window.get("start_ts"),
            "end_ts": window.get("end_ts"),
        })
    return out, total_value


def _position(
    open_rows: list[dict[str, Any]], tick: dict[str, Any] | None
) -> tuple[dict[str, Any] | None, float]:
    """(the newest open wallet trade marked to market, total value of all open)."""
    rows, total_value = _positions(open_rows, tick)
    return (rows[0] if rows else None), total_value


def _trade_rows(
    rows: list[dict[str, Any]], rescales: list[_wallet.Rescale] | None = None
) -> list[dict[str, Any]]:
    """Trades as they happened. ``scale`` is how many times the balance and the
    totals count each one (above 1 for trades before a practice balance switch).
    ``pnl_usd`` is after ``fee_usd``, the fees the wallet paid for the trade."""
    out: list[dict[str, Any]] = []
    for row in rows:
        window = parse_window_slug(row.get("window_slug")) or {}
        cost = _num(row.get("cost_usd")) or 0.0
        exit_type = row.get("exit_type")
        exit_usd = _num(row.get("exit_usd"))
        shares = _num(row.get("shares")) or 0.0
        entry = _price(row.get("entry_price"))
        fee = (_num(row.get("buy_fee_usd")) or 0.0) + (_num(row.get("exit_fee_usd")) or 0.0)
        if exit_type is None or exit_usd is None:
            result, pnl = "open", None
        elif exit_type == "SETTLEMENT":
            result = "won" if exit_usd > 0 else "lost"
            pnl = exit_usd - cost - fee
        else:
            result, pnl = "sold", exit_usd - cost - fee
        out.append(
            {
                "position_id": row.get("position_id"),
                "opened_ts": iso_to_ts(row.get("opened_at")),
                "closed_ts": iso_to_ts(row.get("exit_at") or row.get("closed_at")),
                "start_ts": window.get("start_ts"),
                "end_ts": window.get("end_ts"),
                "timeframe": window.get("timeframe"),
                "side": "Up" if str(row.get("side", "")).lower() == "up" else "Down",
                "shares": round(shares, 4),
                "entry_price": entry,
                "cost_usd": round(cost, 4),
                "payout_usd": None if exit_usd is None else round(exit_usd, 4),
                "pnl_usd": None if pnl is None else round(pnl, 4),
                "fee_usd": round(fee, 4),
                "result": result,
                "exit_reason": row.get("exit_reason"),
                "scale": _wallet.scale_for_txn(row.get("exit_id"), rescales or []),
            }
        )
    return out


def _scaled(closed: list[dict[str, Any]], rescales: list[_wallet.Rescale]) -> list[dict[str, Any]]:
    """Each finished trade with ``scale``: the factor that puts its dollars on
    today's practice balance (a $100 -> $500 switch counts earlier trades 5x,
    so every percentage stays the same)."""
    return [{**r, "scale": _wallet.scale_for_txn(r.get("exit_id"), rescales)} for r in closed]


def _k(row: dict[str, Any]) -> float:
    return _num(row.get("scale")) or 1.0


def _stats(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals of the finished trades, after fees (``fees_usd`` is what they paid).

    ``est_fees_usd`` is gone on purpose: an older open page reads it as "fees
    still to come off" and would subtract them a second time.
    """
    pnls = [p * _k(r) for r in closed if (p := _num(r.get("pnl_usd"))) is not None]
    fees = sum((_num(r.get("fee_usd")) or 0.0) * _k(r) for r in closed
               if _num(r.get("pnl_usd")) is not None)
    count = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    total = sum(pnls)
    return {
        "trades": count,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / count) if count else None,
        "total_pnl_usd": round(total, 4),
        "gross_pnl_usd": round(total + fees, 4),
        "fees_usd": round(fees, 4),
        "best_usd": round(max(pnls), 4) if pnls else None,
        "worst_usd": round(min(pnls), 4) if pnls else None,
        "avg_usd": round(total / count, 4) if count else None,
    }


def _accuracy(closed: list[dict[str, Any]]) -> dict[str, Any]:
    """Calibration of settled wallet trades: what the bot said vs what happened.

    "Said" is the bot's own chance of winning for the side it bought, at entry:
    entry price + executable edge (``fair_side - ask``). A well-calibrated bot
    wins about as often as it says; it only has an edge if it also wins more
    often than the prices it paid imply (plus fees).
    """
    rows: list[tuple[float, float, bool, float, float]] = []
    for r in closed:
        if r.get("exit_type") != "SETTLEMENT":
            continue
        price = _price(r.get("entry_price"))
        edge = _num(r.get("edge"))
        payout = _num(r.get("payout_usd"))
        shares = _num(r.get("shares"))
        pnl = _num(r.get("pnl_usd"))  # after fees
        if price is None or edge is None or payout is None or not shares or pnl is None:
            continue
        said = min(0.999, max(0.0, price + edge))
        k = _k(r)
        fee = _num(r.get("fee_usd")) or 0.0
        # (said, price, won, profit before fees, fees)
        rows.append((said, price, payout > 0, (pnl + fee) * k, fee * k))
    n = len(rows)
    if n == 0:
        return {"trades": 0, "buckets": [], "summary": []}

    def _avg(values: list[float]) -> float:
        return sum(values) / len(values)

    buckets = []
    for lo, hi, label in ACCURACY_BUCKETS:
        group = [r for r in rows if lo <= r[0] < hi]
        if not group:
            continue
        buckets.append({
            "label": label,
            "trades": len(group),
            "bot_said": round(_avg([r[0] for r in group]), 4),
            "won": round(_avg([1.0 if r[2] else 0.0 for r in group]), 4),
            "price_paid": round(_avg([r[1] for r in group]), 4),
            "profit_usd": round(sum(r[3] for r in group), 4),
            "profit_after_fees_usd": round(sum(r[3] - r[4] for r in group), 4),
        })
    said = _avg([r[0] for r in rows])
    won = _avg([1.0 if r[2] else 0.0 for r in rows])
    paid = _avg([r[1] for r in rows])
    brier_bot = _avg([(r[0] - (1.0 if r[2] else 0.0)) ** 2 for r in rows])
    brier_market = _avg([(r[1] - (1.0 if r[2] else 0.0)) ** 2 for r in rows])
    margin = 1.96 * math.sqrt(won * (1.0 - won) / n)

    summary: list[str] = []
    if n < 30:
        summary.append(
            f"Only {n} settled trades so far, which is too few to judge accuracy. "
            "Check back after at least 100."
        )
    else:
        summary.append(
            f"On average the bot said it had a {round(said * 100)}% chance and it won "
            f"{round(won * 100)}% of the time."
        )
        if won >= paid + 0.02:
            summary.append(
                f"That beats the {round(paid * 100)}¢ it paid per share on average, "
                "which is what a real edge looks like."
            )
        elif won <= paid - 0.02:
            summary.append(
                f"That's below the {round(paid * 100)}¢ it paid per share on average, "
                "so it is losing to the market."
            )
        else:
            summary.append(
                f"That's about what the {round(paid * 100)}¢ it paid per share already "
                "implied, so there's no clear edge yet."
            )
        better = "more" if brier_bot < brier_market else "less"
        summary.append(
            f"As forecasts, its chances were {better} accurate than the share prices "
            "themselves."
        )
        summary.append(
            f"With {n} trades, the true win rate could be about "
            f"{round(margin * 100)} points higher or lower than this."
        )
    return {
        "trades": n,
        "bot_said": round(said, 4),
        "won": round(won, 4),
        "price_paid": round(paid, 4),
        "margin_of_error": round(margin, 4),
        "brier_bot": round(brier_bot, 4),
        "brier_market": round(brier_market, 4),
        "buckets": buckets,
        "summary": summary,
    }


_CAP_RE = re.compile(r"per-trade cap: ([\d.]+) USD exceeds ([\d.]+) USD")


def block_reason_plain(error: str | None) -> str:
    """The RiskGate's refusal in plain English (the raw text stays in the log)."""
    text = str(error or "").strip()
    cap = _CAP_RE.search(text)
    if cap:
        return (
            f"each trade would cost ${float(cap.group(1)):,.2f}, but the safety limit is "
            f"${float(cap.group(2)):,.2f} a trade. Set the trade size in shares in Settings."
        )
    if text.startswith("daily bankroll cap"):
        return "today's spending limit is used up."
    if text.startswith("daily loss halt"):
        return "today's loss limit was reached, so new trades are paused."
    if text.startswith("entry slippage guard"):
        return "the price moved away before the bot could buy."
    return text or "a safety rule refused the trade."


def _entry_blocks(raw: dict[str, Any]) -> dict[str, Any]:
    last = raw.get("last_block") or {}
    return {
        "last_hour": int(raw.get("blocks_hour") or 0),
        "reason": block_reason_plain(last.get("error")) if last else None,
        "latest_ts": iso_to_ts(last.get("created_at")) if last else None,
    }


def _verdict(trades: int, mean: float | None, half: float | None) -> str:
    if trades < STRATEGY_MIN_TRADES or mean is None or half is None:
        return "too_early"
    if mean - half > 0:
        return "ahead"
    if mean + half < 0:
        return "behind"
    return "luck"


def _strategy_row(
    sid: str, name: str, about: str, trades: int, wins: int, price_sum: float,
    pnl: float, pnl_sq: float, since: float | None,
) -> dict[str, Any]:
    mean = pnl / trades if trades else None
    half = None
    if trades >= 2:
        var = max(0.0, (pnl_sq - trades * mean * mean) / (trades - 1))
        half = STRATEGY_Z * math.sqrt(var / trades)
    return {
        "id": sid,
        "name": name,
        "about": about,
        "since_ts": since,
        "trades": trades,
        "won": wins / trades if trades else None,
        "price_paid": price_sum / trades if trades else None,
        "profit_after_fees_usd": round(pnl, 4),
        "per_trade_usd": None if mean is None else round(mean, 4),
        "per_trade_range_usd": None if half is None else round(half, 4),
        "verdict": _verdict(trades, mean, half),
    }


def _strategies(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """This bot (since learning started) vs the shadow roster, fee-true."""
    started = iso_to_ts(raw.get("learning_started"))
    by_id = {str(r.get("model_id")): r for r in raw.get("shadows") or []}
    rows: list[dict[str, Any]] = []
    for sid, name, about in STRATEGIES:
        if sid == "main":
            n = wins = 0
            price_sum = pnl = pnl_sq = 0.0
            if started is not None:
                for row in raw.get("closed") or []:
                    opened = iso_to_ts(row.get("opened_at"))
                    shares = _num(row.get("shares")) or 0.0
                    entry = _num(row.get("entry_price"))
                    after = _num(row.get("pnl_usd"))  # the wallet's, after its fees
                    if opened is None or opened < started or entry is None or after is None:
                        continue
                    if shares <= 0:
                        continue
                    gross = after + (_num(row.get("fee_usd")) or 0.0)
                    # Per 5 shares, like every shadow row (10-share trades halve).
                    net = after * STRATEGY_SHARES / shares
                    n += 1
                    wins += 1 if gross > 0 else 0
                    price_sum += entry
                    pnl += net
                    pnl_sq += net * net
            rows.append(_strategy_row(sid, name, about, n, wins, price_sum, pnl, pnl_sq, started))
            continue
        r = by_id.get(sid) or {}
        n = int(r.get("trades") or 0)
        rows.append(_strategy_row(
            sid, name, about, n, int(r.get("wins") or 0),
            (_num(r.get("avg_price")) or 0.0) * n,
            _num(r.get("pnl_usd")) or 0.0, _num(r.get("pnl_sq")) or 0.0,
            iso_to_ts(r.get("first_at")),
        ))
    return rows


def _learning_status(raw: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    out = _learning.status(_learning.Fit.from_json(raw.get("learning_raw")))
    out["enabled"] = bool(rules.get("learning", True))
    out["started_ts"] = iso_to_ts(raw.get("learning_started"))
    return out


def build_payload(
    raw: dict[str, Any],
    *,
    now: float,
    bot: dict[str, Any],
    selection: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the JSON the simple screen renders. Pure: no I/O."""
    tick = raw.get("tick")
    stale_after = max(float(rules.get("tick_seconds") or 5.0) * 3, 20.0)
    market = _market(tick, now, stale_after, bool(rules.get("fee_aware")))
    open_rows = raw.get("open_positions") or []
    positions, open_value = _positions(open_rows, tick)
    position = positions[0] if positions else None
    rescales = _wallet.load_rescales(raw.get("rescales_raw"))
    closed = _scaled(raw.get("closed") or [], rescales)
    fees_from = _wallet.load_fee_catch_up(raw.get("fees_from_raw"))
    open_fees = sum(p["fee_usd"] for p in positions)

    wallet_row = raw.get("wallet")
    wallet = None
    if wallet_row:
        initial = _num(wallet_row.get("initial_cash_usd")) or 0.0
        cash = _num(wallet_row.get("cash_usd")) or 0.0
        equity = cash + open_value
        wallet = {
            "initial_usd": round(initial, 2),
            "cash_usd": round(cash, 4),
            "open_value_usd": round(open_value, 4),
            "equity_usd": round(equity, 4),
            "pnl_usd": round(equity - initial, 4),
            "pnl_pct": ((equity - initial) / initial) if initial else None,
            # Every fee the practice wallet has paid, on today's scale.
            "fees_paid_usd": round(
                sum((_num(r.get("fee_usd")) or 0.0) * _k(r) for r in closed) + open_fees, 4
            ),
            "fees_from_ts": iso_to_ts(fees_from.at) if fees_from else None,
            "since_ts": iso_to_ts(wallet_row.get("initialized_at")),
            # The latest practice balance switch, for the line under the balance.
            "rescale": None if not rescales else {
                "at_ts": iso_to_ts(rescales[-1].at),
                "from_usd": rescales[-1].from_start_usd,
                "to_usd": rescales[-1].to_start_usd,
                "factor": round(rescales[-1].factor, 6),
                "first_start_usd": rescales[0].from_start_usd,
            },
        }

    # Balances booked before fees were charged didn't include them yet: each
    # is shown minus the fees its trades had paid by then, so the chart is
    # after fees all the way back and has no drop on the day fees started.
    fee_events: list[tuple[int, float]] = []
    for r in closed:
        k = _k(r)
        if r.get("buy_id") is not None:
            fee_events.append((int(r["buy_id"]), (_num(r.get("buy_fee_usd")) or 0.0) * k))
        if r.get("exit_id") is not None and _num(r.get("exit_fee_usd")):
            fee_events.append((int(r["exit_id"]), (_num(r.get("exit_fee_usd")) or 0.0) * k))
    for r in open_rows:
        if r.get("buy_id") is not None:
            fee_events.append((int(r["buy_id"]), _num(r.get("fee_usd")) or 0.0))
    shift = _wallet.FeeShift(fees_from, fee_events)
    # Trades still open at each point count at cost (cash + open trades, the
    # balance the Lab and the switch use): several trades in one window
    # settle one ledger row at a time and the chart must not dip in between.
    cost_events: list[tuple[int, float]] = []
    for r in raw.get("closed") or []:
        cost = _num(r.get("cost_usd")) or 0.0
        if r.get("buy_id") is not None and r.get("exit_id") is not None:
            cost_events += [(int(r["buy_id"]), cost), (int(r["exit_id"]), -cost)]
    for r in open_rows:
        if r.get("buy_id") is not None:
            cost_events.append((int(r["buy_id"]), _num(r.get("cost_usd")) or 0.0))
    held = _Running(cost_events)

    curve: list[dict[str, Any]] = []
    if wallet is not None and wallet["since_ts"] is not None:
        curve.append({"t": wallet["since_ts"], "v": wallet["initial_usd"]})
    for row in raw.get("curve") or []:
        ts, value = iso_to_ts(row.get("created_at")), _num(row.get("cash_after_usd"))
        if ts is not None and value is not None:
            value += held.at(row.get("id"))
            # Balances booked before a practice balance switch, on today's
            # scale, so the chart has no jump at the switch.
            value = _wallet.balance_on_today_scale(value, row.get("id"), rescales)
            value -= shift.at(row.get("id"))
            curve.append({"t": ts, "v": round(value, 4)})

    decision = explain(
        bot_state=bot.get("state", "stopped"),
        market=market,
        position=position,
        any_open=int(raw.get("any_open") or 0),
        window_trades=int(raw.get("window_trades") or 0),
        blocked=raw.get("blocked"),
        rules=rules,
        mode=bot.get("mode", "paper"),
        positions=positions,
    )

    curve = downsample(curve)
    if wallet is not None and curve:
        # End the chart at the balance shown on screen (cash + open trade).
        curve.append(
            {"t": max(now, curve[-1]["t"]), "v": round(wallet["equity_usd"], 4), "now": True}
        )

    activity = [
        {
            "t": iso_to_ts(row.get("created_at")),
            "kind": row.get("event_type"),
            "text": row.get("message"),
        }
        for row in raw.get("activity") or []
    ]

    return {
        "server_time": now,
        "bot": {
            "state": bot.get("state", "stopped"),
            "mode": bot.get("mode", "paper"),
            "detail": bot.get("detail", ""),
        },
        "selection": selection,
        "rules": rules,
        "market": market,
        "decision": decision,
        "wallet": wallet,
        "position": position,
        "positions": positions,
        "any_open": int(raw.get("any_open") or 0),
        "stats": _stats(closed),
        "accuracy": _accuracy(closed),
        "learning": _learning_status(raw, rules),
        "entry_blocks": _entry_blocks(raw),
        "strategies": _strategies(raw),
        "trades": _trade_rows(raw.get("trades") or [], rescales),
        "curve": curve,
        "activity": activity,
    }
