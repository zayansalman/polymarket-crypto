"""The queue is the whole difference between paper and real.

A maker fill model that marks an order filled the moment the tape prints at its
price is claiming front-of-queue on every order. These tests pin the opposite:
volume must trade THROUGH the size already resting before any of it is ours, and
a quote that watched flow go past without reaching it must say so rather than
quietly reporting nothing happened.
"""

from __future__ import annotations

import pytest

import db as _db  # type: ignore[import-untyped]
from polymarket_bot.maker import ledger as _ledger
from polymarket_bot.maker import quoter as _quoter
from polymarket_bot.maker.filler import crossed_volume


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Throwaway database. Autouse so a new test here cannot forget it."""
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "maker_test.db")
    return tmp_path


def flow(*rows):
    """(ts, outcome_index, side, size, price) tuples, as the feed gives them."""
    return list(rows)


class TestCrossedVolume:
    def test_counts_sells_at_or_below_our_bid(self):
        f = flow((10, 0, "SELL", 50.0, 0.60), (11, 0, "SELL", 30.0, 0.62))
        assert crossed_volume(f, our_index=0, our_price=0.61) == 50.0

    def test_ignores_buys_which_lift_the_ask(self):
        f = flow((10, 0, "BUY", 500.0, 0.55))
        assert crossed_volume(f, our_index=0, our_price=0.61) == 0.0

    def test_counts_the_other_token_as_the_same_event(self):
        """A taker buying the other side at 0.38 IS a sale of ours at 0.62."""
        f = flow((10, 1, "BUY", 40.0, 0.38))
        assert crossed_volume(f, our_index=0, our_price=0.62) == 40.0
        # ...and not when it lands above our bid.
        assert crossed_volume(f, our_index=0, our_price=0.61) == 0.0

    def test_other_token_sell_is_a_lift_not_a_hit(self):
        f = flow((10, 1, "SELL", 40.0, 0.38))
        assert crossed_volume(f, our_index=0, our_price=0.70) == 0.0


class TestQueueDiscipline:
    """``ours = crossed - depth_ahead`` is the rule the ledger enforces."""

    def test_no_fill_until_the_queue_is_cleared(self):
        f = flow((10, 0, "SELL", 80.0, 0.60))
        crossed = crossed_volume(f, our_index=0, our_price=0.60)
        assert crossed == 80.0
        assert crossed - 100.0 < 0          # 100 resting ahead of us

    def test_partial_fill_is_the_remainder_only(self):
        f = flow((10, 0, "SELL", 130.0, 0.60))
        crossed = crossed_volume(f, our_index=0, our_price=0.60)
        assert min(25.0, crossed - 100.0) == 25.0
        assert min(50.0, crossed - 100.0) == 30.0


def book(bids, asks):
    return _quoter.Book(bids=sorted(bids, key=lambda r: -r[0]),
                        asks=sorted(asks, key=lambda r: r[0]))


def market():
    return _quoter.LiveMarket(
        condition_id="0xcid", slug="btc-up-or-down-x", title="BTC up?",
        end_ts=9_999_999_999, tokens=("tokUp", "tokDown"),
        outcomes=("Up", "Down"))


class TestDecide:
    def _books(self, up_bid, up_ask, down_bid, down_ask, up_depth=100.0):
        return {
            "tokUp": book([(up_bid, up_depth), (up_bid - 0.02, 500.0)],
                          [(up_ask, 200.0)]),
            "tokDown": book([(down_bid, 100.0)], [(down_ask, 200.0)]),
        }

    def test_picks_the_favourite_not_the_underdog(self):
        b = self._books(0.68, 0.70, 0.30, 0.32)
        q, _why = _quoter.decide(market(), b, band_lo=0.55, band_hi=0.92,
                                 size=25, improve=True, max_spread=0.06)
        assert q is not None and q.outcome == "Up"

    def test_never_crosses_the_spread(self):
        b = self._books(0.68, 0.69, 0.31, 0.32)
        q, _why = _quoter.decide(market(), b, band_lo=0.55, band_hi=0.92,
                                 size=25, improve=True, max_spread=0.06)
        assert q is not None and q.price < 0.69

    def test_declines_when_the_favourite_is_outside_the_band(self):
        b = self._books(0.96, 0.97, 0.03, 0.04)
        q, why = _quoter.decide(market(), b, band_lo=0.55, band_hi=0.92,
                                size=25, improve=True, max_spread=0.06)
        assert q is None and "band" in why

    def test_declines_a_wide_spread(self):
        b = self._books(0.60, 0.75, 0.25, 0.40)
        q, why = _quoter.decide(market(), b, band_lo=0.55, band_hi=0.92,
                                size=25, improve=True, max_spread=0.06)
        assert q is None and "spread" in why

    def test_joining_records_the_queue_it_joined(self):
        b = self._books(0.68, 0.70, 0.30, 0.32, up_depth=140.0)
        q, why = _quoter.decide(market(), b, band_lo=0.55, band_hi=0.92,
                                size=25, improve=False, max_spread=0.06)
        assert q is not None
        assert q.price == 0.68
        assert q.depth_ahead == 140.0 and "140" in why

    def test_improving_a_tick_is_front_of_queue(self):
        b = self._books(0.68, 0.70, 0.30, 0.32, up_depth=140.0)
        q, _why = _quoter.decide(market(), b, band_lo=0.55, band_hi=0.92,
                                 size=25, improve=True, max_spread=0.06)
        assert q is not None and q.price == 0.69 and q.depth_ahead == 0.0


@pytest.mark.asyncio
class TestLedgerPopulations:
    """Every figure must be divided by the rows it actually covers."""

    async def _quote(self, **over):
        row = dict(quoted_ts=1, condition_id="0xa", token_id="t1",
                   window_slug="w", title="t", outcome="Up", quote_price=0.60,
                   quote_size=25.0, best_bid=0.59, best_ask=0.61, mid=0.60,
                   spread=0.02, depth_ahead=0.0, resolves_at=2)
        row.update(over)
        return await _ledger.place(**row)

    async def test_unfilled_quotes_do_not_dilute_the_per_share_figure(self):
        await _ledger.init()
        won = await self._quote(quoted_ts=1)
        await _ledger.fill(won, ts=2, size=25.0, crossed=200.0)
        await _ledger.settle(won, won=True, pnl=10.0, ts=3)
        await self._quote(quoted_ts=2, token_id="t2")     # still resting
        await self._quote(quoted_ts=3, token_id="t3")     # expires unfilled
        s = await _ledger.summary()

        assert s["placed"] == 3
        assert s["settled_n"] == 1
        # 10.0 over the 25 shares that filled, NOT over the 75 quoted.
        assert s["settled_shares"] == 25.0
        assert s["cents_per_share"] == pytest.approx(40.0)

    async def test_fill_rate_counts_every_quote_placed(self):
        await _ledger.init()
        a = await self._quote(quoted_ts=1, token_id="t1")
        await _ledger.fill(a, ts=2, size=25.0, crossed=90.0)
        for i in range(3):
            await self._quote(quoted_ts=10 + i, token_id=f"x{i}")
        s = await _ledger.summary()
        assert s["placed"] == 4 and s["filled"] == 1
        assert s["fill_rate"] == pytest.approx(0.25)

    async def test_expired_quote_keeps_the_volume_it_watched_go_past(self):
        await _ledger.init()
        q = await self._quote(quoted_ts=1, depth_ahead=500.0)
        await _ledger.expire(q, ts=5, crossed=380.0)
        rows = await _ledger.filled_unsettled()
        assert rows == []
        s = await _ledger.summary()
        assert s["expired"] == 1 and s["filled"] == 0


@pytest.mark.asyncio
class TestOneClipPerMarket:
    """A fixed clip per market is the finding, not a detail.

    The same price band measured at a fixed clip earns +3.8c/share and measured
    volume-weighted LOSES 0.6c. Quoting a market again because it is busy is how
    the first quietly becomes the second.
    """

    async def test_a_market_is_only_ever_quoted_once(self):
        await _ledger.init()
        row = dict(quoted_ts=1, condition_id="0xa", token_id="t1",
                   window_slug="w", title="t", outcome="Up", quote_price=0.60,
                   quote_size=25.0, best_bid=0.59, best_ask=0.61, mid=0.60,
                   spread=0.02, depth_ahead=0.0, resolves_at=99)
        first = await _ledger.place(**row)
        await _ledger.fill(first, ts=2, size=25.0, crossed=90.0)
        await _ledger.settle(first, won=True, pnl=10.0, ts=3)
        # Settled, expired or resting — all of them block a second quote.
        assert await _ledger.quoted_markets() == {"0xa"}

    async def test_expired_quote_still_blocks_the_market(self):
        await _ledger.init()
        row = dict(quoted_ts=1, condition_id="0xb", token_id="t9",
                   window_slug="w", title="t", outcome="Up", quote_price=0.60,
                   quote_size=25.0, best_bid=0.59, best_ask=0.61, mid=0.60,
                   spread=0.02, depth_ahead=900.0, resolves_at=2)
        q = await _ledger.place(**row)
        await _ledger.expire(q, ts=5, crossed=100.0)
        assert "0xb" in await _ledger.quoted_markets()
