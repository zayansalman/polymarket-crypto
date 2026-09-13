"""ORDER SIZE ticket: live quote for the selected market + compact render (#89)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from polymarket_exec.connectors import updown_quote as uq
from polymarket_exec.connectors.updown_quote import UpDownQuote, UpDownQuoteClient
from polymarket_exec.ops.dashboard import quote_feed
from polymarket_exec.ops.dashboard.panels import controls

# Captured at import: the autouse ``_no_quote_polling`` fixture stubs it per test.
_REAL_RUN_FOREVER = quote_feed.run_forever

# 2026-09-13 18:47:30 UTC == 14:47:30 EDT
_NOW = datetime(2026, 9, 13, 18, 47, 30, tzinfo=UTC)


class TestWindowSlug:
    def test_5m_and_15m_floor_to_the_window_start(self) -> None:
        ts = int(_NOW.timestamp())
        assert uq.window_slug("eth", "5m", _NOW) == f"eth-updown-5m-{ts - ts % 300}"
        assert uq.window_slug("btc", "15m", _NOW) == f"btc-updown-15m-{ts - ts % 900}"

    def test_1h_uses_the_et_start_hour(self) -> None:
        assert (
            uq.window_slug("eth", "1h", _NOW)
            == "ethereum-up-or-down-september-13-2026-2pm-et"
        )

    def test_1h_midnight_and_noon_render_as_12(self) -> None:
        midnight_et = datetime(2026, 9, 14, 4, 10, tzinfo=UTC)
        noon_et = datetime(2026, 9, 14, 16, 10, tzinfo=UTC)
        assert uq.window_slug("btc", "1h", midnight_et).endswith("-14-2026-12am-et")
        assert uq.window_slug("btc", "1h", noon_et).endswith("-14-2026-12pm-et")

    def test_1d_is_dated_the_noon_et_it_resolves_on(self) -> None:
        morning_et = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)  # 09:00 ET
        assert uq.window_slug("sol", "1d", morning_et) == "solana-up-or-down-on-september-13-2026"
        assert uq.window_slug("sol", "1d", _NOW) == "solana-up-or-down-on-september-14-2026"

    def test_unknown_timeframe_raises(self) -> None:
        with pytest.raises(ValueError):
            uq.window_slug("btc", "4d", _NOW)


def _transport(*, gamma_rows: list | None = None, books_status: int = 200) -> httpx.MockTransport:
    calls = {"gamma": 0, "books": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets"):
            calls["gamma"] += 1
            rows = gamma_rows if gamma_rows is not None else [{
                "slug": request.url.params["slug"],
                "outcomes": json.dumps(["Down", "Up"]),  # reversed on purpose
                "clobTokenIds": json.dumps(["tok-down", "tok-up"]),
            }]
            return httpx.Response(200, json=rows)
        calls["books"] += 1
        if books_status != 200:
            return httpx.Response(books_status, json={})
        assert json.loads(request.content) == [{"token_id": "tok-up"}, {"token_id": "tok-down"}]
        return httpx.Response(200, json=[
            {"asset_id": "tok-up", "min_order_size": "5",
             "bids": [{"price": "0.40", "size": "1"}, {"price": "0.58", "size": "9"}],
             "asks": [{"price": "0.70", "size": "1"}, {"price": "0.59", "size": "9"}]},
            {"asset_id": "tok-down", "min_order_size": "5",
             "bids": [{"price": "0.41", "size": "9"}],
             "asks": [{"price": "0.42", "size": "9"}]},
        ])

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


class TestQuoteClient:
    @pytest.mark.asyncio
    async def test_reads_best_levels_per_outcome_label(self) -> None:
        transport = _transport()
        async with httpx.AsyncClient(transport=transport) as http:
            q = await UpDownQuoteClient(http).fetch("eth", "15m", _NOW)
        assert q.error is None
        assert (q.up_ask, q.up_bid, q.down_ask, q.down_bid) == (0.59, 0.58, 0.42, 0.41)
        assert q.min_order_size == 5.0

    @pytest.mark.asyncio
    async def test_token_ids_cached_per_window(self) -> None:
        transport = _transport()
        async with httpx.AsyncClient(transport=transport) as http:
            client = UpDownQuoteClient(http)
            await client.fetch("eth", "5m", _NOW)
            await client.fetch("eth", "5m", _NOW)
        assert transport.calls == {"gamma": 1, "books": 2}  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_missing_market_and_http_errors_are_reported_not_raised(self) -> None:
        async with httpx.AsyncClient(transport=_transport(gamma_rows=[])) as http:
            missing = await UpDownQuoteClient(http).fetch("eth", "5m", _NOW)
        assert missing.error and "no market" in missing.error
        assert missing.up_ask is None
        async with httpx.AsyncClient(transport=_transport(books_status=429)) as http:
            limited = await UpDownQuoteClient(http).fetch("eth", "5m", _NOW)
        assert limited.error and "429" in limited.error


def _quote(**kw) -> UpDownQuote:
    base = dict(asset="eth", timeframe="15m", fetched_at=_NOW.timestamp(),
                slug="eth-updown-15m-1", up_ask=0.59, down_ask=0.42, min_order_size=5.0)
    base.update(kw)
    return UpDownQuote(**base)


class TestQuoteFeed:
    def test_failed_read_keeps_last_good_prices_for_same_market(self) -> None:
        good = _quote()
        bad = _quote(fetched_at=good.fetched_at + 4, up_ask=None, down_ask=None, error="429")
        assert quote_feed.merge(good, bad) is good
        assert quote_feed.merge(None, bad) is bad
        other = _quote(asset="btc", up_ask=None, down_ask=None, error="429")
        assert quote_feed.merge(good, other) is other

    def test_backoff_doubles_to_a_cap(self) -> None:
        assert quote_feed.backoff_seconds(0) == quote_feed.POLL_SECONDS
        assert quote_feed.backoff_seconds(1) == 2 * quote_feed.POLL_SECONDS
        assert quote_feed.backoff_seconds(20) == quote_feed.MAX_BACKOFF_SECONDS

    @pytest.mark.asyncio
    async def test_selection_change_wakes_the_poller(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple[str, str]] = []

        async def fake_fetch(self, asset: str, timeframe: str, now=None) -> UpDownQuote:
            seen.append((asset, timeframe))
            return _quote(asset=asset, timeframe=timeframe)

        monkeypatch.setattr(UpDownQuoteClient, "fetch", fake_fetch)
        monkeypatch.setattr(quote_feed, "POLL_SECONDS", 60.0)
        monkeypatch.setattr(quote_feed, "_wanted", None)
        monkeypatch.setattr(quote_feed, "_latest", None)
        stop = asyncio.Event()
        task = asyncio.create_task(_REAL_RUN_FOREVER(stop))
        try:
            await asyncio.sleep(0.05)
            assert quote_feed.snapshot("btc", "5m") is None  # first ask: nothing yet
            await asyncio.sleep(0.05)
            assert quote_feed.snapshot("btc", "5m").asset == "btc"
            quote_feed.snapshot("sol", "1d")
            await asyncio.sleep(0.05)  # far less than POLL_SECONDS
            assert seen[-1] == ("sol", "1d")
        finally:
            stop.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


def _render(**kw) -> str:
    args = dict(trade_shares_current=10.0, asset="eth", timeframe="15m",
                quote=_quote(), now=_NOW.timestamp() + 2)
    args.update(kw)
    return controls.render(**args)


class TestTicketRender:
    def test_prices_both_sides_for_the_selected_market(self) -> None:
        html = _render()
        assert "ORDER SIZE" in html
        assert "ETH 15m" in html
        assert "$5.90" in html and "$4.20" in html  # 10 × 0.59, 10 × 0.42
        assert "data-up='0.59'" in html and "data-down='0.42'" in html
        assert "saved 10 sh" in html

    def test_lot_presets_are_multiples_of_the_venue_minimum(self) -> None:
        html = _render()
        for n in (5, 10, 25, 50):
            assert f"pickShares({n})" in html

    def test_unset_size_defaults_to_the_venue_minimum(self) -> None:
        html = _render(trade_shares_current=None)
        assert "value='5'" in html and "default 5 sh" in html

    def test_stale_error_and_pending_states_are_visible(self) -> None:
        assert "stale 40s" in _render(now=_NOW.timestamp() + 40)
        errored = _render(quote=_quote(up_ask=None, down_ask=None, error="HTTPStatusError: 429"))
        assert "no quote" in errored and "429" in errored and "$5.90" not in errored
        assert "loading" in _render(quote=None)
