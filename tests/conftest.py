"""Shared pytest fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_marketdata_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep the market-data hub offline.

    The hub's streams and lookups are unit-tested with fakes in test_marketdata_*.py.
    """
    from ems.marketdata.hub import MarketDataHub

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(MarketDataHub, "run", _idle_run)


@pytest.fixture(autouse=True)
def _no_fade_1h_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep Fade 1h Momentum on 15m idle.

    Its loop reads the venue and writes its ledger every pass. The app imports
    ``run_forever`` inside the lifespan, so patching the module attribute is
    enough. Tests of the runner keep the real one (``real_run_forever``).
    Every test also starts from a fresh card state, so a runner test that
    leaves an error in the module-level status never shows up on a later
    test's page.
    """
    from ems.fade_1h_momentum_15m import runner as _fade_runner

    async def _idle(stop_event=None) -> None:  # type: ignore[no-untyped-def]
        if stop_event is not None:
            await stop_event.wait()

    monkeypatch.setattr(_fade_runner, "real_run_forever", _fade_runner.run_forever,
                        raising=False)
    monkeypatch.setattr(_fade_runner, "run_forever", _idle)
    monkeypatch.setattr(_fade_runner, "_STATUS",
                        {"state": "not_started", "last_pass_ts": None, "last_error": None})

