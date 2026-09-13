"""Unit tests for the data-integrity layer (issues #21/#22).

Covers the tie-rule pricing model, executable-edge signal selection, the
Chainlink settlement connector (REST reference stabilization, fast
settlement, REST spot poll), the WS feed frame handling, and the
degraded-feed exit suppression. No external network calls.
"""

from __future__ import annotations

import json

import httpx
import pytest

from polymarket_exec.connectors.chainlink_settlement import (
    ChainlinkSettlementConnector,
    ChainlinkWsFeed,
    build_subscribe_message,
)
from polymarket_bot.strategy import (
    StrategyParams,
    fair_up_probability,
    signal_from_executable_edges,
)

PARAMS = StrategyParams(
    min_trade_usd=1.0,
    max_trade_usd=5.0,
    entry_edge_min=0.045,
    entry_min_remaining_seconds=60,
    min_confidence=0.50,
)


# ---------------------------------------------------------------------------
# Tie rule (markets resolve Up when close >= open)
# ---------------------------------------------------------------------------


def test_fair_up_exceeds_half_when_pinned_at_reference():
    p = fair_up_probability(61000.0, 61000.0, sigma=0.00002, remaining_seconds=10)
    assert p > 0.5


def test_tie_mass_shrinks_with_more_volatility():
    quiet = fair_up_probability(61000.0, 61000.0, 0.00002, 10)
    loud = fair_up_probability(61000.0, 61000.0, 0.0008, 10)
    assert quiet > loud > 0.5


def test_fair_up_stays_within_clamp():
    assert fair_up_probability(61000.0, 61000.0, 1e-9, 1) <= 0.995
    assert fair_up_probability(70000.0, 61000.0, 0.00002, 10) <= 0.995
    assert fair_up_probability(50000.0, 61000.0, 0.00002, 10) >= 0.005


# ---------------------------------------------------------------------------
# Executable-edge signal (issue #22)
# ---------------------------------------------------------------------------


def test_signal_picks_side_with_larger_executable_edge():
    side, _, notional, reason = signal_from_executable_edges(
        edge_up=0.08, edge_down=-0.10, remaining_seconds=120,
        up_ask=0.50, down_ask=0.52, params=PARAMS,
    )
    assert side == "Up" and notional > 0 and "executable edge" in reason


def test_signal_skips_when_both_edges_negative():
    side, _, _, reason = signal_from_executable_edges(
        edge_up=-0.02, edge_down=-0.03, remaining_seconds=120,
        up_ask=0.51, down_ask=0.52, params=PARAMS,
    )
    assert side is None and "below threshold" in reason


def test_signal_skips_without_any_executable_quote():
    side, _, _, reason = signal_from_executable_edges(
        edge_up=None, edge_down=None, remaining_seconds=120,
        up_ask=None, down_ask=None, params=PARAMS,
    )
    assert side is None and "no executable quote" in reason


def test_signal_ignores_unquotable_side():
    # Down has a huge "edge" but no ask — only Up is a candidate.
    side, _, _, _ = signal_from_executable_edges(
        edge_up=0.05, edge_down=None, remaining_seconds=120,
        up_ask=0.50, down_ask=None, params=PARAMS,
    )
    assert side == "Up"


# ---------------------------------------------------------------------------
# WS subscribe frame (byte-exactness gotcha)
# ---------------------------------------------------------------------------


def test_subscribe_message_filters_are_compact_json():
    msg = build_subscribe_message("btc/usd")
    parsed = json.loads(msg)
    filters = parsed["subscriptions"][0]["filters"]
    assert filters == '{"symbol":"btc/usd"}'  # byte-exact, no spaces
    assert parsed["subscriptions"][0]["topic"] == "crypto_prices_chainlink"


# ---------------------------------------------------------------------------
# WS feed frame handling
# ---------------------------------------------------------------------------


def _update_frame(ts_ms: int, value: float) -> str:
    return json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "type": "update",
            "payload": {
                "symbol": "btc/usd",
                "timestamp": ts_ms,
                "value": value,
                "full_accuracy_value": str(int(value * 1e18)),
            },
        }
    )


def test_ws_feed_absorbs_updates_and_dedups():
    feed = ChainlinkWsFeed()
    assert feed.handle_message(_update_frame(1_781_000_000_000, 61000.5)) == 1
    assert feed.handle_message(_update_frame(1_781_000_001_000, 61001.0)) == 1
    # duplicate / older print is ignored
    assert feed.handle_message(_update_frame(1_781_000_001_000, 61001.0)) == 0
    assert feed.latest() == (1_781_000_001.0, 61001.0)
    assert feed.recent_closes() == [61000.5, 61001.0]
    assert feed.is_fresh()


def test_ws_feed_ignores_pong_and_other_topics():
    feed = ChainlinkWsFeed()
    assert feed.handle_message("PONG") == 0
    other = json.dumps({"topic": "crypto_prices", "type": "update",
                        "payload": {"symbol": "btcusdt", "timestamp": 1, "value": 1.0}})
    assert feed.handle_message(other) == 0
    assert not feed.is_fresh()


# ---------------------------------------------------------------------------
# REST settlement connector
# ---------------------------------------------------------------------------


def _connector_with_responses(responses: list[dict], now: float = 2_000_000_000.0):
    """Connector whose transport replays canned crypto-price responses."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = responses[min(len(calls) - 1, len(responses) - 1)]
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://polymarket.com"
    )
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    connector = ChainlinkSettlementConnector(
        client,
        api_base="https://polymarket.com/api/crypto/crypto-price",
        time_fn=lambda: now,
        sleep_fn=fake_sleep,
    )
    return connector, calls, sleeps


@pytest.mark.asyncio
async def test_reference_price_waits_for_stable_reads():
    # Provisional first print, then two identical committed reads.
    connector, calls, _ = _connector_with_responses(
        [
            {"openPrice": 61000.18, "closePrice": None, "completed": False},
            {"openPrice": 61000.53, "closePrice": None, "completed": False},
            {"openPrice": 61000.53, "closePrice": None, "completed": False},
        ]
    )
    ref = await connector.get_reference_price(1_999_999_900)
    assert ref == 61000.53
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_reference_request_uses_fiveminute_variant_and_cache_buster():
    connector, calls, _ = _connector_with_responses(
        [{"openPrice": 61000.0, "closePrice": None, "completed": False}] * 2
    )
    await connector.get_reference_price(1_999_999_900)
    q = dict(httpx.QueryParams(calls[0].url.query.decode()))
    assert q["variant"] == "fiveminute"
    assert q["eventStartTime"] == "1999999900"
    assert "_" in q  # cache buster


@pytest.mark.asyncio
async def test_settle_window_fast_path_reads_next_open():
    # Window over, closePrice not yet committed -> close(N) == open(N+1).
    connector, calls, _ = _connector_with_responses(
        [
            {"openPrice": 61010.0, "closePrice": None, "completed": False},
            {"openPrice": 61005.0, "closePrice": None, "completed": False},
        ],
        now=2_000_000_000.0,
    )
    up_won = await connector.settle_window(1_999_999_000)  # ended long ago
    assert up_won is False  # 61005 < 61010 -> Down
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_settle_window_tie_resolves_up():
    connector, _, _ = _connector_with_responses(
        [{"openPrice": 61010.0, "closePrice": 61010.0, "completed": True}]
    )
    assert await connector.settle_window(1_999_999_000) is True


@pytest.mark.asyncio
async def test_get_recent_print_uses_open_of_offset_window():
    connector, calls, _ = _connector_with_responses(
        [{"openPrice": 62639.34, "closePrice": None, "completed": False}],
        now=2_000_000_000.0,
    )
    result = await connector.get_recent_print(lag_seconds=3)
    assert result == (1_999_999_997.0, 62639.34)
    q = dict(httpx.QueryParams(calls[0].url.query.decode()))
    assert q["eventStartTime"] == "1999999997"


# ---------------------------------------------------------------------------
# Degraded feed must not fake a BAND_REENTRY exit
# ---------------------------------------------------------------------------


def test_band_reentry_suppressed_when_feed_degraded():
    from polymarket_bot.paper import PaperSnapshot, _exit_reason

    snapshot = PaperSnapshot(
        created_at="2026-06-11T00:00:00+00:00",
        window_slug="btc-updown-5m-1781155200",
        market_question="q",
        remaining_seconds=200,
        spot_price=0.0,
        reference_price=0.0,
        sigma_per_second=0.0001,
        market_up_price=0.50,
        market_down_price=0.52,
        fair_up_prob=0.5,
        edge=0.0,
        signal_side=None,
        confidence=0.0,
        notional_usd=0.0,
        reason="skip: settlement feed degraded",
        feed_source="t",
        up_best_bid=0.49,
        up_best_ask=0.50,
        feed_degraded=True,
    )
    pos = {
        "position_id": 1,
        "side": "Up",
        "entry_price": 0.50,
        "shares": 4.0,
        "notional_usd": 2.0,
        "window_slug": "btc-updown-5m-1781155200",
    }
    # edge ~0 would trigger BAND_REENTRY were the feed healthy; degraded must hold.
    assert _exit_reason(snapshot, pos, exit_price=0.49) is None
