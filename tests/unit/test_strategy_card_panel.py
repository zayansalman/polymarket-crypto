"""STRATEGY card: a dropdown of our strategies, and the picked one's summary.

Pure ``render(...)`` transform — the At a glance parts are read from the docs
by ``execution_view.py`` and passed in.
"""

from __future__ import annotations

import re

from polymarket_bot import inventory as _inv
from polymarket_bot import strategy_docs as sd
from polymarket_exec.ops.dashboard.panels import strategy_card as panel

PARTS = {name: f"About {name.lower()}." for name in sd.GLANCE_PARTS}


def _fam(key: str, status: str = _inv.RUNNING) -> _inv.Family:
    return _inv.Family(
        key=key, label=f"Strategy {key}", path=f"polymarket_bot/{key}/",
        what="Does one thing.", status=status, record="never traded",
    )


def _bodies(html: str) -> dict[str, bool]:
    """``{key: shown}`` for every summary body on the card."""
    return {
        m.group(1): m.group(2) != " hidden"
        for m in re.finditer(r"<div class='sc-body' data-strategy='([^']+)'( hidden)?>", html)
    }


def test_dropdown_lists_each_strategy_with_a_summary_under_its_status() -> None:
    html = panel.render(entries=[
        (_fam("a"), PARTS), (_fam("b"), PARTS), (_fam("c", _inv.UNWIRED), PARTS),
    ])
    assert "<select id='strategy-pick'" in html
    assert re.search(
        r"<optgroup label='running now'><option value='a'>.*<option value='b'>.*</optgroup>"
        r"<optgroup label='cannot trade'><option value='c'>",
        html,
    )


def test_a_strategy_without_a_summary_is_left_off() -> None:
    html = panel.render(entries=[(_fam("a"), PARTS), (_fam("b"), {})])
    assert "value='b'" not in html
    assert set(_bodies(html)) == {"a"}


def test_first_strategy_is_shown_the_rest_wait_for_a_pick() -> None:
    html = panel.render(entries=[(_fam("a"), PARTS), (_fam("b"), PARTS)])
    assert _bodies(html) == {"a": True, "b": False}


def test_every_part_is_shown_in_order() -> None:
    html = panel.render(entries=[(_fam("a"), PARTS)])
    heads = re.findall(r"<h4>([^<]+)</h4>", html)
    assert heads == list(sd.GLANCE_PARTS)


def test_docs_button_opens_the_full_doc_in_a_new_tab() -> None:
    html = panel.render(entries=[(_fam("a"), PARTS)])
    assert "href='/strategy-docs/a'" in html
    assert "target='_blank'" in html
    assert ">Docs</a>" in html


def test_maths_is_kept_for_katex() -> None:
    parts = {**PARTS, "The maths": "$$p=\\Phi(z)$$\n\nwith $z$ the score."}
    html = panel.render(entries=[(_fam("a"), parts)])
    assert "\\[p=\\Phi(z)\\]" in html
    assert "\\(z\\)" in html


def test_card_is_kept_across_refreshes() -> None:
    # data-static tells the browser to keep the live node (pick, open
    # dropdown, rendered maths) instead of swapping in the fresh copy.
    html = panel.render(entries=[(_fam("a"), PARTS)])
    assert "data-static='strategy-card'" in html


def test_no_summaries_says_how_to_add_one() -> None:
    html = panel.render(entries=[])
    assert "STRATEGY" in html
    assert "<select" not in html
    assert "At a glance" in html
