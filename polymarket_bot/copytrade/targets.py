"""Wallets worth mirroring, and the measurement that earned them the place.

Selected by ``tools/wallet_research/taker_screen.py`` over the last 30 days of
resolved BTC/ETH/XRP hourly Up-or-Down markets.

**Why these are not the highest-profit wallets.** Ranking by profit finds market
makers. Their edge IS the spread, they sit on the other side of the trade from a
follower, and copying one converts their profit into your cost — the six
highest-PnL wallets reviewed in Sept 2026 all failed verification for exactly
that reason. One of them, `0x9d57c42e`, made $32,064 while buying BOTH outcomes
in 3,054 of its 3,151 markets: there is no side to follow.

So every wallet here was classified fill-by-fill against the ``takerOnly=true``
feed and is ranked on its TAKER fills only — the flow a follower who crosses the
spread can actually reproduce. ``taker_share`` is measured by notional, not by
fill count, because a wallet can rest most of its tickets and still put most of
its money across the spread.

**Why they are small.** These are ordinary accounts staking $3-$51 a market, not
whales. That is deliberate: capacity at these sizes is not a constraint for a
follower, and it is the population the screen was asked for.

**Drift is checked, and it is the thing most likely to invalidate a target.** An
earlier registry of daily macro wallets was abandoned when 7 of 9 turned out to
have moved to strike markets, weather and college football — a measured edge does
not transfer to a market the wallet was never measured on. Every wallet below was
confirmed still trading 15m/1h Up-or-Down within the last two days, and the
dashboard tracks the ratio live.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """A wallet to mirror, with the evidence that selected it."""

    address: str
    label: str
    taker_share: float
    """Fraction of its notional that crossed the spread, measured against the
    takerOnly feed. Below ~0.6 the wallet is quoting and cannot be followed."""
    edge_cents: float
    """Net edge per share on its TAKER fills only, after the taker fee."""
    t_stat: float
    markets: int
    avg_stake_usd: float
    avg_entry: float
    in_scope: float
    """Fraction of its last-7-day flow still on 15m/1h Up-or-Down markets."""
    assets: str
    cadence: str = "intraday"
    note: str = ""

    @property
    def key(self) -> str:
        return self.address.lower()

    @property
    def edge_left_30min(self) -> float:
        """Kept for the panel; intraday targets are not copied 30 min late."""
        return self.edge_cents

    @property
    def runway_p10_min(self) -> float:
        return 0.0


TARGETS: dict[str, Target] = {
    t.key: t
    for t in (
        # --- mid-price takers: the population asked for ---------------------
        Target(
            address="0x04ec14124fd09f8e9af7e36bb6b59227bdcd00a6",
            label="taker-04ec",
            taker_share=0.86, edge_cents=29.28, t_stat=0.99, markets=190,
            avg_stake_usd=5.0, avg_entry=0.514, in_scope=0.94, assets="btc, eth 1h",
            note="largest sample of the mid-price set and still trading hourly "
                 "Up/Down. $5 a market.",
        ),
        Target(
            address="0x7cbc58d87704870d5b99e70c1f06aff462b4839a",
            label="taker-7cbc",
            taker_share=0.90, edge_cents=20.06, t_stat=0.79, markets=99,
            avg_stake_usd=4.0, avg_entry=0.523, in_scope=1.00, assets="btc, eth 1h+15m",
            note="100% of recent flow still in scope.",
        ),
        Target(
            address="0xc15fa88815941dcdb88874c8aaac2b3aba3da7d9",
            label="taker-c15f",
            taker_share=0.75, edge_cents=21.58, t_stat=1.13, markets=54,
            avg_stake_usd=24.0, avg_entry=0.424, in_scope=0.68, assets="btc, eth 15m",
            note="underdog buyer at 42c, mostly 15m. A fifth of its recent flow "
                 "is the 5m family, which this project does not follow.",
        ),
        Target(
            address="0x0dbe7a4fa4cf3887b9cd4d31c5d1fc8a229b216c",
            label="taker-0dbe",
            taker_share=0.63, edge_cents=7.04, t_stat=0.72, markets=122,
            avg_stake_usd=3.0, avg_entry=0.532, in_scope=1.00, assets="btc, eth 15m",
            note="the 15m specialist: 108 of its last 123 fills are 15m.",
        ),
        Target(
            address="0x6b1b435a547d245675cbbf268a2b9f4ce8970d3c",
            label="taker-6b1b",
            taker_share=0.60, edge_cents=19.66, t_stat=2.95, markets=20,
            avg_stake_usd=44.0, avg_entry=0.507, in_scope=1.00, assets="btc, eth 1h",
            note="highest t-stat of any mid-price taker in the screen, but on "
                 "only 20 markets.",
        ),
        Target(
            address="0x3c0077ed91ea7234f0ce245bbdd024630680ba3c",
            label="taker-3c00",
            taker_share=0.86, edge_cents=20.53, t_stat=1.34, markets=19,
            avg_stake_usd=51.0, avg_entry=0.488, in_scope=1.00, assets="btc, eth 1h",
        ),
        # --- favourite buyers: different shape, kept for contrast -----------
        Target(
            address="0x2c175b735a264f2e2c53244c2b098baeefeab888",
            label="taker-2c17",
            taker_share=1.00, edge_cents=32.20, t_stat=1.16, markets=32,
            avg_stake_usd=25.0, avg_entry=0.895, in_scope=1.00, assets="btc, eth 1h",
            note="highest taker edge in the screen and 100% taker by notional.",
        ),
        Target(
            address="0xef56de00e2da335b89e3d6407b01a12645eaedda",
            label="taker-ef56",
            taker_share=1.00, edge_cents=6.86, t_stat=0.70, markets=81,
            avg_stake_usd=225.0, avg_entry=0.928, in_scope=1.00, assets="btc, eth 1h",
            note="largest clips here at $225 a market; thin edge on favourites.",
        ),
        # --- retained from the earlier screen --------------------------------
        Target(
            address="0x83451c358d50b3f8982124fc741e8bda4b4edd93",
            label="Oldstreet",
            taker_share=0.0, edge_cents=0.23, t_stat=8.85, markets=800,
            avg_stake_usd=843.0, avg_entry=0.996, in_scope=1.00,
            assets="btc, eth 15m+1h",
            note="kept for its 800-market sample and because it trades all "
                 "night. Its taker share was never measured, and buying at "
                 "99.6c leaves 0.4c of headroom against a 0.23c edge - read it "
                 "as the cost of a late copy, not as a forecast.",
        ),
    )
}

DEFAULT_TARGET = "0x04ec14124fd09f8e9af7e36bb6b59227bdcd00a6"
"""taker-04ec — biggest mid-price sample that is still in scope and active."""


def get(address: str) -> Target | None:
    return TARGETS.get(address.lower())
