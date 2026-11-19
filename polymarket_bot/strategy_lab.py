"""Strategy Lab: replay the paper bot's recorded history under other rules.

Read-only. Every snapshot the bot logged (both books, bids, the model's raw
chance) is replayed through the SAME entry function the bot uses
(:func:`polymarket_bot.strategy.signal_from_executable_edges`) under different
rules, and each window is settled with its real outcome (labels shared with
:mod:`polymarket_bot.learning`). Questions it answers:

* settle vs scalp exits (scalp sells at the bid on target / stop / time /
  edge-collapse, the bot's own exit rules), with exit fees;
* trade size: same edge per share, bigger swings, and fills capped by the
  shares actually offered at the ask;
* entry rules one at a time (minimum edge, edge cap, price range, timing,
  fee check, learned chances) against the current settings;
* a grid search that picks the best rules on the first half of the history
  and then reports how that pick did on the second half, which it never saw.
  That last number is the honest one: the best of hundreds of rule sets
  always looks good on the data it was picked from.

All money figures are after estimated taker fees (0.07 * p * (1 - p) a share
on every buy and every scalp sale). Learned chances are walk-forward: each
block of windows uses a fit made only from earlier windows. The replay sees
the bot's logged ticks (about every 5 s), not every price, and ignores the
risk gate's daily spending cap; it reproduced 142 of 145 real entries.
"""
from __future__ import annotations

import itertools
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from polymarket_bot import learning, strategy
from polymarket_bot.shadow.fees import DEFAULT_TAKER_FEE_RATE

FEE = DEFAULT_TAKER_FEE_RATE
BLOCKS = 5  # chronological blocks; block 0 only trains learned chances
MAX_WINDOWS = 4000
Z_ONE = 1.96  # one comparison
SIZES = (5, 10, 25, 50, 100)


@dataclass(frozen=True)
class Rules:
    exit_style: str = "settle"  # or "scalp"
    edge_min: float = 0.045
    edge_max: float = 0.07
    price_min: float = 0.50
    price_max: float = 0.95
    min_seconds_left: int = 60
    max_seconds_left: int = 900  # enter only with at most this much left
    fee_aware: bool = False
    learned: bool = False
    shares: float = 5.0
    min_confidence: float = 0.5
    # scalp exits (the bot's runtime knobs)
    target_return: float = 0.10
    stop_return: float = -0.08
    time_exit_seconds: int = 45
    # Several open trades (the bot's "Most trades open at once"): hold to settle
    # makes up to this many trades per window, scalp holds up to this many at
    # once. A new one waits ``spacing_seconds`` after the newest still held.
    max_open: int = 1
    spacing_seconds: int = 0


@dataclass(frozen=True)
class Tick:
    remaining: int
    fair: float
    up_ask: float | None
    down_ask: float | None
    up_bid: float | None
    down_bid: float | None
    up_ask_size: float | None
    down_ask_size: float | None


@dataclass
class Window:
    key: tuple[str, int]
    up: float
    ticks: list[Tick]
    block: int = 0


# ------------------------------------------------------------------ loading

def load_history(db_path: str | Path, max_windows: int = MAX_WINDOWS) -> list[Window]:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        rows = conn.execute(
            "SELECT window_slug, remaining_seconds, fair_up_prob, up_best_ask, down_best_ask, "
            "up_best_bid, down_best_bid, up_ask_size, down_ask_size, reference_price "
            "FROM paper_ticks WHERE fair_up_prob IS NOT NULL AND reference_price > 0 "
            "AND remaining_seconds IS NOT NULL ORDER BY id"
        ).fetchall()
        known = learning.load_known_outcomes(conn)
    finally:
        conn.close()
    first_ref: dict[tuple[str, int], float] = {}
    slug_of: dict[tuple[str, int], str] = {}
    ticks: dict[tuple[str, int], list[Tick]] = defaultdict(list)
    for slug, rem, fair, ua, da, ub, db_, uas, das, ref in rows:
        key = learning.window_key(slug)
        if key is None:
            continue
        first_ref.setdefault(key, float(ref))
        slug_of[key] = slug
        ticks[key].append(Tick(int(rem), float(fair), ua, da, ub, db_, uas, das))
    labels = learning.label_windows(first_ref, slug_of, known)
    keys = sorted(labels, key=lambda k: k[1])[-max_windows:]
    windows = [Window(k, labels[k], sorted(ticks[k], key=lambda t: -t.remaining)) for k in keys]
    for i, w in enumerate(windows):
        w.block = i * BLOCKS // max(len(windows), 1)
    return windows


def _learned_fits(windows: list[Window]) -> dict[int, tuple[float, float] | None]:
    """Walk-forward weights: block k uses a fit from blocks < k only."""
    samples_by_block: dict[int, list[learning.Sample]] = defaultdict(list)
    for w in windows:
        for t in w.ticks:
            if t.remaining <= learning.MIN_REMAINING_SECONDS:
                continue
            mid = learning.market_mid_up(t.up_ask, t.down_ask)
            if mid is not None:
                samples_by_block[w.block].append(
                    learning.Sample(w.key, min(0.9999, max(0.0001, t.fair)), mid, w.up))
    fits: dict[int, tuple[float, float] | None] = {0: None}
    for k in range(1, BLOCKS):
        train = [s for b in range(k) for s in samples_by_block[b]]
        fits[k] = learning.fit_weights(train) if train else None
    return fits


# --------------------------------------------------------------- simulation

def _fee(price: float) -> float:
    return FEE * price * (1.0 - price) if 0.0 < price < 1.0 else 0.0


def _decision(t: Tick, rules: Rules, fit: tuple[float, float] | None) -> float | None:
    if not rules.learned:
        return t.fair
    if fit is None:
        return None
    mid = learning.market_mid_up(t.up_ask, t.down_ask)
    return None if mid is None else learning.blend(t.fair, mid, *fit)


def _buyable(ask: float | None) -> bool:
    return ask is not None and 0.0 < ask < 1.0


def _signal(t: Tick, p: float, params: strategy.StrategyParams, rules: Rules):
    if t.remaining > rules.max_seconds_left:
        return None
    if not (_buyable(t.up_ask) and _buyable(t.down_ask)) or t.up_ask + t.down_ask < 0.999:
        return None  # crossed or one-sided book
    side, _conf, notional, _ = strategy.signal_from_executable_edges(
        p - t.up_ask, (1.0 - p) - t.down_ask, t.remaining, t.up_ask, t.down_ask, params)
    if side is None or notional <= 0:
        return None
    ask = t.up_ask if side == "Up" else t.down_ask
    size = t.up_ask_size if side == "Up" else t.down_ask_size
    shares = rules.shares
    if size is not None:
        if size <= 0:
            return None
        shares = min(shares, float(size))  # the bot's top-of-book cap
    return side, float(ask), shares


@dataclass
class Trade:
    block: int
    side: str
    entry: float
    shares: float
    net: float  # after fees
    gross: float
    exit: str  # SETTLE / TARGET / STOP / TIME / BAND
    capped: bool


def simulate(windows: list[Window], rules: Rules,
             fits: dict[int, tuple[float, float] | None]) -> list[Trade]:
    params = strategy.StrategyParams(
        min_trade_usd=1.0, max_trade_usd=2.0, entry_edge_min=rules.edge_min,
        min_confidence=rules.min_confidence, entry_min_remaining_seconds=rules.min_seconds_left,
        max_entry_price=rules.price_max, min_entry_price=rules.price_min,
        entry_edge_max=rules.edge_max, fee_rate=FEE if rules.fee_aware else 0.0,
    )
    trades: list[Trade] = []
    limit = max(1, int(rules.max_open))
    for w in windows:
        if w.block == 0:
            continue  # training-only block, so every rule set sees the same windows
        fit = fits.get(w.block)
        # (side, ask, shares, capped, seconds left when bought)
        holdings: list[tuple[str, float, float, bool, int]] = []
        entries = 0
        for t in w.ticks:
            p = _decision(t, rules, fit)
            if p is None:
                continue
            exited = False
            if rules.exit_style == "scalp" and holdings:
                kept = []
                for h in holdings:
                    done = _scalp_exit(t, p, h, rules, w.block)
                    if done is None:
                        kept.append(h)
                    else:
                        trades.append(done)
                        exited = True  # scalp style may re-enter from the next tick
                holdings = kept
            if exited or len(holdings) >= limit:
                continue
            if rules.exit_style == "settle" and entries >= limit:
                break
            if holdings and holdings[-1][4] - t.remaining < rules.spacing_seconds:
                continue
            sig = _signal(t, p, params, rules)
            if sig is None:
                continue
            side, ask, shares = sig
            holdings.append((side, ask, shares, shares < rules.shares, t.remaining))
            entries += 1
            if rules.exit_style == "settle" and entries >= limit:
                break
        for side, ask, shares, capped, _left in holdings:
            won = (w.up == 1.0) == (side == "Up")
            gross = shares * ((1.0 - ask) if won else -ask)
            trades.append(Trade(w.block, side, ask, shares, gross - shares * _fee(ask), gross,
                                "SETTLE", capped))
    return trades


def _scalp_exit(t: Tick, p: float, holding: tuple[str, float, float, bool, int],
                rules: Rules, block: int) -> Trade | None:
    """The bot's scalp exits for one held trade, priced at the held side's bid."""
    side, ask, shares, capped, _left = holding
    bid = t.up_bid if side == "Up" else t.down_bid
    if bid is None or not 0.0 < bid < 1.0:
        return None
    mark = shares * (bid - ask)
    notional = shares * ask
    edges = [e for e in ((p - t.up_ask) if _buyable(t.up_ask) else None,
                         ((1 - p) - t.down_ask) if _buyable(t.down_ask) else None) if e is not None]
    reason = None
    if t.remaining <= rules.time_exit_seconds:
        reason = "TIME"
    elif mark >= notional * rules.target_return:
        reason = "TARGET"
    elif mark <= notional * rules.stop_return:
        reason = "STOP"
    elif edges and abs(max(edges)) < rules.edge_min / 2:
        reason = "BAND"
    if reason is None:
        return None
    net = mark - shares * (_fee(ask) + _fee(bid))
    return Trade(block, side, ask, shares, net, mark, reason, capped)


# ------------------------------------------------------------------ scoring

def score(trades: list[Trade], z: float = Z_ONE) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"trades": 0, "won": None, "price_paid": None, "profit_usd": 0.0,
                "profit_before_fees_usd": 0.0, "per_trade_usd": None, "range_usd": None,
                "worst_drawdown_usd": 0.0, "verdict": "too_early"}
    nets = [t.net for t in trades]
    mean = sum(nets) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (n - 1)) if n > 1 else 0.0
    half = z * sd / math.sqrt(n) if n > 1 else None
    peak = run = worst = 0.0
    for x in nets:
        run += x
        peak = max(peak, run)
        worst = min(worst, run - peak)
    if n < 30 or half is None:
        verdict = "too_early"
    elif mean - half > 0:
        verdict = "ahead"
    elif mean + half < 0:
        verdict = "behind"
    else:
        verdict = "luck"
    return {
        "trades": n,
        "won": sum(1 for t in trades if t.gross > 0) / n,
        "price_paid": sum(t.entry for t in trades) / n,
        "profit_usd": round(sum(nets), 4),
        "profit_before_fees_usd": round(sum(t.gross for t in trades), 4),
        "per_trade_usd": round(mean, 4),
        "range_usd": None if half is None else round(half, 4),
        "worst_drawdown_usd": round(worst, 4),
        "verdict": verdict,
    }


def _row(label: str, rules: Rules, trades: list[Trade], **extra: Any) -> dict[str, Any]:
    out = {"label": label, "rules": asdict(rules), **score(trades)}
    out.update(extra)
    return out


def _variants(base: Rules) -> list[tuple[str, str, Rules]]:
    """One change at a time from the current rules: (group, label, rules)."""
    out: list[tuple[str, str, Rules]] = []
    for v in (0.02, 0.03, 0.045, 0.06, 0.08):
        out.append(("Minimum edge", f"{v * 100:g}¢", replace(base, edge_min=v)))
    for v in (0.07, 0.10, 0.15, 1.0):
        out.append(("Skip edges bigger than", "no limit" if v >= 1 else f"{v * 100:g}¢",
                    replace(base, edge_max=max(v, base.edge_min + 0.005))))
    for lo, hi in ((0.50, 0.95), (0.50, 0.85), (0.60, 0.90), (0.60, 0.95), (0.70, 0.95), (0.05, 0.95)):
        out.append(("Share prices", f"{lo * 100:g}¢ to {hi * 100:g}¢",
                    replace(base, price_min=lo, price_max=hi)))
    for lo, hi, label in ((60, 900, "any time with 1+ min left"), (60, 180, "1 to 3 min left"),
                          (60, 120, "1 to 2 min left"), (180, 900, "3+ min left"),
                          (240, 900, "first minute only")):
        out.append(("When to enter", label, replace(base, min_seconds_left=lo, max_seconds_left=hi)))
    for v in (False, True):
        out.append(("Fee check", "on" if v else "off", replace(base, fee_aware=v)))
    for v in (False, True):
        out.append(("Learned chances", "on" if v else "off", replace(base, learned=v)))
    for v in (1, 2, 3, 5):
        out.append(("Trades open at once", "1 (one at a time)" if v == 1 else str(v),
                    replace(base, max_open=v)))
    if base.max_open > 1:
        for v in (0, 15, 30, 60):
            out.append(("Wait between trades in a window", f"{v} s",
                        replace(base, spacing_seconds=v)))
    return out


def _grid(base: Rules) -> list[Rules]:
    out = []
    for edge_min, edge_max, (lo, hi), (smin, smax), fee, learned, style in itertools.product(
        (0.03, 0.045, 0.06),
        (0.07, 1.0),
        ((0.50, 0.95), (0.60, 0.90), (0.50, 0.85), (0.70, 0.95)),
        ((60, 900), (60, 180), (180, 900)),
        (False, True),
        (False, True),
        ("settle", "scalp"),
    ):
        out.append(replace(base, edge_min=edge_min, edge_max=max(edge_max, edge_min + 0.005),
                           price_min=lo, price_max=hi, min_seconds_left=smin,
                           max_seconds_left=smax, fee_aware=fee, learned=learned,
                           exit_style=style))
    return out


def describe(r: Rules) -> str:
    parts = [
        "scalp" if r.exit_style == "scalp" else "hold to settle",
        f"edge {r.edge_min * 100:g}¢" + ("+" if r.edge_max >= 1 else f"–{r.edge_max * 100:g}¢"),
        f"shares {r.price_min * 100:g}–{r.price_max * 100:g}¢",
    ]
    if r.max_seconds_left < 900:
        parts.append(f"{r.min_seconds_left}–{r.max_seconds_left}s left")
    elif r.min_seconds_left != 60:
        parts.append(f"{r.min_seconds_left}s+ left")
    if r.fee_aware:
        parts.append("fee check")
    if r.learned:
        parts.append("learned chances")
    if r.max_open > 1:
        parts.append(f"up to {r.max_open} at once, {r.spacing_seconds}s apart")
    return ", ".join(parts)


def analyze(db_path: str | Path, current: Rules) -> dict[str, Any]:
    started = time.time()
    windows = load_history(db_path)
    if len(windows) < 2 * BLOCKS:
        return {"ready": False, "windows": len(windows),
                "detail": "The lab needs at least a day of recorded windows."}
    fits = _learned_fits(windows)
    tested = [w for w in windows if w.block > 0]
    half = (BLOCKS + 1) // 2  # blocks 1..half-1 tune, half.. check
    tune = [w for w in windows if 0 < w.block < half]
    check = [w for w in windows if w.block >= half]

    cur = simulate(windows, current, fits)
    result: dict[str, Any] = {
        "ready": True,
        "windows": len(tested),
        "first_ts": tested[0].key[1],
        "last_ts": tested[-1].key[1],
        "current": _row("Your current settings", current, cur),
    }

    # settle vs scalp
    exits = []
    for style in ("settle", "scalp"):
        rules = replace(current, exit_style=style)
        trades = cur if style == current.exit_style else simulate(windows, rules, fits)
        counts: dict[str, int] = defaultdict(int)
        for t in trades:
            counts[t.exit] += 1
        exits.append(_row("Hold until the window settles" if style == "settle" else
                          "Scalp: sell early at a target, stop or time limit", rules, trades,
                          exits=dict(counts)))
    result["exit_styles"] = exits

    # sizing
    sizes = []
    for n in SIZES:
        rules = replace(current, shares=float(n))
        trades = simulate(windows, rules, fits)
        capped = sum(1 for t in trades if t.capped)
        row = _row(f"{n} shares", rules, trades,
                   capped_share=(capped / len(trades)) if trades else None,
                   avg_cost_usd=(sum(t.shares * t.entry for t in trades) / len(trades)) if trades else None)
        sizes.append(row)
    result["sizes"] = sizes

    # one change at a time
    result["factors"] = [
        {"group": g, **_row(label, rules, simulate(windows, rules, fits)),
         "is_current": rules == current}
        for g, label, rules in _variants(current)
    ]

    # grid: pick on the first half, judge on the second
    grid = _grid(current)
    scored = []
    for rules in grid:
        t_tune = simulate(tune, rules, fits)
        if len(t_tune) < 30:
            continue
        scored.append((sum(t.net for t in t_tune), rules, t_tune))
    scored.sort(key=lambda x: -x[0])
    picks = []
    for _total, rules, t_tune in scored[:5]:
        picks.append({
            "label": describe(rules),
            "rules": asdict(rules),
            "tune": score(t_tune),
            "check": score(simulate(check, rules, fits)),
        })
    result["grid"] = {
        "tested": len(grid),
        "eligible": len(scored),
        "tune_windows": len(tune),
        "check_windows": len(check),
        "picks": picks,
        "current_check": score(simulate(check, current, fits)),
    }
    result["seconds"] = round(time.time() - started, 1)
    return result


def compare_learning(db_path: str | Path, rules: Rules) -> dict[str, Any]:
    """Learned vs raw chances under ``rules``, walk-forward, after fees.

    Used by :mod:`polymarket_bot.learning` to decide whether the paper bot
    trades on learned chances. Same replay as the lab page; block 0 only
    trains, so both sides are scored on the same later windows.
    """
    windows = load_history(db_path)
    if len(windows) < 2 * BLOCKS:
        return {"ok": False, "windows": len(windows)}
    fits = _learned_fits(windows)
    learned = simulate(windows, replace(rules, learned=True), fits)
    raw = simulate(windows, replace(rules, learned=False), fits)
    return {
        "ok": True,
        "windows": sum(1 for w in windows if w.block > 0),
        "learned_usd": sum(t.net for t in learned),
        "raw_usd": sum(t.net for t in raw),
        "learned_n": len(learned),
        "raw_n": len(raw),
    }


def rules_from_settings(values: dict[str, Any], exit_style: str = "settle",
                        scalp: dict[str, Any] | None = None) -> Rules:
    shares = values.get("trade_shares") or 5.0
    scalp = scalp or {}
    return Rules(
        exit_style=exit_style if exit_style in ("settle", "scalp") else "settle",
        edge_min=float(values["paper_entry_edge_min"]),
        edge_max=float(values["paper_entry_edge_max"]),
        price_min=float(values["paper_min_entry_price"]),
        price_max=float(values["paper_max_entry_price"]),
        min_seconds_left=int(values["paper_entry_min_remaining_seconds"]),
        fee_aware=bool(values.get("paper_fee_aware")),
        learned=bool(values.get("paper_learning")),
        shares=float(shares),
        target_return=float(scalp.get("target_return", 0.10)),
        stop_return=float(scalp.get("stop_return", -0.08)),
        time_exit_seconds=int(scalp.get("time_exit_seconds", 45)),
        max_open=max(1, int(values.get("paper_max_open_positions") or 1)),
        spacing_seconds=max(0, int(values.get("paper_entry_spacing_seconds") or 0)),
    )
