"""Every strategy family in this repo.

``strategies.py`` is the switch registry: only things an operator can turn on
and off. This module is the fuller list — every strategy family that exists in
the tree, whether it runs, whether anything can reach it at runtime, and what it
has actually traded — so the dashboard can show what the codebase really
contains. Statuses, narrowest to widest:

``RUNNING``       auto-runs whenever the dashboard is up.
``GATED``         has a loop, but only runs while the operator holds Start.
``UNWIRED``       cannot reach a market: imported at runtime with nothing
                  wired to it, or built but not wired in yet.
``DEAD``          nothing imports it outside tests; it cannot run at all.

On 2026-09-26 every family but Fade 1h Momentum on 15m was deleted: the BTC
Up/Down loop and its live executor, the maker and the daily altcoin scanner.
``tests/unit/test_inventory.py`` re-checks the falsifiable half of this list on
every run — that each path still exists, and that the switch registry and this
list agree — so a family cannot quietly drop off the card by being deleted or
renamed.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    verdict: str = ""
    """The one-line case for keeping or deleting it. Blank for the ones that
    are plainly working."""


FAMILIES: tuple[Family, ...] = (
    Family(
        key="fade_1h_momentum_15m",
        label="Fade 1h Momentum on 15m",
        path="ems/fade_1h_momentum_15m/",
        what=(
            "Every minute, prices each of BTC, ETH, SOL and XRP's 15m Up/Down "
            "window with the model (the chance it settles Up on the Chainlink "
            "TWAP-60s print at the close) and follows the market as far as the "
            "settled windows say. It prices a paper buy of each side and rests "
            "the one that pays more, or nothing: passive limit orders at or "
            "under the best bid, split across several prices and sized by "
            "Kelly across the four coins together. A position the maths turns "
            "against is cut by offering the shares held for sale. It never "
            "holds both sides and never crosses the spread. After each settled "
            "window the dials move one step toward the result."
        ),
        status=RUNNING,
        record=(
            "Paper orders since 2026-09-22 · starting dials fitted on the "
            "Sep 17-20 tape, learning from every settled window"
        ),
        switch="fade_1h_momentum_15m",
        verdict=(
            "Paper only. Wired before the historical test at the operator's "
            "call (paper-trade and learn): the paper record, not a backtest, "
            "shows whether the maths pays. The starting anchor weights and "
            "coin correlation come from 3.5 days of tape."
        ),
    ),
    Family(
        key="kelly_horse_race",
        label="Kelly horse-race",
        path="ems/kelly_horse_race/",
        what=(
            "Once per BTC 15m Up/Down window it reads the chance of Up as a binary "
            "option on the Chainlink TWAP-60s print: the price now against the price "
            "to beat, with the last hour's move carried forward as the drift and its "
            "volatility as the spread. A die weighted by that chance picks the side, "
            "so over many windows the stake splits between Up and Down in proportion "
            "to their chances (Kelly's horse-race rule). It rests one passive limit "
            "buy at that side's best bid, sized at random between the venue's minimum "
            "order and the notional cap, on paper always and on live when armed."
        ),
        status=RUNNING,
        record="Paper orders from 2026-09-26",
        switch="kelly_horse_race",
        verdict=(
            "Built as the operator asked: the maths as written, with randomness, and "
            "not fitted to outcomes. The records show how its chance of Up compares "
            "with what settles."
        ),
    ),
)


def by_status() -> list[tuple[str, list[Family]]]:
    """``[(status, families)]`` in STATUS_ORDER, registry order within each."""
    out: list[tuple[str, list[Family]]] = []
    for status in STATUS_ORDER:
        rows = [f for f in FAMILIES if f.status == status]
        if rows:
            out.append((status, rows))
    return out
