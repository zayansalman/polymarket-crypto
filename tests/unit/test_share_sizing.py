"""Share-denominated sizing helper (#89).

When the operator sets a target share count, the clip is sized to
``shares × the chosen side's ask`` so the order is ≈N shares regardless of
price. These pin the pure resize function used by the paper/live loop.
"""
from __future__ import annotations

from polymarket_bot.paper import _share_sized_notional


class TestShareSizedNotional:
    def test_resizes_to_shares_times_up_ask(self) -> None:
        # 8 shares at the 0.56 up-ask → $4.48 notional → downstream yields ≈8 shares.
        assert _share_sized_notional("Up", 1.0, 0.56, 0.44, 8.0) == 8.0 * 0.56

    def test_resizes_to_shares_times_down_ask(self) -> None:
        assert _share_sized_notional("Down", 1.0, 0.44, 0.56, 10.0) == 10.0 * 0.56

    def test_unset_target_keeps_dollar_notional(self) -> None:
        # No share knob → backward-compatible dollar path untouched.
        assert _share_sized_notional("Up", 3.0, 0.56, 0.44, None) == 3.0

    def test_no_side_keeps_notional(self) -> None:
        assert _share_sized_notional(None, 3.0, 0.56, 0.44, 8.0) == 3.0

    def test_zero_notional_no_trade_kept(self) -> None:
        assert _share_sized_notional("Up", 0.0, 0.56, 0.44, 8.0) == 0.0

    def test_missing_or_zero_ask_falls_through(self) -> None:
        assert _share_sized_notional("Up", 3.0, None, 0.44, 8.0) == 3.0
        assert _share_sized_notional("Up", 3.0, 0.0, 0.44, 8.0) == 3.0
