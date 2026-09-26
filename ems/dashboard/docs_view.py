r"""Strategy docs in the dashboard: ``/strategy-docs`` and ``/strategy-docs/<key>``.

Each strategy family has a markdown doc under ``docs/strategies/`` (the rules
that keep it in step with the code are in ``ems/strategy_docs.py``).
This module only renders them. The pages are read-only: no dashboard token,
no trading controls and no live script, so nothing on them can move money.

Maths is written ``$...$`` / ``$$...$$``. Markdown would eat the backslashes
and underscores inside it, so math spans are lifted out before rendering and
put back as ``\(...\)`` / ``\[...\]`` for KaTeX, which is told to look for
nothing else — a dollar amount in prose is never mistaken for maths. Inline
``$`` follows pandoc's rule: no space just inside either delimiter, and the
closing ``$`` is not followed by a digit.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from ems import inventory as _inv
from ems import strategy_docs as _sd

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_MD = MarkdownIt("commonmark", {"html": False}).enable("table")

_COMMENT_LINE = re.compile(r"^[ \t]*<!--.*?-->[ \t]*\n?", re.M)
"""Whole-line HTML comments, e.g. the GENERATED markers. Raw HTML is off, so
markdown would otherwise print them as text."""
_FENCE = re.compile(r"^(```|~~~)[^\n]*\n.*?^\1[ \t]*$", re.M | re.S)
_CODE_SPAN = re.compile(r"(`+)[^`].*?(?<!`)\1(?!`)", re.S)
_DISPLAY = re.compile(r"\$\$(.+?)\$\$", re.S)
_INLINE = re.compile(r"(?<!\\)\$(?=[^\s$])([^\n$]*?[^\s$\\])\$(?!\d)")


def register(app: FastAPI) -> None:
    """Add the two docs pages to ``app``.

    Registered as plain routes rather than an included ``APIRouter``: newer
    FastAPI lists an included router as one entry with no ``path``, which
    breaks code and tests that walk ``app.routes``.
    """
    app.add_api_route("/strategy-docs", docs_index, methods=["GET"], response_class=HTMLResponse)
    app.add_api_route("/strategy-docs/{key}", strategy_doc, methods=["GET"], response_class=HTMLResponse)


def render_markdown(text: str) -> str:
    """Markdown to HTML with ``$`` maths preserved for KaTeX."""
    code: list[str] = []
    maths: list[tuple[bool, str]] = []

    def keep_code(m: re.Match[str]) -> str:
        code.append(m.group(0))
        return f"KXCODE{len(code) - 1}KX"

    def keep_math(display: bool):
        def _sub(m: re.Match[str]) -> str:
            maths.append((display, m.group(1).strip()))
            return f"KXMATH{len(maths) - 1}KX"
        return _sub

    text = _COMMENT_LINE.sub("", text)
    text = _FENCE.sub(keep_code, text)
    text = _CODE_SPAN.sub(keep_code, text)
    text = _DISPLAY.sub(keep_math(True), text)
    text = _INLINE.sub(keep_math(False), text)
    text = re.sub(r"KXCODE(\d+)KX", lambda m: code[int(m.group(1))], text)

    html = _MD.render(text)

    def put_back(m: re.Match[str]) -> str:
        display, tex = maths[int(m.group(2))]
        if display:
            return f'<div class="math-display">\\[{escape(tex, quote=False)}\\]</div>'
        return f'<span class="math-inline">\\({escape(tex, quote=False)}\\)</span>'

    html = re.sub(r"(<p>)?KXMATH(\d+)KX(</p>)?", lambda m: _wrap_back(m, put_back), html)
    return html


def _wrap_back(m: re.Match[str], put_back) -> str:
    # A display block on its own line comes back as <p>PH</p>; a <div> cannot
    # sit inside a <p>, so the paragraph tags go with the placeholder.
    inner = put_back(m)
    if m.group(1) and m.group(3):
        return inner
    return f"{m.group(1) or ''}{inner}{m.group(3) or ''}"


def _families_by_status() -> list[tuple[str, list[dict[str, object]]]]:
    groups: list[tuple[str, list[dict[str, object]]]] = []
    for status in _inv.STATUS_ORDER:
        rows = [
            {
                "key": f.key,
                "label": f.label,
                "what": f.what,
                "current": _sd.is_current(f),
                "has_doc": _sd.doc_path(f.key).exists(),
            }
            for f in _inv.FAMILIES
            if f.status == status
        ]
        if rows:
            groups.append((_inv.STATUS_LABEL[status], rows))
    return groups


async def docs_index(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "docs.html",
        {"title": "Strategy docs", "groups": _families_by_status(), "body": None, "problems": []},
    )


async def strategy_doc(request: Request, key: str) -> HTMLResponse:
    key = key.removesuffix(".md")
    try:
        fam = _sd.family(key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no strategy called {key!r}") from None
    path = _sd.doc_path(key)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{fam.label} has no doc yet")
    return _TEMPLATES.TemplateResponse(
        request,
        "docs.html",
        {
            "title": fam.label,
            "groups": None,
            "body": render_markdown(path.read_text(encoding="utf-8")),
            "problems": _sd.problems(fam),
        },
    )
