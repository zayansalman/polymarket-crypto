"""Topbar market selector: open-position glow keys."""
from polymarket_exec.ops.dashboard.panels import market_selector as msel


def test_hourly_slug_rows_glow_the_btc_1h_button() -> None:
    rows = [{"window_slug": "bitcoin-up-or-down-september-13-2026-3pm-et", "side": "Down",
             "entry_price": 0.52, "shares": 5.0}]
    assert msel.open_market_pnl(open_pos=rows, daily_open=[], tick=None) == {("btc", "1h"): None}
