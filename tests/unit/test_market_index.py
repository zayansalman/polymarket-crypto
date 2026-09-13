"""Tests for the token-id -> market resolver (#182).

The on-chain feed only carries a token_id; ``price_the_copy`` needs a slug,
outcome, and conditionId. These tests pin the parsing and caching so a
malformed or reordered Gamma row fails loudly instead of mis-resolving a fill
to the wrong outcome.
"""

from __future__ import annotations

import json

import pytest

from polymarket_bot.pairarb.market_index import (
    MarketTokens,
    TokenIndex,
    parse_market,
    window_slug,
)


def _market(slug="btc-updown-5m-1786705500", outcomes=("Up", "Down"),
            tokens=("111", "222"), condition_id="0xabc"):
    return {
        "slug": slug,
        "conditionId": condition_id,
        "outcomes": json.dumps(list(outcomes)),
        "clobTokenIds": json.dumps(list(tokens)),
    }


def test_window_slug_is_clock_derived():
    assert window_slug("btc", 1786705500) == "btc-updown-5m-1786705500"
    # Anything inside the same 5-minute bucket floors to the same slug.
    assert window_slug("btc", 1786705799) == "btc-updown-5m-1786705500"
    assert window_slug("btc", 1786705800) == "btc-updown-5m-1786705800"


def test_parse_market_aligns_tokens_to_outcomes_up_first():
    mt = parse_market(_market(outcomes=("Up", "Down"), tokens=("up_tok", "down_tok")))
    assert mt == MarketTokens(
        slug="btc-updown-5m-1786705500",
        condition_id="0xabc",
        up_token="up_tok",
        down_token="down_tok",
    )


def test_parse_market_handles_down_first_ordering():
    """Outcome order is not guaranteed — must align by content, not position."""
    mt = parse_market(_market(outcomes=("Down", "Up"), tokens=("down_tok", "up_tok")))
    assert mt.up_token == "up_tok"
    assert mt.down_token == "down_tok"


@pytest.mark.parametrize(
    "market",
    [
        {},
        {"slug": "x", "outcomes": "not json", "clobTokenIds": "[]"},
        {"slug": "x", "outcomes": json.dumps(["Up"]), "clobTokenIds": json.dumps(["1", "2"])},
        {"slug": "", "outcomes": json.dumps(["Up", "Down"]), "clobTokenIds": json.dumps(["1", "2"])},
        _market(tokens=("", "222")),
    ],
)
def test_parse_market_rejects_malformed_rows(market):
    assert parse_market(market) is None


class _FakeResponse:
    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


class _FakeClient:
    """Records requested slugs, returns a canned market row per slug."""

    def __init__(self, rows_by_slug):
        self._rows_by_slug = rows_by_slug
        self.requested: list[str] = []

    async def get(self, url, params=None, timeout=None):
        slug = (params or {}).get("slug", "")
        self.requested.append(slug)
        rows = self._rows_by_slug.get(slug, [])
        return _FakeResponse(rows)


@pytest.mark.asyncio
async def test_refresh_indexes_current_and_previous_window():
    now = 1786705800  # exact boundary of a window
    cur = window_slug("btc", now)
    prev = window_slug("btc", now - 300)
    client = _FakeClient({
        cur: [_market(slug=cur, tokens=("cur_up", "cur_down"))],
        prev: [_market(slug=prev, tokens=("prev_up", "prev_down"))],
    })
    idx = TokenIndex(["btc"])
    await idx.refresh(client, now=now)

    assert idx.resolve("cur_up") == (cur, "Up", "0xabc")
    assert idx.resolve("cur_down") == (cur, "Down", "0xabc")
    assert idx.resolve("prev_up") == (prev, "Up", "0xabc")
    assert sorted(client.requested) == sorted([cur, prev])


@pytest.mark.asyncio
async def test_refresh_skips_already_indexed_windows():
    now = 1786705800
    cur = window_slug("btc", now)
    client = _FakeClient({cur: [_market(slug=cur)]})
    idx = TokenIndex(["btc"])
    await idx.refresh(client, now=now)
    await idx.refresh(client, now=now + 1)  # same windows, no new fetch expected

    assert client.requested.count(cur) == 1


@pytest.mark.asyncio
async def test_refresh_tolerates_a_failing_asset():
    class _RaisingClient(_FakeClient):
        async def get(self, url, params=None, timeout=None):
            if (params or {}).get("slug", "").startswith("eth"):
                raise RuntimeError("network down")
            return await super().get(url, params=params, timeout=timeout)

    now = 1786705800
    btc_slug = window_slug("btc", now)
    client = _RaisingClient({btc_slug: [_market(slug=btc_slug)]})
    idx = TokenIndex(["eth", "btc"])
    await idx.refresh(client, now=now)  # must not raise

    assert idx.resolve("111") == (btc_slug, "Up", "0xabc")


def test_resolve_unknown_token_returns_none():
    assert TokenIndex(["btc"]).resolve("nope") is None
