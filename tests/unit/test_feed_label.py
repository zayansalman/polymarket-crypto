"""Unit tests for the status-panel feed label (issue #151).

``_feed_label`` renders the per-component ``feed_source`` provenance string
(``spot=…;ref=…;vol=…;quotes=…``) into a human-readable line for the dashboard
detail. The bug it fixes: the old hardcoded "Binance public fallback while
Chainlink Streams access is pending" line misstated the settlement story —
spot and reference resolve on Chainlink (Polymarket's settlement feed), and
only the volatility SHAPE ever falls back to Binance.

Pure function, no DB — but still parametrized on the exact ``feed_source``
strings the loop journals in production (verified against the live ledger).
"""
from __future__ import annotations

from polymarket_bot.paper import _feed_label, _parse_feed_source


class TestParseFeedSource:
    def test_parses_all_components(self) -> None:
        parts = _parse_feed_source(
            "spot=chainlink_ws;ref=chainlink_rest;vol=binance_shape_fallback;quotes=clob"
        )
        assert parts == {
            "spot": "chainlink_ws",
            "ref": "chainlink_rest",
            "vol": "binance_shape_fallback",
            "quotes": "clob",
        }

    def test_lenient_on_garbage(self) -> None:
        assert _parse_feed_source("garbage") == {}
        assert _parse_feed_source(None) == {}
        assert _parse_feed_source(123) == {}  # type: ignore[arg-type]


class TestFeedLabel:
    def test_fully_chainlink_is_settlement_aligned(self) -> None:
        """The healthy case the old label wrongly called 'Binance fallback'."""
        out = _feed_label(
            "spot=chainlink_ws;ref=chainlink_rest;vol=chainlink_ws;quotes=clob"
        )
        assert "spot Chainlink WS" in out
        assert "ref Chainlink REST" in out
        assert "(settlement-aligned)" in out
        # The misleading legacy string must be gone.
        assert "Binance public fallback" not in out

    def test_binance_vol_shape_is_still_settlement_aligned(self) -> None:
        """Vol shape on Binance does NOT break settlement — spot/ref are Chainlink."""
        out = _feed_label(
            "spot=chainlink_rest_poll;ref=chainlink_rest;"
            "vol=binance_shape_fallback;quotes=clob"
        )
        assert "spot Chainlink REST-poll" in out
        assert "vol Binance (vol shape)" in out
        assert "(settlement-aligned)" in out  # the key correction

    def test_spot_off_chainlink_warns(self) -> None:
        """If spot leaves Chainlink, that IS a settlement risk — flag it."""
        out = _feed_label(
            "spot=unavailable;ref=unavailable;vol=binance_shape_fallback;quotes=clob"
        )
        assert "settlement risk" in out
        assert "settlement-aligned" not in out

    def test_unknown_token_passes_through_verbatim(self) -> None:
        """A source token with no friendly label is shown as-is, not dropped."""
        out = _feed_label("spot=some_new_feed;ref=chainlink_rest;vol=x;quotes=clob")
        assert "spot some_new_feed" in out

    def test_empty_or_garbage_source_is_safe(self) -> None:
        assert _feed_label("garbage") == "Feed: source unavailable"
        assert _feed_label("") == "Feed: source unavailable"

    def test_missing_quotes_component_omitted(self) -> None:
        out = _feed_label("spot=chainlink_ws;ref=chainlink_rest;vol=chainlink_ws")
        assert "spot Chainlink WS" in out
        assert "quotes" not in out
