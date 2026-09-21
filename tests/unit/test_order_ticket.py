"""ORDER SIZE ticket: live quote for the selected market + compact render (#89)."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from polymarket_exec.connectors import updown_quote as uq
from polymarket_exec.connectors.updown_quote import UpDownQuote, UpDownQuoteClient
from polymarket_exec.marketdata import hub as hub_mod
from polymarket_exec.marketdata import universe as uq_universe
from polymarket_exec.marketdata.order_book import TopOfBook
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


class TestDailyReferenceInstant:
    """The noon-ET close a daily Up/Down market compares against: noon ET on
    the calendar day BEFORE it resolves. That is 24h before resolution on a
    normal day, but 23h or 25h across a DST switch."""

    def test_normal_day_is_24h_before_resolution(self) -> None:
        resolves = datetime(2026, 1, 15, 17, 0, tzinfo=UTC)  # noon EST
        assert uq.daily_reference_instant(resolves) == datetime(
            2026, 1, 14, 17, 0, tzinfo=UTC
        )

    def test_fall_back_day_is_25h_before_resolution(self) -> None:
        resolves = datetime(2026, 11, 1, 17, 0, tzinfo=UTC)  # noon EST
        assert uq.daily_reference_instant(resolves) == datetime(
            2026, 10, 31, 16, 0, tzinfo=UTC  # noon EDT
        )

    def test_spring_forward_day_is_23h_before_resolution(self) -> None:
        resolves = datetime(2027, 3, 14, 16, 0, tzinfo=UTC)  # noon EDT
        assert uq.daily_reference_instant(resolves) == datetime(
            2027, 3, 13, 17, 0, tzinfo=UTC  # noon EST
        )


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


def _hub_quote(asset: str, timeframe: str, *, live: bool) -> hub_mod.MarketQuote:
    """The hub's books for the window the REST poll would read right now."""
    slug = uq.window_slug(asset, timeframe, datetime.now(UTC))
    market = uq_universe.MarketRef(asset, timeframe, slug, 1.0, 2.0, "tok-up", "tok-down")
    up = TopOfBook("tok-up", 0.60, 0.61, 7.0, 8.0, 0.01, None, 1_000, 1_000, live=live)
    down = TopOfBook("tok-down", 0.39, 0.40, 3.0, 4.0, 0.01, None, 1_000, 1_000, live=live)
    return hub_mod.MarketQuote(market, up, down, live)


def _offline_hub(*, live: bool | None) -> hub_mod.MarketDataHub:
    """A real hub (real want/release), with its quote reads answered by the test.

    ``live=None`` stands for a hub that has no window for the selection yet.
    """
    hub = hub_mod.MarketDataHub(assets=("btc", "eth"), timeframes=("5m", "15m"))

    def quote(asset: str, timeframe: str, which: str = uq_universe.CURRENT):
        return None if live is None else _hub_quote(asset, timeframe, live=live)

    hub.quote = quote  # type: ignore[method-assign]
    return hub


class TestTicketOnTheHub:
    """The ticket follows the operator's selection on the market-data hub (#242)."""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(quote_feed, "POLL_SECONDS", 0.01)
        monkeypatch.setattr(quote_feed, "_wanted", None)
        monkeypatch.setattr(quote_feed, "_latest", None)
        monkeypatch.setattr(quote_feed, "_demand", None)
        monkeypatch.setattr(quote_feed, "_min_sizes", {})
        yield
        hub_mod.set_current(None)

    async def _run(self, transport: httpx.MockTransport, steps) -> None:
        """Run the real poller against ``transport`` while ``steps`` drives it."""
        real_client = httpx.AsyncClient
        monkey = lambda **kw: real_client(transport=transport)  # noqa: E731
        stop = asyncio.Event()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(quote_feed.httpx, "AsyncClient", monkey)
            task = asyncio.create_task(_REAL_RUN_FOREVER(stop))
            try:
                await steps()
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=2)

    @pytest.mark.asyncio
    async def test_hub_quotes_replace_the_rest_poll_once_min_order_size_is_known(
        self,
    ) -> None:
        hub = _offline_hub(live=True)
        hub_mod.set_current(hub)
        transport = _transport()

        async def steps() -> None:
            quote_feed.snapshot("eth", "15m")
            await _until(lambda: quote_feed.snapshot("eth", "15m") is not None)
            await asyncio.sleep(0.1)  # many poll intervals
            quote = quote_feed.snapshot("eth", "15m")
            assert (quote.up_ask, quote.down_ask) == (0.61, 0.40)  # the hub's books
            assert (quote.up_bid, quote.down_bid) == (0.60, 0.39)
            assert quote.min_order_size == 5.0  # carried from the one REST read
            assert transport.calls["books"] == 1  # type: ignore[attr-defined]

        await self._run(transport, steps)

    @pytest.mark.asyncio
    async def test_a_hub_that_is_not_live_keeps_the_rest_poll(self) -> None:
        hub_mod.set_current(_offline_hub(live=False))
        transport = _transport()

        async def steps() -> None:
            quote_feed.snapshot("eth", "15m")
            await _until(lambda: transport.calls["books"] >= 3)  # type: ignore[attr-defined]
            quote = quote_feed.snapshot("eth", "15m")
            assert (quote.up_ask, quote.down_ask) == (0.59, 0.42)  # the REST books

        await self._run(transport, steps)

    @pytest.mark.asyncio
    async def test_demand_follows_the_selection_and_goes_on_shutdown(self) -> None:
        hub = _offline_hub(live=None)
        hub_mod.set_current(hub)

        async def steps() -> None:
            quote_feed.snapshot("eth", "15m")
            await _until(lambda: ("eth", "15m") in hub.wanted())
            assert hub.wanted()[("eth", "15m")] == frozenset({quote_feed.OWNER})
            quote_feed.snapshot("btc", "5m")
            await _until(lambda: ("btc", "5m") in hub.wanted())
            assert ("eth", "15m") not in hub.wanted()  # released with the selection

        await self._run(_transport(), steps)
        assert dict(hub.wanted()) == {}  # the ticket lets go when the poller stops

    @pytest.mark.asyncio
    async def test_a_selection_the_hub_does_not_carry_still_gets_rest_prices(self) -> None:
        hub = hub_mod.MarketDataHub(assets=("btc",), timeframes=("5m",))
        hub_mod.set_current(hub)
        transport = _transport()

        async def steps() -> None:
            quote_feed.snapshot("eth", "15m")  # not on this hub's grid
            await _until(lambda: transport.calls["books"] >= 2)  # type: ignore[attr-defined]
            assert quote_feed.snapshot("eth", "15m").up_ask == 0.59
            assert dict(hub.wanted()) == {}

        await self._run(transport, steps)

    @pytest.mark.asyncio
    async def test_no_demand_without_a_dashboard_open(self) -> None:
        hub = _offline_hub(live=None)
        hub_mod.set_current(hub)

        async def steps() -> None:
            await asyncio.sleep(0.05)
            assert dict(hub.wanted()) == {}  # nobody asked for a render

        await self._run(_transport(), steps)


async def _until(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


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

    def test_the_card_is_a_fold_the_operator_can_close(self) -> None:
        html = _render()
        # Same <details> pattern as the ACTIVITY LOG, so it collapses to its header.
        assert "<details class='card ticket fold' data-fold='ticket'" in html
        assert "<summary class='card-h'><span class='fold-title'>ORDER SIZE</span>" in html
        assert "rememberFold(this)'" in html and " open>" in html
        assert html.rstrip().endswith("</details>")

    def test_stale_error_and_pending_states_are_visible(self) -> None:
        assert "stale 40s" in _render(now=_NOW.timestamp() + 40)
        errored = _render(quote=_quote(up_ask=None, down_ask=None, error="HTTPStatusError: 429"))
        assert "no quote" in errored and "429" in errored and "$5.90" not in errored
        assert "loading" in _render(quote=None)
