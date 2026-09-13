"""Unit tests for the dashboard-editable runtime-knob registry (#206)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import runtime_knobs as _knobs


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_runtime_knobs.db")
    await _db.init_db()
    # Each test gets a clean in-memory cache too, so one test's writes never
    # leak into the next via the process-wide `_cache` dict.
    for name, knob in _knobs.KNOBS.items():
        _knobs._cache[name] = knob.default
    yield


@pytest.mark.asyncio
async def test_get_returns_default_when_unset() -> None:
    assert await _knobs.get("daily_entry_edge_min") == pytest.approx(0.045)


@pytest.mark.asyncio
async def test_set_persists_and_get_reflects_it() -> None:
    await _knobs.set("daily_entry_edge_min", 0.06)
    assert await _knobs.get("daily_entry_edge_min") == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_reset_clears_override_back_to_default() -> None:
    await _knobs.set("daily_entry_edge_min", 0.06)
    await _knobs.reset("daily_entry_edge_min")
    assert await _knobs.get("daily_entry_edge_min") == pytest.approx(0.045)


@pytest.mark.asyncio
async def test_get_override_is_none_when_unset() -> None:
    assert await _knobs.get_override("live_daily_loss_halt_usd") is None
    await _knobs.set("live_daily_loss_halt_usd", 25.0)
    assert await _knobs.get_override("live_daily_loss_halt_usd") == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_set_rejects_below_min() -> None:
    with pytest.raises(ValueError):
        await _knobs.set("daily_entry_edge_min", -0.1)


@pytest.mark.asyncio
async def test_set_rejects_above_max() -> None:
    with pytest.raises(ValueError):
        await _knobs.set("live_max_entry_slippage", 1.5)


@pytest.mark.asyncio
async def test_set_rejects_invalid_enum_choice() -> None:
    with pytest.raises(ValueError):
        await _knobs.set("exit_style", "moonshot")


@pytest.mark.asyncio
async def test_bool_knob_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    # No bool knob is registered since the auto-pause knobs were archived with
    # the v0 strategy; register a throwaway one to keep the kind covered.
    monkeypatch.setitem(
        _knobs.KNOBS, "test_flag", _knobs.Knob("runtime.test.flag", True, "bool", "Test flag")
    )
    await _knobs.set("test_flag", False)
    assert (await _knobs.get("test_flag")) is False
    await _knobs.set("test_flag", True)
    assert (await _knobs.get("test_flag")) is True


@pytest.mark.asyncio
async def test_int_knob_coerces_float_input() -> None:
    await _knobs.set("paper_time_exit_seconds", 90.0)
    assert await _knobs.get("paper_time_exit_seconds") == 90


@pytest.mark.asyncio
async def test_refresh_cache_populates_cached_reads() -> None:
    await _knobs.set("paper_target_return", 0.20)
    await _knobs.refresh_cache()
    assert _knobs.cached("paper_target_return") == pytest.approx(0.20)


def test_cached_returns_default_before_any_refresh() -> None:
    assert _knobs.cached("paper_stop_return") == pytest.approx(-0.08)
