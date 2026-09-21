"""STRATEGY card, under ORDER SIZE: pick a strategy, read how it works.

A dropdown of every strategy whose doc has an ``At a glance`` section, and
under it the picked one's concept, main assumption, maths, how it works, how
it was derived and its references. The Docs button opens the full doc at
``/strategy-docs/<key>`` for everything else.

The text is the doc's own ``At a glance`` section
(``polymarket_bot/strategy_docs.py``), so the card cannot say something the
doc does not, and the doc is already held in step with the code.

The pick changes what this card shows and nothing about what trades — the
switches that decide that are on MY STRATEGIES.

Every strategy's summary is rendered; the browser shows the picked one and
remembers the pick. The card is reference text, so it is marked
``data-static``: the refresh keeps the live node instead of swapping it in
fresh, which keeps the pick, an open dropdown and the rendered maths.

Pure ``render(...) -> str``: the summaries are read by the caller
(``execution_view.py``).
"""

from __future__ import annotations

from functools import lru_cache
from html import escape

from polymarket_bot import inventory as _inv
from polymarket_bot.strategy_docs import GLANCE_PARTS
from polymarket_exec.ops.dashboard.docs_view import render_markdown


def _options_html(entries: list[tuple[_inv.Family, dict[str, str]]]) -> str:
    """``<optgroup>`` per status, in the order the entries arrive."""
    html, group = "", None
    for fam, _ in entries:
        if fam.status != group:
            html += "</optgroup>" if group is not None else ""
            group = fam.status
            html += f"<optgroup label='{escape(_inv.STATUS_LABEL[fam.status])}'>"
        html += f"<option value='{escape(fam.key)}'>{escape(fam.label)}</option>"
    return html + ("</optgroup>" if group is not None else "")


def _body_html(fam: _inv.Family, parts: dict[str, str], shown: bool) -> str:
    sections = "".join(
        "<div class='sc-part'>"
        f"<h4>{escape(name)}</h4>"
        f"<div class='sc-md'>{render_markdown(parts[name])}</div>"
        "</div>"
        for name in GLANCE_PARTS
        if parts.get(name)
    )
    return (
        f"<div class='sc-body' data-strategy='{escape(fam.key)}'"
        f"{'' if shown else ' hidden'}>"
        "<div class='sc-meta'>"
        f"<span class='strategy-status st-{escape(fam.status)}'>"
        f"{escape(_inv.STATUS_LABEL[fam.status])}</span>"
        f"<span class='strategy-path'>{escape(fam.path)}</span>"
        "</div>"
        f"{sections}"
        "</div>"
    )


def render(*, entries: list[tuple[_inv.Family, dict[str, str]]]) -> str:
    """Render the card. ``entries`` is ``[(family, At a glance parts)]`` in
    dropdown order; the first one is shown until the browser restores a pick."""
    # The markdown only changes when a doc does, and the view is rebuilt every
    # few seconds: render each distinct set of summaries once.
    return _render(tuple(
        (fam, tuple(parts.items())) for fam, parts in entries if parts
    ))


@lru_cache(maxsize=4)
def _render(frozen: tuple[tuple[_inv.Family, tuple[tuple[str, str], ...]], ...]) -> str:
    entries = [(fam, dict(parts)) for fam, parts in frozen]
    head = (
        "<section class='card strategy-card' data-static='strategy-card'>"
        "<div class='card-h'>STRATEGY"
        f"<span class='win'>{len(entries)} with a summary</span></div>"
    )
    if not entries:
        return (
            head
            + "<div class='gr-toggle-hint'>No strategy doc has an At a glance "
            "section yet. Add one under docs/strategies/ and it appears here.</div>"
            "</section>"
        )
    first = entries[0][0].key
    return (
        head
        + "<div class='sc-pick'>"
        "<select id='strategy-pick' class='ctl-input sc-select' "
        "onchange='pickStrategy(this)' onblur='releaseHeldView()' "
        "aria-label='Strategy'>"
        f"{_options_html(entries)}</select>"
        f"<a class='gr-btn sc-docs' href='/strategy-docs/{escape(first)}' "
        "target='_blank' rel='noopener' title='The full doc, in a new tab'>Docs</a>"
        "</div>"
        + "".join(_body_html(fam, parts, fam.key == first) for fam, parts in entries)
        + "</section>"
    )
