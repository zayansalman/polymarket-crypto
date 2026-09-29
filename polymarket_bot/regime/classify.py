"""A-priori regime bands and the advisory strategy-feasibility table.

Two guards this project learned the hard way (docs/archive/FINDINGS.md §3–4)
shape everything here:

* **Thresholds are declared up front, never fitted.** Every cutoff in
  :class:`RegimeThresholds` is a round, documented number chosen for its
  units, not for any PnL it produced. The loop's 1-second sigma keeps the
  exact cutoffs ``tools/regime_attribution.py:vol_band`` froze (3e-5 / 6e-5
  per second); the bar-derived estimators, a different instrument (see
  :mod:`.features`), get their own bands in annualized terms. The session
  bands reuse the tool's ``time_of_day_band``. Both are pinned by a unit
  test rather than an import, so this live package never drags the tool's
  numpy dependency into the dashboard. ``THRESHOLDS_VERSION`` is stamped on
  every snapshot and the values behind it are journaled once per version,
  so a later change never silently re-labels history.
* **Fit means feasibility, never edge.** Regime *switching* within a
  family was falsified on this venue (0/12 cells, 0/75 slices survive FDR;
  a switcher lost to holding), so this table does not rank families by
  expected profit. It reports whether each family's mechanics currently
  work — round-trip cost vs. its own edge gate, maker capture at the touch,
  depth vs. the venue minimum, whether its volatility input is stale — with
  the measured numbers attached so a router can apply its own cutoffs,
  hysteresis and dwell time. No family is ever "favoured" here; that label
  is reserved for an FDR-surviving ``regime_attribution`` candidate, which
  today does not exist. The session and move bands are display-only: no
  rule reads them (the night-hours effect failed replication, p=0.51).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from polymarket_bot.regime.features import ANNUALIZE
from polymarket_bot.regime.types import (
    QUALITY_CODES,
    BookState,
    Grade,
    QualityFlag,
    RegimeFeatures,
    StrategyFit,
)

__all__ = ["QUALITY_CODES"]  # re-exported: the vocabulary lives in types.py

THRESHOLDS_VERSION = "2026-09-29.v1"

UNKNOWN = "unknown"
TAKER_FEE_RATE = 0.07   # polymarket_bot/shadow/fees.py: 0.07·p·(1−p) per share


@dataclass(frozen=True)
class RegimeThresholds:
    """Every band cutoff, in one place, with units."""

    # Loop 1s sigma (per second) — regime_attribution.vol_band cutoffs.
    vol_1s_low: float = 3e-5
    vol_1s_high: float = 6e-5
    # Bar-derived (Garman–Klass) vol, ANNUALIZED fraction: lo <30%, hi ≥55%.
    vol_bar_low_ann: float = 0.30
    vol_bar_high_ann: float = 0.55
    # 1h vol vs. its same-UTC-hour 7-day median.
    vol_expanding: float = 1.5
    vol_contracting: float = 0.67
    # Last-hour quote volume vs. its same-UTC-hour 7-day median.
    volume_quiet: float = 0.6
    volume_active: float = 1.5
    # |move z| above this is a "big move" (≈2σ of the window's own vol).
    move_big_z: float = 2.0
    # Jump diagnostics on the last hour of 1m bars.
    jump_rv_bv: float = 1.5
    jump_max_z: float = 4.0
    # Polymarket book cost, from the phase-conditioned overround
    # (1-tick both sides ≈ 0.01).
    book_cheap: float = 0.02
    book_expensive: float = 0.04
    book_stale_seconds: int = 60
    # Venue minimum order (polymarket_exec/execution/live.py DEFAULT_MIN_ORDER_SIZE).
    min_order_shares: float = 5.0
    # Session bands, UTC hour (same cutoffs as regime_attribution).
    session_day_start: int = 8
    session_evening_start: int = 16
    version: str = THRESHOLDS_VERSION

    def as_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


DEFAULT_THRESHOLDS = RegimeThresholds()

# --- Strategy family registry ----------------------------------------------
# Stable ids: a router and the backtest that validates it key on these. Add
# new families by appending; never rename an id that has been persisted.
STRATEGY_FAMILIES: dict[str, str] = {
    "btc_5m_taker": "5m pricing loop (taker, hold-to-settle)",
    "pairarb_maker": "5m two-sided maker quoting (shadow)",
    "daily_altcoin": "Daily altcoin Up/Down scanner (shadow)",
}

# --- Rule registry --------------------------------------------------------------
# Persisted fits carry these ids; the prose lives here so rows stay small and
# a backtest can count rule firings. Each id is prefixed with its provenance:
#   m. = measured on this snapshot   p. = prior / context, not evidence
RULES: dict[str, str] = {
    "m.no_vol": "no volatility estimate from any source",
    "m.book_unknown": "book unknown: no in-phase read (start the loop, or wait for the window's quotable phase)",
    "m.book_stale": "book read is stale",
    "m.book_expensive_taker": (
        "expensive book (overround at or above the a-priori cutoff): the edge gate already "
        "nets the spread, so this means few entries clear it — and wide-spread entries were "
        "fee-negative in the #149 replay (n=21, small sample)"
    ),
    "m.depth_below_min": "touch depth below the venue minimum order",
    "m.jump_gaussian": "jump in the last hour: a Gaussian fair value is mis-specified",
    "m.loop_sigma_unusable": "loop sigma on the safety floor or absent",
    "m.no_capture": "bids sum to ≥ 1: nothing to capture at the touch",
    "m.capture_positive": "positive maker capture at the touch",
    "m.capture_unknown": "maker capture not measured: one side has no bid to quote against",
    "m.jump_maker": "jump in the last hour: resting quotes get run over",
    "p.high_vol_maker": "high vol: adverse selection on resting quotes rises (context, not evidence)",
    "m.no_24h_bars": "no 24h bars: the scanner's volatility input cannot be checked",
    "m.vol_expanding_stale_sigma": "vol expanding vs. its baseline: the scanner's 30-day sigma is stale",
    "m.jump_lognormal": "jump in the last hour: a log-normal 24h fair value is mis-specified",
    "m.vol_consistent": "volatility input consistent with its baseline",
    "m.daily_proxy_asset": "judged on an asset the daily scanner does not trade — select one of its assets for an asset-true read",
}


# --- Bands ------------------------------------------------------------------


def volatility_band(features: RegimeFeatures, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """``low`` / ``mid`` / ``high`` on the Garman–Klass 1h vol (annualized cutoffs)."""
    vol = features.vol_1h_gk
    if vol is None:
        return UNKNOWN
    ann = vol * ANNUALIZE
    if ann < t.vol_bar_low_ann:
        return "low"
    if ann < t.vol_bar_high_ann:
        return "mid"
    return "high"


def vol_1s_band(features: RegimeFeatures, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """The loop's own sigma on its legacy per-second cutoffs (``unknown`` when
    the loop is not running or reported the floor)."""
    vol = features.vol_1s
    if vol is None:
        return UNKNOWN
    if vol < t.vol_1s_low:
        return "low"
    if vol < t.vol_1s_high:
        return "mid"
    return "high"


def vol_trend_band(features: RegimeFeatures, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """``expanding`` / ``stable`` / ``contracting`` vs. the same-hour baseline."""
    r = features.vol_ratio_seasonal
    if r is None:
        return UNKNOWN
    if r >= t.vol_expanding:
        return "expanding"
    if r <= t.vol_contracting:
        return "contracting"
    return "stable"


def volume_band(features: RegimeFeatures, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """``quiet`` / ``normal`` / ``active`` vs. the same-hour baseline."""
    r = features.volume_ratio_seasonal
    if r is None:
        return UNKNOWN
    if r < t.volume_quiet:
        return "quiet"
    if r > t.volume_active:
        return "active"
    return "normal"


def move_band(features: RegimeFeatures, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """``big_up`` / ``flat`` / ``big_down`` from the 1h move in its own sigma units.

    Descriptive only: it says how far price travelled, not whether it will
    keep going. Persistence is not measurable on one klines call, so no
    "trending" label exists and nothing in the fit table reads this band.
    """
    z = features.move_z_1h
    if z is None:
        return UNKNOWN
    if z >= t.move_big_z:
        return "big_up"
    if z <= -t.move_big_z:
        return "big_down"
    return "flat"


def jump_band(features: RegimeFeatures, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """``jump`` when either diagnostic fires, else ``continuous``."""
    rvbv, mz = features.rv_bv_ratio_1h, features.max_return_z_1h
    if rvbv is None and mz is None:
        return UNKNOWN
    if (rvbv is not None and rvbv > t.jump_rv_bv) or (mz is not None and mz > t.jump_max_z):
        return "jump"
    return "continuous"


def book_band(
    features: RegimeFeatures,
    book: BookState | None,
    t: RegimeThresholds = DEFAULT_THRESHOLDS,
) -> str:
    """``cheap`` / ``normal`` / ``expensive`` / ``stale`` / ``unknown`` from the overround.

    ``unknown`` means no in-phase read exists; ``stale`` means the newest
    read is older than ``book_stale_seconds``.
    """
    if book is None:
        return UNKNOWN
    if book.newest_age_seconds is None or book.newest_age_seconds > t.book_stale_seconds:
        return "stale"
    if features.overround is None:
        return UNKNOWN
    if features.overround <= t.book_cheap:
        return "cheap"
    if features.overround >= t.book_expensive:
        return "expensive"
    return "normal"


def _parse_utc(created_at: str) -> datetime:
    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def session_band(created_at: str, t: RegimeThresholds = DEFAULT_THRESHOLDS) -> str:
    """``night`` 00–07, ``day`` 08–15, ``evening`` 16–23 UTC (naive = UTC)."""
    hour = _parse_utc(created_at).hour
    if hour < t.session_day_start:
        return "night"
    if hour < t.session_evening_start:
        return "day"
    return "evening"


def weekday_band(created_at: str) -> str:
    """``weekday`` / ``weekend`` (UTC)."""
    return "weekend" if _parse_utc(created_at).weekday() >= 5 else "weekday"


def bands_for(
    features: RegimeFeatures,
    book: BookState | None,
    created_at: str,
    t: RegimeThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, str]:
    return {
        "volatility": volatility_band(features, t),
        "vol_1s": vol_1s_band(features, t),
        "vol_trend": vol_trend_band(features, t),
        "volume": volume_band(features, t),
        "move": move_band(features, t),
        "jumps": jump_band(features, t),
        "book": book_band(features, book, t),
        "session": session_band(created_at, t),
        "weekday": weekday_band(created_at),
    }


# Which estimator produced each banded axis — persisted alongside the bands.
BAND_ESTIMATORS: dict[str, str] = {
    "volatility": "garman_klass_1m_60",
    "vol_1s": "loop_sigma_per_second",
    "vol_trend": "garman_klass_1m_60 / same_hour_median_1h_168",
    "volume": "quote_volume_1m_60 / same_hour_median_1h_168",
    "move": "return_1h / (garman_klass_1m_60 * sqrt(3600))",
    "jumps": "rv_bv_ratio_1m_60 | max_return_z_1m_60",
    "book": "overround, phase [60,270]s remaining",
}


def headline(features: RegimeFeatures, bands: dict[str, str], degraded: bool = False) -> str:
    """One display line, e.g. ``VOL 41% ann (MID) · VOLUME 1.3× (NORMAL) · FLAT · BOOK CHEAP``."""
    parts: list[str] = []
    vol = bands.get("volatility", UNKNOWN)
    if features.vol_1h_gk is not None and vol != UNKNOWN:
        vt = bands.get("vol_trend", UNKNOWN)
        suffix = f", {vt}" if vt not in (UNKNOWN, "stable") else ""
        parts.append(f"VOL {features.vol_1h_gk * ANNUALIZE * 100:.0f}% ann ({vol.upper()}{suffix})")
    else:
        parts.append("VOL UNKNOWN")
    volume = bands.get("volume", UNKNOWN)
    if features.volume_ratio_seasonal is not None and volume != UNKNOWN:
        parts.append(f"VOLUME {features.volume_ratio_seasonal:.1f}× ({volume.upper()})")
    else:
        parts.append("VOLUME UNKNOWN")
    move = bands.get("move", UNKNOWN)
    if move != UNKNOWN:
        parts.append(move.replace("_", " ").upper())
    if bands.get("jumps") == "jump":
        parts.append("JUMP")
    book = bands.get("book", UNKNOWN)
    if book != UNKNOWN:
        parts.append(f"BOOK {book.upper()}")
    parts.append(f"{bands.get('session', UNKNOWN).upper()} UTC")
    if bands.get("weekday") == "weekend":
        parts.append("WEEKEND")
    line = " · ".join(parts)
    return f"PARTIAL DATA · {line}" if degraded else line


# --- Quality grade ---------------------------------------------------------------


def grade_for(quality: Sequence[QualityFlag]) -> Grade:
    """``none`` with no market data at all, ``partial`` with any flag, else ``full``."""
    codes = {q.code for q in quality}
    if "bars_unavailable" in codes:
        return "none"
    return "partial" if codes else "full"


# --- Strategy feasibility ------------------------------------------------------


def taker_fee_per_share(price: float, fee_rate: float = TAKER_FEE_RATE) -> float:
    """The venue's taker fee at ``price`` (same formula as shadow/fees.py)."""
    return fee_rate * price * (1.0 - price)


def _worst(labels: list[str]) -> str:
    if "blocked" in labels:
        return "blocked"
    if "degraded" in labels:
        return "degraded"
    return "feasible"


def _fit(sid: str, labels: list[str], reasons: list[str], metrics: dict[str, float]) -> StrategyFit:
    unknown = [r for r in reasons if r not in RULES]
    if unknown:
        raise KeyError(f"unregistered rule id(s): {unknown}")
    return StrategyFit(
        sid, STRATEGY_FAMILIES[sid], _worst(labels),  # type: ignore[arg-type]
        tuple(reasons), metrics,
    )


def _btc_5m_taker(
    f: RegimeFeatures, b: dict[str, str], edge_gate: float, t: RegimeThresholds
) -> StrategyFit:
    """Feasibility of the taker loop. The loop's edge is fair value minus the
    executable ASK, so the spread is already inside its gate: a wide book
    means fewer entries clear it, not that gate-passing entries lose. The
    one cost the gate does not net is the taker fee, reported as a metric
    (at 7% it is always far below the gate, so it never degrades a fit)."""
    labels: list[str] = []
    reasons: list[str] = []
    metrics: dict[str, float] = {"edge_gate": edge_gate}
    if b["volatility"] == UNKNOWN and f.vol_1s is None:
        labels.append("blocked")
        reasons.append("m.no_vol")
    book = b["book"]
    if book == UNKNOWN:
        labels.append("degraded")
        reasons.append("m.book_unknown")
    elif book == "stale":
        labels.append("degraded")
        reasons.append("m.book_stale")
    elif f.overround is not None:
        mean_ask = (1.0 + f.overround) / 2.0
        metrics["overround"] = round(f.overround, 4)
        metrics["taker_entry_fee"] = round(taker_fee_per_share(mean_ask), 4)
        if book == "expensive":
            labels.append("degraded")
            reasons.append("m.book_expensive_taker")
        if f.executable_depth_usd is not None:
            min_usd = t.min_order_shares * mean_ask
            metrics["executable_depth_usd"] = round(f.executable_depth_usd, 2)
            metrics["venue_min_usd"] = round(min_usd, 2)
            if f.executable_depth_usd < min_usd:
                labels.append("degraded")
                reasons.append("m.depth_below_min")
        if f.vol_1s is None:
            labels.append("degraded")
            reasons.append("m.loop_sigma_unusable")
    if b["jumps"] == "jump":
        labels.append("degraded")
        reasons.append("m.jump_gaussian")
    if f.vol_1h_gk is not None:
        metrics["vol_1h_annualized"] = round(f.vol_1h_gk * ANNUALIZE, 4)
    return _fit("btc_5m_taker", labels, reasons, metrics)


def _pairarb_maker(f: RegimeFeatures, b: dict[str, str], t: RegimeThresholds) -> StrategyFit:
    labels: list[str] = []
    reasons: list[str] = []
    metrics: dict[str, float] = {}
    if b["volatility"] == UNKNOWN and f.vol_1s is None:
        labels.append("blocked")
        reasons.append("m.no_vol")
    book = b["book"]
    if book == UNKNOWN:
        labels.append("degraded")
        reasons.append("m.book_unknown")
    elif book == "stale":
        labels.append("degraded")
        reasons.append("m.book_stale")
    elif f.maker_capture is None:
        labels.append("degraded")
        reasons.append("m.capture_unknown")
    else:
        metrics["maker_capture"] = round(f.maker_capture, 4)
        if f.maker_capture <= 0:
            labels.append("degraded")
            reasons.append("m.no_capture")
        else:
            reasons.append("m.capture_positive")
    if f.executable_depth_usd is not None:
        metrics["executable_depth_usd"] = round(f.executable_depth_usd, 2)
    if b["jumps"] == "jump":
        labels.append("degraded")
        reasons.append("m.jump_maker")
    if b["volatility"] == "high":
        reasons.append("p.high_vol_maker")
    return _fit("pairarb_maker", labels, reasons, metrics)


def _daily_altcoin(
    f: RegimeFeatures, b: dict[str, str], asset: str, daily_assets: Sequence[str],
    t: RegimeThresholds,
) -> StrategyFit:
    labels: list[str] = []
    reasons: list[str] = []
    metrics: dict[str, float] = {}
    if asset not in daily_assets:
        labels.append("degraded")
        reasons.append("m.daily_proxy_asset")
    if f.vol_24h_gk is None:
        labels.append("blocked")
        reasons.append("m.no_24h_bars")
    else:
        metrics["vol_24h_annualized"] = round(f.vol_24h_gk * ANNUALIZE, 4)
    if f.vol_ratio_seasonal is not None:
        metrics["vol_ratio_seasonal"] = round(f.vol_ratio_seasonal, 3)
    if b["vol_trend"] == "expanding":
        labels.append("degraded")
        reasons.append("m.vol_expanding_stale_sigma")
    if b["jumps"] == "jump":
        labels.append("degraded")
        reasons.append("m.jump_lognormal")
    if f.vol_24h_gk is not None and b["vol_trend"] != "expanding" and b["jumps"] != "jump":
        reasons.append("m.vol_consistent")
    return _fit("daily_altcoin", labels, reasons, metrics)


def strategy_fits(
    features: RegimeFeatures,
    bands: dict[str, str],
    *,
    edge_gate: float,
    asset: str,
    daily_assets: Sequence[str],
    t: RegimeThresholds = DEFAULT_THRESHOLDS,
) -> tuple[StrategyFit, ...]:
    """Every family's feasibility.

    ``edge_gate`` is the loop's entry edge minimum
    (``config.PAPER_ENTRY_EDGE_MIN``), reported beside the taker's entry fee; ``daily_assets`` is what the daily scanner actually trades
    (``config.DAILY_ASSETS``), so a snapshot on another asset is labelled a
    proxy rather than passed off as a read on that family.
    """
    return (
        _btc_5m_taker(features, bands, edge_gate, t),
        _pairarb_maker(features, bands, t),
        _daily_altcoin(features, bands, asset, daily_assets, t),
    )


def recommendation(fits: Sequence[StrategyFit]) -> str:
    """One display line: which families are feasible, or ``stand down`` when none are."""
    feasible = [f.strategy_id for f in fits if f.fit == "feasible"]
    degraded = [f.strategy_id for f in fits if f.fit == "degraded"]
    if feasible:
        line = f"feasible: {', '.join(feasible)}"
        if degraded:
            line += f" · degraded: {', '.join(degraded)}"
        return line
    if degraded:
        return f"nothing fully feasible · degraded: {', '.join(degraded)}"
    return "stand down: no family can be evaluated"
