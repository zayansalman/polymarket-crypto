"""Wallets worth mirroring, and the measurement that earned them the place.

**One target, chosen 2026-09-20 on a deliberately short window.**
`tools/wallet_research/recent_wallets.py` ranked every wallet on the last THREE
days of resolved 15-minute Up-or-Down markets (996 markets, 822k fills, each
fill labelled maker or taker against the `takerOnly=true` feed). The previous
40-name registry was screened over 30 days and `holdout_test.py` showed that
screen had no predictive value out of sample, so length of window was not what
was missing. This list is short on purpose: follow one wallet, log every trade,
and let real paper outcomes settle it.

**Why this one.** Three filters, in the order they eliminate:

  - **Copyable at all.** A maker's profit IS the spread a follower pays, so only
    the taker share of a wallet's edge is reachable. This wallet is 96% taker by
    notional.
  - **Holds to resolution.** 309 buys and ZERO sells over 195 markets. A wallet
    that sells before settlement is timing exits, and a follower 20-60s behind
    cannot reproduce an exit.
  - **Has not drifted.** Checked live against the API, not the archive: its most
    recent 500 fills are BTC/SOL/ETH 15m Up-or-Down, traded minutes ago, 500
    buys and 0 sells. The runner-up (`0x84389cfc`, +23.5c/share on the same
    screen) failed exactly here — it had moved to 5-minute markets, which is the
    drift that cost an earlier registry 7 of its 9 names.

**What is weak about it, stated plainly.** Three days is 195 markets. It was up
on 2 of those 3 days, 62% of its markets are positive, and its best 19 markets
hold 148% of the total — meaning the losers are large too. The per-share edge
below is a three-day number and nothing here corrects it for multiple testing,
because with one candidate there is nothing to correct. It is a hypothesis the
paper ledger is being asked to test, not a finding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    """A wallet to mirror, with the evidence that selected it."""

    address: str
    label: str
    taker_share: float
    """Fraction of its NOTIONAL that crossed the spread, measured against the
    takerOnly feed. Below ~0.55 the wallet is quoting and cannot be followed."""
    edge_cents: float
    """Net edge per share on its TAKER fills only, after the taker fee."""
    t_stat: float
    markets: int
    avg_stake_usd: float
    avg_entry: float
    in_scope: float
    """Fraction of last-7-day flow still on 15m/1h Up-or-Down markets."""
    fills_per_day: float
    assets: str = "btc, eth 15m+1h"
    cadence: str = "intraday"
    note: str = ""

    @property
    def key(self) -> str:
        return self.address.lower()

    @property
    def edge_left_30min(self) -> float:
        """Shown by the panel; intraday targets are never copied 30 min late."""
        return self.edge_cents

    @property
    def runway_p10_min(self) -> float:
        return 0.0


TARGETS: dict[str, Target] = {
    t.key: t
    for t in (
        Target(
            address="0xd9013df863c1ba932780857b020dfdeacedf8e14",
            label="t-d901",
            taker_share=0.96, edge_cents=13.95, t_stat=0.0,
            markets=195, avg_stake_usd=8, avg_entry=0.540,
            in_scope=1.00, fills_per_day=103.0,
            assets="btc, eth, sol, xrp 15m",
            cadence="intraday",
            note=(
                "3-day screen: +$2,076 taker on 14,880sh (+13.95c/share) over "
                "195 markets, 309 buys and 0 sells. Up 2 of 3 days, 62% of "
                "markets positive. t_stat is 0 because none was computed - a "
                "3-day window on one pre-chosen wallet has no multiple-testing "
                "correction to make, and quoting a t here would dress a "
                "hypothesis up as a result."
            ),
        ),
    )
}

DEFAULT_TARGET = "0xd9013df863c1ba932780857b020dfdeacedf8e14"
"""The only target. See the module docstring for why it is alone."""


def get(address: str) -> Target | None:
    return TARGETS.get(address.lower())
