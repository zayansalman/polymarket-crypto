"""Every strategy family in this repo, including the ones that do nothing.

``strategies.py`` is the switch registry: only things an operator can turn on
and off. This module is the fuller, uglier list — every strategy family that
exists in the tree, whether it runs, whether anything can reach it at runtime,
and what it has actually traded.

It exists so the dashboard can show what the codebase really contains. A card
that lists three working strategies, next to a tree holding eleven more in
various states of abandonment, tells the operator the repo is tidy when it is
not. Seeing the dead ones is the point: they get deleted because they are
visible, not in spite of it.

Statuses, narrowest to widest:

``RUNNING``       auto-runs whenever the dashboard is up.
``GATED``         has a loop, but only runs while the operator holds Start.
``UNWIRED``       cannot reach a market: imported at runtime with nothing
                  wired to it, or built but not wired in yet.
``DEAD``          nothing imports it outside tests; it cannot run at all.
                  No family holds this status today — chronos_signal.py and
                  the shadow roster's table were deleted on 2026-09-21.

Every entry was verified against the tree on 2026-09-21, the day
chronos_signal.py, the shadow roster's table and five dead scratch databases
were deleted off the back of this list — and the day #267 deleted the v0 stack
and the pair-arb strategy, and copy-trade was removed, all of which dropped
off it the same way. The same day the whole offline-only group went too —
the 5m backtest and replay, the wallet-research scripts, the forecast journal
and the Chainlink lead-lag study — and its status with it. ``tests/unit/
test_inventory.py`` re-checks the falsifiable half of that on every run — that
each path still exists, and that the switch registry and this list agree — so
a family cannot quietly drop off the card by being deleted or renamed.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymarket_bot.strategies import MINE

RUNNING = "running"
GATED = "operator_gated"
UNWIRED = "unwired"
DEAD = "dead"

STATUS_LABEL: dict[str, str] = {
    RUNNING: "running now",
    GATED: "runs on Start",
    UNWIRED: "cannot trade",
    DEAD: "dead",
}

STATUS_ORDER: list[str] = [RUNNING, GATED, UNWIRED, DEAD]


@dataclass(frozen=True)
class Family:
    """One strategy family, as it actually exists in the tree."""

    key: str
    label: str
    path: str
    """Repo-relative module or package, and checked by the inventory test.

    Empty when the source is already gone and the only thing left is data —
    a deleted family still belongs on the card while its rows are still in
    the database, because those rows are the thing left to clean up."""
    what: str
    status: str
    record: str
    """What it has traded, in plain numbers. 'Never traded' when it has not."""
    switch: str | None = None
    """The ``strategies.STRATEGIES`` key that turns it on, when it has one."""
    group: str = MINE
    verdict: str = ""
    """The one-line case for keeping or deleting it. Blank for the ones that
    are plainly working."""


FAMILIES: tuple[Family, ...] = (
    # ---- running ------------------------------------------------------
    Family(
        key="maker",
        label="Maker — rest on the favourite",
        path="polymarket_bot/maker/",
        what=(
            "Rests one passive bid per crypto Up/Down market when the "
            "favourite sits in the 0.55-0.92 band, never crosses, holds to "
            "resolution. One fixed clip per market."
        ),
        status=RUNNING,
        record="30 quotes · 16 settled −$36.44 · 3 filled, 11 resting",
        switch="maker",
    ),
    Family(
        key="daily_altcoin",
        label="Daily altcoin scanner",
        path="polymarket_bot/daily/",
        what=(
            "Scores each tracked altcoin's 24h Up/Down market against a "
            "realized-volatility model every 60s and opens one flat-size "
            "paper position on the strongest edge."
        ),
        status=RUNNING,
        record="19 positions · 18 settled −$67.08 · 1 open",
        switch="daily_altcoin",
    ),
    # ---- wired, but cannot trade --------------------------------------
    Family(
        key="btc_updown",
        label="BTC Up/Down loop",
        path="polymarket_bot/paper.py",
        what=(
            "Prices the selected Up/Down window against the Chainlink feed "
            "and takes one confidence-sized entry per window."
        ),
        status=UNWIRED,
        record="254 ticks · 0 positions, ever",
        switch="btc_updown",
        verdict=(
            "market_selection.LOOP_SUPPORTED is empty and paper.py still "
            "builds the retired btc-updown-5m slug. It ticks and never "
            "enters. Either point it at the 15m family or delete it — the "
            "TRADE BLOTTER card is permanently empty because of this."
        ),
    ),
    # ---- built, nothing wired to trade it -----------------------------
    Family(
        key="fade_1h_momentum_15m",
        label="Fade 1h Momentum on 15m",
        path="tools/fade_1h_momentum_15m/",
        what=(
            "Prices each 15m Up/Down window from a Brownian-motion model of "
            "the hour — the 1h market price, the trailing spot return and a "
            "mean reversion that decays through the hour — and computes the "
            "entry price instead of using a threshold."
        ),
        status=UNWIRED,
        record="Never traded · maths validated 2026-09-21, historical test pending",
        verdict=(
            "Not wired to trade until the pre-registered historical test says "
            "whether a fee-free edge survives on the real 15m tape; paper only "
            "after that."
        ),
    ),
)


def in_group(group: str) -> tuple[Family, ...]:
    """Every family one card owns, in status order then registry order."""
    ordered = sorted(
        (f for f in FAMILIES if f.group == group),
        key=lambda f: STATUS_ORDER.index(f.status),
    )
    return tuple(ordered)


def by_status(group: str) -> list[tuple[str, list[Family]]]:
    """``[(status, families)]`` for one card, in STATUS_ORDER."""
    out: list[tuple[str, list[Family]]] = []
    for status in STATUS_ORDER:
        rows = [f for f in FAMILIES if f.group == group and f.status == status]
        if rows:
            out.append((status, rows))
    return out
