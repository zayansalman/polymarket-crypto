"""Market universe: window slugs and bounds, next windows, token capture, Gamma lookups."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import config as _config
from polymarket_exec.marketdata import clob_messages as cm
from polymarket_exec.marketdata import universe as uv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "marketdata"
HOUR = 3600.0


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _ts(*args: int) -> float:
    return _utc(*args).timestamp()


# --- window math ------------------------------------------------------------------


def test_five_and_fifteen_minute_bounds_come_from_the_slug() -> None:
    at = _utc(2026, 9, 16, 12, 3, 20)
    slug, start, end = uv.window_bounds("btc", "5m", at)
    assert (slug, start, end) == (
        f"btc-updown-5m-{int(_ts(2026, 9, 16, 12, 0))}", _ts(2026, 9, 16, 12, 0),
        _ts(2026, 9, 16, 12, 5))
    slug, start, end = uv.window_bounds("eth", "15m", at)
    assert slug == f"eth-updown-15m-{int(_ts(2026, 9, 16, 12, 0))}"
    assert end - start == 900
    assert uv.next_window("eth", "15m", at)[1:] == (_ts(2026, 9, 16, 12, 15),
                                                     _ts(2026, 9, 16, 12, 30))


def test_next_five_minute_window_follows_on() -> None:
    at = _utc(2026, 9, 17, 10, 24, 59)
    current = uv.window_bounds("doge", "5m", at)
    nxt = uv.next_window("doge", "5m", at)
    assert current[0] == "doge-updown-5m-1789640400"
    assert nxt == ("doge-updown-5m-1789640700", 1_789_640_700.0, 1_789_641_000.0)


def test_hour_window_is_the_eastern_hour() -> None:
    slug, start, end = uv.window_bounds("btc", "1h", _utc(2026, 9, 16, 12, 30))
    assert slug == "bitcoin-up-or-down-september-16-2026-8am-et"
    assert (start, end) == (_ts(2026, 9, 16, 12), _ts(2026, 9, 16, 13))
    assert uv.next_window("sol", "1h", _utc(2026, 9, 16, 12, 30))[0] == (
        "solana-up-or-down-september-16-2026-9am-et")


def test_hour_windows_across_the_spring_forward() -> None:
    at = _utc(2026, 3, 8, 6, 30)  # 01:30 EST; at 02:00 the clock jumps to 03:00 EDT
    assert uv.window_bounds("btc", "1h", at) == (
        "bitcoin-up-or-down-march-8-2026-1am-et", _ts(2026, 3, 8, 6), _ts(2026, 3, 8, 7))
    assert uv.next_window("btc", "1h", at) == (
        "bitcoin-up-or-down-march-8-2026-3am-et", _ts(2026, 3, 8, 7), _ts(2026, 3, 8, 8))


def test_hour_windows_across_the_fall_back() -> None:
    first = uv.window_bounds("btc", "1h", _utc(2026, 11, 1, 5, 30))  # 01:30 EDT
    second = uv.window_bounds("btc", "1h", _utc(2026, 11, 1, 6, 30))  # 01:30 EST
    assert first == ("bitcoin-up-or-down-november-1-2026-1am-et",
                     _ts(2026, 11, 1, 5), _ts(2026, 11, 1, 6))
    assert second == ("bitcoin-up-or-down-november-1-2026-1am-et",
                      _ts(2026, 11, 1, 6), _ts(2026, 11, 1, 7))
    assert uv.next_window("btc", "1h", _utc(2026, 11, 1, 6, 30)) == (
        "bitcoin-up-or-down-november-1-2026-2am-et", _ts(2026, 11, 1, 7), _ts(2026, 11, 1, 8))


def test_day_window_runs_noon_to_noon_eastern() -> None:
    morning = uv.window_bounds("btc", "1d", _utc(2026, 9, 16, 15))  # 11:00 EDT
    assert morning == ("bitcoin-up-or-down-on-september-16-2026",
                       _ts(2026, 9, 15, 16), _ts(2026, 9, 16, 16))
    afternoon = uv.window_bounds("btc", "1d", _utc(2026, 9, 16, 17))  # 13:00 EDT
    assert afternoon == ("bitcoin-up-or-down-on-september-17-2026",
                         _ts(2026, 9, 16, 16), _ts(2026, 9, 17, 16))
    assert uv.next_window("btc", "1d", _utc(2026, 9, 16, 15)) == afternoon


def test_day_windows_on_the_dst_change_days() -> None:
    spring = uv.window_bounds("eth", "1d", _utc(2026, 3, 8, 10))
    assert spring == ("ethereum-up-or-down-on-march-8-2026",
                      _ts(2026, 3, 7, 17), _ts(2026, 3, 8, 16))  # 23 hours
    fall = uv.window_bounds("eth", "1d", _utc(2026, 11, 1, 10))
    assert fall == ("ethereum-up-or-down-on-november-1-2026",
                    _ts(2026, 10, 31, 16), _ts(2026, 11, 1, 17))  # 25 hours
    assert uv.window_bounds("eth", "1d", _utc(2026, 3, 7, 18)) == spring  # 1pm EST, Mar 7
    assert uv.next_window("eth", "1d", _utc(2026, 3, 7, 10)) == spring  # 5am EST, Mar 7


def test_unknown_timeframe_is_an_error() -> None:
    with pytest.raises(ValueError):
        uv.window_bounds("btc", "4h", _utc(2026, 9, 16))


# --- universe -----------------------------------------------------------------------


def _tokens_for(slug: str) -> tuple[str, str]:
    return f"{slug}:up", f"{slug}:down"


class Gamma:
    """A fake Gamma /markets endpoint; outcomes listed Down-first to check the alignment."""

    def __init__(self, missing: set[str] | None = None, status: int = 200,
                 delay_s: float = 0.0) -> None:
        self.calls: list[str] = []
        self.missing = missing or set()
        self.status = status
        self.delay_s = delay_s
        self.active = 0
        self.max_active = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(f"{_config.POLYMARKET_GAMMA_API}/markets?")
        slug = request.url.params["slug"]
        self.calls.append(slug)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
        finally:
            self.active -= 1
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "busy"})
        if slug in self.missing:
            return httpx.Response(200, json=[])
        up, down = _tokens_for(slug)
        return httpx.Response(200, json=[{
            "slug": slug, "conditionId": f"cond:{slug}",
            "outcomes": json.dumps(["Down", "Up"]), "clobTokenIds": json.dumps([down, up]),
        }])


def _universe(gamma: Gamma, clock: dict, **kw) -> uv.MarketUniverse:
    kw.setdefault("assets", ("btc",))
    kw.setdefault("timeframes", ("5m",))
    return uv.MarketUniverse(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(gamma)),
        time_fn=lambda: clock["t"], **kw,
    )


T0 = 1_789_640_410.0  # 10 s into doge-updown-5m-1789640400 (and the same btc window)


@pytest.mark.asyncio
async def test_refresh_follows_current_and_next_windows_with_one_lookup_each() -> None:
    gamma = Gamma()
    clock = {"t": T0}
    universe = _universe(gamma, clock)
    try:
        update = await universe.refresh()
        current = universe.market("btc", "5m")
        nxt = universe.market("btc", "5m", "next")
        assert current == uv.MarketRef(
            "btc", "5m", "btc-updown-5m-1789640400", 1_789_640_400.0, 1_789_640_700.0,
            "btc-updown-5m-1789640400:up", "btc-updown-5m-1789640400:down",
            "cond:btc-updown-5m-1789640400")
        assert nxt is not None and nxt.slug == "btc-updown-5m-1789640700"
        assert update.tokens == frozenset({current.up_token, current.down_token,
                                           nxt.up_token, nxt.down_token})
        assert update.opened == (current,)
        assert update.groups == {("btc", "5m"): update.tokens}
        clock["t"] += 2
        again = await universe.refresh()
        assert again.tokens == update.tokens and again.opened == ()
        assert sorted(gamma.calls) == ["btc-updown-5m-1789640400", "btc-updown-5m-1789640700"]
        assert universe.window_for_token(nxt.down_token) == (nxt, "down")
        assert universe.window_for_token("nope") is None
        st = universe.status()
        assert (st.markets, st.tokens, st.lookups, st.lookup_errors) == (2, 4, 2, 0)
    finally:
        await universe.aclose()


@pytest.mark.asyncio
async def test_new_market_announcements_save_the_lookup() -> None:
    gamma = Gamma()
    clock = {"t": T0}
    universe = _universe(gamma, clock, assets=("doge",))
    event = cm.parse_frame((FIXTURES / "clob_new_market.json").read_text())[0]
    assert isinstance(event, cm.NewMarketEvent)
    universe.observe(event)
    universe.observe(uv_event(event, slug="hype-updown-5m-1789640400"))  # not tracked
    universe.observe(uv_event(event, slug="some-sports-market"))
    try:
        await universe.refresh()
    finally:
        await universe.aclose()
    current = universe.market("doge", "5m")
    assert current is not None
    assert current.up_token == event.token_ids[0] and current.down_token == event.token_ids[1]
    assert current.condition_id == event.condition_id
    assert gamma.calls == ["doge-updown-5m-1789640700"]  # only the unannounced next window
    assert universe.status().announced == 1


def uv_event(event: cm.NewMarketEvent, *, slug: str) -> cm.NewMarketEvent:
    return cm.NewMarketEvent(event.market, slug, event.question, event.outcomes,
                             event.token_ids, event.condition_id, event.active,
                             event.tick_size, event.fee_rate, event.ts_ms)


def test_announcements_are_aligned_to_outcomes_and_capped() -> None:
    universe = uv.MarketUniverse(assets=("btc",), timeframes=("5m",), announced_cap=3)
    for i in range(5):
        universe.observe(cm.NewMarketEvent(
            "m", f"btc-updown-5m-{1_000 + 300 * i}", "q", ("Down", "Up"), ("d", "u"), "c",
            False, 0.01, None, 0))
    assert universe.status().announced == 3
    assert universe.known_tokens("btc-updown-5m-2200") == ("u", "d", "c")
    assert universe.known_tokens("btc-updown-5m-1000") is None  # the oldest was dropped


@pytest.mark.asyncio
async def test_missing_markets_are_retried_no_more_than_every_30_seconds() -> None:
    gamma = Gamma(missing={"btc-updown-5m-1789640700"})
    clock = {"t": T0}
    universe = _universe(gamma, clock)
    try:
        await universe.refresh()
        assert universe.market("btc", "5m", "next") is None
        for step in (2, 27):  # T0+2, T0+29
            clock["t"] += step
            await universe.refresh()
        assert gamma.calls.count("btc-updown-5m-1789640700") == 1
        clock["t"] = T0 + 30
        gamma.missing.clear()
        update = await universe.refresh()
        assert gamma.calls.count("btc-updown-5m-1789640700") == 2
        assert universe.market("btc", "5m", "next") is not None and len(update.tokens) == 4
        st = universe.status()
        assert (st.lookups, st.misses, st.lookup_errors) == (3, 1, 0)
    finally:
        await universe.aclose()


@pytest.mark.asyncio
async def test_lookup_errors_are_counted_and_retried_later() -> None:
    gamma = Gamma(status=503)
    clock = {"t": T0}
    universe = _universe(gamma, clock)
    try:
        update = await universe.refresh()
        assert update.tokens == frozenset() and universe.market("btc", "5m") is None
        st = universe.status()
        assert st.lookup_errors == 2 and "503" in (st.last_error or "")
        clock["t"] += 10
        await universe.refresh()
        assert len(gamma.calls) == 2
        gamma.status = 200
        clock["t"] = T0 + 30
        update = await universe.refresh()
        assert len(update.tokens) == 4 and len(gamma.calls) == 4
    finally:
        await universe.aclose()


@pytest.mark.asyncio
async def test_lookups_run_at_most_four_at_a_time() -> None:
    gamma = Gamma(delay_s=0.01)
    clock = {"t": T0}
    universe = _universe(gamma, clock, assets=uv.ASSETS, timeframes=uv.TIMEFRAMES)
    try:
        update = await universe.refresh()
    finally:
        await universe.aclose()
    assert len(gamma.calls) == 6 * 4 * 2
    assert gamma.max_active == 4
    assert len(update.tokens) == 6 * 4 * 2 * 2
    assert len(update.opened) == 24
    assert len(update.groups) == 24 and universe.grid == tuple(
        (a, tf) for a in uv.ASSETS for tf in uv.TIMEFRAMES)
    assert all(len(tokens) == 4 for tokens in update.groups.values())
    assert frozenset().union(*update.groups.values()) == update.tokens
    btc_hour = update.groups[("btc", "1h")]
    assert universe.market("btc", "1h").up_token in btc_hour
    assert universe.market("btc", "1h", "next").down_token in btc_hour


@pytest.mark.asyncio
async def test_ended_windows_wait_for_resolution_then_drop() -> None:
    gamma = Gamma()
    clock = {"t": T0}
    universe = _universe(gamma, clock)
    try:
        first = (await universe.refresh()).tokens
        old = universe.market("btc", "5m")
        assert old is not None
        clock["t"] = old.window_end + 10  # rolled over: old, new current and new next
        rolled = await universe.refresh()
        assert len(rolled.tokens) == 6 and first <= rolled.tokens
        assert [r.slug for r in rolled.opened] == ["btc-updown-5m-1789640700"]
        clock["t"] = old.window_end + 120  # past the 30 s grace, not resolved yet: kept
        assert old.up_token in (await universe.refresh()).tokens
        assert universe.mark_resolved(old.down_token) == old
        assert universe.mark_resolved("unknown-token") is None
        assert universe.tokens() == rolled.tokens - {old.up_token, old.down_token}
        assert universe.groups() == {("btc", "5m"): universe.tokens()}
        dropped = await universe.refresh()
        assert old.up_token not in dropped.tokens and len(dropped.tokens) == 4
        # An unresolved window is let go once the resolution wait is over.
        nxt = universe.market("btc", "5m", "next")
        clock["t"] = nxt.window_end + 300
        late = await universe.refresh()
        assert nxt.up_token in late.tokens
        clock["t"] = nxt.window_end + 301
        assert nxt.up_token not in (await universe.refresh()).tokens
    finally:
        await universe.aclose()


@pytest.mark.asyncio
async def test_without_a_resolution_wait_ended_windows_go_after_30_seconds() -> None:
    gamma = Gamma()
    clock = {"t": T0}
    universe = _universe(gamma, clock, await_resolution_s=0.0)
    try:
        await universe.refresh()
        old = universe.market("btc", "5m")
        clock["t"] = old.window_end + 30
        assert old.up_token in (await universe.refresh()).tokens
        clock["t"] = old.window_end + 31
        assert old.up_token not in (await universe.refresh()).tokens
    finally:
        await universe.aclose()


@pytest.mark.asyncio
async def test_an_asset_without_hourly_markets_is_skipped() -> None:
    gamma = Gamma()
    clock = {"t": T0}
    universe = _universe(gamma, clock, assets=("hype", "btc"), timeframes=("1h", "5m"))
    try:
        update = await universe.refresh()
    finally:
        await universe.aclose()
    assert universe.market("btc", "1h") is not None and universe.market("hype", "5m") is not None
    assert universe.market("hype", "1h") is None
    assert len(update.tokens) == 3 * 2 * 2
    assert "hype" in (universe.status().last_error or "")


def test_default_grid() -> None:
    universe = uv.MarketUniverse()
    assert universe.assets == ("btc", "eth", "sol", "xrp", "doge", "bnb")
    assert universe.timeframes == ("5m", "15m", "1h", "1d")
