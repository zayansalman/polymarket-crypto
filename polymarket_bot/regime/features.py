"""Pure regime feature math over :class:`~polymarket_bot.regime.types.Bar` lists.

No I/O, no clock, no globals: every function takes bars (oldest-first) and
returns a float, or ``None`` when the input cannot support the estimate.
``None`` is the honest answer for "not enough data" — a zero would read as
a real (and extreme) observation downstream.

Volatility conventions
----------------------
Every volatility is **per-second-equivalent**: a bar-level sigma divided by
``sqrt(bar_seconds)``, i.e. the diffusion coefficient
:func:`polymarket_bot.strategy.fair_up_probability` consumes as
``sigma * sqrt(remaining_seconds)``. Multiply by :data:`ANNUALIZE`
(``sqrt(seconds per year)``) for the familiar annualized figure.

The estimators are deliberately NOT treated as interchangeable with the
loop's own ``sigma_per_second``:

* **Garman–Klass** (primary, :func:`garman_klass_vol_per_second`) uses each
  bar's open/high/low/close — ~7× as efficient as close-to-close on the
  same bar count, robust to a single outlier print, and built from trade
  prices so it carries no quote-bounce inflation.
* **Close-to-close** (:func:`realized_vol_per_second`) is kept as a
  cross-check; its ratio to bipower variation is the jump diagnostic.
* The loop's **1-second** sigma is a different instrument (Chainlink oracle
  prints, positively autocorrelated, floored at 2e-5): it reads well below a
  bar-based estimate on BTC and above it on tick-coarse alts. The classifier
  therefore bands the 1s sigma on its own legacy cutoffs and the bar
  estimates on annualized cutoffs, and reports the variance ratio between
  them as a diagnostic rather than pretending they agree.

The bars handed in are COMPLETED bars only (``sources.fetch_bars`` drops the
still-forming candle). The seasonal baselines are matched on hour of day AND
day class (weekday / weekend, by each bar's own UTC date), and they exclude
every hourly bar that overlaps the rolling last-hour window being compared, so
the numerator can never leak into its own baseline.
"""
from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from polymarket_bot.regime.types import Bar, BookState, RegimeFeatures, VenueMarket

SECONDS_PER_YEAR = 365.0 * 86400.0
ANNUALIZE = math.sqrt(SECONDS_PER_YEAR)   # per-second sigma × this = annualized

_MIN_RETURNS = 3          # fewer log-returns than this → no stdev-based estimate
_MIN_RANGE_BARS = 3       # for the range estimators
_GK_CLOSE_WEIGHT = 2.0 * math.log(2.0) - 1.0


def log_returns(bars: Sequence[Bar]) -> list[float]:
    """Consecutive close-to-close log returns, skipping non-positive prices."""
    out: list[float] = []
    for prev, cur in zip(bars, bars[1:]):
        if prev.close > 0 and cur.close > 0:
            out.append(math.log(cur.close / prev.close))
    return out


def realized_vol_per_second(bars: Sequence[Bar], bar_seconds: float) -> float | None:
    """Sample stdev of bar log-returns, scaled to per-second-equivalent.

    Same estimator family as :func:`polymarket_bot.strategy.sigma_per_second`
    (``statistics.stdev`` of log returns) but WITHOUT its 2e-5 safety floor:
    the floor exists so a live pricing model can never claim false
    certainty, whereas here a genuinely quiet reading is information.
    """
    if bar_seconds <= 0:
        return None
    returns = log_returns(bars)
    if len(returns) < _MIN_RETURNS:
        return None
    return statistics.stdev(returns) / math.sqrt(bar_seconds)


def garman_klass_variance(bar: Bar) -> float | None:
    """Per-bar Garman–Klass variance: ``0.5 ln(H/L)² − (2 ln 2 − 1) ln(C/O)²``.

    For a consistent bar (``L ≤ min(O, C)`` and ``max(O, C) ≤ H``) ``ln(H/L) ≥
    |ln(C/O)|``, so the term is non-negative. A bar whose open or close lies
    outside its own high–low range is malformed data and yields ``None``
    rather than a number of unknown meaning.
    """
    if bar.low <= 0 or bar.open <= 0 or bar.close <= 0:
        return None
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        return None
    hl = math.log(bar.high / bar.low)
    co = math.log(bar.close / bar.open)
    return 0.5 * hl * hl - _GK_CLOSE_WEIGHT * co * co


def garman_klass_vol_per_second(bars: Sequence[Bar], bar_seconds: float) -> float | None:
    """Garman–Klass realized vol over the bars, per-second-equivalent.

    ``sqrt(mean(per-bar GK variance) / bar_seconds)``; malformed bars are
    skipped. The ``max(0, …)`` is a floating-point guard only — a consistent
    bar's term is non-negative (see :func:`garman_klass_variance`).
    """
    if bar_seconds <= 0:
        return None
    terms = [v for v in (garman_klass_variance(b) for b in bars) if v is not None]
    if len(terms) < _MIN_RANGE_BARS:
        return None
    mean_var = max(0.0, sum(terms) / len(terms))
    return math.sqrt(mean_var / bar_seconds)


def rv_bv_ratio(bars: Sequence[Bar]) -> float | None:
    """Realized variance / bipower variation over the bar returns.

    ``RV = Σ r²``, ``BV = (π/2) Σ |r_i||r_{i−1}|``. Under continuous
    diffusion the ratio is ≈1; a jump inflates RV but barely BV, so a ratio
    well above 1 flags a discontinuous move inside the window.
    """
    r = log_returns(bars)
    if len(r) < _MIN_RETURNS + 1:
        return None
    rv = sum(x * x for x in r)
    bv = (math.pi / 2.0) * sum(abs(a) * abs(b) for a, b in zip(r, r[1:]))
    if bv <= 0:
        return None
    return rv / bv


def max_return_z(bars: Sequence[Bar]) -> float | None:
    """Largest |bar return| in robust-sigma units: ``max|r| / (1.4826 · MAD)``."""
    r = log_returns(bars)
    if len(r) < _MIN_RETURNS + 1:
        return None
    med = statistics.median(r)
    mad = statistics.median(abs(x - med) for x in r)
    scale = 1.4826 * mad
    if scale <= 0:
        return None
    return max(abs(x) for x in r) / scale


def quote_volume_sum(bars: Sequence[Bar]) -> float | None:
    """Total quote-asset (USDT) volume over the bars; ``None`` when empty."""
    if not bars:
        return None
    return float(sum(b.quote_volume for b in bars))


def trades_sum(bars: Sequence[Bar]) -> float | None:
    if not bars:
        return None
    return float(sum(b.trades for b in bars))


def hourly_average_quote_volume(bars: Sequence[Bar], bar_seconds: float) -> float | None:
    """Quote volume per hour, normalised by the span the bars actually cover."""
    total = quote_volume_sum(bars)
    if total is None or bar_seconds <= 0:
        return None
    hours = len(bars) * bar_seconds / 3600.0
    if hours <= 0:
        return None
    return total / hours


def taker_buy_ratio(bars: Sequence[Bar]) -> float | None:
    """Share of quote volume that was aggressive BUYING (0.5 = balanced)."""
    total = sum(b.quote_volume for b in bars)
    if total <= 0:
        return None
    return float(sum(b.taker_buy_quote for b in bars) / total)


def window_log_return(bars: Sequence[Bar]) -> float | None:
    """Log return from the first bar's open to the last bar's close."""
    if not bars or bars[0].open <= 0 or bars[-1].close <= 0:
        return None
    return math.log(bars[-1].close / bars[0].open)


def move_z(log_return: float | None, vol_per_second: float | None, seconds: float) -> float | None:
    """The window's move in diffusion units: ``return / (sigma · sqrt(seconds))``.

    This is the ONE trend-like statistic kept: the drift t-statistic and the
    Kaufman efficiency ratio are algebraically the same z-score on a fixed
    window, and none of them measures persistence (which is not detectable
    on a single klines call). It says "how big was the move", nothing more.
    """
    if log_return is None or vol_per_second is None or vol_per_second <= 0 or seconds <= 0:
        return None
    return log_return / (vol_per_second * math.sqrt(seconds))


def range_position(bars: Sequence[Bar]) -> float | None:
    """Where the last close sits in the bars' high–low range (0 = low, 1 = high)."""
    if not bars:
        return None
    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    if hi <= lo:
        return None
    return (bars[-1].close - lo) / (hi - lo)


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    """``numerator / denominator`` when both present and the denominator > 0."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


# --- Seasonal baselines -------------------------------------------------------


_HOUR_MS = 3_600_000


def _is_weekend(dt: datetime) -> bool:
    return dt.astimezone(UTC).weekday() >= 5


def same_hour_median(
    hourly_bars: Sequence[Bar],
    hour: int,
    weekend: bool,
    value_of,
    *,
    closed_by_ms: int | None = None,
) -> float | None:
    """Median of ``value_of(bar)`` over hourly bars at UTC ``hour`` on days of one class.

    ``weekend`` selects Saturday/Sunday bars (by each bar's own UTC open date)
    versus Monday–Friday bars, so a weekend hour is compared with weekend
    hours rather than with a mostly-weekday pool. ``closed_by_ms`` drops any
    bar that had not finished by that instant (used to keep the compared
    window out of its own baseline). ``None`` when fewer than 3 bars remain
    or every value is ``None``.
    """
    vals: list[float] = []
    for b in hourly_bars:
        if closed_by_ms is not None and b.open_time_ms + _HOUR_MS > closed_by_ms:
            continue
        dt = datetime.fromtimestamp(b.open_time_ms / 1000.0, tz=UTC)
        if dt.hour != hour or _is_weekend(dt) != weekend:
            continue
        v = value_of(b)
        if v is not None:
            vals.append(v)
    if len(vals) < 3:
        return None
    return float(statistics.median(vals))


def blended_hour_baseline(
    hourly_bars: Sequence[Bar], at: datetime, value_of
) -> float | None:
    """Same-UTC-hour, same-day-class baseline for the rolling hour ending ``at``.

    The rolling last-60-minutes window straddles two clock hours; the
    baseline blends their same-hour medians by overlap:
    ``w · median(h) + (1 − w) · median(h − 1)`` with ``w = minute / 60``. Each
    of the two hours takes the day class (weekday / weekend) of its OWN date,
    so a scan at Monday 00:15 blends Monday's hour 0 with Sunday's hour 23.
    Only bars that closed before the rolling window began contribute. Falls
    back to whichever median exists when only one does; ``None`` when neither.
    """
    at = at.astimezone(UTC)
    window_start_ms = int(at.timestamp() * 1000) - _HOUR_MS
    w = at.minute / 60.0
    cur_start = at.replace(minute=0, second=0, microsecond=0)
    prev_start = cur_start - timedelta(hours=1)
    cur = same_hour_median(
        hourly_bars, cur_start.hour, _is_weekend(cur_start), value_of,
        closed_by_ms=window_start_ms,
    )
    prev = same_hour_median(
        hourly_bars, prev_start.hour, _is_weekend(prev_start), value_of,
        closed_by_ms=window_start_ms,
    )
    if cur is None and prev is None:
        return None
    if cur is None:
        return prev
    if prev is None:
        return cur
    return w * cur + (1.0 - w) * prev


def _gk_per_second_of_bar(bar_seconds: float):
    def value_of(bar: Bar) -> float | None:
        v = garman_klass_variance(bar)
        if v is None:
            return None
        return math.sqrt(max(0.0, v) / bar_seconds)
    return value_of


# --- Book / venue -------------------------------------------------------------


def median_or_none(values: Sequence[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else None


# --- Assembly -----------------------------------------------------------------


def compute_features(
    *,
    bars_1m: Sequence[Bar],
    bars_5m: Sequence[Bar],
    bars_1h: Sequence[Bar],
    at: datetime,
    book: BookState | None,
    venue_current: VenueMarket | None,
    venue_completed: Sequence[VenueMarket] = (),
) -> RegimeFeatures:
    """Assemble every feature from the raw inputs (each independently optional).

    ``bars_1m`` is the last hour, ``bars_5m`` the last 24h, ``bars_1h`` the
    last 28 days (seasonal baselines); ``at`` is the scan instant (for the
    hour-of-day blend). ``venue_completed`` are the last few *completed*
    windows of the selected family, whose volumes are comparable; the
    current window's in-progress volume is deliberately not a feature.
    """
    vol_1h_gk = garman_klass_vol_per_second(bars_1m, 60.0)
    vol_1h_cc = realized_vol_per_second(bars_1m, 60.0)
    vol_24h_gk = garman_klass_vol_per_second(bars_5m, 300.0)
    vol_24h_cc = realized_vol_per_second(bars_5m, 300.0)
    vol_baseline = blended_hour_baseline(bars_1h, at, _gk_per_second_of_bar(3600.0))

    volume_1h = quote_volume_sum(bars_1m)
    volume_baseline = blended_hour_baseline(bars_1h, at, lambda b: b.quote_volume)
    trades_1h = trades_sum(bars_1m)
    trades_baseline = blended_hour_baseline(bars_1h, at, lambda b: float(b.trades))
    volume_24h_hourly = hourly_average_quote_volume(bars_5m, 300.0)

    return_1h = window_log_return(bars_1m)
    return_24h = window_log_return(bars_5m)

    vol_1s = book.sigma_per_second if book is not None else None

    return RegimeFeatures(
        vol_1s=vol_1s,
        vol_1h_gk=vol_1h_gk,
        vol_1h_cc=vol_1h_cc,
        vol_24h_gk=vol_24h_gk,
        vol_24h_cc=vol_24h_cc,
        vol_ratio_seasonal=ratio(vol_1h_gk, vol_baseline),
        vol_ratio_1h_24h=ratio(vol_1h_gk, vol_24h_gk),
        vol_variance_ratio=(
            ratio(vol_1h_gk * vol_1h_gk, vol_1s * vol_1s)
            if vol_1h_gk is not None and vol_1s is not None
            else None
        ),
        rv_bv_ratio_1h=rv_bv_ratio(bars_1m),
        max_return_z_1h=max_return_z(bars_1m),
        volume_1h_usd=volume_1h,
        volume_baseline_usd=volume_baseline,
        volume_ratio_seasonal=ratio(volume_1h, volume_baseline),
        volume_ratio_24h=ratio(volume_1h, volume_24h_hourly),
        trades_1h=trades_1h,
        trades_ratio_seasonal=ratio(trades_1h, trades_baseline),
        taker_buy_ratio_1h=taker_buy_ratio(bars_1m),
        return_1h=return_1h,
        return_24h=return_24h,
        move_z_1h=move_z(return_1h, vol_1h_gk, 3600.0),
        move_z_24h=move_z(return_24h, vol_24h_gk, 86400.0),
        range_position_24h=range_position(bars_5m),
        overround=book.overround if book else None,
        maker_capture=book.maker_capture if book else None,
        executable_depth_usd=book.executable_depth_usd if book else None,
        book_ticks_used=float(book.ticks_used) if book else None,
        book_age_seconds=(
            float(book.newest_age_seconds)
            if book and book.newest_age_seconds is not None
            else None
        ),
        venue_liquidity_usd=venue_current.liquidity_usd if venue_current else None,
        venue_volume_per_window_median_usd=median_or_none(
            [v.volume_usd for v in venue_completed]
        ),
        venue_liquidity_median_usd=median_or_none(
            [v.liquidity_usd for v in venue_completed]
        ),
        venue_windows_used=float(len(venue_completed)) if venue_completed else None,
    )
