"""Learned chances: the paper bot corrects its own chances from its results.

Measured on the bot's own ledger (2026-11-16..18, 285 settled windows), the
pricing model (:func:`polymarket_bot.strategy.fair_up_probability`) is
overconfident whenever it says 70% or more (it said 97% where 92.6% happened),
while the market's own prices are close to calibrated. This module learns how
much to trust each, from the bot's recorded snapshots and settled windows:

    learned_up = sigmoid(a * logit(model_up) + b * logit(market_mid_up))

Honesty guards, all measured before this was written (walk-forward: fit on
earlier windows, score the next block):

* **No intercept.** A free bias term chased the Up/Down base rate and lost out
  of sample; without it the blend beat both inputs (Brier 0.1658 vs model
  0.1721 and market 0.1681).
* **Trained where the bot trades:** ticks with more than 60 s left, and every
  window counts once however many ticks it logged.
* **Adopted only if it beats the raw model on held-out windows:** the newest
  20% of windows are left out of the fit and scored; the weights then used
  are refit on everything. Weights are clamped to [0, 1.5].
* **Outcomes** come from the next window's reference print on the same
  settlement feed (Up iff ref(next) >= ref(this), the venue's tie rule),
  validated in tools/replay_race.py at 99.5% against known outcomes; known
  outcomes from the shadow ledger always win.
* Read-only on the ledger, refit at most hourly in a worker thread, persisted
  under one config key so a restart resumes with the last fit.

Paper only: callers apply it to paper decisions and paper shadow candidates,
never to live orders. More accurate chances are not the same as profit — on
the same history no rule set beat luck after fees (see the strategies table).
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

CONFIG_KEY = "learning.blend_v1"
STARTED_KEY = "learning.started_at"  # first time a fit passed its check
MIN_WINDOWS = 100          # never fit on fewer finished windows
MAX_WINDOWS = 4000         # newest ~2 weeks of 5-minute windows
HOLDOUT_FRACTION = 0.2     # newest windows scored, not fitted
MIN_REMAINING_SECONDS = 60  # train on ticks the bot is allowed to enter on
REFIT_EVERY_SECONDS = 3600
RETRY_AFTER_FAILURE_SECONDS = 600
WEIGHT_MIN, WEIGHT_MAX = 0.0, 1.5
_EPS = 1e-4
_WINDOW_SECONDS = {"5m": 300, "15m": 900}


@dataclass(frozen=True)
class Fit:
    """One refit's outcome. ``active`` means decisions may use the weights."""

    a_model: float
    b_market: float
    active: bool
    reason: str
    fitted_at: int
    windows: int
    holdout_windows: int
    brier_model: float | None
    brier_market: float | None
    brier_learned: float | None
    # Trading replay (since 2026-11-19): the paper bot's current rules replayed
    # over past windows with learned vs raw chances, walk-forward, after fees.
    trade_windows: int | None = None
    trade_learned_usd: float | None = None
    trade_raw_usd: float | None = None
    trade_learned_n: int | None = None
    trade_raw_n: int | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> Fit | None:
        """Parse a stored fit; fits saved before a field existed still load."""
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return cls(**{name: data[name] for name in cls.__dataclass_fields__ if name in data})
        except (ValueError, TypeError, KeyError):
            return None


@dataclass(frozen=True)
class Sample:
    window: tuple[str, int]  # (timeframe, window start ts)
    model: float
    market: float
    up: float  # 1.0 if the window resolved Up


# ----------------------------------------------------------------- pure math

def _clip(p: float) -> float:
    return min(1.0 - _EPS, max(_EPS, p))


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def market_mid_up(up_ask: float | None, down_ask: float | None) -> float | None:
    """The market's own Up chance from the two executable asks.

    Buying Down at ``down_ask`` is selling Up at ``1 - down_ask``, so the
    midpoint of ``up_ask`` and ``1 - down_ask`` is the book's Up price. None
    for an empty, one-sided or crossed book.
    """
    if up_ask is None or down_ask is None:
        return None
    if not (0.0 < up_ask < 1.0 and 0.0 < down_ask < 1.0):
        return None
    if up_ask + down_ask < 0.999:  # crossed: not a real two-sided market
        return None
    return _clip((up_ask + (1.0 - down_ask)) / 2.0)


def blend(model_up: float, market_up: float, a_model: float, b_market: float) -> float:
    p = _sigmoid(a_model * _logit(model_up) + b_market * _logit(market_up))
    return min(0.995, max(0.005, p))


def fit_weights(samples: list[Sample], ridge: float = 1e-3) -> tuple[float, float] | None:
    """Window-weighted logistic fit of (a, b) by Newton's method; None if it fails."""
    if not samples:
        return None
    per_window: dict[tuple[str, int], int] = defaultdict(int)
    for s in samples:
        per_window[s.window] += 1
    rows = [
        (_logit(s.model), _logit(s.market), s.up, 1.0 / per_window[s.window])
        for s in samples
    ]
    a, b = 0.5, 0.5
    for _ in range(60):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for x0, x1, y, w in rows:
            p = _sigmoid(a * x0 + b * x1)
            r = w * (p - y)
            v = w * p * (1.0 - p)
            g0 += r * x0
            g1 += r * x1
            h00 += v * x0 * x0
            h01 += v * x0 * x1
            h11 += v * x1 * x1
        h00 += ridge
        h11 += ridge
        det = h00 * h11 - h01 * h01
        if not math.isfinite(det) or abs(det) < 1e-12:
            return None
        da = (h11 * g0 - h01 * g1) / det
        db = (h00 * g1 - h01 * g0) / det
        a, b = a - da, b - db
        if not (math.isfinite(a) and math.isfinite(b)):
            return None
        if max(abs(da), abs(db)) < 1e-9:
            break
    return (
        min(WEIGHT_MAX, max(WEIGHT_MIN, a)),
        min(WEIGHT_MAX, max(WEIGHT_MIN, b)),
    )


def brier_scores(samples: list[Sample], a: float, b: float) -> tuple[float, float, float]:
    """Window-weighted Brier for (model, market, learned); lower is better."""
    per_window: dict[tuple[str, int], int] = defaultdict(int)
    for s in samples:
        per_window[s.window] += 1
    tm = tq = tl = den = 0.0
    for s in samples:
        w = 1.0 / per_window[s.window]
        tm += w * (s.model - s.up) ** 2
        tq += w * (s.market - s.up) ** 2
        tl += w * (blend(s.model, s.market, a, b) - s.up) ** 2
        den += w
    return tm / den, tq / den, tl / den


def compute_fit(
    samples: list[Sample], now: int | None = None, trading: dict[str, Any] | None = None
) -> Fit:
    """Fit over ``samples`` (any order), adopted by a trading or accuracy check.

    With ``trading`` (from :func:`polymarket_bot.strategy_lab.compare_learning`)
    the verdict is the money: learned chances are used while, replayed with the
    bot's current rules over past windows (each judged with a fit from earlier
    windows only), they made more after fees than the raw model did. That
    replaced the accuracy-only check on 2026-11-19: on 387 windows the learned
    chances made +$42.50 vs -$54.20 for the raw model across all four blocks,
    while their overall Brier score was slightly worse on the newest windows
    (most of their benefit is in the trades they skip). Without ``trading`` the
    holdout Brier check below decides, as before.
    """
    now = int(time.time()) if now is None else now
    windows = sorted({s.window[1] for s in samples})
    n = len(windows)
    if n < MIN_WINDOWS:
        return Fit(0.0, 0.0, False, f"needs {MIN_WINDOWS} finished windows (has {n})",
                   now, n, 0, None, None, None)
    cut = windows[int(n * (1.0 - HOLDOUT_FRACTION))]
    train = [s for s in samples if s.window[1] < cut]
    test = [s for s in samples if s.window[1] >= cut]
    held = len({s.window for s in test})
    weights = fit_weights(train)
    if weights is None:
        return Fit(0.0, 0.0, False, "the fit did not converge", now, n, held, None, None, None)
    bm, bq, bl = brier_scores(test, *weights)
    if trading and trading.get("ok"):
        final = fit_weights(samples) or weights
        learned_usd = float(trading["learned_usd"])
        raw_usd = float(trading["raw_usd"])
        better = learned_usd > raw_usd
        reason = (
            f"replayed over the last {trading['windows']} windows with the current settings, "
            f"learned chances made {_usd(learned_usd)} after fees vs {_usd(raw_usd)} with the raw model"
        )
        return Fit(*final, better, reason, now, n, held, bm, bq, bl,
                   trade_windows=int(trading["windows"]), trade_learned_usd=round(learned_usd, 2),
                   trade_raw_usd=round(raw_usd, 2), trade_learned_n=int(trading["learned_n"]),
                   trade_raw_n=int(trading["raw_n"]))
    if not bl < bm:
        return Fit(*weights, False,
                   "learned chances were not more accurate than the raw model on held-out windows",
                   now, n, held, bm, bq, bl)
    final = fit_weights(samples) or weights
    return Fit(*final, True, "learned chances beat the raw model on held-out windows",
               now, n, held, bm, bq, bl)


def _usd(x: float) -> str:
    return ("-" if x < 0 else "+") + f"${abs(x):,.2f}"


def current_rules() -> Any:
    """The paper bot's live Settings as Strategy Lab rules (5 shares: the size
    doesn't change which of learned or raw chances made more)."""
    import config as _config
    from polymarket_bot import runtime_knobs as _knobs
    from polymarket_bot import strategy_lab as _lab

    return _lab.Rules(
        exit_style=str(_knobs.cached("exit_style") or "settle"),
        edge_min=float(_knobs.cached("paper_entry_edge_min")),
        edge_max=float(_knobs.cached("paper_entry_edge_max")),
        price_min=float(_knobs.cached("paper_min_entry_price")),
        price_max=float(_knobs.cached("paper_max_entry_price")),
        min_seconds_left=int(_knobs.cached("paper_entry_min_remaining_seconds")),
        fee_aware=bool(_knobs.cached("paper_fee_aware")),
        shares=5.0,
        min_confidence=float(_config.PAPER_MIN_CONFIDENCE),
        target_return=float(_knobs.cached("paper_target_return")),
        stop_return=float(_knobs.cached("paper_stop_return")),
        time_exit_seconds=int(_knobs.cached("paper_time_exit_seconds")),
        max_open=max(1, int(_knobs.cached("paper_max_open_positions") or 1)),
        spacing_seconds=max(0, int(_knobs.cached("paper_entry_spacing_seconds") or 0)),
    )


# ------------------------------------------------------------ ledger reading

def window_key(slug: str) -> tuple[str, int] | None:
    parts = str(slug).rsplit("-", 2)
    if len(parts) != 3 or parts[1] not in _WINDOW_SECONDS:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def label_windows(
    first_ref: dict[tuple[str, int], float],
    slug_of: dict[tuple[str, int], str],
    known: dict[str, str],
) -> dict[tuple[str, int], float]:
    """1.0/0.0 per finished window: known outcome, else next window's reference."""
    labels: dict[tuple[str, int], float] = {}
    for key, ref in first_ref.items():
        outcome = known.get(slug_of[key])
        if outcome is None:
            nxt = first_ref.get((key[0], key[1] + _WINDOW_SECONDS[key[0]]))
            if nxt is None:
                continue
            outcome = "Up" if nxt >= ref else "Down"
        labels[key] = 1.0 if outcome == "Up" else 0.0
    return labels


def load_known_outcomes(conn: sqlite3.Connection) -> dict[str, str]:
    known: dict[str, str] = {}
    try:
        for slug, outcome in conn.execute(
            "SELECT window_slug, outcome FROM model_shadow_positions "
            "WHERE state = 'settled' AND outcome IN ('Up', 'Down')"
        ):
            known[slug] = outcome
    except sqlite3.OperationalError:
        pass  # no shadow ledger yet
    return known


def load_samples(db_path: str | Path, max_windows: int = MAX_WINDOWS) -> list[Sample]:
    """Training samples from the tick journal (read-only, synchronous)."""
    # as_uri() gives a well-formed file: URI on Windows too (drive letter,
    # backslashes, spaces); mode=ro keeps this reader from ever writing.
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        ticks = conn.execute(
            "SELECT window_slug, remaining_seconds, fair_up_prob, up_best_ask, "
            "down_best_ask, reference_price FROM paper_ticks "
            "WHERE fair_up_prob IS NOT NULL AND reference_price IS NOT NULL "
            "AND reference_price > 0 AND remaining_seconds IS NOT NULL "
            "ORDER BY id"
        ).fetchall()
        known = load_known_outcomes(conn)
    finally:
        conn.close()

    first_ref: dict[tuple[str, int], float] = {}
    slug_of: dict[tuple[str, int], str] = {}
    by_window: dict[tuple[str, int], list[tuple[int, float, float | None, float | None]]] = defaultdict(list)
    for slug, remaining, fair, up_ask, down_ask, ref in ticks:
        key = window_key(slug)
        if key is None:
            continue
        first_ref.setdefault(key, float(ref))
        slug_of[key] = slug
        by_window[key].append((int(remaining), float(fair), up_ask, down_ask))

    labels = label_windows(first_ref, slug_of, known)

    keep = sorted(labels, key=lambda k: k[1])[-max_windows:]
    samples: list[Sample] = []
    for key in keep:
        for remaining, fair, up_ask, down_ask in by_window[key]:
            if remaining <= MIN_REMAINING_SECONDS:
                continue
            mid = market_mid_up(up_ask, down_ask)
            if mid is None:
                continue
            samples.append(Sample(key, _clip(fair), mid, labels[key]))
    return samples


# ------------------------------------------------------------- runtime state

_current: Fit | None = None
_last_attempt: float = 0.0
_last_failed = False
_task: asyncio.Task | None = None


def current() -> Fit | None:
    return _current


def adjust(
    model_up: float,
    up_ask: float | None,
    down_ask: float | None,
    *,
    require_active: bool = True,
) -> float | None:
    """Learned Up chance, or None when not in use.

    The main paper bot only uses a fit that passed its holdout check
    (``require_active``). Shadow candidates pass ``require_active=False`` so
    learned chances keep being tested forward even while the bot sits them
    out; they still need a real fit (one that got as far as its holdout check).
    """
    fit = _current
    if fit is None:
        return None
    if require_active and not fit.active:
        return None
    if not require_active and fit.brier_learned is None:
        return None  # never fitted (too few windows / no convergence)
    mid = market_mid_up(up_ask, down_ask)
    if mid is None:
        return None
    return blend(model_up, mid, fit.a_model, fit.b_market)


def _set_current(fit: Fit | None) -> None:
    global _current
    _current = fit


async def load_saved() -> Fit | None:
    """Resume the last persisted fit (startup)."""
    from db import get_config

    fit = Fit.from_json(await get_config(CONFIG_KEY))
    _set_current(fit)
    return fit


async def refit_now(db_path: str | Path | None = None) -> Fit:
    """Refit from the ledger in a worker thread and persist the result."""
    global _last_attempt, _last_failed
    import config as _config
    from db import get_config, set_config

    path = Path(db_path) if db_path is not None else _config.DB_PATH
    _last_attempt = time.time()
    try:
        from polymarket_bot import strategy_lab as _lab

        samples = await asyncio.to_thread(load_samples, path)
        try:
            trading = await asyncio.to_thread(_lab.compare_learning, path, current_rules())
        except Exception as exc:  # noqa: BLE001 — fall back to the accuracy check
            log.warning("learning.trading_check_failed", error=str(exc))
            trading = None
        fit = await asyncio.to_thread(compute_fit, samples, None, trading)
    except Exception:
        _last_failed = True
        raise
    _last_failed = False
    # The latest verdict always wins: learned chances are used only while they
    # keep beating the raw model (in the trading replay, else on accuracy).
    _set_current(fit)
    await set_config(CONFIG_KEY, fit.to_json())
    if fit.active and await get_config(STARTED_KEY) is None:
        started = datetime.fromtimestamp(fit.fitted_at, UTC).isoformat(timespec="seconds")
        await set_config(STARTED_KEY, started)
    log.info("learning.refit", active=fit.active, a=round(fit.a_model, 3),
             b=round(fit.b_market, 3), windows=fit.windows, reason=fit.reason)
    return fit


def refit_due(now: float | None = None) -> bool:
    now = time.time() if now is None else now
    wait = RETRY_AFTER_FAILURE_SECONDS if _last_failed else REFIT_EVERY_SECONDS
    if _current is None and not _last_failed and _last_attempt == 0.0:
        return True
    if _current is not None and _last_attempt == 0.0:
        # Resumed from a saved fit: refit when that fit is an hour old.
        return now - _current.fitted_at >= REFIT_EVERY_SECONDS
    return now - _last_attempt >= wait


def schedule_refit(db_path: str | Path | None = None) -> None:
    """Start a background refit when one is due (never blocks the tick)."""
    global _task
    if _task is not None and not _task.done():
        return
    if not refit_due():
        return

    async def _run() -> None:
        try:
            await refit_now(db_path)
        except Exception as exc:  # noqa: BLE001 — learning must never break the loop
            log.warning("learning.refit_failed", error=str(exc))

    _task = asyncio.get_running_loop().create_task(_run())


def status(fit: Fit | None) -> dict[str, Any]:
    """Plain data for the simple screen (no I/O)."""
    if fit is None:
        return {"state": "waiting", "detail": "The first fit runs within a few minutes of starting."}
    data = asdict(fit)
    data["state"] = "active" if fit.active else "inactive"
    if fit.active:
        data["example_model"] = 0.97
        data["example_market"] = 0.92
        data["example_learned"] = round(blend(0.97, 0.92, fit.a_model, fit.b_market), 4)
    return data
