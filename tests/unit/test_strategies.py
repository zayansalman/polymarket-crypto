"""Operator strategy switches: a strategy opens new positions only while it is ON.

The switches are the MY STRATEGIES card's model. A strategy reads its own
switch at the top of its pass, so turning it off stops NEW entries without
touching anything already open — settlement always keeps running.
"""

from __future__ import annotations


from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import strategies as _strategies

FADE = "fade_1h_momentum_15m"


@pytest_asyncio.fixture(autouse=True)
async def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "test_strategies.db")
    await _db.init_db()
    yield


@pytest.mark.asyncio
async def test_every_strategy_is_on_before_the_operator_touches_anything() -> None:
    for name in _strategies.STRATEGIES:
        assert await _strategies.enabled(name) is True, name


@pytest.mark.asyncio
async def test_turning_a_strategy_off_persists() -> None:
    await _strategies.set_enabled(FADE, False)
    assert await _strategies.enabled(FADE) is False


@pytest.mark.asyncio
async def test_a_strategy_can_be_turned_back_on() -> None:
    await _strategies.set_enabled(FADE, False)
    await _strategies.set_enabled(FADE, True)
    assert await _strategies.enabled(FADE) is True


@pytest.mark.asyncio
async def test_an_unknown_strategy_is_rejected() -> None:
    with pytest.raises(ValueError):
        await _strategies.set_enabled("no_such_strategy", False)


@pytest.mark.asyncio
async def test_enabled_map_reports_one_entry_per_strategy() -> None:
    await _strategies.set_enabled(FADE, False)
    # Built from the registry rather than a fixed dict, so registering a new
    # strategy does not fail a test that is not about it.
    expected = {
        name: (False if name == FADE else strategy.default)
        for name, strategy in _strategies.STRATEGIES.items()
    }
    assert await _strategies.enabled_map() == expected


def test_only_fade_is_registered() -> None:
    # Every other family (the BTC Up/Down loop, the maker, the daily altcoin
    # scanner) was deleted on 2026-09-26. No switch or knob outlives its code.
    from polymarket_bot import runtime_knobs

    assert list(_strategies.STRATEGIES) == [FADE]
    assert all(n.startswith("fade1h_") for n in runtime_knobs.KNOBS)


def test_every_strategy_stores_under_its_own_runtime_key() -> None:
    keys = {s.key for s in _strategies.STRATEGIES.values()}
    assert len(keys) == len(_strategies.STRATEGIES)
    assert all(k.startswith("runtime.strategy.") for k in keys)
