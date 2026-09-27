"""Regime bands and strategy feasibility (polymarket_bot/regime/classify.py).

Pins the a-priori cutoffs (including equality with tools/regime_attribution's
frozen bands at the boundaries — by test, not by import, so the live package
never imports the numpy-backed tool), the feasibility rule table, the data
grade, and the invariant that every persisted reason is a registered rule id.
"""
from __future__ import annotations

import pytest

from polymarket_bot.regime import classify as C
from polymarket_bot.regime.features import ANNUALIZE
from polymarket_bot.regime.types import BookState, QualityFlag, RegimeFeatures
from tools.regime_attribution import time_of_day_band, vol_band

T = C.DEFAULT_THRESHOLDS
DAILY = ("doge", "sol", "xrp", "bnb", "eth")


def _feats(**kw) -> RegimeFeatures:
    return RegimeFeatures(**kw)


def _book(age: int = 5, **kw) -> BookState:
    base = dict(overround=0.012, maker_capture=0.008, executable_depth_usd=150.0,
                ticks_used=12, newest_age_seconds=age, sigma_per_second=4e-5,
                vol_source="chainlink_ws")
    base.update(kw)
    return BookState(**base)  # type: ignore[arg-type]


# --- bands ---------------------------------------------------------------------


@pytest.mark.parametrize("sigma", [1e-5, 2.9e-5, 3e-5, 4e-5, 5.9e-5, 6e-5, 1e-4])
def test_vol_1s_band_matches_regime_attribution_cutoffs(sigma: float) -> None:
    mapping = {"lo": "low", "mid": "mid", "hi": "high"}
    assert C.vol_1s_band(_feats(vol_1s=sigma)) == mapping[vol_band(sigma)]


@pytest.mark.parametrize("hour", [0, 7, 8, 15, 16, 23])
def test_session_band_matches_regime_attribution_cutoffs(hour: int) -> None:
    ts = f"2026-09-27T{hour:02d}:30:00+00:00"
    assert C.session_band(ts) == time_of_day_band(ts)


def test_session_band_treats_naive_as_utc_and_converts_offsets() -> None:
    assert C.session_band("2026-09-27T03:00:00") == "night"
    # 03:00 at +05:00 is 22:00 UTC the previous day → evening.
    assert C.session_band("2026-09-27T03:00:00+05:00") == "evening"


def test_weekday_band() -> None:
    assert C.weekday_band("2026-09-26T12:00:00+00:00") == "weekend"  # Saturday
    assert C.weekday_band("2026-09-28T12:00:00+00:00") == "weekday"  # Monday


@pytest.mark.parametrize(
    "ann, band",
    [(0.10, "low"), (0.2999, "low"), (0.30, "mid"), (0.45, "mid"), (0.5499, "mid"), (0.55, "high"), (1.2, "high")],
)
def test_volatility_band_uses_annualized_bar_cutoffs(ann: float, band: str) -> None:
    assert C.volatility_band(_feats(vol_1h_gk=ann / ANNUALIZE)) == band


def test_volatility_band_unknown_without_bar_vol_even_if_loop_sigma_exists() -> None:
    """The loop's 1s sigma is a different instrument: it never stands in for the bar band."""
    assert C.volatility_band(_feats(vol_1s=4e-5)) == "unknown"


@pytest.mark.parametrize("r, band", [(0.5, "contracting"), (0.67, "contracting"), (1.0, "stable"), (1.5, "expanding"), (3.0, "expanding")])
def test_vol_trend_band(r: float, band: str) -> None:
    assert C.vol_trend_band(_feats(vol_ratio_seasonal=r)) == band


@pytest.mark.parametrize("r, band", [(0.3, "quiet"), (0.6, "normal"), (1.0, "normal"), (1.5, "normal"), (1.51, "active")])
def test_volume_band(r: float, band: str) -> None:
    assert C.volume_band(_feats(volume_ratio_seasonal=r)) == band


@pytest.mark.parametrize("z, band", [(-3.0, "big_down"), (-2.0, "big_down"), (-1.9, "flat"), (0.0, "flat"), (1.9, "flat"), (2.0, "big_up")])
def test_move_band(z: float, band: str) -> None:
    assert C.move_band(_feats(move_z_1h=z)) == band


def test_jump_band_fires_on_either_diagnostic() -> None:
    assert C.jump_band(_feats(rv_bv_ratio_1h=1.2, max_return_z_1h=2.0)) == "continuous"
    assert C.jump_band(_feats(rv_bv_ratio_1h=1.6, max_return_z_1h=2.0)) == "jump"
    assert C.jump_band(_feats(rv_bv_ratio_1h=1.0, max_return_z_1h=4.5)) == "jump"
    assert C.jump_band(_feats()) == "unknown"


def test_book_band_by_overround_stale_and_unknown() -> None:
    assert C.book_band(_feats(overround=0.01), _book()) == "cheap"
    assert C.book_band(_feats(overround=0.03), _book()) == "normal"
    assert C.book_band(_feats(overround=0.05), _book()) == "expensive"
    assert C.book_band(_feats(overround=0.01), _book(age=61)) == "stale"
    assert C.book_band(_feats(overround=0.01), _book(age=None)) == "stale"
    assert C.book_band(_feats(), None) == "unknown"
    assert C.book_band(_feats(), _book()) == "unknown"  # in-phase read but no two-sided ask


def test_bands_for_has_every_axis_and_estimator_labels() -> None:
    bands = C.bands_for(_feats(), None, "2026-09-27T12:00:00+00:00")
    assert set(bands) == {"volatility", "vol_1s", "vol_trend", "volume", "move", "jumps", "book", "session", "weekday"}
    assert set(C.BAND_ESTIMATORS) <= set(bands)


def test_headline_renders_bands_and_partial_prefix() -> None:
    f = _feats(vol_1h_gk=0.40 / ANNUALIZE, volume_ratio_seasonal=1.3, move_z_1h=0.2, rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    bands = C.bands_for(f, None, "2026-09-27T12:00:00+00:00")  # Sunday
    line = C.headline(f, bands)
    assert line.startswith("VOL 40% ann (MID)")
    assert "VOLUME 1.3× (NORMAL)" in line and "FLAT" in line and "DAY UTC" in line and "WEEKEND" in line
    assert "BOOK" not in line  # unknown book is omitted
    assert C.headline(f, bands, degraded=True).startswith("PARTIAL DATA · ")
    assert C.headline(_feats(), C.bands_for(_feats(), None, "2026-09-28T12:00:00+00:00")).startswith("VOL UNKNOWN · VOLUME UNKNOWN")


# --- grade ---------------------------------------------------------------------


def test_grade_for() -> None:
    assert C.grade_for(()) == "full"
    assert C.grade_for((QualityFlag("book_absent"),)) == "partial"
    assert C.grade_for((QualityFlag("book_absent"), QualityFlag("bars_unavailable"))) == "none"


def test_every_quality_code_used_by_the_monitor_is_registered() -> None:
    from polymarket_bot.regime import monitor

    import inspect
    src = inspect.getsource(monitor)
    for code in C.QUALITY_CODES:
        assert code in src or code in ("book_not_for_selected_market",), code


# --- fits ----------------------------------------------------------------------


def _fits(f: RegimeFeatures, book: BookState | None, asset: str = "btc", edge_gate: float = 0.045):
    bands = C.bands_for(f, book, "2026-09-28T12:00:00+00:00")
    return {x.strategy_id: x for x in C.strategy_fits(f, bands, edge_gate=edge_gate, asset=asset, daily_assets=DAILY)}


def test_all_reasons_are_registered_rule_ids_with_provenance_prefix() -> None:
    for rid in C.RULES:
        assert rid.startswith(("m.", "p."))
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, vol_24h_gk=0.4 / ANNUALIZE, overround=0.02, maker_capture=0.01,
               executable_depth_usd=100.0, rv_bv_ratio_1h=2.0, vol_ratio_seasonal=2.0)
    for fit in _fits(f, _book()).values():
        for r in fit.reasons:
            assert r in C.RULES


def test_unregistered_rule_id_raises() -> None:
    with pytest.raises(KeyError):
        C._fit("btc_5m_taker", [], ["m.not_a_rule"], {})


def test_taker_feasible_when_cost_inside_gate_and_loop_sigma_present() -> None:
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, vol_1s=4e-5, overround=0.012, executable_depth_usd=150.0,
               rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    fit = _fits(f, _book())["btc_5m_taker"]
    assert fit.fit == "feasible"
    assert "m.taker_cost_inside_gate" in fit.reasons
    mean_ask = (1 + 0.012) / 2
    assert fit.metrics["taker_round_trip_cost"] == pytest.approx(0.006 + 0.07 * mean_ask * (1 - mean_ask), abs=1e-4)
    assert fit.metrics["edge_gate"] == 0.045


def test_taker_degraded_when_cost_exceeds_gate() -> None:
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, vol_1s=4e-5, overround=0.08, executable_depth_usd=150.0,
               rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    fit = _fits(f, _book(), edge_gate=0.045)["btc_5m_taker"]
    assert fit.fit == "degraded" and "m.taker_cost_over_gate" in fit.reasons


def test_taker_degraded_on_thin_depth_and_missing_loop_sigma() -> None:
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, vol_1s=None, overround=0.012, executable_depth_usd=1.0,
               rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    fit = _fits(f, _book(sigma_per_second=None))["btc_5m_taker"]
    assert fit.fit == "degraded"
    assert {"m.depth_below_min", "m.loop_sigma_unusable"} <= set(fit.reasons)
    assert fit.metrics["venue_min_usd"] == pytest.approx(5 * (1.012 / 2), abs=1e-2)


def test_taker_degraded_with_unknown_or_stale_book_and_jump() -> None:
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, rv_bv_ratio_1h=2.0, max_return_z_1h=1.0)
    fit = _fits(f, None)["btc_5m_taker"]
    assert fit.fit == "degraded" and {"m.book_unknown", "m.jump_gaussian"} <= set(fit.reasons)
    fit = _fits(_feats(vol_1h_gk=0.4 / ANNUALIZE, overround=0.01), _book(age=99))["btc_5m_taker"]
    assert "m.book_stale" in fit.reasons


def test_taker_blocked_without_any_volatility() -> None:
    fit = _fits(_feats(), None)["btc_5m_taker"]
    assert fit.fit == "blocked" and "m.no_vol" in fit.reasons


def test_maker_feasible_with_positive_capture() -> None:
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, overround=0.012, maker_capture=0.01, executable_depth_usd=100.0,
               rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    fit = _fits(f, _book())["pairarb_maker"]
    assert fit.fit == "feasible" and "m.capture_positive" in fit.reasons
    assert fit.metrics["maker_capture"] == 0.01


def test_maker_degraded_without_capture_and_context_rule_on_high_vol() -> None:
    f = _feats(vol_1h_gk=0.9 / ANNUALIZE, overround=0.012, maker_capture=-0.01,
               rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    fit = _fits(f, _book())["pairarb_maker"]
    assert fit.fit == "degraded"
    assert "m.no_capture" in fit.reasons and "p.high_vol_maker" in fit.reasons


def test_maker_degraded_on_jump_and_unknown_book() -> None:
    fit = _fits(_feats(vol_1h_gk=0.4 / ANNUALIZE, rv_bv_ratio_1h=3.0), None)["pairarb_maker"]
    assert fit.fit == "degraded" and {"m.book_unknown", "m.jump_maker"} <= set(fit.reasons)


def test_daily_feasible_only_on_a_scanner_asset_with_consistent_vol() -> None:
    f = _feats(vol_24h_gk=0.5 / ANNUALIZE, vol_ratio_seasonal=1.0, rv_bv_ratio_1h=1.0, max_return_z_1h=1.0)
    assert _fits(f, None, asset="sol")["daily_altcoin"].fit == "feasible"
    btc = _fits(f, None, asset="btc")["daily_altcoin"]
    assert btc.fit == "degraded" and "m.daily_proxy_asset" in btc.reasons


def test_daily_degraded_when_vol_expanding_and_blocked_without_24h_bars() -> None:
    f = _feats(vol_24h_gk=0.5 / ANNUALIZE, vol_ratio_seasonal=2.0)
    fit = _fits(f, None, asset="sol")["daily_altcoin"]
    assert fit.fit == "degraded" and "m.vol_expanding_stale_sigma" in fit.reasons
    assert "m.vol_consistent" not in fit.reasons
    assert _fits(_feats(), None, asset="sol")["daily_altcoin"].fit == "blocked"


def test_no_rule_reads_session_or_move_bands() -> None:
    """Display-only axes (FINDINGS §4: the night effect failed replication)."""
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, vol_24h_gk=0.4 / ANNUALIZE, vol_1s=4e-5, overround=0.012,
               maker_capture=0.01, executable_depth_usd=150.0, rv_bv_ratio_1h=1.0, max_return_z_1h=1.0,
               vol_ratio_seasonal=1.0)
    book = _book()
    for ts in ("2026-09-27T03:00:00+00:00", "2026-09-28T12:00:00+00:00"):  # weekend night vs weekday day
        for z in (-5.0, 0.0, 5.0):
            bands = C.bands_for(RegimeFeatures(**{**f.as_dict(), "move_z_1h": z}), book, ts)
            fits = C.strategy_fits(RegimeFeatures(**{**f.as_dict(), "move_z_1h": z}), bands,
                                   edge_gate=0.045, asset="sol", daily_assets=DAILY)
            assert [x.fit for x in fits] == ["feasible", "feasible", "feasible"]


def test_strategy_families_are_stable_ids() -> None:
    assert list(C.STRATEGY_FAMILIES) == ["btc_5m_taker", "pairarb_maker", "daily_altcoin"]


def test_recommendation_lines() -> None:
    f = _feats(vol_1h_gk=0.4 / ANNUALIZE, vol_24h_gk=0.4 / ANNUALIZE, vol_1s=4e-5, overround=0.012,
               maker_capture=0.01, executable_depth_usd=150.0, rv_bv_ratio_1h=1.0, max_return_z_1h=1.0,
               vol_ratio_seasonal=1.0)
    fits = tuple(_fits(f, _book(), asset="btc").values())
    assert C.recommendation(fits) == "feasible: btc_5m_taker, pairarb_maker · degraded: daily_altcoin"
    blocked = tuple(_fits(_feats(), None).values())
    assert C.recommendation(blocked).startswith("nothing fully feasible") or C.recommendation(blocked).startswith("stand down")
    from polymarket_bot.regime.types import StrategyFit
    assert C.recommendation((StrategyFit("btc_5m_taker", "x", "blocked"),)) == "stand down: no family can be evaluated"


def test_thresholds_are_versioned_and_serialisable() -> None:
    d = T.as_dict()
    assert d["version"] == C.THRESHOLDS_VERSION
    assert d["vol_1s_low"] == 3e-5 and d["vol_bar_low_ann"] == 0.30
