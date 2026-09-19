"""Wallets worth mirroring, and the measurement that earned them the place.

Every entry here came out of ``tools/wallet_research`` over 7.6M fills on
resolved Up-or-Down markets. The numbers are recorded alongside the address so
a later reader can see *why* a wallet is here and re-derive it, rather than
inheriting an unexplained list.

**Why these and not the wallets that made the most money.** Polymarket charges
takers ``shares * 0.07 * p * (1-p)`` and charges makers nothing. That is
1.75c/share at 50c but only 0.03c at 99.6c. Ranking by profit surfaces market
makers, whose profit *is* the spread a copier pays — every one of the six
highest-PnL wallets reviewed failed verification. Ranking by net edge per share
surfaces wallets whose edge survives the fee, and those all trade near $1.

**Why the daily macro names rather than the hourly crypto ones.** The best
hourly wallet (Oldstreet) nets 0.23c/share with 10% of its entries leaving under
2.1 minutes to settlement, against a measured 9.56c median slippage at 11-30s
observation lag — the stale feed costs 40x the edge, so copying it needs a
Polygon websocket. These daily wallets net 0.93-1.80c/share with a median 2-3.5
HOURS of runway, and a tape replay shows a copier entering 30 minutes late still
keeps the edge. A 20s-stale poll is irrelevant at that horizon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """A wallet to mirror, with the evidence that selected it."""

    address: str
    label: str
    edge_cents: float
    """Net edge per share after the taker fee a copier pays, in cents."""
    t_stat: float
    markets: int
    avg_stake_usd: float
    runway_p10_min: float
    """Minutes to settlement at the 10th percentile of its entries."""
    edge_left_30min: float
    """Edge remaining for a copier entering 30 minutes late, from tape replay."""
    assets: str
    note: str = ""
    cadence: str = "daily"
    """'daily' settles once a day; 'intraday' settles every 15-60 minutes."""

    @property
    def key(self) -> str:
        return self.address.lower()


# Ranked by edge per share. All survive Benjamini-Hochberg FDR at q=0.10 across
# the 44 structurally-copyable candidates in the macro universe, and all get
# STRONGER when their three best markets are deleted.
TARGETS: dict[str, Target] = {
    t.key: t
    for t in (
        Target(
            address="0x39fa5aed3e0a26e89acf8088b8a8ad132481cf2a",
            label="gegegu84",
            edge_cents=2.15, t_stat=13.84, markets=112, avg_stake_usd=9.0,
            runway_p10_min=4.3, edge_left_30min=2.15,
            assets="btc, eth 1h", cadence="intraday",
            note="highest edge per share of the crypto set at a t of 13.8, but "
                 "trades in $9 clips - the edge is real and the size is tiny.",
        ),
        Target(
            address="0x6c2be10fb07747a3c751c77774938a15fbcf36f4",
            label="lmcaimiku",
            edge_cents=1.14, t_stat=5.26, markets=41, avg_stake_usd=999.0,
            runway_p10_min=2.3, edge_left_30min=1.14,
            assets="btc, eth 1h", cadence="intraday",
            note="only 9 days of history - the shortest record here. Included "
                 "for overnight coverage; do not size it on this evidence.",
        ),
        Target(
            address="0x57169c875485b2eae4895119bde41bff12dcbbf6",
            label="hdai298yf98763h",
            edge_cents=0.15, t_stat=19.28, markets=214, avg_stake_usd=137.0,
            runway_p10_min=3.1, edge_left_30min=0.15,
            assets="btc, eth 1h", cadence="intraday",
            note="the highest t-stat in the whole screen, on the thinnest edge. "
                 "A good test of whether a 0.15c edge survives real execution.",
        ),
        Target(
            address="0x83451c358d50b3f8982124fc741e8bda4b4edd93",
            label="Oldstreet",
            edge_cents=0.23, t_stat=8.85, markets=800, avg_stake_usd=843.0,
            runway_p10_min=2.1, edge_left_30min=0.11,
            assets="btc, eth 15m + 1h", cadence="intraday",
            note="thinnest edge of the set and the only one where lag really "
                 "bites (this repo measures 9.56c slippage at 11-30s lag). "
                 "Included because its markets settle every 15-60 min, so it is "
                 "the only target that produces settled paper results overnight "
                 "- treat its numbers as the honest cost of a late copy, not as "
                 "a forecast of the daily wallets.",
        ),
        Target(
            address="0xcbd0f3b660c1c0609ac25919ec0cea828f7edec4",
            label="kodeoed",
            edge_cents=1.55, t_stat=3.70, markets=31, avg_stake_usd=1529.0,
            runway_p10_min=72.0, edge_left_30min=2.39,
            assets="spy, silver, gold, oil",
            note="largest net ($745) of the macro set; 67 of 90 entries had "
                 "follow-on tape within a minute, the best coverage measured.",
        ),
        Target(
            address="0x83e8f2ea25df8e71fcaf2271c9edaae02860b9bc",
            label="JudasPr13st",
            edge_cents=1.80, t_stat=6.25, markets=37, avg_stake_usd=151.0,
            runway_p10_min=112.0, edge_left_30min=1.57,
            assets="oil, silver, gold",
            note="most consistent across time: $30 / $39 / $33 over the three "
                 "30-day buckets.",
        ),
        Target(
            address="0xfcaaa4cbd0a7553ac886eb5456d4806f128c5add",
            label="zedamanga",
            edge_cents=1.68, t_stat=5.47, markets=51, avg_stake_usd=18.0,
            runway_p10_min=27.0, edge_left_30min=1.90,
            assets="oil, gold, silver, spy",
            note="also clears the crypto screen — the only wallet in both.",
        ),
        Target(
            address="0xe3164027a2be859579fcd84fce34513f0bc9bcf9",
            label="Late4CakeJohn",
            edge_cents=1.20, t_stat=7.79, markets=46, avg_stake_usd=164.0,
            runway_p10_min=26.0, edge_left_30min=0.86,
            assets="spy, oil, gold, silver",
        ),
        Target(
            address="0xe06cac28c493a2536e5b111e20e5f7a5f3843eb4",
            label="FocusSharp",
            edge_cents=0.97, t_stat=10.47, markets=34, avg_stake_usd=74.0,
            runway_p10_min=104.0, edge_left_30min=1.27,
            assets="oil, gold, silver",
            note="highest t-stat of the set.",
        ),
    )
}

DEFAULT_TARGET = "0xcbd0f3b660c1c0609ac25919ec0cea828f7edec4"
"""kodeoed — biggest edge-times-size, and the best tape coverage."""


def get(address: str) -> Target | None:
    return TARGETS.get(address.lower())
