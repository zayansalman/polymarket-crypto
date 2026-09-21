"""Unit tests for the shared domain model (polymarket_exec.core.model)."""

from __future__ import annotations

from polymarket_exec.core.model import Fill, Market, Position, Quote, Side


# ============================================================================
# Side
# ============================================================================


class TestSide:
    def test_values_match_the_strings_already_used_everywhere(self) -> None:
        """The enum types an existing convention; it must not silently rename it."""
        assert Side.UP.value == "Up"
        assert Side.DOWN.value == "Down"

    def test_is_a_str_so_it_compares_equal_to_the_raw_string(self) -> None:
        """paper.py, maker and daily all compare against bare "Up"/"Down"
        strings — Side must interoperate with that unmigrated code, not just
        with itself."""
        assert Side.UP == "Up"
        assert Side.DOWN == "Down"
        assert Side.UP != Side.DOWN


# ============================================================================
# Quote
# ============================================================================


class TestQuote:
    def test_empty_quote_is_not_crossed_and_not_buyable(self) -> None:
        q = Quote()
        assert q.crossed is False
        assert q.buyable is False

    def test_normal_book_is_buyable(self) -> None:
        q = Quote(best_bid=0.48, best_ask=0.52, bid_size=100.0, ask_size=80.0)
        assert q.crossed is False
        assert q.buyable is True

    def test_bid_above_ask_is_crossed_and_not_buyable(self) -> None:
        """A crossed book is a malformed or momentarily inconsistent snapshot —
        never something to buy into."""
        q = Quote(best_bid=0.60, best_ask=0.55)
        assert q.crossed is True
        assert q.buyable is False

    def test_bid_equal_to_ask_is_not_crossed(self) -> None:
        """Equality is a locked, not crossed, book — crossed means strictly
        bid > ask."""
        q = Quote(best_bid=0.50, best_ask=0.50)
        assert q.crossed is False
        assert q.buyable is True

    def test_no_ask_is_not_buyable_even_with_a_bid(self) -> None:
        q = Quote(best_bid=0.40)
        assert q.buyable is False


# ============================================================================
# Fill
# ============================================================================


class TestFill:
    def test_notional_is_price_times_size(self) -> None:
        f = Fill(market_slug="btc-updown-15m-1", side=Side.UP, price_usd=0.55, size_shares=10.0)
        assert f.notional_usd == 5.5

    def test_cost_adds_the_fee_on_top_of_notional(self) -> None:
        """cost_usd is what actually left the account — notional plus fee,
        never fee-inclusive-in-price or any other blending."""
        f = Fill(
            market_slug="btc-updown-15m-1", side=Side.UP,
            price_usd=0.55, size_shares=10.0, fee_usd=0.20,
        )
        assert f.notional_usd == 5.5
        assert f.cost_usd == 5.7

    def test_zero_fee_by_default(self) -> None:
        """A resting maker fill pays no fee — fee_usd defaults to 0, not None,
        so cost_usd never needs a null check."""
        f = Fill(market_slug="s", side=Side.DOWN, price_usd=0.30, size_shares=5.0)
        assert f.fee_usd == 0.0
        assert f.cost_usd == f.notional_usd


# ============================================================================
# Position
# ============================================================================


class TestPosition:
    def test_open_by_default(self) -> None:
        p = Position(
            market_slug="btc-updown-15m-1", side=Side.UP,
            entry_price_usd=0.55, size_shares=10.0, opened_at="2026-09-21T00:00:00Z",
        )
        assert p.is_open is True
        assert p.state == "open"
        assert p.closed_at is None
        assert p.realized_pnl_usd is None
        assert p.id is None
        assert p.mode is None
        assert p.exit_reason is None

    def test_notional_is_entry_price_times_size(self) -> None:
        p = Position(
            market_slug="btc-updown-15m-1", side=Side.DOWN,
            entry_price_usd=0.40, size_shares=25.0, opened_at="2026-09-21T00:00:00Z",
        )
        assert p.notional_usd == 10.0

    def test_closed_position_is_not_open(self) -> None:
        p = Position(
            market_slug="btc-updown-15m-1", side=Side.UP,
            entry_price_usd=0.55, size_shares=10.0, opened_at="2026-09-21T00:00:00Z",
            state="closed", exit_price_usd=0.90, closed_at="2026-09-21T00:15:00Z",
            realized_pnl_usd=3.5,
        )
        assert p.is_open is False
        assert p.realized_pnl_usd == 3.5


# ============================================================================
# Market
# ============================================================================


class TestMarket:
    def test_optional_fields_default_to_unset(self) -> None:
        """Only slug/condition_id/tokens are required — question and the
        window bounds are display/derivation conveniences, not identity."""
        m = Market(
            slug="btc-updown-15m-1789934400", condition_id="0xabc",
            up_token_id="111", down_token_id="222",
        )
        assert m.question == ""
        assert m.start_ts is None
        assert m.end_ts is None

    def test_is_frozen(self) -> None:
        """Market identity does not change after construction — a strategy
        that wants a different window builds a new Market, it does not
        mutate one in place."""
        m = Market(slug="s", condition_id="c", up_token_id="u", down_token_id="d")
        try:
            m.slug = "other"  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("Market must be frozen")
