"""Unit tests for Layer 2 strategy-params proposer / apply path (#37, #206)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import params as p
from polymarket_bot import runtime_knobs as _knobs


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own throwaway SQLite so the real journal is untouched."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_params.db")
    await _db.init_db()
    yield


@pytest.mark.asyncio
async def test_load_active_falls_back_to_default_when_no_override(
    isolated_data_dir: Path,
) -> None:
    a = await p.load_active()
    assert a.source == "default"
    assert a.entry_edge_min == pytest.approx(_knobs.KNOBS["paper_entry_edge_min"].default)


@pytest.mark.asyncio
async def test_load_active_reflects_operator_override(isolated_data_dir: Path) -> None:
    await _knobs.set("paper_entry_edge_min", 0.06)
    a = await p.load_active()
    assert a.source == "operator"
    assert a.entry_edge_min == pytest.approx(0.06)


def test_load_proposed_returns_none_when_missing(isolated_data_dir: Path) -> None:
    assert p.load_proposed() is None


def test_save_and_load_proposed_roundtrip(isolated_data_dir: Path) -> None:
    pr = p.ActiveParams(
        entry_edge_min=0.06,
        entry_edge_max=0.08,
        min_confidence=0.62,
        min_remaining_seconds=120,
        max_entry_price=0.85,
        min_entry_price=0.40,
        source="proposed",
        proposed_at="2026-06-15T07:00:00+00:00",
        backtest_meta={"recommended_pnl": 12.34},
    )
    path = p.save_proposed(pr)
    assert path.exists()
    loaded = p.load_proposed()
    assert loaded is not None
    assert loaded.entry_edge_min == pytest.approx(0.06)
    assert loaded.min_confidence == pytest.approx(0.62)
    assert loaded.backtest_meta == {"recommended_pnl": 12.34}
