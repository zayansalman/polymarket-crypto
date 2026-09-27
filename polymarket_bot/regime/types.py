"""Shared data contracts for the market regime overview.

Every object is a frozen dataclass so a snapshot cannot drift between the
moment it is classified and the moment it is journaled or rendered — the
same purity discipline :mod:`polymarket_bot.shadow.types` uses.

Three kinds of object flow through the package:

* **Inputs** — :class:`Bar` (one Binance kline), :class:`BookState` (a
  phase-conditioned read of the Polymarket CLOB top-of-book, from the
  loop's tick journal or fetched directly) and :class:`VenueMarket` (one
  Polymarket market's own volume / liquidity / token ids). All optional at
  the snapshot level: the monitor degrades gracefully and *says so* in
  ``quality`` rather than fabricating a number.
* **Features** — :class:`RegimeFeatures`, every field ``float | None``;
  ``None`` always means "not computable from the data we had", never zero.
* **Verdicts** — :class:`StrategyFit` per strategy family and the
  :class:`RegimeSnapshot` that bundles features, bands, fits, provenance
  and the join keys a router backtest needs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Feasibility, not favour: this package can measure whether a family's
# mechanics currently WORK (costs, depth, inputs present); it cannot measure
# edge, and it never claims to (docs/archive/FINDINGS.md §3–4).
FitLabel = Literal["feasible", "degraded", "blocked"]
# Snapshot-level data grade. A router may change state only on ``full``.
Grade = Literal["full", "partial", "none"]


@dataclass(frozen=True)
class QualityFlag:
    """One recorded degradation: an enumerated ``code`` plus free-text ``detail``.

    Codes are the machine-readable contract (see
    :data:`polymarket_bot.regime.classify.QUALITY_CODES`); ``detail`` is for
    humans and is never parsed.
    """

    code: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Bar:
    """One OHLCV kline, as Binance ``/api/v3/klines`` reports it.

    ``quote_volume`` (USDT traded) is the volume measure used everywhere in
    this package — base volume changes meaning across assets, quote volume
    does not. ``taker_buy_quote`` is the USDT bought by market takers, so
    ``taker_buy_quote / quote_volume`` is the aggressive-buy share.
    """

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_quote: float


@dataclass(frozen=True)
class BookState:
    """Phase-conditioned read of the CLOB top-of-book for one Up/Down window.

    Built either from the loop's recent ``paper_ticks`` rows
    (:func:`polymarket_bot.regime.sources.book_from_ticks`, ``source =
    "paper_ticks"``) or from a direct ``/book`` read when the loop is not
    running (:func:`~polymarket_bot.regime.sources.book_from_clob`,
    ``source = "clob_direct"``). Only reads inside the window's *quotable*
    phase count — outside it the book widens for reasons tied to the window
    clock, not the regime.

    Attributes:
        overround: ``mean(up_ask + down_ask) - 1`` — the taker's round-trip
            cost before fees (a 1-tick book on both sides ≈ 0.01).
        maker_capture: ``1 - mean(up_bid + down_bid)`` — what a two-sided
            resting quote at the touch earns per completed pair.
        executable_depth_usd: mean over reads of the thinner side's
            displayed ask size × its ask price.
        ticks_used: how many reads the averages are over.
        newest_age_seconds: age of the most recent read used.
        sigma_per_second: the newest tick's 1s sigma, or ``None`` when the
            loop reported the safety floor / no volatility source, or when
            the book was read directly (no loop sigma exists then).
        vol_source: the newest tick's ``vol=`` provenance label.
        feed_degraded: the newest tick was journaled with a degraded feed.
        source: ``"paper_ticks"`` or ``"clob_direct"``.
    """

    overround: float | None
    maker_capture: float | None
    executable_depth_usd: float | None
    ticks_used: int
    newest_age_seconds: int | None
    sigma_per_second: float | None
    vol_source: str | None
    feed_degraded: bool = False
    source: str = "paper_ticks"


@dataclass(frozen=True)
class VenueMarket:
    """One Polymarket market's participation numbers and outcome tokens (Gamma)."""

    slug: str
    volume_usd: float | None
    liquidity_usd: float | None
    up_token: str = ""
    down_token: str = ""


@dataclass(frozen=True)
class RegimeFeatures:
    """Numeric regime features. ``None`` = not computable, never zero.

    Every volatility is **per-second-equivalent** (bar sigma / sqrt(bar
    seconds)); multiply by :data:`~polymarket_bot.regime.features.ANNUALIZE`
    for annualized percent. The bar-derived estimates (Garman–Klass primary,
    close-to-close cross-check) and the loop's 1s sigma are NOT
    interchangeable — see :mod:`.features` — so each carries its own
    a-priori bands.

    Fields no band or rule reads (``taker_buy_ratio_1h``, ``range_position_24h``,
    ``move_z_24h``, ``trades_ratio_seasonal``, ``vol_variance_ratio``) are
    descriptive only; a router must not consume an unbanded feature.
    """

    # --- volatility -------------------------------------------------------
    vol_1s: float | None = None            # loop's sigma_per_second, if running & not floored
    vol_1h_gk: float | None = None         # Garman–Klass on 1m bars, last 60 (primary)
    vol_1h_cc: float | None = None         # close-to-close on the same bars (cross-check)
    vol_24h_gk: float | None = None        # Garman–Klass on 5m bars, last 288
    vol_24h_cc: float | None = None
    vol_ratio_seasonal: float | None = None   # vol_1h_gk / same-UTC-hour 7-day median
    vol_ratio_1h_24h: float | None = None     # unadjusted secondary
    vol_variance_ratio: float | None = None   # vol_1h_gk² / vol_1s² — estimator diagnostic
    rv_bv_ratio_1h: float | None = None       # realized / bipower variance; >1.5 = jump
    max_return_z_1h: float | None = None      # max |1m return| / robust scale; >4 = jump
    # --- volume -----------------------------------------------------------
    volume_1h_usd: float | None = None
    volume_baseline_usd: float | None = None      # same-UTC-hour 7-day median (blended)
    volume_ratio_seasonal: float | None = None
    volume_ratio_24h: float | None = None         # unadjusted secondary
    trades_1h: float | None = None
    trades_ratio_seasonal: float | None = None
    taker_buy_ratio_1h: float | None = None       # descriptive only
    # --- move -------------------------------------------------------------
    return_1h: float | None = None
    return_24h: float | None = None
    move_z_1h: float | None = None         # return_1h / (vol_1h_gk * sqrt(3600))
    move_z_24h: float | None = None
    range_position_24h: float | None = None
    # --- Polymarket book (phase-conditioned) -------------------------------
    overround: float | None = None
    maker_capture: float | None = None
    executable_depth_usd: float | None = None
    book_ticks_used: float | None = None
    book_age_seconds: float | None = None
    # --- venue --------------------------------------------------------------
    venue_liquidity_usd: float | None = None            # current window
    venue_volume_per_window_median_usd: float | None = None  # last completed windows
    venue_liquidity_median_usd: float | None = None
    venue_windows_used: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyFit:
    """Advisory feasibility of one strategy family under the current regime.

    ``fit`` is one of ``feasible`` (its mechanics work on today's numbers),
    ``degraded`` (it would run, but an input it relies on is stale, thin or
    mis-specified — each rule says which) or ``blocked`` (it cannot be
    evaluated at all). ``reasons`` are rule ids from
    :data:`polymarket_bot.regime.classify.RULES` (prose lives there, keyed
    by id, so the persisted row stays small and queryable); ``metrics`` are
    the measured quantities the label was derived from, so a router can
    apply its own cutoffs. This is feasibility, never edge.
    """

    strategy_id: str
    label: str
    fit: FitLabel
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "label": self.label,
            "fit": self.fit,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class RegimeSnapshot:
    """One scan's complete regime read.

    Join keys for a router backtest: ``created_ts`` (epoch seconds),
    ``window_slug`` (the clock-derived Up/Down window the scan fell in, for
    the 5m/15m/1h families; ``None`` for the daily family, which joins on
    the latest snapshot with ``created_ts`` ≤ the trade's entry time),
    ``run_id`` / ``scan_seq`` (monitor process boot + monotonic counter, so
    gaps and restarts are detectable).

    ``bands`` maps each axis to its a-priori band label; ``headline`` and
    ``recommendation`` are display-only renderings of bands / fits and are
    never parsed; ``quality`` is the enumerated list of degradations and
    ``grade`` its roll-up — a router may change state only on ``full``.
    ``thresholds_version`` pins which
    :class:`~polymarket_bot.regime.classify.RegimeThresholds` produced the
    bands; the ledger stores the values behind each version.
    """

    created_at: str
    created_ts: int
    asset: str
    symbol: str
    timeframe: str
    window_slug: str | None
    run_id: str
    scan_seq: int
    features: RegimeFeatures
    bands: dict[str, str]
    headline: str
    fits: tuple[StrategyFit, ...]
    recommendation: str = ""
    quality: tuple[QualityFlag, ...] = ()
    grade: Grade = "full"
    thresholds_version: str = ""
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when any input the classifier wanted was missing or stale."""
        return bool(self.quality)

    @property
    def usable_for_router(self) -> bool:
        """The router contract: transitions only on a fully-measured snapshot."""
        return self.grade == "full"

    def as_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "created_ts": self.created_ts,
            "asset": self.asset,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "window_slug": self.window_slug,
            "run_id": self.run_id,
            "scan_seq": self.scan_seq,
            "features": self.features.as_dict(),
            "bands": dict(self.bands),
            "headline": self.headline,
            "recommendation": self.recommendation,
            "fits": [f.as_dict() for f in self.fits],
            "quality": [q.as_dict() for q in self.quality],
            "grade": self.grade,
            "usable_for_router": self.usable_for_router,
            "thresholds_version": self.thresholds_version,
            "sources": dict(self.sources),
        }
