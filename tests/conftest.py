"""Shared pytest fixtures for the polymarket_exec test suite."""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Real-money isolation (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_live_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may inherit the operator's .env private key or LIVE consent.

    config.py loads .env at import, and several dashboard tests hit /api/start
    on the real app — without this a local run could build a real
    LiveExecutor. Tests that need an armed gate set a dummy key explicitly.
    """
    import config as _config

    monkeypatch.setattr(_config, "POLYMARKET_PRIVATE_KEY", "")
    # No wallet either: keeps dashboard renders off the network (wallet stat).
    monkeypatch.setattr(_config, "POLYMARKET_FUNDER", "")
    try:
        from polymarket_bot import controller as _controller
    except Exception:  # noqa: BLE001 — bot package optional in some envs
        return
    monkeypatch.setattr(_controller, "_live_consent", False)


@pytest.fixture(autouse=True)
def _no_quote_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard app tests must not poll the venue for the order-size ticket.

    The app lifespan starts ``quote_feed.run_forever``; swap it for a no-op that
    just waits to be stopped. Tests of the poller itself call the real one.
    """
    try:
        from polymarket_exec.ops.dashboard import quote_feed as _quote_feed
    except Exception:  # noqa: BLE001 — dashboard package optional in some envs
        return

    async def _idle(stop_event) -> None:  # type: ignore[no-untyped-def]
        await stop_event.wait()

    monkeypatch.setattr(_quote_feed, "run_forever", _idle)


@pytest.fixture(autouse=True)
def _no_feed_monitor_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep its feed monitor offline.

    The monitor's WS connection and REST checks are unit-tested with fakes in
    test_feed_monitor.py.
    """
    from polymarket_exec.ops.feed_monitor import FeedMonitor

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(FeedMonitor, "run", _idle_run)


@pytest.fixture(autouse=True)
def _no_flow_recorder_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep the venue flow recorder offline.

    The recorder's REST and WS paths are unit-tested with fakes in test_flow_recorder.py.
    """
    from polymarket_exec.ops.flow_recorder import FlowRecorder

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(FlowRecorder, "run", _idle_run)


@pytest.fixture(autouse=True)
def _no_macro_recorder_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep the macro calendar recorder offline.

    The recorder's sources are unit-tested with fakes in test_macro_recorder.py.
    """
    from polymarket_exec.ops.macro_recorder import MacroRecorder

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(MacroRecorder, "run", _idle_run)


@pytest.fixture(autouse=True)
def _no_marketdata_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep the market-data hub offline.

    The hub's streams and lookups are unit-tested with fakes in test_marketdata_*.py.
    """
    from polymarket_exec.marketdata.hub import MarketDataHub

    async def _idle_run(self, stop_event):  # noqa: ANN001
        await stop_event.wait()

    monkeypatch.setattr(MarketDataHub, "run", _idle_run)


@pytest.fixture(autouse=True)
def _no_fade_1h_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dashboard tests boot the app lifespan; keep Fade 1h Momentum on 15m idle.

    Its loop reads the venue and writes its ledger every pass. The app imports
    ``run_forever`` inside the lifespan, so patching the module attribute is
    enough. Tests of the runner keep the real one (``real_run_forever``).
    """
    from polymarket_bot.fade_1h_momentum_15m import runner as _fade_runner

    async def _idle(stop_event=None) -> None:  # type: ignore[no-untyped-def]
        if stop_event is not None:
            await stop_event.wait()

    monkeypatch.setattr(_fade_runner, "real_run_forever", _fade_runner.run_forever,
                        raising=False)
    monkeypatch.setattr(_fade_runner, "run_forever", _idle)

