"""Regime I/O helpers (polymarket_bot/regime/sources.py) — parsing and the fake-client
fetchers; no network. Also pins the symbol map to the market selector."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from polymarket_bot import market_selection
from polymarket_bot.regime import sources as S


def _kline(i: int, close: float = 100.0, qv: float = 1000.0) -> list[Any]:
    return [1_700_000_000_000 + i * 60_000, "99.5", "101", "99", str(close), "10",
            1_700_000_059_999, str(qv), 42, "5", str(qv / 2), "0"]


# --- kline parsing ----------------------------------------------------------------


def test_parse_kline_maps_documented_columns() -> None:
    bar = S.parse_kline(_kline(0, close=100.5, qv=1234.0))
    assert bar is not None
    assert (bar.open, bar.high, bar.low, bar.close) == (99.5, 101.0, 99.0, 100.5)
    assert bar.quote_volume == 1234.0 and bar.trades == 42 and bar.taker_buy_quote == 617.0


def test_parse_kline_rejects_short_or_bad_rows() -> None:
    assert S.parse_kline(_kline(0)[:5]) is None
    row = _kline(0)
    row[4] = "not-a-number"
    assert S.parse_kline(row) is None
    assert S.parse_kline("nope") is None


def test_parse_klines_drops_malformed_and_keeps_order() -> None:
    rows = [_kline(0), "junk", _kline(1, close=101.0)]
    bars = S.parse_klines(rows)
    assert [b.close for b in bars] == [100.0, 101.0]
    assert S.parse_klines({"not": "a list"}) == []


class _FakeResp:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None) -> _FakeResp:
        self.calls.append((url, params))
        for key, payload in self.routes.items():
            if key in url:
                if callable(payload):
                    return payload(params or {})
                return payload
        return _FakeResp([], status=404)


@pytest.mark.asyncio
async def test_fetch_bars_requests_one_extra_and_drops_the_forming_bar() -> None:
    def klines(params: dict[str, Any]) -> _FakeResp:
        n = int(params["limit"])
        return _FakeResp([_kline(i, close=100.0 + i) for i in range(n)])

    client = _FakeClient({"/api/v3/klines": klines})
    bars = await S.fetch_bars(client, "BTCUSDT", "1m", 5)  # type: ignore[arg-type]
    assert client.calls[0][1]["limit"] == 6
    assert [b.close for b in bars] == [100.0, 101.0, 102.0, 103.0, 104.0]
    raw = await S.fetch_bars(client, "BTCUSDT", "1m", 5, completed_only=False)  # type: ignore[arg-type]
    assert len(raw) == 5 and raw[-1].close == 104.0


@pytest.mark.asyncio
async def test_fetch_bars_returns_empty_on_http_or_decode_error() -> None:
    client = _FakeClient({"/api/v3/klines": _FakeResp(None, status=500)})
    assert await S.fetch_bars(client, "BTCUSDT", "1m", 5) == []  # type: ignore[arg-type]
    client = _FakeClient({"/api/v3/klines": _FakeResp(ValueError("bad json"))})
    assert await S.fetch_bars(client, "BTCUSDT", "1m", 5) == []  # type: ignore[arg-type]


# --- venue ---------------------------------------------------------------------------


def test_window_slug_and_starts() -> None:
    assert S.window_slug("btc", "5m", 1_790_000_100) == "btc-updown-5m-1790000100"
    assert S.window_slug("eth", "1d", 1) is None
    current, completed = S.window_starts("5m", 1_790_000_130, 3)
    assert current == 1_790_000_100
    assert completed == [1_790_000_100 - 900, 1_790_000_100 - 600, 1_790_000_100 - 300]
    assert S.window_starts("1d", 1, 3) == (None, [])


def test_outcome_token_ids_follow_outcome_labels() -> None:
    m = {"clobTokenIds": '["111", "222"]', "outcomes": '["Down", "Up"]'}
    assert S.outcome_token_ids(m) == ("222", "111")
    assert S.outcome_token_ids({"clobTokenIds": ["1"]}) == ("", "")
    assert S.outcome_token_ids({}) == ("", "")


def test_parse_venue_market_prefers_num_fields_and_carries_tokens() -> None:
    v = S.parse_venue_market({"slug": "s", "volume": "1", "volumeNum": 2.5, "liquidity": "3",
                              "clobTokenIds": ["u", "d"], "outcomes": ["Up", "Down"]})
    assert v.volume_usd == 2.5 and v.liquidity_usd == 3.0
    assert (v.up_token, v.down_token) == ("u", "d")
    v = S.parse_venue_market({"slug": "s", "volume": "bad"})
    assert v.volume_usd is None and v.liquidity_usd is None


@pytest.mark.asyncio
async def test_fetch_venue_market_paths() -> None:
    client = _FakeClient({"/markets": _FakeResp([{"slug": "x", "volumeNum": 7.0}])})
    v = await S.fetch_venue_market(client, "x")  # type: ignore[arg-type]
    assert v is not None and v.volume_usd == 7.0
    assert await S.fetch_venue_market(_FakeClient({"/markets": _FakeResp([])}), "x") is None  # type: ignore[arg-type]
    assert await S.fetch_venue_market(_FakeClient({"/markets": _FakeResp(None, 500)}), "x") is None  # type: ignore[arg-type]


# --- book from ticks -----------------------------------------------------------------


_NOW = datetime(2026, 9, 27, 12, 0, 30, tzinfo=UTC)


def _tick(age: int, remaining: int, *, ua=0.51, da=0.51, ub=0.49, db=0.49, uas=200.0, das=300.0,
          sigma=4e-5, vol="chainlink_ws", reason="skip: no strategy loaded", slug="btc-updown-5m-1") -> dict[str, Any]:
    created = datetime(2026, 9, 27, 12, 0, 30 - age, tzinfo=UTC).isoformat(timespec="seconds")
    return {
        "created_at": created, "window_slug": slug, "reason": reason,
        "feed_source": f"spot=chainlink_ws;ref=chainlink_rest;vol={vol};quotes=clob",
        "remaining_seconds": remaining, "sigma_per_second": sigma,
        "up_best_bid": ub, "up_best_ask": ua, "up_bid_size": 100.0, "up_ask_size": uas,
        "down_best_bid": db, "down_best_ask": da, "down_bid_size": 100.0, "down_ask_size": das,
    }


def test_book_from_ticks_averages_in_phase_only() -> None:
    ticks = [_tick(0, 150, ua=0.52, da=0.52), _tick(5, 20, ua=0.9, da=0.9), _tick(10, 200, ua=0.50, da=0.50)]
    book = S.book_from_ticks(ticks, _NOW)
    assert book is not None
    assert book.ticks_used == 2
    assert book.overround == pytest.approx(((0.52 + 0.52 - 1) + (0.50 + 0.50 - 1)) / 2)
    assert book.maker_capture == pytest.approx(1 - 0.98)
    assert book.executable_depth_usd == pytest.approx((200 * 0.52 + 200 * 0.50) / 2)
    assert book.newest_age_seconds == 0 and book.sigma_per_second == 4e-5
    assert book.vol_source == "chainlink_ws" and book.source == "paper_ticks"


def test_book_from_ticks_none_when_nothing_in_phase() -> None:
    assert S.book_from_ticks([_tick(0, 30), _tick(5, 290)], _NOW) is None
    assert S.book_from_ticks([], _NOW) is None


def test_book_from_ticks_floor_sigma_and_fallback_source_are_unusable() -> None:
    assert S.book_from_ticks([_tick(0, 150, sigma=2e-5)], _NOW).sigma_per_second is None
    assert S.book_from_ticks([_tick(0, 150, sigma=4e-5, vol="floor")], _NOW).sigma_per_second is None
    b = S.book_from_ticks([_tick(0, 150, sigma=4e-5, vol="binance_shape_fallback")], _NOW)
    assert b.sigma_per_second == 4e-5 and b.vol_source == "binance_shape_fallback"


def test_book_from_ticks_flags_degraded_feed_and_filters_by_family() -> None:
    b = S.book_from_ticks([_tick(0, 150, reason="skip: settlement feed degraded (x)")], _NOW)
    assert b is not None and b.feed_degraded
    assert S.book_from_ticks([_tick(0, 150, slug="btc-updown-5m-1")], _NOW, window_prefix="eth-updown-5m-") is None
    assert S.book_from_ticks([_tick(0, 150, slug="btc-updown-5m-1")], _NOW, window_prefix="btc-updown-5m-") is not None


def test_book_from_ticks_skips_one_sided_rows_for_costs() -> None:
    b = S.book_from_ticks([_tick(0, 150, ua=None, da=0.5, ub=None, db=0.5)], _NOW)
    assert b is not None and b.overround is None and b.maker_capture is None and b.ticks_used == 1


def test_age_seconds() -> None:
    assert S.age_seconds("2026-09-27T12:00:00+00:00", _NOW) == 30
    assert S.age_seconds("2026-09-27T12:00:00", _NOW) == 30  # naive = UTC
    assert S.age_seconds("garbage", _NOW) is None
    assert S.age_seconds(None, _NOW) is None


# --- direct CLOB book ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_clob_top_takes_the_last_level_and_handles_errors() -> None:
    book = {"bids": [{"price": "0.40", "size": "10"}, {"price": "0.49", "size": "120"}],
            "asks": [{"price": "0.60", "size": "5"}, {"price": "0.51", "size": "80"}]}
    client = _FakeClient({"/book": _FakeResp(book)})
    assert await S.fetch_clob_top(client, "tok") == (0.49, 0.51, 120.0, 80.0)  # type: ignore[arg-type]
    assert await S.fetch_clob_top(client, "") == (None, None, None, None)  # type: ignore[arg-type]
    err = _FakeClient({"/book": _FakeResp(None, 500)})
    assert await S.fetch_clob_top(err, "tok") == (None, None, None, None)  # type: ignore[arg-type]
    weird = _FakeClient({"/book": _FakeResp({"bids": [{"price": "x"}], "asks": "nope"})})
    assert await S.fetch_clob_top(weird, "tok") == (None, None, None, None)  # type: ignore[arg-type]


def test_book_from_clob_costs_phase_and_source() -> None:
    up = (0.49, 0.51, 120.0, 80.0)
    down = (0.48, 0.52, 100.0, 90.0)
    b = S.book_from_clob(up, down, 150)
    assert b is not None
    assert b.overround == pytest.approx(0.03)
    assert b.maker_capture == pytest.approx(0.03)
    assert b.executable_depth_usd == pytest.approx(min(80 * 0.51, 90 * 0.52))
    assert b.source == "clob_direct" and b.ticks_used == 1 and b.sigma_per_second is None
    assert S.book_from_clob(up, down, 20) is None      # edge phase
    assert S.book_from_clob(up, down, 299) is None
    assert S.book_from_clob((None,) * 4, (None,) * 4, 150) is None


# --- symbol map -------------------------------------------------------------------------


def test_spot_symbol_covers_every_selectable_asset() -> None:
    assert set(market_selection.ASSETS) <= set(S.SPOT_SYMBOL)
    assert all(v.endswith("USDT") for v in S.SPOT_SYMBOL.values())


# --- phase as a fraction of the window ---------------------------------------------------


def test_quotable_phase_is_a_fraction_of_the_window() -> None:
    assert S.phase_bounds(300) == (pytest.approx(60.0), pytest.approx(270.0))  # the 5m family
    assert S.phase_bounds(900) == (pytest.approx(180.0), pytest.approx(810.0))
    assert S.in_quotable_phase(60, 300) and S.in_quotable_phase(270, 300)
    assert not S.in_quotable_phase(59, 300) and not S.in_quotable_phase(271, 300)
    # 120s left in a 15m window is the closing phase; it only looked quotable under a fixed 60-270s band.
    assert not S.in_quotable_phase(120, 900) and S.in_quotable_phase(120, 300)
    assert S.in_quotable_phase(1000, 3600) and not S.in_quotable_phase(400, 3600)


def test_book_from_clob_scales_the_phase_to_the_window() -> None:
    up, down = (0.49, 0.51, 120.0, 80.0), (0.48, 0.52, 100.0, 90.0)
    assert S.book_from_clob(up, down, 120, window_seconds=900) is None
    assert S.book_from_clob(up, down, 200, window_seconds=900) is not None


# --- crossed books -------------------------------------------------------------------------


def test_book_from_ticks_skips_crossed_sides_for_every_cost_measure() -> None:
    """Regression: a crossed side (bid above ask) is not a quotable market; the loop skips it, and
    averaging it in pushed overround negative and maker capture down."""
    good = _tick(0, 150, ua=0.51, da=0.51, ub=0.49, db=0.49)
    crossed = _tick(5, 150, ua=0.40, da=0.51, ub=0.55, db=0.49)   # up bid 0.55 > up ask 0.40
    book = S.book_from_ticks([good, crossed], _NOW)
    assert book is not None and book.ticks_used == 2
    assert book.overround == pytest.approx(0.02) and book.maker_capture == pytest.approx(0.02)
    assert S.book_from_ticks([crossed], _NOW).overround is None
    assert S.book_from_ticks([crossed], _NOW).maker_capture is None


def test_book_from_clob_rejects_a_crossed_book() -> None:
    assert S.book_from_clob((0.55, 0.40, 10.0, 10.0), (0.48, 0.52, 10.0, 10.0), 150) is None
