"""Topbar market selector: open-position glow keys."""
from polymarket_exec.ops.dashboard.panels import market_selector as msel


def test_hourly_slug_rows_glow_the_btc_1h_button() -> None:
    rows = [{"window_slug": "bitcoin-up-or-down-september-13-2026-3pm-et", "side": "Down",
             "entry_price": 0.52, "shares": 5.0}]
    assert msel.open_market_pnl(open_pos=rows, daily_open=[], tick=None) == {("btc", "1h"): None}


def test_daily_btc_slug_rows_glow_the_btc_1d_button() -> None:
    rows = [{"window_slug": "bitcoin-up-or-down-on-september-17-2026", "side": "Up",
             "entry_price": 0.52, "shares": 5.0}]
    assert msel.open_market_pnl(open_pos=rows, daily_open=[], tick=None) == {("btc", "1d"): None}


def test_daily_altcoin_slug_never_matches_the_daily_btc_pattern() -> None:
    """The pre-existing daily altcoin scanner's shadow rows arrive via ``daily_open``, not
    ``open_pos``, so a daily-BTC-shaped slug reaching ``open_pos`` for another asset must
    still glow correctly and not collide with the hourly/updown patterns."""
    rows = [{"window_slug": "ethereum-up-or-down-on-september-17-2026", "side": "Down",
             "entry_price": 0.4, "shares": 3.0}]
    assert msel.open_market_pnl(open_pos=rows, daily_open=[], tick=None) == {("eth", "1d"): None}
