"""Settings panel: every dashboard-editable runtime knob (#206).

Auto-generated from ``polymarket_bot.runtime_knobs.KNOBS`` — one row per knob,
grouped by ``Knob.group`` — so adding a knob to the registry is the only step
needed to surface it here; no per-knob markup to hand-write. Pure
``render(...) -> str`` transform, no DB access (values loaded once in
``execution_view.py`` and passed in), matching this dashboard's convention.

Every control posts to the same generic endpoint
(``POST /api/runtime-config {key, value}``) via the same generic JS helper
(``setKnob``, ``static/dashboard.js``) — one code path for all ~23 knobs
instead of one bespoke function per knob.
"""
from __future__ import annotations

from html import escape
from typing import Any

from polymarket_bot.runtime_knobs import Knob


def _input_html(name: str, knob: Knob, value: Any) -> str:
    input_id = f"knob-{name}"
    if knob.kind == "bool":
        checked = "checked" if value else ""
        return (
            f"<input id='{input_id}' type='checkbox' {checked} "
            f"aria-label='{escape(knob.label)}' />"
        )
    if knob.kind == "enum":
        options = "".join(
            f"<option value='{escape(c)}'{' selected' if c == value else ''}>{escape(c)}</option>"
            for c in (knob.choices or ())
        )
        return (
            f"<select id='{input_id}' class='ctl-input' "
            f"aria-label='{escape(knob.label)}'>{options}</select>"
        )
    step = "1" if knob.kind == "int" else "any"
    bounds = ""
    if knob.min_value is not None:
        bounds += f" min='{knob.min_value:g}'"
    if knob.max_value is not None:
        bounds += f" max='{knob.max_value:g}'"
    return (
        f"<input id='{input_id}' class='ctl-input' type='number' step='{step}'{bounds} "
        f"value='{value:g}' aria-label='{escape(knob.label)}' />"
    )


def _row_html(name: str, knob: Knob, value: Any) -> str:
    unit = f" <span class='ctl-unit'>{escape(knob.unit)}</span>" if knob.unit else ""
    apply_call = f"setKnob('{name}', '{knob.kind}')"
    return (
        "<div class='ctl-row'>"
        f"<span class='settings-label' title='{escape(knob.key)}'>{escape(knob.label)}</span>"
        f"{_input_html(name, knob, value)}{unit}"
        f"<button class='gr-btn btn-ok' onclick=\"{apply_call}\">Apply</button>"
        "</div>"
    )


def render(*, values: dict[str, Any], knobs: dict[str, Knob]) -> str:
    groups: dict[str, list[str]] = {}
    for name, knob in knobs.items():
        groups.setdefault(knob.group, []).append(name)

    sections = ""
    for group, names in groups.items():
        rows = "".join(_row_html(name, knobs[name], values[name]) for name in names)
        sections += (
            f"<div class='settings-group'><div class='settings-group-h'>{escape(group)}</div>"
            f"{rows}</div>"
        )

    return (
        "<section class='card wide'>"
        "<div class='card-h'>SETTINGS"
        "<span class='win'>runtime · no restart · applies on the next tick</span></div>"
        "<div class='gr-toggle-hint' style='margin-bottom:10px'>"
        "every control here used to be a .env value — changes take effect immediately, "
        "no restart, and are logged to the activity feed."
        "</div>"
        f"{sections}"
        "</section>"
    )
