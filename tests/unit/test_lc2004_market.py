"""The daily BTC Up/Down market adapter (ems/lc2004_kronos_btc_24h/market.py).

Pure tests with no network: every fetcher runs against ``httpx.MockTransport``. Pins the noon-ET
window across both 2026 clock changes and at the exact noon boundary. For the Binance 1h candles:
the forming candle is dropped, and gaps, a stale last hour and an unrolled Binance hour are
refused. The strike/settle minute is read only once it has closed. The CLOB book comes back
best first. Gamma discovery refuses a row whose eventStartTime or endDate belongs to another
window. Claude, 2026-09-22; moved onto ``ems`` by Claude, 2026-09-29.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from ems import config
from ems.lc2004_kronos_btc_24h.market import (
    SERIES_ID,
    Book,
    BtcDailyMarket,
    Candle,
    DayWindow,
    MarketDataError,
    discover,
    fetch_book,
    fetch_closed_1h_candles,
    fetch_minute_close,
    window_at,
)

HOUR_MS = 3_600_000
ET = ZoneInfo("America/New_York")


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- window_at --------------------------------------------------------------------------------


def test_normal_day_window_is_24h_noon_to_noon_named_for_the_resolution_date() -> None:
    w = window_at(_ts("2026-09-22T14:00:00"))  # 10:00 EDT on Sep 22
    assert w == DayWindow(slug="bitcoin-up-or-down-on-september-22-2026",
                          start_ts=_ts("2026-09-21T16:00:00"), end_ts=_ts("2026-09-22T16:00:00"))
    assert w.end_ts - w.start_ts == 86_400


def test_exactly_noon_et_belongs_to_the_new_window() -> None:
    noon = _ts("2026-09-22T16:00:00")  # 12:00:00 EDT
    assert window_at(noon).slug == "bitcoin-up-or-down-on-september-23-2026"
    assert window_at(noon).start_ts == noon
    assert window_at(noon - 0.001).slug == "bitcoin-up-or-down-on-september-22-2026"
    assert window_at(noon - 0.001).end_ts == noon


def test_fall_back_window_is_25h() -> None:
    # Clocks go back at 02:00 EDT on Sunday 2026-11-01: noon Oct 31 is 16:00Z, noon Nov 1 17:00Z.
    w = window_at(_ts("2026-10-31T20:00:00"))
    assert w.slug == "bitcoin-up-or-down-on-november-1-2026"
    assert (w.start_ts, w.end_ts) == (_ts("2026-10-31T16:00:00"), _ts("2026-11-01T17:00:00"))
    assert w.end_ts - w.start_ts == 25 * 3600
    # Both 01:30s of the repeated hour are in it, and so is 16:30Z (11:30 EST, not yet noon).
    for iso in ("2026-11-01T05:30:00", "2026-11-01T06:30:00", "2026-11-01T16:30:00"):
        assert window_at(_ts(iso)) == w


def test_window_after_fall_back_is_24h_and_starts_at_17z() -> None:
    w = window_at(_ts("2026-11-01T17:00:00"))  # exactly noon EST on Nov 1
    assert w.slug == "bitcoin-up-or-down-on-november-2-2026"
    assert (w.start_ts, w.end_ts) == (_ts("2026-11-01T17:00:00"), _ts("2026-11-02T17:00:00"))
    assert window_at(_ts("2026-11-02T16:59:59")) == w


def test_spring_forward_window_is_23h() -> None:
    # Clocks go forward at 02:00 EST on Sunday 2026-03-08: noon Mar 7 is 17:00Z, noon Mar 8 16:00Z.
    w = window_at(_ts("2026-03-07T18:00:00"))
    assert w.slug == "bitcoin-up-or-down-on-march-8-2026"
    assert (w.start_ts, w.end_ts) == (_ts("2026-03-07T17:00:00"), _ts("2026-03-08T16:00:00"))
    assert w.end_ts - w.start_ts == 23 * 3600
    assert window_at(_ts("2026-03-08T15:59:59")) == w
    nxt = window_at(_ts("2026-03-08T16:00:00"))  # noon EDT on Mar 8
    assert nxt.slug == "bitcoin-up-or-down-on-march-9-2026"
    assert (nxt.start_ts, nxt.end_ts) == (_ts("2026-03-08T16:00:00"), _ts("2026-03-09T16:00:00"))


@pytest.mark.parametrize("day", ["2026-03-06", "2026-09-20", "2026-10-30"])
def test_every_instant_lies_inside_its_window_and_windows_chain(day: str) -> None:
    start = _ts(f"{day}T00:00:00")
    prev: DayWindow | None = None
    for step in range(0, 4 * 86_400, 900):
        now = start + step
        w = window_at(now)
        assert w.start_ts <= now < w.end_ts
        assert w.end_ts - w.start_ts in (23 * 3600, 24 * 3600, 25 * 3600)
        for edge in (w.start_ts, w.end_ts):
            et = datetime.fromtimestamp(edge, ET)
            assert (et.hour, et.minute, et.second) == (12, 0, 0)
        if prev is not None and w != prev:
            assert w.start_ts == prev.end_ts
        prev = w


# --- fetch_closed_1h_candles ------------------------------------------------------------------

H = _ts("2026-09-22T15:00:00") * 1000  # the hour now is in
NOW_MS = H + 5 * 60_000


def _kline(open_ms: int, interval_ms: int = HOUR_MS, close: float | None = None) -> list:
    px = close if close is not None else 100_000.0 + open_ms / HOUR_MS % 97
    return [open_ms, str(px - 5), str(px + 20), str(px - 30), str(px), "12.5",
            open_ms + interval_ms - 1, str(px * 12.5), 1000, "6.0", str(px * 6.0), "0"]


def _hours(first_open: int, n: int) -> list[list]:
    return [_kline(first_open + i * HOUR_MS) for i in range(n)]


def _klines_handler(rows: list[list], seen: list[httpx.Request] | None = None,
                    status: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.path == "/api/v3/klines"
        return httpx.Response(status, json=rows if status == 200 else {"msg": "boom"})
    return handle


@pytest.mark.asyncio
async def test_candles_drop_the_forming_hour_and_end_at_the_last_closed_hour() -> None:
    seen: list[httpx.Request] = []
    rows = _hours(H - 5 * HOUR_MS, 6)  # 5 closed hours + the forming one at H
    async with _client(_klines_handler(rows, seen)) as client:
        got = await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=5)
    assert [c.open_time_ms for c in got] == [H - k * HOUR_MS for k in (5, 4, 3, 2, 1)]
    assert got[-1] == Candle(
        open_time_ms=H - HOUR_MS, open=float(rows[4][1]), high=float(rows[4][2]),
        low=float(rows[4][3]), close=float(rows[4][4]), volume=12.5,
        quote_volume=float(rows[4][7]))
    params = seen[0].url.params
    assert (params["symbol"], params["interval"], params["limit"]) == ("BTCUSDT", "1h", "6")
    assert str(seen[0].url).startswith(config.BINANCE_API_BASE)


@pytest.mark.asyncio
async def test_default_is_512_candles_from_one_513_row_request() -> None:
    seen: list[httpx.Request] = []
    rows = _hours(H - 512 * HOUR_MS, 513)
    async with _client(_klines_handler(rows, seen)) as client:
        got = await fetch_closed_1h_candles(client, now_ms=NOW_MS)
    assert len(got) == 512 and got[-1].open_time_ms == H - HOUR_MS
    assert seen[0].url.params["limit"] == "513"


@pytest.mark.asyncio
async def test_a_gap_is_refused() -> None:
    rows = _hours(H - 6 * HOUR_MS, 7)
    del rows[2]  # still 5 closed + forming, but one hour is missing
    async with _client(_klines_handler(rows)) as client:
        with pytest.raises(MarketDataError, match="not consecutive"):
            await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=5)


@pytest.mark.asyncio
async def test_a_stale_last_hour_is_refused() -> None:
    rows = _hours(H - 7 * HOUR_MS, 6)  # Binance data stops two hours back
    async with _client(_klines_handler(rows)) as client:
        with pytest.raises(MarketDataError, match="expected"):
            await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=5)


@pytest.mark.asyncio
async def test_binance_not_yet_rolled_to_the_new_hour_is_refused() -> None:
    # Just after the top of the hour: every row looks closed on our clock, and Binance has no
    # forming row yet, so the last hour may still be taking trades on Binance's clock.
    rows = _hours(H - 6 * HOUR_MS, 6)
    async with _client(_klines_handler(rows)) as client:
        with pytest.raises(MarketDataError, match="exactly 5"):
            await fetch_closed_1h_candles(client, now_ms=H + 200, count=5)


@pytest.mark.asyncio
async def test_too_few_candles_are_refused() -> None:
    rows = _hours(H - 3 * HOUR_MS, 4)
    async with _client(_klines_handler(rows)) as client:
        with pytest.raises(MarketDataError, match="exactly 5"):
            await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=5)


@pytest.mark.asyncio
async def test_candle_request_and_parse_failures_raise_market_data_error() -> None:
    async with _client(_klines_handler([], status=503)) as client:
        with pytest.raises(MarketDataError, match="request failed"):
            await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=5)
    async with _client(_klines_handler([[H - HOUR_MS, "x"]])) as client:
        with pytest.raises(MarketDataError, match="malformed"):
            await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=5)


@pytest.mark.asyncio
async def test_candle_count_out_of_range_raises_value_error() -> None:
    async with _client(_klines_handler([])) as client:
        for count in (0, 1000):
            with pytest.raises(ValueError):
                await fetch_closed_1h_candles(client, now_ms=NOW_MS, count=count)


# --- fetch_minute_close -----------------------------------------------------------------------

NOON = _ts("2026-09-21T16:00:00")  # a strike minute


@pytest.mark.asyncio
async def test_minute_close_once_closed() -> None:
    seen: list[httpx.Request] = []
    rows = [_kline(NOON * 1000, 60_000, close=115_234.56)]
    async with _client(_klines_handler(rows, seen)) as client:
        assert await fetch_minute_close(client, NOON, NOON + 61) == 115_234.56
    params = seen[0].url.params
    assert (params["symbol"], params["interval"]) == ("BTCUSDT", "1m")
    assert (params["startTime"], params["limit"]) == (str(NOON * 1000), "1")


@pytest.mark.asyncio
async def test_minute_close_refuses_misaligned_or_unclosed_minutes_without_a_request() -> None:
    seen: list[httpx.Request] = []
    rows = [_kline(NOON * 1000, 60_000)]
    async with _client(_klines_handler(rows, seen)) as client:
        assert await fetch_minute_close(client, NOON + 1, NOON + 600) is None
        assert await fetch_minute_close(client, NOON, NOON + 59.999) is None
    assert seen == []


@pytest.mark.asyncio
async def test_minute_close_wrong_or_missing_candle_is_a_failure_not_a_wait() -> None:
    """Once the minute is over, a reply without it is a failure the card shows (review
    finding, Claude, 2026-09-29)."""
    rows = [_kline((NOON + 60) * 1000, 60_000)]  # Binance skipped to the next minute
    async with _client(_klines_handler(rows)) as client:
        with pytest.raises(MarketDataError, match=f"opening at {(NOON + 60) * 1000}, not"):
            await fetch_minute_close(client, NOON, NOON + 600)
    async with _client(_klines_handler([])) as client:
        with pytest.raises(MarketDataError, match="no 1m candle"):
            await fetch_minute_close(client, NOON, NOON + 600)


@pytest.mark.asyncio
async def test_minute_close_refuses_a_candle_still_open_on_our_clock() -> None:
    row = _kline(NOON * 1000, 60_000)
    row[6] = (NOON + 60) * 1000 + 500  # close time after now
    async with _client(_klines_handler([row])) as client:
        assert await fetch_minute_close(client, NOON, NOON + 60.2) is None


@pytest.mark.asyncio
async def test_minute_close_request_and_parse_failures_raise() -> None:
    async with _client(_klines_handler([], status=451)) as client:
        with pytest.raises(MarketDataError, match=r"request failed \(HTTP 451\)"):
            await fetch_minute_close(client, NOON, NOON + 600)
    async with _client(_klines_handler([["x"]])) as client:
        with pytest.raises(MarketDataError, match="malformed"):
            await fetch_minute_close(client, NOON, NOON + 600)


# --- fetch_book -------------------------------------------------------------------------------


def _levels(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"price": p, "size": s} for p, s in pairs]


RAW_BOOK = {
    "market": "0xcond",
    "asset_id": "111",
    # The CLOB sends both sides worst to best: the best level is the LAST element.
    "bids": _levels([("0.01", "500"), ("0.38", "12"), ("0.43", "0"), ("0.48", "70"),
                     ("0.52", "40")]),
    "asks": _levels([("0.99", "800"), ("0.60", "25"), ("0.53", "30")]),
    "tick_size": "0.01",
    "min_order_size": "5",
    "last_trade_price": "0.52",
}


def _book_handler(payload: object, seen: list[httpx.Request] | None = None,
                  status: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.path == "/book"
        return httpx.Response(status, json=payload)
    return handle


@pytest.mark.asyncio
async def test_book_comes_back_best_first() -> None:
    seen: list[httpx.Request] = []
    async with _client(_book_handler(RAW_BOOK, seen)) as client:
        book = await fetch_book(client, "111")
    assert book == Book(
        bids=((0.52, 40.0), (0.48, 70.0), (0.38, 12.0), (0.01, 500.0)),  # zero-size 0.43 dropped
        asks=((0.53, 30.0), (0.60, 25.0), (0.99, 800.0)),
    )
    assert book.best_bid == 0.52 and book.best_ask == 0.53
    assert seen[0].url.params["token_id"] == "111"
    assert str(seen[0].url).startswith(config.POLYMARKET_CLOB_API)
    assert seen[0].headers["user-agent"].startswith("Mozilla/")


@pytest.mark.asyncio
async def test_empty_book_has_no_best_prices() -> None:
    async with _client(_book_handler({"bids": [], "asks": []})) as client:
        book = await fetch_book(client, "111")
    assert book is not None and book.best_bid is None and book.best_ask is None


@pytest.mark.asyncio
async def test_book_failures_are_none() -> None:
    async with _client(_book_handler({"error": "not found"}, status=404)) as client:
        assert await fetch_book(client, "111") is None
    async with _client(_book_handler(["not", "a", "book"])) as client:
        assert await fetch_book(client, "111") is None
    async with _client(_book_handler({"bids": [{"price": "0.5"}], "asks": []})) as client:
        assert await fetch_book(client, "111") is None


# --- discover ---------------------------------------------------------------------------------

WINDOW = window_at(_ts("2026-09-21T18:52:00"))


def _gamma_row(**overrides: object) -> dict:
    row: dict[str, object] = {
        "slug": "bitcoin-up-or-down-on-september-22-2026",
        "question": "Bitcoin Up or Down on September 22?",
        "conditionId": "0xcond",
        "startDate": "2026-09-20T16:07:10Z",       # market creation, NOT the window start
        "eventStartTime": "2026-09-21T16:00:00Z",
        "endDate": "2026-09-22T16:00:00Z",
        "outcomes": json.dumps(["Up", "Down"]),
        "clobTokenIds": json.dumps(["111", "222"]),
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
    }
    row.update(overrides)
    return row


def _gamma_handler(slug_rows: object, events: object, seen: list[httpx.Request],
                   slug_status: int = 200) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/markets":
            return httpx.Response(slug_status, json=slug_rows)
        if request.url.path == "/events":
            return httpx.Response(200, json=events)
        return httpx.Response(404)
    return handle


EXPECTED = BtcDailyMarket(
    slug="bitcoin-up-or-down-on-september-22-2026",
    condition_id="0xcond",
    question="Bitcoin Up or Down on September 22?",
    start_ts=_ts("2026-09-21T16:00:00"),
    end_ts=_ts("2026-09-22T16:00:00"),
    up_token="111",
    down_token="222",
)


@pytest.mark.asyncio
async def test_discover_by_slug() -> None:
    assert WINDOW.slug == EXPECTED.slug
    seen: list[httpx.Request] = []
    async with _client(_gamma_handler([_gamma_row()], [], seen)) as client:
        assert await discover(client, WINDOW) == EXPECTED
    assert len(seen) == 1
    assert seen[0].url.params["slug"] == "bitcoin-up-or-down-on-september-22-2026"
    assert str(seen[0].url).startswith(config.POLYMARKET_GAMMA_API)
    assert seen[0].headers["user-agent"].startswith("Mozilla/")


@pytest.mark.asyncio
async def test_discover_maps_tokens_by_outcome_label() -> None:
    row = _gamma_row(outcomes=json.dumps(["Down", "Up"]), clobTokenIds=json.dumps(["222", "111"]))
    seen: list[httpx.Request] = []
    async with _client(_gamma_handler([row], [], seen)) as client:
        got = await discover(client, WINDOW)
    assert got is not None
    assert (got.up_token, got.down_token) == ("111", "222")


@pytest.mark.asyncio
async def test_discover_rejects_a_mismatched_event_start_and_falls_back_to_the_series() -> None:
    wrong = _gamma_row(eventStartTime="2026-09-20T16:07:10Z")
    events = [{"markets": [_gamma_row(slug="other", conditionId="0xold",
                                      eventStartTime="2026-09-20T16:00:00Z",
                                      endDate="2026-09-21T16:00:00Z"),
                           _gamma_row()]}]
    seen: list[httpx.Request] = []
    async with _client(_gamma_handler([wrong], events, seen)) as client:
        assert await discover(client, WINDOW) == EXPECTED
    assert [r.url.path for r in seen] == ["/markets", "/events"]
    params = seen[1].url.params
    assert (params["series_id"], params["closed"]) == (str(SERIES_ID), "false")


@pytest.mark.asyncio
async def test_discover_rejects_a_mismatched_end_date_everywhere() -> None:
    wrong = _gamma_row(endDate="2026-09-22T17:00:00Z")
    seen: list[httpx.Request] = []
    async with _client(_gamma_handler([wrong], [{"markets": [wrong]}], seen)) as client:
        assert await discover(client, WINDOW) is None


@pytest.mark.asyncio
async def test_discover_rejects_rows_without_a_condition_id_or_two_tokens() -> None:
    for bad in (_gamma_row(conditionId=None), _gamma_row(clobTokenIds=json.dumps(["111"])),
                _gamma_row(outcomes=json.dumps(["Yes", "No"]))):
        seen: list[httpx.Request] = []
        async with _client(_gamma_handler([bad], [], seen)) as client:
            assert await discover(client, WINDOW) is None


@pytest.mark.asyncio
async def test_a_failed_gamma_lookup_raises_rather_than_reads_as_not_listed() -> None:
    """Gamma lists the daily market two days ahead, so a failed lookup is not "not listed
    yet" (review finding, Claude, 2026-09-29)."""
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Cloudflare")
    async with _client(forbidden) as client:
        with pytest.raises(MarketDataError) as caught:
            await discover(client, WINDOW)
    assert str(caught.value) == ("Gamma: the slug lookup failed (HTTP 403); the series lookup "
                                 "failed (HTTP 403)")
    # One lookup failing and the other finding nothing is still a failure, not "not listed".
    seen: list[httpx.Request] = []
    async with _client(_gamma_handler({"error": "down"}, [], seen, slug_status=502)) as client:
        with pytest.raises(MarketDataError, match=r"the slug lookup failed \(HTTP 502\)$"):
            await discover(client, WINDOW)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)
    async with _client(timeout) as client:
        with pytest.raises(MarketDataError, match="ReadTimeout: timed out"):
            await discover(client, WINDOW)


@pytest.mark.asyncio
async def test_discover_survives_a_failed_slug_lookup() -> None:
    seen: list[httpx.Request] = []
    handler = _gamma_handler({"error": "down"}, [{"markets": [_gamma_row()]}], seen,
                             slug_status=502)
    async with _client(handler) as client:
        assert await discover(client, WINDOW) == EXPECTED
