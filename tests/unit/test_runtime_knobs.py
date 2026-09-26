"""Unit tests for the dashboard-editable runtime-knob registry (#206)."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from ems import db as _db
from ems import runtime_knobs as _knobs


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
    assert await _knobs.get("fade1h_kelly_multiplier") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_set_persists_and_get_reflects_it() -> None:
    await _knobs.set("fade1h_kelly_multiplier", 0.06)
    assert await _knobs.get("fade1h_kelly_multiplier") == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_reset_clears_override_back_to_default() -> None:
    await _knobs.set("fade1h_kelly_multiplier", 0.06)
    await _knobs.reset("fade1h_kelly_multiplier")
    assert await _knobs.get("fade1h_kelly_multiplier") == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_get_override_is_none_when_unset() -> None:
    assert await _knobs.get_override("fade1h_bankroll_usd") is None
    await _knobs.set("fade1h_bankroll_usd", 25.0)
    assert await _knobs.get_override("fade1h_bankroll_usd") == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_set_rejects_below_min() -> None:
    with pytest.raises(ValueError):
        await _knobs.set("fade1h_kelly_multiplier", -0.1)


@pytest.mark.asyncio
async def test_set_rejects_above_max() -> None:
    with pytest.raises(ValueError):
        await _knobs.set("fade1h_kelly_multiplier", 1.5)


@pytest.mark.asyncio
async def test_set_rejects_invalid_enum_choice() -> None:
    with pytest.raises(ValueError):
        await _knobs.set("fade1h_spot_feed", "moonshot")


@pytest.mark.asyncio
async def test_a_stored_choice_no_longer_offered_reads_as_the_default() -> None:
    # A value an earlier build stored (the fade strategy's old "chainlink_twap60" price feed)
    # is not a choice any more, so the default applies instead of the raw text.
    knob = _knobs.KNOBS["fade1h_spot_feed"]
    await _db.set_config(knob.key, "chainlink_twap60")
    assert await _knobs.get_override("fade1h_spot_feed") is None
    assert await _knobs.get("fade1h_spot_feed") == knob.default == "chainlink"
    await _knobs.set("fade1h_spot_feed", "binance")
    assert await _knobs.get("fade1h_spot_feed") == "binance"


@pytest.mark.asyncio
async def test_bool_knob_roundtrip() -> None:
    await _knobs.set("fade1h_reduce_positions", False)
    assert (await _knobs.get("fade1h_reduce_positions")) is False
    await _knobs.set("fade1h_reduce_positions", True)
    assert (await _knobs.get("fade1h_reduce_positions")) is True


@pytest.mark.asyncio
async def test_int_knob_coerces_float_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # No int knob is registered today; register a throwaway one to keep the kind covered.
    monkeypatch.setitem(
        _knobs.KNOBS, "test_int", _knobs.Knob("runtime.test.int", 45, "int", "Test int", 0, 300)
    )
    monkeypatch.setitem(_knobs._cache, "test_int", 45)
    await _knobs.set("test_int", 90.0)
    assert await _knobs.get("test_int") == 90


@pytest.mark.asyncio
async def test_refresh_cache_populates_cached_reads() -> None:
    await _knobs.set("fade1h_kelly_multiplier", 0.20)
    await _knobs.refresh_cache()
    assert _knobs.cached("fade1h_kelly_multiplier") == pytest.approx(0.20)


def test_cached_returns_default_before_any_refresh() -> None:
    assert _knobs.cached("fade1h_max_order_usd") == pytest.approx(25.0)
