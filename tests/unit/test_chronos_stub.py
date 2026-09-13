"""Unit tests for the Chronos integration stub (Layer 3, design only).

These tests assert the documented invariants of the stub: it is OFF by
default, ``predict()`` returns None, the ensemble is identity when no
Chronos signal is supplied, and the activation marker round-trips.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polymarket_bot import chronos_signal as cs


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


def test_stub_is_inactive_by_default(isolated_data_dir: Path) -> None:
    assert cs.is_active() is False
    assert cs.load_activation() is None


def test_predict_returns_none_in_stub(isolated_data_dir: Path) -> None:
    assert cs.predict([60000.0] * 60, 60000.0) is None


def test_ensemble_is_identity_when_chronos_returns_none() -> None:
    assert cs.apply_ensemble(0.63, None, weight_cal=1.0, weight_chronos=0.5) == 0.63


def test_ensemble_blends_when_both_present() -> None:
    # 0.50 cal weight=1, 0.80 chronos weight=1 -> 0.65
    assert cs.apply_ensemble(0.50, 0.80, weight_cal=1.0, weight_chronos=1.0) == pytest.approx(0.65)


def test_ensemble_clips_to_unit_interval() -> None:
    assert cs.apply_ensemble(0.99, 1.5, weight_cal=0.0, weight_chronos=1.0) == 1.0
    assert cs.apply_ensemble(0.01, -0.5, weight_cal=0.0, weight_chronos=1.0) == 0.0


def test_ensemble_collapses_to_baseline_when_zero_weights() -> None:
    # both weights zero -> falls back to baseline (avoids /0).
    assert cs.apply_ensemble(0.55, 0.90, weight_cal=0.0, weight_chronos=0.0) == pytest.approx(0.55)


def test_activation_marker_roundtrip(isolated_data_dir: Path) -> None:
    payload = {
        "activated_at": "2026-07-01T00:00:00+00:00",
        "weight_cap": 0.30,
        "samples_required": 200,
        "model_id": "amazon/chronos-bolt-small",
    }
    (isolated_data_dir / cs.CHRONOS_ACTIVE_FILE).write_text(json.dumps(payload))
    a = cs.load_activation()
    assert a is not None
    assert a.weight_cap == pytest.approx(0.30)
    assert a.samples_required == 200
    assert a.model_id == "amazon/chronos-bolt-small"
    assert cs.is_active() is True


def test_malformed_activation_treated_as_off(isolated_data_dir: Path) -> None:
    (isolated_data_dir / cs.CHRONOS_ACTIVE_FILE).write_text(json.dumps({"foo": "bar"}))
    assert cs.is_active() is False
