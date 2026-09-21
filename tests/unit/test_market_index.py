"""Tests for the token-id -> market resolver.

The daily scanner only carries a Gamma market row; parse_market needs a
slug, outcome, and conditionId out of it. These tests pin the parsing so a
malformed or reordered Gamma row fails loudly instead of mis-resolving a
market to the wrong outcome.
"""

from __future__ import annotations

import json

import pytest

from polymarket_bot.pairarb.market_index import MarketTokens, parse_market


def _market(slug="btc-updown-5m-1786705500", outcomes=("Up", "Down"),
            tokens=("111", "222"), condition_id="0xabc"):
    return {
        "slug": slug,
        "conditionId": condition_id,
        "outcomes": json.dumps(list(outcomes)),
        "clobTokenIds": json.dumps(list(tokens)),
    }


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
