"""Daily altcoin market discovery/resolution tests.

Pins three things a live run caught: (1) the resolution instant is 24h
BEFORE ``endDate``, not ``startDate`` (the trading window opens up to ~2
days before the actual comparison period) — see ``build_market_view``;
(2) Up/Down's complementary-book pricing; and (3) discovery asks for the
market resolving at the NEXT noon ET, not the one named by the UTC date —
from noon ET to UTC midnight the UTC date names the market that has just
resolved, and Gamma no longer lists it.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from polymarket_bot.daily.market import (
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


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# (scan instant, date in the slug of the market that must be requested).
# That market resolves at noon New York time on its date: 16:00Z under EDT,
# 17:00Z under EST (checked against real markets on both DST switch days).
_NEXT_NOON_ET_CASES = [
    # EDT (UTC-4)
    (_utc(2026, 9, 16, 10, 0), "september-16-2026"),  # 06:00 EDT
    (_utc(2026, 9, 16, 15, 59, 59), "september-16-2026"),  # 11:59:59 EDT
    (_utc(2026, 9, 16, 16, 0), "september-17-2026"),  # 12:00 EDT
    (_utc(2026, 9, 16, 18, 38), "september-17-2026"),  # the live miss
    (_utc(2026, 9, 17, 2, 0), "september-17-2026"),  # 22:00 EDT on the 16th
    (_utc(2026, 9, 5, 13, 0), "september-5-2026"),  # day is not zero-padded
    # EST (UTC-5)
    (_utc(2026, 12, 10, 16, 30), "december-10-2026"),  # 11:30 EST
    (_utc(2026, 12, 10, 17, 0), "december-11-2026"),  # 12:00 EST
    (_utc(2026, 12, 31, 17, 0), "january-1-2027"),  # 12:00 EST, year rolls
    # DST ends Sun 2026-11-01 02:00: noon is 16:00Z on the 31st, 17:00Z on the 1st
    (_utc(2026, 10, 31, 16, 0), "november-1-2026"),  # 12:00 EDT
    (_utc(2026, 11, 1, 16, 30), "november-1-2026"),  # 11:30 EST
    # DST starts Sun 2027-03-14 02:00: noon is 17:00Z on the 13th, 16:00Z on the 14th
    (_utc(2027, 3, 13, 16, 30), "march-13-2027"),  # 11:30 EST
    (_utc(2027, 3, 14, 16, 0), "march-15-2027"),  # 12:00 EDT
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("now", "want_date"), _NEXT_NOON_ET_CASES)
async def test_discover_requests_the_market_resolving_at_the_next_noon_et(now, want_date):
    client = _FakeGammaClient({})
    await discover_daily_markets(client, tracked_assets=["sol"], now=now)
    assert client.requested_slugs[0] == f"solana-up-or-down-on-{want_date}"


@pytest.mark.asyncio
async def test_discover_finds_every_tracked_asset_after_noon_et_without_a_sweep():
    """Live miss, 2026-09-16 18:38Z (14:38 EDT): discovery asked for the
    September 16 markets, which had resolved at noon ET and were no longer
    listed, then fell back to a 335-request Gamma sweep that found none."""
    want = {
        "doge": "dogecoin-up-or-down-on-september-17-2026",
        "sol": "solana-up-or-down-on-september-17-2026",
        "xrp": "xrp-up-or-down-on-september-17-2026",
        "bnb": "bnb-up-or-down-on-september-17-2026",
        "eth": "ethereum-up-or-down-on-september-17-2026",
    }
    client = _FakeGammaClient(
        {slug: _gamma_market(slug=slug, end_date="2026-09-17T16:00:00Z") for slug in want.values()}
    )
    found = await discover_daily_markets(
        client, tracked_assets=list(want), now=_utc(2026, 9, 16, 18, 38)
    )
    assert {asset: m["slug"] for asset, m in found.items()} == want
    assert client.requested_slugs == list(want.values())


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
    sol_slug = "solana-up-or-down-on-august-30-2026"
    client = _FakeGammaClient(
        {sol_slug: _gamma_market(slug=sol_slug, end_date="2026-08-30T16:00:00Z")}
    )
    found = await discover_daily_markets(
        client, tracked_assets=["sol", "doge"], now=_utc(2026, 8, 30, 10, 0)
    )
    assert list(found.keys()) == ["sol"]
    assert found["sol"]["slug"] == sol_slug


@pytest.mark.asyncio
async def test_discover_skips_an_asset_without_a_daily_slug_name():
    """A config typo or unsupported asset must not abort the other assets'
    lookup (the shared slug builder raises on names it doesn't know)."""
    sol_slug = "solana-up-or-down-on-august-30-2026"
    client = _FakeGammaClient(
        {sol_slug: _gamma_market(slug=sol_slug, end_date="2026-08-30T16:00:00Z")}
    )
    found = await discover_daily_markets(
        client, tracked_assets=["shib", "sol"], now=_utc(2026, 8, 30, 10, 0)
    )
    assert list(found.keys()) == ["sol"]
    assert client.requested_slugs == [sol_slug]


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
