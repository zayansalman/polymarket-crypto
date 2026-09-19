"""Reconciliation BTC/account separation (issue #113).

The funder wallet sees ALL the operator's Polymarket activity. The lifetime
split must isolate the bot's BTC 5-minute trades by the structural slug prefix
``btc-updown-1h-`` rather than a fragile ``"Bitcoin Up or Down" in title``
substring (titles can be null or renamed; the slug is the bot's discovery
contract and is verified robust on real data: 648/648 BTC, 0 false positives).
"""

from __future__ import annotations

import pytest

from tools.reconcile_live_ledger import lifetime_pnls


class TestLifetimePnlSeparation:
    def test_btc_isolated_by_slug_not_title(self) -> None:
        activity = [
            # BTC buy with a NULL title but the correct slug — must count as BTC.
            {"type": "TRADE", "side": "BUY", "usdcSize": 10.0,
             "eventSlug": "btc-updown-1h-1781679000", "title": None},
            # BTC redeem (win) in the same window — proceeds.
            {"type": "REDEEM", "usdcSize": 15.0,
             "eventSlug": "btc-updown-1h-1781679000", "title": None},
            # Non-bot trade — excluded from BTC, included in the account total.
            {"type": "TRADE", "side": "BUY", "usdcSize": 50.0,
             "eventSlug": "nba-phi-nyk-2026-05-04", "title": "NBA PHI/NYK"},
        ]
        btc, acct = lifetime_pnls(activity)
        assert btc == pytest.approx(5.0)    # redeem 15 - buy 10
        assert acct == pytest.approx(-45.0)  # +5 BTC - 50 NBA buy

    def test_slug_fallback_to_slug_field(self) -> None:
        # Some activity rows carry ``slug`` instead of ``eventSlug``.
        activity = [
            {"type": "TRADE", "side": "BUY", "usdcSize": 4.0,
             "slug": "btc-updown-1h-1781680000", "title": ""},
            {"type": "TRADE", "side": "SELL", "usdcSize": 7.0,
             "slug": "btc-updown-1h-1781680000", "title": ""},
        ]
        btc, acct = lifetime_pnls(activity)
        assert btc == pytest.approx(3.0)   # sell 7 - buy 4
        assert acct == pytest.approx(3.0)

    def test_non_btc_with_bitcoin_in_title_is_excluded(self) -> None:
        # A non-bot market that merely mentions Bitcoin must NOT pollute BTC.
        activity = [
            {"type": "TRADE", "side": "BUY", "usdcSize": 20.0,
             "eventSlug": "will-bitcoin-hit-200k-2026", "title": "Bitcoin Up or Down 200k?"},
        ]
        btc, acct = lifetime_pnls(activity)
        assert btc == pytest.approx(0.0)     # excluded from BTC by slug
        assert acct == pytest.approx(-20.0)  # still in the account total
