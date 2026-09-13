"""Unit tests for the regime-attribution instrument (tools/regime_attribution.py).

The instrument answers one question rigorously: does any *a-priori* regime
carry real, two-sided edge in the shadow ledger? Every guard the project
learned the hard way is encoded here as a test — a-priori bands (not mined),
attribution by the side BET (not outcome), a two-sided-edge gate, permutation
significance, Benjamini-Hochberg FDR, and a power gate that keeps the verdict
honest while the sample is still small.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.regime_attribution import (
    Cell,
    analyze,
    attribute,
    basis_band,
    benjamini_hochberg,
    edge_band,
    format_report,
    load_settled_rows,
    one_vs_rest_p,
    time_of_day_band,
    two_sided_edge,
    vol_band,
)


def _make_db(path: Path, rows: list[dict]) -> None:
    """Create a minimal shadow-positions table and insert ``rows``."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE model_shadow_positions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT, window_slug TEXT, model_id TEXT, side TEXT,
          entry_price REAL, notional_usd REAL, shares REAL, fair_prob REAL,
          edge REAL, confidence REAL, reason TEXT, state TEXT, outcome TEXT,
          settlement_price REAL, resolved_at TEXT, realized_pnl_usd REAL,
          quote_source TEXT, feed_source TEXT
        )
        """
    )
    for r in rows:
        conn.execute(
            """INSERT INTO model_shadow_positions
               (created_at, model_id, side, entry_price, shares, edge, state,
                outcome, realized_pnl_usd)
               VALUES (:created_at, :model_id, :side, :entry_price, :shares,
                       :edge, :state, :outcome, :realized_pnl_usd)""",
            r,
        )
    conn.commit()
    conn.close()


def _row(
    *,
    model_id: str = "M",
    side: str = "Up",
    outcome: str = "Up",
    entry_price: float = 0.6,
    shares: float = 5.0,
    realized_pnl_usd: float = 1.0,
    created_at: str = "2026-06-23T10:00:00",
    edge: float = 0.05,
) -> dict:
    return {
        "model_id": model_id,
        "side": side,
        "outcome": outcome,
        "entry_price": entry_price,
        "shares": shares,
        "realized_pnl_usd": realized_pnl_usd,
        "created_at": created_at,
        "edge": edge,
    }


def _by_key(cells: list[Cell]) -> dict[tuple[str, str, str], Cell]:
    return {(c.model_id, c.regime, c.side): c for c in cells}


class TestTimeOfDayBand:
    """A-priori UTC bands, declared up front: night 00-08, day 08-16,
    evening 16-24. Grounded in the prior 'night bleed' observation, NOT mined
    from this sample."""

    def test_night_is_00_to_08_utc(self) -> None:
        assert time_of_day_band("2026-06-23T00:00:00") == "night"
        assert time_of_day_band("2026-06-23T03:15:00+00:00") == "night"
        assert time_of_day_band("2026-06-23T07:59:00") == "night"

    def test_day_is_08_to_16_utc(self) -> None:
        assert time_of_day_band("2026-06-23T08:00:00") == "day"
        assert time_of_day_band("2026-06-23T15:30:00") == "day"

    def test_evening_is_16_to_24_utc(self) -> None:
        assert time_of_day_band("2026-06-23T16:00:00") == "evening"
        assert time_of_day_band("2026-06-23T23:59:00Z") == "evening"

    def test_non_utc_offset_is_normalized_to_utc(self) -> None:
        # 23:30 at -05:00 is 04:30 UTC -> night, not evening. Honors the UTC
        # contract regardless of the source offset.
        assert time_of_day_band("2026-06-23T23:30:00-05:00") == "night"
        # +05:30 at 04:00 local is 22:30 UTC -> evening.
        assert time_of_day_band("2026-06-24T04:00:00+05:30") == "evening"


class TestEdgeBand:
    """A-priori edge bands mirroring the strategy's own edge gate: lo <0.06,
    mid 0.06-0.09, hi >=0.09. Fixed cutoffs, not fitted to this sample."""

    def test_lo_below_0_06(self) -> None:
        assert edge_band(0.045) == "lo"
        assert edge_band(0.059) == "lo"
        assert edge_band(-0.01) == "lo"

    def test_mid_0_06_to_0_09(self) -> None:
        assert edge_band(0.06) == "mid"
        assert edge_band(0.089) == "mid"

    def test_hi_at_or_above_0_09(self) -> None:
        assert edge_band(0.09) == "hi"
        assert edge_band(0.15) == "hi"


class TestVolBand:
    """A-priori 1s-vol bands (#122): lo <3e-5, mid 3e-5-6e-5, hi >=6e-5.
    Round cutoffs bracketing the observed ~4e-5/s median, fixed in advance."""

    def test_lo_below_3e5(self) -> None:
        assert vol_band(2.9e-5) == "lo"
        assert vol_band(0.0) == "lo"

    def test_mid_3e5_to_6e5(self) -> None:
        assert vol_band(3e-5) == "mid"
        assert vol_band(5.9e-5) == "mid"

    def test_hi_at_or_above_6e5(self) -> None:
        assert vol_band(6e-5) == "hi"
        assert vol_band(8e-4) == "hi"


class TestBasisBand:
    """A-priori spot-vs-reference dislocation bands (#122): near <5bps,
    mid 5-15bps, far >=15bps. Magnitude only; the signed cushion is separate."""

    def test_near_below_5bps(self) -> None:
        # 62800 vs 62790 → ~1.6bps → near.
        assert basis_band(62_800.0, 62_790.0) == "near"

    def test_mid_5_to_15bps(self) -> None:
        # 62800 vs 62750 → ~8bps → mid.
        assert basis_band(62_800.0, 62_750.0) == "mid"

    def test_far_at_or_above_15bps(self) -> None:
        # 62800 vs 62650 → ~24bps → far.
        assert basis_band(62_800.0, 62_650.0) == "far"

    def test_sign_agnostic(self) -> None:
        """Only the magnitude of dislocation matters — sign does not."""
        assert basis_band(62_750.0, 62_800.0) == basis_band(62_800.0, 62_750.0)

    def test_degenerate_zero_spot_is_na(self) -> None:
        assert basis_band(0.0, 0.0) == "na"


class TestAttribute:
    """Per (model x regime x side-BET) cells. The side is the bet placed, NOT
    the realized winner: a Down bet that loses still lands in the Down cell."""

    def test_groups_by_model_regime_and_side_bet(self) -> None:
        rows = [
            # M, night, Up: one win (+1.0) and one loss (-3.0)
            _row(side="Up", outcome="Up", realized_pnl_usd=1.0,
                 created_at="2026-06-23T03:00:00"),
            _row(side="Up", outcome="Down", realized_pnl_usd=-3.0,
                 created_at="2026-06-23T04:00:00"),
            # M, day, Down: one win (+1.5)
            _row(side="Down", outcome="Down", realized_pnl_usd=1.5,
                 entry_price=0.55, created_at="2026-06-23T10:00:00"),
        ]
        cells = _by_key(
            attribute(rows, lambda r: time_of_day_band(r["created_at"]), min_n=2)
        )

        night_up = cells[("M", "night", "Up")]
        assert night_up.n == 2
        assert night_up.wins == 1
        assert night_up.win_rate == 0.5
        assert night_up.net_pnl == -2.0
        assert night_up.expectancy == -1.0

        day_down = cells[("M", "day", "Down")]
        assert day_down.n == 1
        assert day_down.wins == 1
        assert day_down.net_pnl == 1.5
        assert day_down.expectancy == 1.5

    def test_power_gate_flags_thin_cells(self) -> None:
        rows = [_row(created_at="2026-06-23T10:00:00") for _ in range(5)]
        cells = _by_key(
            attribute(rows, lambda r: time_of_day_band(r["created_at"]), min_n=30)
        )
        assert cells[("M", "day", "Up")].n == 5
        assert cells[("M", "day", "Up")].powered is False

        rows = [_row(created_at="2026-06-23T10:00:00") for _ in range(30)]
        cells = _by_key(
            attribute(rows, lambda r: time_of_day_band(r["created_at"]), min_n=30)
        )
        assert cells[("M", "day", "Up")].powered is True

    def test_win_is_side_bet_matching_outcome_not_pnl_sign(self) -> None:
        # A Down bet whose outcome was Down is a win even if we deliberately
        # mislabel pnl — wins counts side==outcome, not pnl > 0.
        rows = [_row(side="Down", outcome="Down", realized_pnl_usd=-99.0)]
        cells = _by_key(
            attribute(rows, lambda r: "all", min_n=1)
        )
        assert cells[("M", "all", "Down")].wins == 1


def _cell(
    side: str,
    *,
    expectancy: float,
    wilson_low: float,
    breakeven: float,
    powered: bool = True,
    model_id: str = "M",
    regime: str = "day",
) -> Cell:
    return Cell(
        model_id=model_id, regime=regime, side=side, n=50, wins=30,
        win_rate=0.6, net_pnl=expectancy * 50, expectancy=expectancy,
        avg_entry=0.6, wilson_low=wilson_low, wilson_high=wilson_low + 0.1,
        breakeven=breakeven, powered=powered,
    )


class TestTwoSidedEdge:
    """A regime is real edge ONLY if BOTH the Up and the Down bet are positive,
    powered, and clear fee breakeven. A one-sided 'edge' is just directional
    tilt — a mechanical artifact, not edge."""

    def test_true_when_both_sides_clear_the_bar(self) -> None:
        cells = [
            _cell("Up", expectancy=0.2, wilson_low=0.65, breakeven=0.62),
            _cell("Down", expectancy=0.1, wilson_low=0.64, breakeven=0.62),
        ]
        assert two_sided_edge(cells, "M", "day") is True

    def test_false_when_one_side_loses(self) -> None:
        cells = [
            _cell("Up", expectancy=0.2, wilson_low=0.65, breakeven=0.62),
            _cell("Down", expectancy=-0.05, wilson_low=0.40, breakeven=0.62),
        ]
        assert two_sided_edge(cells, "M", "day") is False

    def test_false_when_a_side_is_underpowered(self) -> None:
        cells = [
            _cell("Up", expectancy=0.2, wilson_low=0.65, breakeven=0.62),
            _cell("Down", expectancy=0.1, wilson_low=0.64, breakeven=0.62,
                  powered=False),
        ]
        assert two_sided_edge(cells, "M", "day") is False

    def test_false_when_only_one_side_traded(self) -> None:
        cells = [_cell("Up", expectancy=0.2, wilson_low=0.65, breakeven=0.62)]
        assert two_sided_edge(cells, "M", "day") is False

    def test_false_when_band_does_not_clear_breakeven(self) -> None:
        cells = [
            _cell("Up", expectancy=0.2, wilson_low=0.60, breakeven=0.62),
            _cell("Down", expectancy=0.1, wilson_low=0.64, breakeven=0.62),
        ]
        assert two_sided_edge(cells, "M", "day") is False


class TestBenjaminiHochberg:
    """FDR correction across the many model x regime hypotheses, so testing a
    lot of cells doesn't manufacture a spurious 'significant' winner."""

    def test_empty(self) -> None:
        assert benjamini_hochberg([]) == []

    def test_only_smallest_survives(self) -> None:
        # m=4, alpha=0.05: only 0.001 <= (1/4)*0.05 = 0.0125.
        assert benjamini_hochberg([0.001, 0.5, 0.7, 0.9], alpha=0.05) == [
            True, False, False, False,
        ]

    def test_all_reject_when_all_tiny(self) -> None:
        assert benjamini_hochberg([0.001, 0.002, 0.003], alpha=0.05) == [
            True, True, True,
        ]

    def test_none_reject_when_all_large(self) -> None:
        assert benjamini_hochberg([0.5, 0.6, 0.9], alpha=0.05) == [
            False, False, False,
        ]

    def test_step_up_lifts_a_smaller_p_that_failed_its_own_rank(self) -> None:
        # Sorted [0.03, 0.04, 0.05] @ m=3, alpha=0.05: rank-1 threshold is
        # 0.0167 (0.03 fails alone) but rank-3 (0.05 <= 0.05) passes, so BH
        # step-up rejects ALL three. Order is preserved in the returned mask.
        assert benjamini_hochberg([0.05, 0.03, 0.04], alpha=0.05) == [
            True, True, True,
        ]


def _sr(side: str, pnl: float, regime: str, model_id: str = "M") -> dict:
    return {
        "model_id": model_id, "side": side,
        "realized_pnl_usd": pnl, "regime": regime,
    }


class TestOneVsRest:
    """Per-(model, regime) one-vs-rest permutation on SIDE-RESIDUALIZED PnL:
    tests whether THIS regime differs from the model's other regimes, net of
    directional side tilt. Seeded, so p is reproducible."""

    def test_target_clearly_different_is_significant(self) -> None:
        rows = (
            [_sr(s, 1.0, "A") for s in ("Up", "Down") for _ in range(15)]
            + [_sr(s, -1.0, "B") for s in ("Up", "Down") for _ in range(15)]
        )
        p = one_vs_rest_p(rows, lambda r: r["regime"], "A", n_perm=2000, seed=0)
        assert p < 0.05

    def test_homogeneous_is_not_significant(self) -> None:
        rows = [
            _sr(s, 1.0, rg)
            for rg in ("A", "B") for s in ("Up", "Down") for _ in range(15)
        ]
        p = one_vs_rest_p(rows, lambda r: r["regime"], "A", n_perm=500, seed=0)
        assert p == 1.0

    def test_pure_side_mix_shift_is_not_significant(self) -> None:
        # Up always +1, Down always -1. Regime A is 90% Up, B is 90% Down, so
        # RAW regime means differ hugely — but there is ZERO within-side regime
        # effect. Side-residualization must neutralize it (the finding-3 guard).
        rows = (
            [_sr("Up", 1.0, "A") for _ in range(18)]
            + [_sr("Down", -1.0, "A") for _ in range(2)]
            + [_sr("Up", 1.0, "B") for _ in range(2)]
            + [_sr("Down", -1.0, "B") for _ in range(18)]
        )
        p = one_vs_rest_p(rows, lambda r: r["regime"], "A", n_perm=500, seed=0)
        assert p == 1.0

    def test_single_regime_has_no_contrast(self) -> None:
        rows = [_sr(s, 1.0, "A") for s in ("Up", "Down") for _ in range(10)]
        p = one_vs_rest_p(rows, lambda r: r["regime"], "A", n_perm=500, seed=0)
        assert p == 1.0


class TestLoadSettledRows:
    """Read-only load of settled shadow rows, carrying the axis columns."""

    def test_returns_only_settled_with_axis_columns(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_db(db, [
            dict(created_at="2026-06-23T03:00:00", model_id="M", side="Up",
                 entry_price=0.6, shares=5.0, edge=0.05, state="settled",
                 outcome="Up", realized_pnl_usd=1.9),
            dict(created_at="2026-06-23T10:00:00", model_id="M", side="Down",
                 entry_price=0.55, shares=5.0, edge=0.07, state="settled",
                 outcome="Up", realized_pnl_usd=-3.0),
            dict(created_at="2026-06-23T11:00:00", model_id="M", side="Up",
                 entry_price=0.6, shares=5.0, edge=0.05, state="open",
                 outcome=None, realized_pnl_usd=None),
        ])
        rows = load_settled_rows(db)
        assert len(rows) == 2
        assert all(r["realized_pnl_usd"] is not None for r in rows)
        assert {r["side"] for r in rows} == {"Up", "Down"}
        assert all(r["created_at"] and r["edge"] is not None for r in rows)

    def test_missing_db_raises(self, tmp_path: Path) -> None:
        import pytest

        with pytest.raises(FileNotFoundError):
            load_settled_rows(tmp_path / "nope.db")


def _settled(side: str, won: bool, created_at: str) -> dict:
    """A settled row: outcome matches the bet side iff ``won``."""
    opposite = "Down" if side == "Up" else "Up"
    return dict(
        model_id="M", side=side, outcome=(side if won else opposite),
        entry_price=0.6, shares=5.0, edge=0.05,
        realized_pnl_usd=(1.9 if won else -3.0), created_at=created_at,
    )


class TestAnalyze:
    """End-to-end wiring: cells -> two-sided gate -> permutation -> FDR.
    A candidate requires BOTH significant regime structure AND a two-sided
    profitable regime — a model that's uniformly good everywhere is a model
    edge, not a regime edge."""

    DAY = "2026-06-23T10:00:00"
    NIGHT = "2026-06-23T03:00:00"

    def test_uniformly_profitable_model_is_not_a_regime_candidate(self) -> None:
        # Both regimes identical and two-sided profitable -> no DIFFERENTIAL,
        # so permutation is not significant -> no regime candidate.
        rows = []
        for ts in (self.DAY, self.NIGHT):
            for i in range(40):
                for side in ("Up", "Down"):
                    rows.append(_settled(side, won=(i % 10 != 0), created_at=ts))
        result = analyze(rows, axis="time", min_n=20, n_perm=400, seed=0)
        assert result.candidates == []

    def test_regime_with_two_sided_edge_and_structure_is_a_candidate(self) -> None:
        rows = []
        for i in range(40):  # day: ~90% win both sides (real two-sided edge)
            for side in ("Up", "Down"):
                rows.append(_settled(side, won=(i % 10 != 0), created_at=self.DAY))
        for i in range(40):  # night: ~10% win both sides (bleeds)
            for side in ("Up", "Down"):
                rows.append(_settled(side, won=(i % 10 == 0), created_at=self.NIGHT))
        result = analyze(rows, axis="time", min_n=20, n_perm=500, seed=0)
        assert ("M", "day") in result.candidates
        assert ("M", "night") not in result.candidates


class TestFormatReport:
    """The human-facing report leads with the verdict and lists per-cell rows."""

    def test_includes_verdict_and_cells(self) -> None:
        rows = [
            _settled(side, won=(i % 10 != 0), created_at="2026-06-23T10:00:00")
            for i in range(40) for side in ("Up", "Down")
        ]
        result = analyze(rows, axis="time", min_n=20, n_perm=300, seed=0)
        report = format_report(result)
        assert result.verdict in report
        assert "day" in report
        assert "Up" in report and "Down" in report

    def test_empty_sample_is_graceful(self) -> None:
        result = analyze([], axis="time", min_n=30, n_perm=10, seed=0)
        report = format_report(result)
        assert "no regime clears the bar" in report
