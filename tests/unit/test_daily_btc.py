"""Daily BTC Up/Down market helpers and the fade-the-3-day-direction strategy."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from polymarket_bot.daily_btc import fade_three_day as fade
from polymarket_bot.daily_btc import market as dm


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())


def test_window_uses_new_york_noon_across_dst():
    summer = dm.window_for(date(2026, 9, 15))
    assert (summer.reference_ts, summer.settle_ts) == (_ts("2026-09-14T16:00"), _ts("2026-09-15T16:00"))
    spring_forward = dm.window_for(date(2026, 3, 8))
    assert spring_forward.reference_ts == _ts("2026-03-07T17:00")
    assert spring_forward.settle_ts - spring_forward.reference_ts == 23 * 3600


def test_next_window_is_the_market_whose_reference_noon_comes_next():
    before_noon = dm.next_window(_ts("2026-09-14T15:55"))  # 11:55 ET
    assert before_noon.market_date == date(2026, 9, 15)
    after_noon = dm.next_window(_ts("2026-09-14T16:30"))  # 12:30 ET
    assert after_noon.market_date == date(2026, 9, 16)


def test_outcome_tie_is_its_own_result():
    assert dm.outcome(100.0, 101.0) == "Up"
    assert dm.outcome(100.0, 99.0) == "Down"
    assert dm.outcome(100.0, 100.0) == "tie"


def _row(window: dm.DayWindow, **overrides) -> dict:
    row = {
        "slug": "bitcoin-up-or-down-on-september-15-2026",
        "question": "Bitcoin Up or Down on September 15?",
        "eventStartTime": datetime.fromtimestamp(window.reference_ts, UTC).isoformat(),
        "endDate": datetime.fromtimestamp(window.settle_ts, UTC).isoformat(),
        "outcomes": json.dumps(["Down", "Up"]),
        "clobTokenIds": json.dumps(["down-token", "up-token"]),
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True},
    }
    row.update(overrides)
    return row


def test_parse_market_matches_window_tokens_and_fees():
    window = dm.window_for(date(2026, 9, 15))
    market = dm.parse_market(_row(window), window)
    assert (market.up_token_id, market.down_token_id) == ("up-token", "down-token")
    assert (market.fee_rate, market.fee_exponent) == (0.07, 1.0)
    assert dm.parse_market(_row(window, feesEnabled=False), window).fee_rate == 0.0
    assert dm.parse_market(_row(window, eventStartTime="2026-09-13T16:00:00Z"), window) is None
    assert dm.parse_market(_row(window, feeSchedule=None), window) is None


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Client:
    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        return _Resp(self.routes[url.rsplit("/", 1)[-1]])


@pytest.mark.asyncio
async def test_discover_by_slug_then_series_fallback():
    window = dm.window_for(date(2026, 9, 15))
    hit = _Client({"markets": [_row(window)], "events": []})
    market = await dm.discover(hit, window)
    assert market.slug == "bitcoin-up-or-down-on-september-15-2026"
    assert hit.calls[0][1]["slug"] == "bitcoin-up-or-down-on-september-15-2026"

    renamed = _row(window, slug="bitcoin-up-or-down-on-september-15")
    fallback = _Client({"markets": [], "events": [{"markets": [renamed]}]})
    assert (await dm.discover(fallback, window)).slug == "bitcoin-up-or-down-on-september-15"

    missing = _Client({"markets": [], "events": [{"markets": [_row(dm.window_for(date(2026, 9, 16)))]}]})
    assert await dm.discover(missing, window) is None


def _kline(open_s: int, close: float) -> list:
    return [open_s * 1000, "0", "0", "0", str(close), "1", open_s * 1000 + 59_999, "0", 0, "0", "0", "0"]


@pytest.mark.asyncio
async def test_minute_close_only_after_candle_closes():
    minute = _ts("2026-09-14T16:00")
    client = _Client({"klines": [_kline(minute, 115_000.5)]})
    assert await dm.fetch_minute_close(client, minute, now_ts=minute + 30) is None
    assert client.calls == []
    assert await dm.fetch_minute_close(client, minute, now_ts=minute + 61) == 115_000.5
    assert await dm.fetch_minute_close(client, minute + 7, now_ts=minute + 600) is None
    wrong = _Client({"klines": [_kline(minute - 60, 1.0)]})
    assert await dm.fetch_minute_close(wrong, minute, now_ts=minute + 61) is None


@pytest.mark.asyncio
async def test_price_at_reads_the_candle_ending_at_that_instant():
    t_dec = _ts("2026-09-14T15:55")
    client = _Client({"klines": [_kline(t_dec - 60, 116_000.0)]})
    assert await dm.fetch_price_at(client, t_dec, now_ts=t_dec + 1) == 116_000.0
    assert client.calls[0][1]["startTime"] == (t_dec - 60) * 1000


def test_fade_bets_against_the_three_day_move():
    up = fade.decide(110.0, 100.0)
    assert up.side == "Down" and up.return_3d == pytest.approx(0.10)
    assert fade.decide(90.0, 100.0).side == "Up"
    assert fade.decide(100.0, 100.0).side is None
    assert fade.decide(None, 100.0).side is None
    assert fade.decide(100.0, 0.0).side is None
    assert (fade.LOOKBACK_S, fade.DECISION_LEAD_S) == (259_200, 300)


def _utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp())


def test_current_window_is_the_latest_noon_at_or_before_now() -> None:
    # 2026-09-16 12:00 EDT = 16:00 UTC. Market dated Sep 17 covers Sep 16 noon -> Sep 17 noon.
    at_noon = dm.current_window(_utc(2026, 9, 16, 16))
    assert at_noon.market_date == date(2026, 9, 17)
    assert at_noon.reference_ts == _utc(2026, 9, 16, 16)
    before = dm.current_window(_utc(2026, 9, 16, 15, 59, 59))
    assert before.market_date == date(2026, 9, 16)
    winter = dm.current_window(_utc(2026, 1, 15, 17, 0, 5))  # 12:00 EST = 17:00 UTC
    assert winter.reference_ts == _utc(2026, 1, 15, 17)


def test_current_window_across_fall_back_is_25_hours() -> None:
    w = dm.current_window(_utc(2026, 10, 31, 16, 0, 30))
    assert w.reference_ts == _utc(2026, 10, 31, 16)   # noon EDT
    assert w.settle_ts == _utc(2026, 11, 1, 17)       # noon EST
    assert w.settle_ts - w.reference_ts == 25 * 3600


def test_payout_pays_half_on_a_tie() -> None:
    assert dm.payout("Up", "Up") == 1.0
    assert dm.payout("Down", "Up") == 0.0
    assert dm.payout("Down", "tie") == 0.5
