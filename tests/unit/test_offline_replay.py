"""Unit tests for the offline replay harness (issue #56).

Deterministic synthetic data, no network — exercises replay_market end-to-end
and the metric aggregator. The HF dataset loaders are NOT exercised here;
they're integration-tested by running the script in CI separately.
"""
from __future__ import annotations

import polars as pl

from polymarket_bot.strategy import StrategyParams
from tools.offline_replay import (
    ReplayEntry,
    aggregate_metrics,
    replay_market,
)


def _params(
    *,
    entry_edge_min: float = 0.01,
    entry_edge_max: float = 1.0,
    min_confidence: float = 0.50,
    min_entry_price: float = 0.05,
) -> StrategyParams:
    """Loose params that let synthetic ticks trigger an entry."""
    return StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=entry_edge_min,
        min_confidence=min_confidence,
        entry_min_remaining_seconds=30,
        max_entry_price=0.95,
        min_entry_price=min_entry_price,
        entry_edge_max=entry_edge_max,
    )


def _synthetic_chainlink(open_ts: int, close_ts: int, prices: list[float]) -> pl.DataFrame:
    """Evenly-spaced Chainlink prints across the window."""
    step = max(1, (close_ts - open_ts) // max(len(prices) - 1, 1))
    rows = [
        {"ts_ms": (open_ts + i * step) * 1_000, "price": p, "ts_s": open_ts + i * step}
        for i, p in enumerate(prices)
    ]
    return pl.DataFrame(rows)


def _synthetic_market_prices(
    market_id: str, ts_list: list[int], up_px: list[float], down_px: list[float]
) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {"market_id": market_id, "timestamp": t, "up_price": u, "down_price": d}
            for t, u, d in zip(ts_list, up_px, down_px, strict=True)
        ]
    )


def test_replay_market_enters_up_when_market_underprices_up() -> None:
    """Spot rises through the window → fair_up climbs → market still cheap on UP."""
    open_ts = 1_700_000_000
    close_ts = open_ts + 300  # 5-min window
    market = {
        "market_id": "M1",
        "window_open_ts": open_ts,
        "window_close_ts": close_ts,
        "resolution": 1,  # Up wins (matches the rising spot)
    }
    # Spot drifts upward — fair_up should climb above 0.5.
    chainlink = _synthetic_chainlink(
        open_ts - 1,
        close_ts,
        [60_000.0, 60_010.0, 60_020.0, 60_030.0, 60_040.0, 60_050.0],
    )
    # Market keeps UP cheap at 0.52 even as spot rises — that's the
    # mispricing the bot should exploit.
    ts = [open_ts + 60, open_ts + 120, open_ts + 180]
    mp = _synthetic_market_prices("M1", ts, [0.52, 0.52, 0.52], [0.48, 0.48, 0.48])

    entry = replay_market(market, chainlink, mp, _params())
    assert entry is not None
    assert entry.side == "Up"
    assert entry.won is True
    assert entry.outcome == 1
    assert entry.edge > 0
    assert entry.predicted_up > 0.5


def test_replay_market_returns_none_when_no_chainlink_prints() -> None:
    """No Chainlink prints in window → can't estimate σ → skip."""
    open_ts = 1_700_000_000
    close_ts = open_ts + 300
    market = {
        "market_id": "M2",
        "window_open_ts": open_ts,
        "window_close_ts": close_ts,
        "resolution": 1,
    }
    chainlink = _synthetic_chainlink(open_ts - 100, open_ts - 50, [60_000.0])
    mp = _synthetic_market_prices("M2", [open_ts + 60], [0.52], [0.48])
    assert replay_market(market, chainlink, mp, _params()) is None


def test_replay_market_returns_none_when_gates_fail() -> None:
    """Flat spot → fair_up ≈ 0.5 → no edge → no entry."""
    open_ts = 1_700_000_000
    close_ts = open_ts + 300
    market = {
        "market_id": "M3",
        "window_open_ts": open_ts,
        "window_close_ts": close_ts,
        "resolution": 0,
    }
    chainlink = _synthetic_chainlink(
        open_ts - 1, close_ts, [60_000.0] * 6
    )
    # Tight, symmetric market — no mispricing.
    ts = [open_ts + 60, open_ts + 120, open_ts + 180]
    mp = _synthetic_market_prices("M3", ts, [0.50, 0.50, 0.50], [0.50, 0.50, 0.50])
    assert replay_market(market, chainlink, mp, _params(entry_edge_min=0.05)) is None


def test_aggregate_metrics_empty() -> None:
    assert aggregate_metrics([]) == {"n": 0, "brier": None, "roi": None, "win_rate": None}


def test_aggregate_metrics_handcrafted() -> None:
    """Three entries, two wins → win_rate 2/3, ROI matches binary payoff math."""
    entries = [
        ReplayEntry(
            market_id="A",
            window_close_ts=1,
            side="Up",
            entry_price=0.50,
            predicted_up=0.60,
            confidence=0.70,
            edge=0.10,
            notional=5.0,
            outcome=1,
            won=True,
        ),
        ReplayEntry(
            market_id="B",
            window_close_ts=2,
            side="Down",
            entry_price=0.40,
            predicted_up=0.30,
            confidence=0.70,
            edge=0.20,
            notional=5.0,
            outcome=0,
            won=True,
        ),
        ReplayEntry(
            market_id="C",
            window_close_ts=3,
            side="Up",
            entry_price=0.55,
            predicted_up=0.65,
            confidence=0.70,
            edge=0.10,
            notional=5.0,
            outcome=0,
            won=False,
        ),
    ]
    m = aggregate_metrics(entries)
    assert m["n"] == 3
    assert m["wins"] == 2
    assert m["losses"] == 1
    assert m["win_rate"] == 2 / 3
    # Win A: $5 * (1/0.50 - 1) = $5; Win B: $5 * (1/0.40 - 1) = $7.50; Loss C: -$5
    # Total = +7.50
    assert m["pnl_usd"] == 7.5
    assert m["notional_usd"] == 15.0
    # Brier per entry: (0.6-1)^2 + (0.3-0)^2 + (0.65-0)^2 = 0.16 + 0.09 + 0.4225 = 0.6725
    # Brier mean = 0.6725/3 ≈ 0.2242
    assert abs(m["brier"] - 0.2242) < 0.001
    assert m["by_side"] == {"Up": 2, "Down": 1}
