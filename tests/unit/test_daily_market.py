"""Daily altcoin market discovery/resolution tests.

Pins two things a live run caught: (1) the resolution instant is 24h
BEFORE ``endDate``, not ``startDate`` (the trading window opens up to ~2
days before the actual comparison period) — see ``build_market_view``; and
(2) Up/Down's complementary-book pricing.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from polymarket_bot.daily.market import (
    _todays_slug,
    build_market_view,
    discover_daily_markets,
    market_prices,
)


def _gamma_market(
    *,
    slug: str,
    end_date: str,
    start_date: str = "2026-08-28T16:07:15Z",
    condition_id: str = "0xabc",
    up_token: str = "1",
    down_token: str = "2",
    best_bid: float = 0.40,
    best_ask: float = 0.53,
    liquidity: float | str = 5000.0,
    order_min_size: float | str = 5.0,
    outcome_prices: tuple[str, str] = ("0.47", "0.53"),
) -> dict:
    return {
        "slug": slug,
        "conditionId": condition_id,
        "outcomes": json.dumps(["Up", "Down"]),
        "clobTokenIds": json.dumps([up_token, down_token]),
        "startDate": start_date,
        "endDate": end_date,
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "liquidity": liquidity,
        "orderMinSize": order_min_size,
        "outcomePrices": json.dumps(list(outcome_prices)),
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeGammaClient:
    """Dispatches ``/markets?slug=X`` to a canned market row per slug."""

    def __init__(self, rows_by_slug: dict[str, dict]):
        self._rows_by_slug = rows_by_slug
        self.requested_slugs: list[str] = []

    async def get(self, url, params=None, timeout=None):
        slug = (params or {}).get("slug", "")
        self.requested_slugs.append(slug)
        row = self._rows_by_slug.get(slug)
        return _FakeResponse([row] if row else [])


class _FakeBinanceClient:
    """Dispatches Binance klines/ticker calls by interval/startTime."""

    def __init__(self, *, spot: float, closes: list[float], reference_close: float):
        self._spot = spot
        self._closes = closes
        self._reference_close = reference_close
        self.kline_calls: list[dict] = []

    async def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/ticker/price"):
            return _FakeResponse({"price": str(self._spot)})
        if url.endswith("/klines"):
            self.kline_calls.append(params)
            if params.get("interval") == "1d":
                # oldest-first daily closes; each row's close is index 4
                return _FakeResponse([[0, 0, 0, 0, c] for c in self._closes])
            if params.get("interval") == "1m":
                return _FakeResponse([[0, 0, 0, 0, self._reference_close]])
        raise AssertionError(f"unexpected request: {url} {params}")


def test_todays_slug_format():
    now = datetime(2026, 8, 30, tzinfo=UTC)
    assert _todays_slug("solana", now) == "solana-up-or-down-on-august-30-2026"


def test_todays_slug_no_zero_padding_on_day():
    now = datetime(2026, 9, 5, tzinfo=UTC)
    assert _todays_slug("bnb", now) == "bnb-up-or-down-on-september-5-2026"


def test_market_prices_derive_down_from_up_complementary_book():
    up_bid, up_ask, down_bid, down_ask = market_prices(
        {"bestBid": 0.40, "bestAsk": 0.53}
    )
    assert up_bid == pytest.approx(0.40)
    assert up_ask == pytest.approx(0.53)
    assert down_bid == pytest.approx(1.0 - 0.53)
    assert down_ask == pytest.approx(1.0 - 0.40)


def test_market_prices_none_when_book_empty():
    assert market_prices({}) == (None, None, None, None)


@pytest.mark.asyncio
async def test_discover_daily_markets_only_returns_tracked_assets():
    # Discovery looks up TODAY's slug, so the fixture must too — a hardcoded
    # date only passed on that one day.
    sol_slug = _todays_slug("solana", datetime.now(UTC))
    client = _FakeGammaClient(
        {sol_slug: _gamma_market(slug=sol_slug, end_date="2026-08-30T16:00:00Z")}
    )
    found = await discover_daily_markets(client, tracked_assets=["sol", "doge"])
    assert list(found.keys()) == ["sol"]
    assert found["sol"]["slug"] == sol_slug


@pytest.mark.asyncio
async def test_build_market_view_uses_end_minus_24h_not_start_date():
    """Regression pin: a live check found this family's actual comparison
    window is [endDate-24h, endDate], NOT [startDate, endDate] — startDate
    is merely when trading opened, up to ~2 days earlier."""
    slug = "solana-up-or-down-on-august-30-2026"
    market = _gamma_market(
        slug=slug,
        start_date="2026-08-28T16:07:15Z",  # ~2 days before endDate
        end_date="2026-08-30T16:00:00Z",
    )
    binance = _FakeBinanceClient(spot=105.0, closes=[100.0] * 30, reference_close=100.5)
    view = await build_market_view(binance, "sol", market)
    assert view is not None
    assert view.reference == pytest.approx(100.5)

    reference_call = next(c for c in binance.kline_calls if c.get("interval") == "1m")
    end_ts = int(datetime.fromisoformat("2026-08-30T16:00:00+00:00").timestamp())
    start_ts = int(datetime.fromisoformat("2026-08-28T16:07:15+00:00").timestamp())
    expected_ref_ts = end_ts - 86400
    assert reference_call["startTime"] == expected_ref_ts * 1000
    assert reference_call["startTime"] != start_ts * 1000


@pytest.mark.asyncio
async def test_build_market_view_reference_is_noon_et_on_fall_back_day():
    """DST fall-back: 2026-11-01's market compares noon EDT on Oct 31
    (16:00Z) with noon EST on Nov 1 (17:00Z) — 25h apart, so a flat
    ``endDate - 86400`` reads 13:00 EDT, a full hour late."""
    market = _gamma_market(
        slug="ethereum-up-or-down-on-november-1-2026",
        start_date="2026-10-30T16:00:00Z",
        end_date="2026-11-01T17:00:00Z",
    )
    binance = _FakeBinanceClient(
        spot=3100.0, closes=[3000.0] * 30, reference_close=3050.0
    )
    view = await build_market_view(binance, "eth", market)
    assert view is not None

    reference_call = next(c for c in binance.kline_calls if c.get("interval") == "1m")
    expected_ref_ts = int(
        datetime.fromisoformat("2026-10-31T16:00:00+00:00").timestamp()
    )
    assert reference_call["startTime"] == expected_ref_ts * 1000


@pytest.mark.asyncio
async def test_build_market_view_reference_is_noon_et_on_spring_forward_day():
    """DST spring-forward: 2027-03-14's market compares noon EST on Mar 13
    (17:00Z) with noon EDT on Mar 14 (16:00Z) — 23h apart, so a flat
    ``endDate - 86400`` reads 11:00 EST, a full hour early."""
    market = _gamma_market(
        slug="ethereum-up-or-down-on-march-14-2027",
        start_date="2027-03-12T17:00:00Z",
        end_date="2027-03-14T16:00:00Z",
    )
    binance = _FakeBinanceClient(
        spot=3100.0, closes=[3000.0] * 30, reference_close=3050.0
    )
    view = await build_market_view(binance, "eth", market)
    assert view is not None

    reference_call = next(c for c in binance.kline_calls if c.get("interval") == "1m")
    expected_ref_ts = int(
        datetime.fromisoformat("2027-03-13T17:00:00+00:00").timestamp()
    )
    assert reference_call["startTime"] == expected_ref_ts * 1000


@pytest.mark.asyncio
async def test_build_market_view_populates_asset_and_tokens():
    slug = "dogecoin-up-or-down-on-august-30-2026"
    market = _gamma_market(
        slug=slug, end_date="2026-08-30T16:00:00Z",
        up_token="up_tok", down_token="down_tok", condition_id="0xdef",
    )
    binance = _FakeBinanceClient(spot=0.20, closes=[0.19] * 30, reference_close=0.195)
    view = await build_market_view(binance, "doge", market)
    assert view is not None
    assert view.asset == "doge"
    assert view.window_slug == slug
    assert view.condition_id == "0xdef"
    assert view.up_token == "up_tok"
    assert view.down_token == "down_tok"
    assert view.binance_symbol == "DOGEUSDT"
    assert view.resolves_at == "2026-08-30T16:00:00Z"
