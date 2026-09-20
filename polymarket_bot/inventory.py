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
``UNWIRED``       code is imported at runtime but cannot reach a market.
``DEAD``          nothing imports it outside tests; it cannot run at all.
``RESEARCH``      an offline script or CLI. Never part of the live process.

Every entry was verified against the tree on 2026-09-21. ``tests/unit/
test_inventory.py`` re-checks the falsifiable half of that on every run — that
each path still exists, and that the switch registry and this list agree — so
a family cannot quietly drop off the card by being deleted or renamed.
"""

from __future__ import annotations

from dataclasses import dataclass

from polymarket_bot.strategies import COPY, MINE

RUNNING = "running"
GATED = "operator_gated"
UNWIRED = "unwired"
DEAD = "dead"
RESEARCH = "research_only"

STATUS_LABEL: dict[str, str] = {
    RUNNING: "running now",
    GATED: "runs on Start",
    UNWIRED: "cannot trade",
    DEAD: "dead",
    RESEARCH: "offline only",
}

STATUS_ORDER: list[str] = [RUNNING, GATED, UNWIRED, DEAD, RESEARCH]


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
    Family(
        key="copytrade",
        label="Copy trade — followed wallets",
        path="polymarket_bot/copytrade/",
        what=(
            "Polls each followed wallet's activity feed (~20s behind) and "
            "books a paper copy of its fills, priced against the live ask."
        ),
        status=RUNNING,
        record="55 copies · all settled −$93.45 · 200 decisions logged",
        switch="copy_macro_daily",
        group=COPY,
        verdict="switches are on the COPY TRADE WALLETS card",
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
    # ---- not wired ----------------------------------------------------
    Family(
        key="pairarb",
        label="Pair-arb — two-sided 5m quoting",
        path="polymarket_bot/pairarb/",
        what=(
            "Rests bids on both legs of one 5m window so the pair costs "
            "under 1.00, then simulates fills and settles."
        ),
        status=RESEARCH,
        record="data/pairarb_shadow.db · 79 windows, 9 execs, last written "
        "2026-08-29",
        verdict=(
            "7 of its 9 modules are reachable only from CLI scripts. Two are "
            "NOT dead and must survive any delete: mirror.py "
            "(price_the_copy, used by copytrade) and market_index.py "
            "(parse_market, used by the daily scanner)."
        ),
    ),
    Family(
        key="v0_fair_value",
        label="v0 BTC 5m fair-value stack",
        path="polymarket_exec/strategy/",
        what=(
            "The archived v0 engine: Gaussian fair-value pricing, "
            "edge→signal, confidence sizing, its own tick controller, paper "
            "order state machine, risk service and two backtest engines."
        ),
        status=UNWIRED,
        record="Never traded · its tables were never even created",
        verdict=(
            "~1,600 lines nothing reaches: strategy/, ops/controller.py, "
            "backtest/{harness,conditional}.py, storage/replay.py. Note "
            "core/types.py, core/interfaces.py, execution/paper.py and "
            "execution/risk.py ARE loaded by the live process — do not "
            "delete the directory wholesale."
        ),
    ),
    # ---- dead ---------------------------------------------------------
    Family(
        key="model_shadow_roster",
        label="Shadow model race roster",
        path="",  # source removed by deb3c22; only the table survives
        what=(
            "Ran five competing pricing models side by side on the same 5m "
            "window and booked a shadow position for each."
        ),
        status=DEAD,
        record="2 orphan rows in model_shadow_positions from 2026-09-13",
        verdict=(
            "Source was removed by deb3c22 — nothing is left in the repo but "
            "a stale __pycache__ in working checkouts. Nothing writes that "
            "table any more. Drop model_shadow_positions and it is gone."
        ),
    ),
    Family(
        key="chronos",
        label="Chronos time-series ensemble",
        path="polymarket_bot/chronos_signal.py",
        what=(
            "Intended second probability source, to blend with the "
            "Black-Scholes baseline via a weighted ensemble."
        ),
        status=DEAD,
        record="Never traded",
        verdict=(
            "105 lines whose predict() returns None unconditionally, so the "
            "ensemble is the identity function. No caller outside its own "
            "test. Safe to delete."
        ),
    ),
    # ---- offline only -------------------------------------------------
    Family(
        key="btc5m_backtest",
        label="BTC 5m offline backtest / replay",
        path="polymarket_bot/backtest.py",
        what=(
            "Offline grid search over recorded 5m windows for the retired "
            "BTC binary pricing strategy, plus a deterministic replay."
        ),
        status=RESEARCH,
        record="Never traded — offline only",
        verdict="Backtests a strategy that no longer exists.",
    ),
    Family(
        key="copytrade_v1",
        label="Copy trade v1 CLI (shadow / live / on-chain)",
        path="tools/copytrade_shadow.py",
        what=(
            "The earlier copy-trade line: polls a wallet by RPC or API and "
            "mirrors its fills. One variant sent REAL orders to the CLOB."
        ),
        status=RESEARCH,
        record=(
            "4 separate DBs, none written since August: copytrade_min.db "
            "25,188 fills (13.9 MB), copytrade_doge.db 10,450 (5.9 MB), "
            "copytrade_rpc.db 178, copytrade_shadow.db 99"
        ),
        verdict=(
            "Superseded by polymarket_bot/copytrade/. One variant can place "
            "real orders from a CLI with no operator switch in front of it."
        ),
    ),
    Family(
        key="wallet_research",
        label="Wallet-research programme",
        path="tools/wallet_research/",
        what=(
            "Offline wallet screening — maker/taker labelling, edge-per-"
            "share ranking, band tests, holdout falsification. Produced the "
            "0.55-0.92 band the maker now quotes."
        ),
        status=RESEARCH,
        record="Never traded · 8.0 GB of scratch DBs under data/wallet_research/",
        verdict=(
            "The scripts earn their place; the 8 GB of scratch databases do "
            "not. m15.db alone is 4.6 GB."
        ),
    ),
    Family(
        key="forecast_journal",
        label="Slow-market forecasting pilot",
        path="tools/forecast_journal.py",
        what=(
            "Logs a hand-made probability on a slow market before looking at "
            "the book, then scores that forecast once it resolves."
        ),
        status=RESEARCH,
        record="Never traded · not one forecast ever logged",
        verdict="A CLI whose signal generator is you. Never started.",
    ),
    Family(
        key="chainlink_lead_lag",
        label="Chainlink-vs-Binance lead-lag study",
        path="tools/chainlink_lead_lag.py",
        what=(
            "Measures how far Chainlink BTC/USD lags Binance, and whether "
            "the next Chainlink print is predictable from it."
        ),
        status=RESEARCH,
        record="Never traded · never even run (no output file exists)",
        verdict=(
            "Diagnostics behind the entry_edge_max cap, not a trading rule."
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
