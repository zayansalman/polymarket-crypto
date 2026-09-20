"""The inventory must keep matching the tree, or the card starts lying.

``polymarket_bot/inventory.py`` is hand-written prose about what exists. Prose
goes stale. These tests pin the half of it that is falsifiable — the paths, the
switch keys, the statuses that imply a runtime importer — so a family cannot
quietly drop off the dashboard by being deleted, renamed or wired up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from polymarket_bot import inventory as _inv
from polymarket_bot import strategies as _strategies

ROOT = Path(__file__).resolve().parents[2]


def test_every_family_still_exists_on_disk() -> None:
    # The whole point of the card is that a dead family is visible. A family
    # whose path has already been deleted is worse than invisible: it is a
    # claim about code that is not there.
    for family in _inv.FAMILIES:
        assert (ROOT / family.path).exists(), f"{family.key}: {family.path} is gone"


def test_every_switch_names_a_real_strategy() -> None:
    for family in _inv.FAMILIES:
        if family.switch is not None:
            assert family.switch in _strategies.STRATEGIES, family.key


def test_every_strategy_switch_belongs_to_some_family() -> None:
    # A switch with no family behind it is a control the card cannot explain.
    claimed = {f.switch for f in _inv.FAMILIES if f.switch}
    for name in _strategies.STRATEGIES:
        if name == "copy_autocopy":
            continue  # a second switch on the copytrade family, not its own
        assert name in claimed, f"{name} has a switch but no inventory entry"


def test_statuses_are_from_the_known_set() -> None:
    for family in _inv.FAMILIES:
        assert family.status in _inv.STATUS_ORDER, family.key
        assert family.status in _inv.STATUS_LABEL, family.key


def test_keys_are_unique() -> None:
    keys = [f.key for f in _inv.FAMILIES]
    assert len(keys) == len(set(keys))


def test_a_family_that_cannot_run_carries_the_case_for_deleting_it() -> None:
    # "This is dead" with no reason attached is not actionable; the operator
    # asked to be told what to remove, not merely that something is unused.
    for family in _inv.FAMILIES:
        if family.status in (_inv.DEAD, _inv.UNWIRED, _inv.RESEARCH):
            assert family.verdict, f"{family.key} has no verdict"


def test_a_dead_family_never_offers_a_switch() -> None:
    for family in _inv.FAMILIES:
        if family.status in (_inv.DEAD, _inv.RESEARCH):
            assert family.switch is None, family.key


def test_the_running_families_are_the_ones_the_dashboard_starts() -> None:
    # If a loop is added to or removed from app.py's lifespan without the
    # inventory following, this is the test that says so.
    lifespan = (ROOT / "polymarket_exec/ops/dashboard/app.py").read_text()
    for key, module in (
        ("maker", "polymarket_bot.maker.runner"),
        ("daily_altcoin", "polymarket_bot.daily.scanner"),
        ("copytrade", "polymarket_bot.copytrade.watcher"),
    ):
        family = next(f for f in _inv.FAMILIES if f.key == key)
        assert family.status == _inv.RUNNING, key
        assert module in lifespan, f"{key}: {module} no longer started on boot"


def test_a_dead_family_really_has_no_runtime_importer() -> None:
    # Claiming "dead" is the claim most likely to be wrong and most likely to
    # get something deleted that is load-bearing. Check it rather than trust it.
    for family in _inv.FAMILIES:
        if family.status != _inv.DEAD:
            continue
        module = Path(family.path).stem
        found = subprocess.run(
            ["grep", "-rn", "--include=*.py", f"import {module}", "polymarket_bot",
             "polymarket_exec", "main.py"],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout
        live = [
            line for line in found.splitlines()
            if "__pycache__" not in line and f"/{module}.py" not in line
        ]
        assert not live, f"{family.key} is marked dead but is imported:\n{found}"
