"""Fade 1h Momentum on 15m: the live inputs read (``inputs.gather``).

A duck-typed fake hub stands in for ``MarketDataHub`` (the real frozen types are used for
what it returns), and an ``httpx.MockTransport`` serves Binance klines and Gamma. Window rows
go to the real schema on a temp database, the same way the runner will use them.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems.fade_1h_momentum_15m import inputs as fi
from ems.fade_1h_momentum_15m import ledger
from ems.fade_1h_momentum_15m.inputs import (
    ASSETS,
    OWNER,
    InputMemory,
    Inputs,
    Problem,
    gather,
    start_reference,
    window_average,
)
from ems.marketdata.hub import MarketQuote
from ems.marketdata.order_book import TopOfBook
from ems.marketdata.rtds_stream import PricePoint
from ems.marketdata.universe import MarketRef

HOUR = 1_789_934_400  # a UTC hour boundary
S0 = HOUR + 900  # the hour's second quarter
END = S0 + 900
NOW = S0 + 125.4
BINANCE_BASE = "https://binance.test"
BASE = {"btc": 100_000.0, "eth": 4_000.0, "sol": 200.0, "xrp": 3.0}
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT", "xrp": "XRPUSDT"}
ASSET_OF = {v: k for k, v in SYMBOL.items()}
GAP = range(S0 + 10, S0 + 15)  # five seconds with no TWAP-60s print
INTERVAL_S = {"1m": 60, "15m": 900, "1h": 3600}


# --------------------------------------------------------------------------- fake market data


def twap(asset: str, s: int) -> float:
    return BASE[asset] * (1 + 1e-5 * ((s * 7) % 11 - 5))


def raw(asset: str, s: int) -> float:
    return BASE[asset] * (1 + 2e-5 * ((s * 3) % 7 - 3))


def bar(symbol: str, interval_s: int, open_s: int) -> tuple[float, float]:
    """(open, close) of a synthetic Binance candle."""
    k = open_s // interval_s
    o = BASE[ASSET_OF[symbol]] * math.exp(1e-4 * (k % 7 - 3))
    return o, o * math.exp(2e-4 * ((k * 3) % 5 - 2))


def point(source: str, asset: str, s: float, value: float) -> PricePoint:
    obs_ms = int(s * 1000)
    return PricePoint(source=source, asset=asset, value=value, obs_ms=obs_ms, publish_ms=None,
                      received_ms=obs_ms + 2000)


def history(asset: str, source: str = fi.TWAP60, first: int = S0 - 40,
            last: int | None = None, skip: Any = GAP) -> tuple[PricePoint, ...]:
    last = int(NOW) - 2 if last is None else last
    fn = twap if source == fi.TWAP60 else raw
    return tuple(point(source, asset, s, fn(asset, s)) for s in range(first, last + 1)
                 if s not in skip)


def top(token: str, bid: float | None, ask: float | None, *, live: bool = True,
        tick: float | None = 0.01, source: str = "stream", received: float = NOW - 0.3
        ) -> TopOfBook:
    return TopOfBook(token_id=token, best_bid=bid, best_ask=ask, bid_size=100.0,
                     ask_size=80.0, tick_size=tick, last_trade_price=None,
                     server_ts_ms=int(received * 1000), received_ms=int(received * 1000),
                     live=live, source=source)


@dataclass
class FakeHub:
    markets: dict[tuple[str, str], MarketRef | None] = field(default_factory=dict)
    tops: dict[str, TopOfBook] = field(default_factory=dict)
    rest_tops: dict[str, TopOfBook] = field(default_factory=dict)
    depth: dict[tuple[str, str], tuple[tuple[float, float], ...] | None] = field(
        default_factory=dict)
    history: dict[tuple[str, str], tuple[PricePoint, ...]] = field(default_factory=dict)
    wants: list[tuple[str, str, str, bool]] = field(default_factory=list)
    waits: list[tuple[str, str]] = field(default_factory=list)
    want_error: Exception | None = None
    market_error: dict[str, Exception] = field(default_factory=dict)

    def want(self, asset: str, timeframe: str, owner: str, *, hot: bool = False) -> None:
        if self.want_error is not None:
            raise self.want_error
        self.wants.append((asset, timeframe, owner, hot))

    async def wait_ready(self, asset: str, timeframe: str, timeout_s: float = 10.0) -> bool:
        self.waits.append((asset, timeframe))
        return False

    def market(self, asset: str, timeframe: str, which: str = "current") -> MarketRef | None:
        if asset in self.market_error:
            raise self.market_error[asset]
        return self.markets.get((asset, timeframe))

    def quote(self, asset: str, timeframe: str, which: str = "current") -> MarketQuote | None:
        ref = self.markets.get((asset, timeframe))
        if ref is None:
            return None
        up, down = self.tops.get(ref.up_token), self.tops.get(ref.down_token)
        live = up is not None and down is not None and up.live and down.live
        return MarketQuote(ref, up, down, live)

    def top(self, token: str) -> TopOfBook | None:
        return self.tops.get(token)

    def book_top(self, token: str, max_age_s: float | None = None) -> TopOfBook | None:
        if token in self.rest_tops:
            return self.rest_tops[token]
        t = self.tops.get(token)
        return t if t is not None and t.live else None

    def levels(self, token: str, side: str = "bid", n: int = 10):
        got = self.depth.get((token, side), ())
        return None if got is None else got[:n]

    def price(self, source: str, asset: str) -> PricePoint | None:
        held = self.history.get((source, asset), ())
        return held[-1] if held else None

    def prices(self, source: str, asset: str, seconds: float | None = None):
        return self.history.get((source, asset), ())


def ref15(asset: str, start: int = S0, cid: str | None = "auto") -> MarketRef:
    return MarketRef(asset=asset, timeframe="15m", slug=f"{asset}-updown-15m-{start}",
                     window_start=float(start), window_end=float(start + 900),
                     up_token=f"UP-{asset}", down_token=f"DN-{asset}",
                     condition_id=f"cid-{asset}-{start}" if cid == "auto" else cid)


def ref1h(asset: str, start: int = HOUR) -> MarketRef:
    return MarketRef(asset=asset, timeframe="1h", slug=f"{asset}-up-or-down-{start}",
                     window_start=float(start), window_end=float(start + 3600),
                     up_token=f"HUP-{asset}", down_token=f"HDN-{asset}",
                     condition_id=f"hcid-{asset}")


def scene(**changes: Any) -> FakeHub:
    hub = FakeHub()
    for a in ASSETS:
        hub.markets[(a, "15m")] = ref15(a)
        hub.markets[(a, "1h")] = ref1h(a)
        hub.tops[f"UP-{a}"] = top(f"UP-{a}", 0.54, 0.56)
        hub.tops[f"DN-{a}"] = top(f"DN-{a}", 0.44, 0.46)
        hub.tops[f"HUP-{a}"] = top(f"HUP-{a}", 0.60, 0.62, received=NOW - 25)
        hub.tops[f"HDN-{a}"] = top(f"HDN-{a}", 0.38, 0.40, received=NOW - 25)
        hub.depth[(f"UP-{a}", "bid")] = ((0.54, 100.0), (0.53, 200.0), (0.50, 300.0))
        hub.depth[(f"UP-{a}", "ask")] = ((0.56, 80.0), (0.57, 120.0))
        hub.depth[(f"DN-{a}", "bid")] = ((0.44, 90.0), (0.43, 150.0))
        hub.depth[(f"DN-{a}", "ask")] = ((0.46, 70.0),)
        hub.history[(fi.TWAP60, a)] = history(a)
        hub.history[(fi.CHAINLINK, a)] = history(a, fi.CHAINLINK, first=int(NOW) - 60)
        hub.history[(fi.BINANCE, a)] = history(a, fi.BINANCE, first=int(NOW) - 60,
                                               last=int(NOW))
    for key, value in changes.items():
        setattr(hub, key, value)
    return hub


@dataclass
class FakeVenue:
    """Binance klines (every candle from startTime up to the forming one, whatever the
    limit) and Gamma markets."""

    now: float = NOW
    fail: dict[tuple[str, str], int] = field(default_factory=dict)
    drop: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    gamma: dict[str, Any] = field(default_factory=dict)
    gamma_status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = request.url.params
        if request.url.host == "gamma-api.polymarket.com":
            if self.gamma_status != 200:
                return httpx.Response(self.gamma_status, json={"error": "down"})
            slug = params["slug"]
            return httpx.Response(200, json=[self.gamma[slug]] if slug in self.gamma else [])
        assert str(request.url).startswith(f"{BINANCE_BASE}/api/v3/klines")
        symbol, interval = params["symbol"], params["interval"]
        if (symbol, interval) in self.fail:
            return httpx.Response(self.fail[(symbol, interval)], json={"msg": "busy"})
        step = INTERVAL_S[interval]
        start = int(params["startTime"]) // 1000
        rows = []
        open_s = start
        while open_s <= self.now:  # includes the forming candle
            if open_s not in self.drop.get((symbol, interval), set()):
                o, c = bar(symbol, step, open_s)
                rows.append([open_s * 1000, f"{o:.10f}", "0", "0", f"{c:.10f}", "1.0",
                             (open_s + step) * 1000 - 1])
            open_s += step
        return httpx.Response(200, json=rows)

    def asked(self, interval: str) -> int:
        return sum(1 for r in self.requests if r.url.params.get("interval") == interval)


def client_for(venue: FakeVenue) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(venue))


@pytest.fixture(autouse=True)
def _binance_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config, "BINANCE_API_BASE", BINANCE_BASE)


@pytest_asyncio.fixture
async def fade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "fade.db")
    await _db.init_db()
    return _db


async def run(hub: FakeHub | None, venue: FakeVenue | None = None, now: float = NOW,
              memory: InputMemory | None = None) -> dict[str, Inputs | Problem]:
    venue = venue or FakeVenue(now=now)
    async with client_for(venue) as client:
        return await gather(hub, client, now, memory=memory or InputMemory())


def locf_average(asset: str, s0: int, last: int, skip: Any = GAP) -> tuple[float, float]:
    """The documented average, written out the slow way."""
    vals, value = [], twap(asset, s0)
    for s in range(s0, last + 1):
        if s not in skip:
            value = twap(asset, s)
        vals.append(value)
    return sum(vals) / len(vals), sum(math.log(v) for v in vals) / len(vals)


# --------------------------------------------------------------------------- the whole read


@pytest.mark.asyncio
async def test_full_read_for_every_coin(fade_db) -> None:
    hub, venue = scene(), FakeVenue()
    got = await run(hub, venue)

    assert set(got) == set(ASSETS)
    assert all(isinstance(v, Inputs) for v in got.values()), got
    btc = got["btc"]
    assert isinstance(btc, Inputs)

    # The window, its tokens and its hour.
    assert (btc.window_slug, btc.window_start, btc.window_end) == (
        f"btc-updown-15m-{S0}", S0, END)
    assert (btc.up_token, btc.down_token, btc.condition_id) == ("UP-btc", "DN-btc",
                                                                f"cid-btc-{S0}")
    assert btc.tick_size == 0.01
    assert (btc.hour_start, btc.hour_slug, btc.hour_condition_id) == (
        HOUR, f"btc-up-or-down-{HOUR}", "hcid-btc")
    assert btc.quarter == 2
    assert btc.t == pytest.approx((NOW - HOUR) / 3600)
    assert btc.tau == pytest.approx(125.4 / 900)
    assert btc.h == pytest.approx((END - NOW) / 3600)

    # Books: both tokens, both sides, depth best first.
    assert (btc.up_book.best_bid, btc.up_book.best_ask) == (0.54, 0.56)
    assert (btc.down_book.best_bid, btc.down_book.best_ask) == (0.44, 0.46)
    assert btc.market_up == pytest.approx(0.55)
    assert btc.up_book.depth_ahead(0.53) == pytest.approx(300.0)
    assert btc.up_book.depth_ahead(0.55) == 0.0
    assert btc.down_book.asks == ((0.46, 70.0),)
    assert btc.up_book.age_s == pytest.approx(0.3, abs=1e-3)

    # The 1h market's Up price is its Up book's mid; a 25 s quiet 1h book is still used.
    assert btc.hour_up == pytest.approx(0.61)
    assert btc.hour_book_age_s == pytest.approx(25.0, abs=1e-3)

    # Prices now.
    newest = int(NOW) - 2
    assert btc.twap60.value == pytest.approx(twap("btc", newest))
    assert btc.twap60.age_s == pytest.approx(NOW - newest)
    assert btc.chainlink.value == pytest.approx(raw("btc", newest))
    assert btc.binance.value == pytest.approx(raw("btc", int(NOW)))

    # Start reference and the average so far (the five-second hole is held over).
    assert btc.start_ref == pytest.approx(twap("btc", S0))
    assert btc.start_ref_source.startswith("TWAP-60s print at the open, read 125 s after")
    avg, log_avg = locf_average("btc", S0, newest)
    assert btc.window_avg.value == pytest.approx(avg, rel=1e-12)
    assert btc.window_avg.log_value == pytest.approx(log_avg, rel=1e-12)
    assert btc.window_avg.through_s == newest
    assert btc.window_avg.seconds == newest - S0 + 1
    assert btc.window_avg.printed == newest - S0 + 1 - len(GAP)
    assert btc.window_avg.longest_gap_s == len(GAP)
    # The price now is the live Chainlink price against the opening print; the TWAP-60s print
    # now (a 60 s average) is recorded but is not the price now.
    assert btc.d == pytest.approx(math.log(raw("btc", newest) / twap("btc", S0)))
    assert btc.d != pytest.approx(math.log(twap("btc", newest) / twap("btc", S0)))
    assert btc.as_record()["derived"]["d"] == pytest.approx(btc.d)
    assert btc.as_record()["derived"]["d_binance"] == pytest.approx(
        math.log(btc.binance.value / twap("btc", S0)))
    assert btc.abar == pytest.approx(log_avg - math.log(twap("btc", S0)))

    # Binance: the forming candles are dropped, newest first.
    end = int((NOW - fi.KLINE_SETTLE_S) // 60) * 60
    closes = [bar("BTCUSDT", 60, end - 60 * k)[1] for k in range(1, 62)]  # newest first
    want_minutes = [math.log(closes[i] / closes[i + 1]) for i in range(60)]
    assert btc.minute_returns == pytest.approx(want_minutes, rel=1e-9)
    assert btc.minute_returns_end == end
    assert btc.sigma == pytest.approx(math.sqrt(sum(r * r for r in want_minutes)))
    assert btc.mu_l == pytest.approx(math.log(closes[0] / closes[60]))
    want_r15 = [math.log(c / o) for o, c in
                (bar("BTCUSDT", 900, S0 - 900 * j) for j in range(1, 13))]
    assert btc.r15 == pytest.approx(want_r15, rel=1e-9)
    assert btc.hour_open == pytest.approx(bar("BTCUSDT", 3600, HOUR)[0])
    assert btc.x == pytest.approx(math.log(raw("btc", int(NOW)) / btc.hour_open))
    assert btc.notes == ()

    # Demand: the 15m market hot, the 1h market not, under the strategy's own owner name.
    assert ("btc", "15m", OWNER, True) in hub.wants
    assert ("btc", "1h", OWNER, False) in hub.wants
    # Every Binance read went to the configured base.
    assert all(str(r.url).startswith(BINANCE_BASE) for r in venue.requests)

    # The window was recorded with its start reference, for every coin.
    row = await ledger.get_window(f"btc-updown-15m-{S0}")
    assert row is not None
    assert row["start_ref_price"] == pytest.approx(twap("btc", S0))
    assert row["start_ref_source"] == btc.start_ref_source
    assert (row["condition_id"], row["hour_slug"], row["hour_condition_id"]) == (
        f"cid-btc-{S0}", f"btc-up-or-down-{HOUR}", "hcid-btc")
    for a in ASSETS:
        assert (await ledger.get_window(f"{a}-updown-15m-{S0}")) is not None

    # The record is JSON and carries the derived values.
    record = json.loads(json.dumps(btc.as_record()))
    assert record["status"] == "ok"
    assert record["derived"]["quarter"] == 2
    assert record["derived"]["sigma"] == pytest.approx(btc.sigma)
    assert len(record["up_book"]["bids"]) == 3


@pytest.mark.asyncio
async def test_no_hub_is_a_problem_for_every_coin() -> None:
    got = await run(None)
    assert {a: p.code for a, p in got.items()} == dict.fromkeys(ASSETS, "no_hub")
    assert all(isinstance(p, Problem) and "not running" in p.message for p in got.values())


@pytest.mark.asyncio
async def test_market_not_offered(fade_db) -> None:
    got = await run(scene(want_error=ValueError("btc 15m is not an available market")))
    assert all(p.code == "market_unavailable" for p in got.values())


@pytest.mark.asyncio
async def test_one_coin_failing_leaves_the_others(fade_db) -> None:
    hub = scene(market_error={"eth": RuntimeError("boom")})
    got = await run(hub)
    assert isinstance(got["eth"], Problem)
    assert got["eth"].code == "internal_error" and "RuntimeError: boom" in got["eth"].message
    assert all(isinstance(got[a], Inputs) for a in ("btc", "sol", "xrp"))


# --------------------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_window_bounds_guard(fade_db) -> None:
    hub = scene()
    hub.markets[("btc", "15m")] = ref15("btc", start=S0 - 900)  # the hub has not rolled yet
    hub.markets[("eth", "15m")] = None
    got = await run(hub)
    assert got["btc"].code == "window_rolling" and "ended at" in got["btc"].message
    assert got["eth"].code == "window_unknown"
    assert await ledger.get_window(f"btc-updown-15m-{S0 - 900}") is None


@pytest.mark.asyncio
async def test_hour_guards(fade_db) -> None:
    hub = scene()
    hub.markets[("btc", "1h")] = ref1h("btc", start=HOUR - 3600)
    hub.tops["HUP-eth"] = top("HUP-eth", 0.60, 0.62, live=False)
    hub.tops["HUP-sol"] = top("HUP-sol", None, 0.62)
    hub.markets[("xrp", "1h")] = None
    got = await run(hub)
    assert got["btc"].code == "hour_mismatch"
    assert got["eth"].code == "hour_book_not_live"
    assert got["sol"].code == "hour_book_one_sided" and "no bids" in got["sol"].message
    assert got["xrp"].code == "hour_market_unknown"
    # The window is still recorded, with the hour's slug when it was known.
    row = await ledger.get_window(f"eth-updown-15m-{S0}")
    assert row is not None and row["hour_slug"] == f"eth-up-or-down-{HOUR}"


@pytest.mark.asyncio
async def test_book_guards(fade_db) -> None:
    hub = scene()
    hub.tops["UP-btc"] = top("UP-btc", 0.54, 0.56, live=False)
    hub.tops["DN-eth"] = top("DN-eth", 0.44, None)
    hub.rest_tops["UP-sol"] = top("UP-sol", 0.57, 0.56, source="rest")
    hub.rest_tops["UP-xrp"] = top("UP-xrp", 0.55, 0.56, source="rest", received=NOW - 0.1)
    got = await run(hub)
    assert got["btc"].code == "book_not_live" and "Up book" in got["btc"].message
    assert got["eth"].code == "book_one_sided" and "Down book has no asks" in got["eth"].message
    assert got["sol"].code == "book_crossed"
    xrp = got["xrp"]
    assert isinstance(xrp, Inputs)
    assert (xrp.up_book.best_bid, xrp.up_book.source) == (0.55, "rest")  # the fresher top
    assert xrp.up_book.bids[0] == (0.54, 100.0)  # depth stays the stream book


@pytest.mark.asyncio
async def test_price_guards(fade_db) -> None:
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = history("btc", last=int(NOW) - 8)
    hub.history[(fi.CHAINLINK, "eth")] = ()
    hub.history[(fi.BINANCE, "sol")] = (point(fi.BINANCE, "sol", NOW - 1, float("nan")),)
    got = await run(hub)
    assert got["btc"].code == "price_stale" and "8 s old" in got["btc"].message
    assert got["eth"].code == "price_missing" and "Chainlink print for ETH" in got["eth"].message
    assert got["sol"].code == "price_invalid"
    assert isinstance(got["xrp"], Inputs)


@pytest.mark.asyncio
async def test_problem_keeps_every_issue_and_what_was_known(fade_db) -> None:
    hub = scene()
    hub.tops["DN-btc"] = top("DN-btc", None, 0.46)
    hub.history[(fi.BINANCE, "btc")] = history("btc", fi.BINANCE, last=int(NOW) - 30)
    got = await run(hub)
    btc = got["btc"]
    assert isinstance(btc, Problem)
    assert btc.codes == ("book_one_sided", "price_stale")
    assert btc.window_slug == f"btc-updown-15m-{S0}"
    assert btc.known["condition_id"] == f"cid-btc-{S0}"
    assert btc.known["start_ref"] == pytest.approx(twap("btc", S0))
    assert btc.known["up_bid"] == 0.54
    record = json.loads(json.dumps(btc.as_record()))
    assert record["status"] == "problem" and record["also"][0]["code"] == "price_stale"


@pytest.mark.asyncio
async def test_default_tick_is_a_note(fade_db) -> None:
    hub = scene()
    hub.tops["UP-btc"] = top("UP-btc", 0.54, 0.56, tick=None)
    hub.tops["DN-btc"] = top("DN-btc", 0.44, 0.46, tick=None)
    hub.tops["UP-eth"] = top("UP-eth", 0.54, 0.56, tick=0.001)
    got = await run(hub)
    assert got["btc"].tick_size == 0.01 and "standard 0.01" in got["btc"].notes[0]
    assert got["eth"].tick_size == 0.01  # the coarser of 0.001 and 0.01


# --------------------------------------------------------------------------- start reference


@pytest.mark.asyncio
async def test_start_reference_waits_for_the_open_print_then_sticks(fade_db) -> None:
    memory = InputMemory()
    early = S0 + 1.0
    hub = scene()
    for a in ASSETS:
        hub.history[(fi.TWAP60, a)] = history(a, last=S0 - 1)
    got = await run(hub, now=early, memory=memory)
    assert "start_ref_pending" in got["btc"].codes
    messages = [got["btc"].message, *(m for _, m in got["btc"].also)]
    assert any("arrives about 2 s after the open" in m for m in messages)
    row = await ledger.get_window(f"btc-updown-15m-{S0}")
    assert row is not None and row["start_ref_price"] is None  # recorded, reference unknown

    got = await run(scene(), memory=memory)
    first = got["btc"].start_ref
    assert first == pytest.approx(twap("btc", S0))
    assert (await ledger.get_window(f"btc-updown-15m-{S0}"))["start_ref_price"] == first

    # A later pass whose held prints disagree keeps the first value.
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = tuple(
        replace(p, value=p.value * 1.01) for p in history("btc"))
    got = await run(hub, memory=memory)
    assert got["btc"].start_ref == first

    # So does a restart: the stored row wins over the feed.
    got = await run(hub, memory=InputMemory())
    assert got["btc"].start_ref == first
    assert got["btc"].start_ref_source.startswith("TWAP-60s print at the open")


@pytest.mark.asyncio
async def test_start_reference_stand_ins_when_the_open_second_is_skipped(fade_db) -> None:
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = history("btc", skip={S0, *GAP})
    hub.history[(fi.TWAP60, "eth")] = history("eth", skip={*range(S0 - 6, S0 + 2), *GAP})
    got = await run(hub)
    btc, eth = got["btc"], got["eth"]
    assert btc.start_ref == pytest.approx(twap("btc", S0 - 1))
    assert "1 s before the open (none at the open second)" in btc.start_ref_source
    assert eth.start_ref == pytest.approx(twap("eth", S0 + 2))
    assert "2 s after the open" in eth.start_ref_source
    # With no print at the open second, the average starts from the reference.
    assert eth.window_avg.value == pytest.approx(
        locf_average("eth", S0, int(NOW) - 2, skip={*range(S0, S0 + 2), *GAP})[0])


@pytest.mark.asyncio
async def test_open_missed_after_a_restart(fade_db) -> None:
    late = [p for p in history("btc") if p.obs_ms >= (S0 + 60) * 1000]  # ~68 s backfill
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = tuple(late)
    got = await run(hub)
    assert got["btc"].code == "start_ref_missing"

    # Stored by the earlier process: the reference is known, the average is not.
    await ledger.upsert_window(window_slug=f"btc-updown-15m-{S0}", asset="btc",
                               window_start=S0, window_end=END, ts=S0 + 2,
                               start_ref_price=twap("btc", S0),
                               start_ref_source="TWAP-60s print at the open, read 2 s after "
                                                "the open")
    got = await run(hub)
    # The window's average so far has a hole from the open, but it decides nothing: the
    # window settles on the print at its close, so the coin still has its inputs.
    btc = got["btc"]
    assert isinstance(btc, Inputs), btc
    assert btc.start_ref == pytest.approx(twap("btc", S0))
    assert btc.window_avg.longest_gap_s == 59
    assert btc.as_record()["window_avg"]["longest_gap_s"] == 59


# --------------------------------------------------------------------------- the average


@pytest.mark.asyncio
async def test_average_keeps_prints_the_hub_has_dropped(fade_db) -> None:
    memory = InputMemory()
    await run(scene(), memory=memory)
    later = NOW + 60
    hub = scene()
    for a in ASSETS:  # the hub no longer holds the window's first ~2 minutes
        hub.history[(fi.TWAP60, a)] = history(a, first=int(NOW) - 10, last=int(later) - 2)
        hub.history[(fi.CHAINLINK, a)] = history(a, fi.CHAINLINK, first=int(later) - 5,
                                                 last=int(later) - 2)
        hub.history[(fi.BINANCE, a)] = history(a, fi.BINANCE, first=int(later) - 5,
                                               last=int(later))
    got = await run(hub, now=later, memory=memory)
    btc = got["btc"]
    assert isinstance(btc, Inputs), btc
    assert btc.window_avg.value == pytest.approx(locf_average("btc", S0, int(later) - 2)[0],
                                                 rel=1e-12)
    assert btc.window_avg.through_s == int(later) - 2


@pytest.mark.asyncio
async def test_a_long_hole_in_the_window_average_is_recorded_not_a_problem(fade_db) -> None:
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = history("btc", skip=range(S0 + 20, S0 + 60))
    got = await run(hub)
    btc = got["btc"]
    assert isinstance(btc, Inputs), btc
    assert btc.window_avg.longest_gap_s == 40 and btc.notes == ()


def test_window_average_by_hand() -> None:
    avg = window_average({S0: 10.0, S0 + 1: 12.0, S0 + 4: 6.0}, S0, 10.0)
    assert avg.value == pytest.approx((10 + 12 + 12 + 12 + 6) / 5)
    assert avg.log_value == pytest.approx((math.log(10) + 3 * math.log(12) + math.log(6)) / 5)
    assert (avg.through_s, avg.seconds, avg.printed, avg.longest_gap_s) == (S0 + 4, 5, 3, 2)
    # No print at the open second: the reference stands in for it.
    avg = window_average({S0 + 2: 8.0}, S0, 10.0)
    assert avg.value == pytest.approx((10 + 10 + 8) / 3) and avg.longest_gap_s == 1
    # Prints before the window are not part of it.
    assert window_average({S0 - 5: 1.0}, S0, 10.0).value == 10.0


def test_start_reference_by_hand() -> None:
    pts = [point(fi.TWAP60, "btc", s, float(s - S0 + 100)) for s in (S0 - 3, S0 + 1)]
    assert start_reference(pts[:1], S0, S0 + 1).status == "pending"
    got = start_reference(pts, S0, S0 + 3)
    assert (got.status, got.value) == ("found", 97.0)
    far = [point(fi.TWAP60, "btc", s, 1.0) for s in (S0 - 9, S0 + 9)]
    assert start_reference(far, S0, S0 + 10).status == "missing"


# --------------------------------------------------------------------------- Binance


@pytest.mark.asyncio
async def test_binance_failures_are_named(fade_db) -> None:
    venue = FakeVenue(
        fail={("BTCUSDT", "1m"): 500},
        drop={("ETHUSDT", "15m"): {S0 - 900 * 5}, ("SOLUSDT", "1h"): {HOUR},
              ("XRPUSDT", "1m"): {int((NOW - 2) // 60) * 60 - 60}},
    )
    got = await run(scene(), venue)
    assert got["btc"].code == "klines_failed" and "500" in got["btc"].message
    assert got["eth"].code == "klines_incomplete" and "11 of the 12" in got["eth"].message
    assert got["sol"].code == "hour_open_missing"
    assert got["xrp"].code == "klines_incomplete" and "60 of the 61" in got["xrp"].message


@pytest.mark.asyncio
async def test_candles_are_cached_for_their_window_hour_and_minute(fade_db) -> None:
    memory, venue = InputMemory(), FakeVenue()
    await run(scene(), venue, memory=memory)
    assert (venue.asked("1m"), venue.asked("15m"), venue.asked("1h")) == (4, 4, 4)

    venue.now = NOW + 20  # the same minute: nothing new to read
    await run(scene(), venue, now=NOW + 20, memory=memory)
    assert (venue.asked("1m"), venue.asked("15m"), venue.asked("1h")) == (4, 4, 4)

    venue.now = NOW + 60  # a new minute: only the minute candles again
    hub = scene()
    for a in ASSETS:
        hub.history[(fi.TWAP60, a)] = history(a, last=int(NOW) + 58)
        hub.history[(fi.CHAINLINK, a)] = history(a, fi.CHAINLINK, first=int(NOW),
                                                 last=int(NOW) + 58)
        hub.history[(fi.BINANCE, a)] = history(a, fi.BINANCE, first=int(NOW),
                                               last=int(NOW) + 60)
    got = await run(hub, venue, now=NOW + 60, memory=memory)
    assert all(isinstance(v, Inputs) for v in got.values()), got
    assert (venue.asked("1m"), venue.asked("15m"), venue.asked("1h")) == (8, 4, 4)


@pytest.mark.asyncio
async def test_the_candle_ending_at_the_open_is_read_once_it_has_closed(fade_db) -> None:
    got = await run(scene(), now=S0 + 1.0)
    assert "klines_incomplete" in got["btc"].codes
    assert any("closed under 2 s ago" in m for c, m in [(got["btc"].code,
                                                         got["btc"].message),
                                                        *got["btc"].also])


# --------------------------------------------------------------------------- condition id


@pytest.mark.asyncio
async def test_condition_id_from_gamma_when_the_hub_lacks_it(fade_db) -> None:
    hub = scene()
    hub.markets[("btc", "15m")] = ref15("btc", cid=None)
    hub.markets[("eth", "15m")] = ref15("eth", cid=None)
    venue = FakeVenue(gamma={f"btc-updown-15m-{S0}": {"slug": f"btc-updown-15m-{S0}",
                                                     "conditionId": "0xabc"}})
    memory = InputMemory()
    got = await run(hub, venue, memory=memory)
    assert got["btc"].condition_id == "0xabc"
    assert got["eth"].code == "condition_id_unknown" and "Gamma does not list it" in \
        got["eth"].message
    gamma = [r for r in venue.requests if r.url.host == "gamma-api.polymarket.com"]
    assert gamma[0].headers["user-agent"].startswith("Mozilla/5.0")

    # Found once, kept; a failed lookup waits before asking again.
    venue.requests.clear()
    got = await run(hub, venue, now=NOW + 1, memory=memory)
    assert got["btc"].condition_id == "0xabc"
    assert "retried in 29 s" in got["eth"].message
    assert not [r for r in venue.requests if r.url.host == "gamma-api.polymarket.com"]
    row = await ledger.get_window(f"btc-updown-15m-{S0}")
    assert row["condition_id"] == "0xabc"


# --------------------------------------------------------------------------- persistence


@pytest.mark.asyncio
async def test_a_database_failure_is_a_warning_not_a_crash(fade_db, monkeypatch) -> None:
    async def broken(**_: Any) -> None:
        raise RuntimeError("disk full")

    async def unreadable(_: str) -> None:
        raise RuntimeError("locked")

    monkeypatch.setattr(ledger, "upsert_window", broken)
    monkeypatch.setattr(ledger, "get_window", unreadable)
    memory = InputMemory()
    got = await run(scene(), memory=memory)
    btc = got["btc"]
    assert isinstance(btc, Inputs)
    # A failure is a warning (the runner reports it as an error of the pass), not a plain note.
    assert any("Could not save this window's row" in w and "RuntimeError: disk full" in w
               for w in btc.warnings)
    assert any("stored start reference" in w for w in btc.warnings)
    assert not any("Could not" in n for n in btc.notes)
    assert btc.as_record()["warnings"] == list(btc.warnings)
    assert f"btc-updown-15m-{S0}" not in memory.saved  # tried again next pass


@pytest.mark.asyncio
async def test_the_window_row_is_written_only_when_something_changes(fade_db,
                                                                     monkeypatch) -> None:
    calls: list[str] = []
    real = ledger.upsert_window

    async def counting(**kw: Any) -> None:
        calls.append(kw["window_slug"])
        await real(**kw)

    monkeypatch.setattr(ledger, "upsert_window", counting)
    memory = InputMemory()
    await run(scene(), memory=memory)
    await run(scene(), now=NOW + 10, memory=memory)
    assert len(calls) == len(ASSETS)


def test_memory_forgets_old_windows() -> None:
    memory = InputMemory()
    memory.seen("old", END)
    memory.start_refs["old"] = (1.0, "x")
    memory.saved["old"] = ()
    memory.db_checked.add("old")
    memory.prune(END + 3601)
    assert not memory.start_refs and not memory.saved and not memory.db_checked


# --------------------------------------------------------------------------- settlement (2026-09-22)
# A 15m window settles Up iff the TWAP-60s print at the close >= the print at the open, and
# Gamma's priceToBeat is that opening print.


def gamma_event(slug: str, price_to_beat: float | None) -> dict[str, Any]:
    meta = {} if price_to_beat is None else {"priceToBeat": price_to_beat}
    return {"slug": slug, "eventMetadata": meta}


def gamma_asks(venue: FakeVenue) -> list[httpx.Request]:
    return [r for r in venue.requests if r.url.host == "gamma-api.polymarket.com"]


@pytest.mark.asyncio
async def test_price_to_beat_from_gamma_when_the_open_print_is_gone(fade_db) -> None:
    slug = f"btc-updown-15m-{S0}"
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = tuple(
        p for p in history("btc") if p.obs_ms >= (S0 + 60) * 1000)  # restarted after the open
    venue = FakeVenue(gamma={slug: gamma_event(slug, twap("btc", S0))})
    got = await run(hub, venue)
    btc = got["btc"]
    # A restart after the open trades on once Gamma gives the opening print: the hole in the
    # window's average so far is information only.
    assert isinstance(btc, Inputs), btc
    assert btc.start_ref == pytest.approx(twap("btc", S0))
    assert btc.start_ref_source.startswith(fi.GAMMA_PTB)
    (asked,) = gamma_asks(venue)
    assert asked.url.path == "/events" and asked.url.params["slug"] == slug
    row = await ledger.get_window(slug)
    assert row["start_ref_price"] == pytest.approx(twap("btc", S0))
    assert row["start_ref_source"].startswith(fi.GAMMA_PTB)


@pytest.mark.asyncio
async def test_a_stand_in_is_a_named_fallback_until_gamma_publishes(fade_db) -> None:
    slug = f"btc-updown-15m-{S0}"
    hub = scene()
    hub.history[(fi.TWAP60, "btc")] = history("btc", skip={S0, *GAP})
    venue = FakeVenue(gamma={slug: gamma_event(slug, None)})  # not published yet
    memory = InputMemory()
    btc = (await run(hub, venue, memory=memory))["btc"]
    assert isinstance(btc, Inputs)
    assert btc.start_ref == pytest.approx(twap("btc", S0 - 1))
    assert "fallback: Gamma has no priceToBeat for this window yet" in btc.start_ref_source
    assert (await ledger.get_window(slug))["start_ref_price"] is None  # a stand-in is not stored

    venue.requests.clear()  # asked at most once per GAMMA_RETRY_S
    btc = (await run(hub, venue, memory=memory))["btc"]
    assert not gamma_asks(venue) and "asked again in" in btc.start_ref_source

    venue.gamma[slug] = gamma_event(slug, twap("btc", S0))  # published
    memory.ptb_retry_at[slug] = NOW - 1
    btc = (await run(hub, venue, memory=memory))["btc"]
    assert btc.start_ref == pytest.approx(twap("btc", S0))
    assert btc.start_ref_source.startswith(fi.GAMMA_PTB)
    assert (await ledger.get_window(slug))["start_ref_price"] == pytest.approx(twap("btc", S0))


@pytest.mark.asyncio
async def test_the_open_print_itself_needs_no_gamma_lookup(fade_db) -> None:
    venue = FakeVenue()
    btc = (await run(scene(), venue))["btc"]
    assert btc.start_ref == pytest.approx(twap("btc", S0))
    assert not gamma_asks(venue)


def late_scene(now: float) -> FakeHub:
    hub = scene()
    for a in ASSETS:
        hub.history[(fi.TWAP60, a)] = history(a, last=int(now) - 2)
        hub.history[(fi.CHAINLINK, a)] = history(a, fi.CHAINLINK, first=int(now) - 120,
                                                 last=int(now) - 2)
        hub.history[(fi.BINANCE, a)] = history(a, fi.BINANCE, first=int(now) - 60,
                                               last=int(now))
    return hub


@pytest.mark.asyncio
async def test_the_known_part_of_the_closing_average(fade_db) -> None:
    early = (await run(scene()))["btc"]
    assert early.close_avg is None and early.close_abar is None
    assert early.as_record()["close_avg"] is None

    now = END - 5.4
    hub = late_scene(now)
    hub.history[(fi.CHAINLINK, "eth")] = history("eth", fi.CHAINLINK, first=END - 20,
                                                 last=int(now) - 2)
    got = await run(hub, FakeVenue(now=now), now=now)
    btc = got["btc"]
    s0, last = END - 60, int(now) - 2
    values = [raw("btc", s) for s in range(s0, last + 1)]
    assert btc.close_avg.value == pytest.approx(sum(values) / len(values), rel=1e-12)
    assert btc.close_avg.through_s == last and btc.close_avg.seconds == last - s0 + 1
    assert btc.close_abar == pytest.approx(
        sum(math.log(v) for v in values) / len(values) - math.log(btc.start_ref))
    assert btc.as_record()["close_avg"]["seconds"] == last - s0 + 1
    assert btc.twap60.value == pytest.approx(twap("btc", last))  # the current TWAP-60s print
    # Chainlink prints only from 40 s into the closing minute: its average is not known.
    assert "average_incomplete" in got["eth"].codes
