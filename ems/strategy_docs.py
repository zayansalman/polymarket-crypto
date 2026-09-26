"""One document per strategy family, kept in step with the code it describes.

Every family in ``inventory.FAMILIES`` has ``docs/strategies/<key>.md``: what
it does, how it was formed, how it works, its sources and a changelog. The
dashboard serves them at ``/strategy-docs``.

Each doc carries a generated block — the family's name, status, switch, code
path and a fingerprint of that code — and a changelog whose newest entry
cites the current fingerprint. Change the code, the name, the status or the
switch and the fingerprint moves; ``tests/unit/test_strategy_docs.py`` then
fails until the doc is re-stamped with a note saying what changed::

    python tools/strategy_docs.py stamp <key> "what changed and why"

The note is the point. A fingerprint alone proves the doc was touched; the
note is what makes the changelog worth reading a month later.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ems import inventory as _inv
from ems.strategies import STRATEGIES

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs" / "strategies"
INDEX_FILES = frozenset({"README.md"})

REQUIRED_SECTIONS: tuple[str, ...] = (
    "What it does",
    "How it was formed",
    "How it works",
    "Sources",
    "Changelog",
)

GLANCE = "At a glance"
"""Optional section: the short summary the dashboard's STRATEGY card shows,
one ``### `` part per entry in ``GLANCE_PARTS``. A doc that has it must have
every part; a doc without it just stays off the card's dropdown."""
GLANCE_PARTS: tuple[str, ...] = (
    "Concept",
    "Main assumption",
    "The maths",
    "How it works",
    "How it was derived",
    "References",
)

_BEGIN = "<!-- BEGIN GENERATED:strategy -->"
_END = "<!-- END GENERATED:strategy -->"
_GENERATED_RE = re.compile(re.escape(_BEGIN) + r"\n(.*?)\n" + re.escape(_END), re.S)
_TITLE_RE = re.compile(r"^# (.+)$", re.M)
_SECTION_RE = re.compile(r"^## (.+)$", re.M)
_PART_RE = re.compile(r"^### (.+)$", re.M)
_ENTRY_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · `([0-9a-f]{12})` · (.*)$", re.M)


@dataclass(frozen=True)
class ChangelogEntry:
    day: str
    fingerprint: str
    note: str


@dataclass
class Doc:
    title: str
    generated: str | None
    sections: dict[str, str] = field(default_factory=dict)
    changelog: list[ChangelogEntry] = field(default_factory=list)


def family(key: str) -> _inv.Family:
    for fam in _inv.FAMILIES:
        if fam.key == key:
            return fam
    raise KeyError(f"no strategy family {key!r} in ems/inventory.py")


def doc_path(key: str) -> Path:
    return DOCS_DIR / f"{key}.md"


def tracked_files(fam: _inv.Family) -> list[Path]:
    """The code a family's doc answers for: its file, or every .py under its package."""
    if not fam.path:
        return []
    target = ROOT / fam.path
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*.py") if "__pycache__" not in p.parts)
    return []


def fingerprint(fam: _inv.Family) -> str:
    """12 hex chars over the family's identity and every tracked file's bytes.

    Identity (name, status, path, switch) is in the hash because a
    renamed or re-statused strategy is a changed strategy. ``record`` and
    ``verdict`` are not: they are running commentary, not behaviour.
    """
    h = hashlib.sha256()
    # "mine" is the value every family held in the removed ``Family.group``.
    # Hashing it keeps each doc's fingerprint unchanged by that removal.
    for part in (fam.key, fam.label, fam.status, fam.path, fam.switch or "", "mine"):
        h.update(part.encode() + b"\n")
    for path in tracked_files(fam):
        h.update(path.relative_to(ROOT).as_posix().encode() + b"\n")
        h.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\n")
    return h.hexdigest()[:12]


def _switch_text(fam: _inv.Family) -> str:
    if fam.switch is None or fam.switch not in STRATEGIES:
        return "none — nothing to turn on"
    return f"`{fam.switch}` on the MY STRATEGIES card"


def generated_block(fam: _inv.Family) -> str:
    files = tracked_files(fam)
    code = f"`{fam.path}`" if fam.path else "source deleted"
    if len(files) > 1:
        code += f" — {len(files)} files"
    rows = [
        ("Name", fam.label),
        ("Key", f"`{fam.key}`"),
        ("Status", _inv.STATUS_LABEL[fam.status]),
        ("Switch", _switch_text(fam)),
        ("Code", code),
        ("Code fingerprint", f"`{fingerprint(fam)}`"),
    ]
    lines = ["| | |", "|---|---|", *(f"| {k} | {v} |" for k, v in rows)]
    return "\n".join(lines)


def parse(text: str) -> Doc:
    title_m = _TITLE_RE.search(text)
    gen_m = _GENERATED_RE.search(text)
    doc = Doc(
        title=title_m.group(1).strip() if title_m else "",
        generated=gen_m.group(1) if gen_m else None,
    )
    heads = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        doc.sections[m.group(1).strip()] = text[m.end():end].strip()
    doc.changelog = [
        ChangelogEntry(day, fp, note.strip())
        for day, fp, note in _ENTRY_RE.findall(doc.sections.get("Changelog", ""))
    ]
    return doc


def glance_parts(doc: Doc) -> dict[str, str]:
    """``{part: markdown}`` from a parsed doc's At a glance section, in file order."""
    body = doc.sections.get(GLANCE, "")
    heads = list(_PART_RE.finditer(body))
    return {
        m.group(1).strip(): body[m.end():(heads[i + 1].start() if i + 1 < len(heads) else len(body))].strip()
        for i, m in enumerate(heads)
    }


def glance(key: str) -> dict[str, str]:
    """One family's At a glance parts; empty when its doc has none (or no doc)."""
    path = doc_path(key)
    if not path.exists():
        return {}
    return glance_parts(parse(path.read_text(encoding="utf-8")))


def problems(fam: _inv.Family) -> list[str]:
    """Everything wrong with one family's doc; empty when it is in step."""
    path = doc_path(fam.key)
    if not path.exists():
        return [f"no doc at {path.relative_to(ROOT)} — run: python tools/strategy_docs.py new {fam.key}"]
    doc = parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    if doc.title != fam.label:
        out.append(f"title is {doc.title!r} but the family is called {fam.label!r}")
    out += [f"missing section: ## {s}" for s in REQUIRED_SECTIONS if s not in doc.sections]
    if doc.generated is None:
        out.append("no generated block")
    elif doc.generated != generated_block(fam):
        out.append("generated block is stale: the code, name, status or switch changed")
    current = fingerprint(fam)
    if not doc.changelog:
        out.append("changelog has no entries")
    else:
        newest = doc.changelog[0]
        if newest.fingerprint != current:
            out.append(
                f"newest changelog entry is for `{newest.fingerprint}`, the code is now "
                f"`{current}` — run: python tools/strategy_docs.py stamp {fam.key} \"what changed\""
            )
        if not newest.note:
            out.append("newest changelog entry has no note")
        days = [e.day for e in doc.changelog]
        if days != sorted(days, reverse=True):
            out.append("changelog is not newest-first")
    if "Sources" in doc.sections and not doc.sections["Sources"]:
        out.append("Sources is empty")
    if GLANCE in doc.sections:
        parts = glance_parts(doc)
        out += [
            f"{GLANCE} is missing ### {p}" if p not in parts else f"{GLANCE} has an empty ### {p}"
            for p in GLANCE_PARTS
            if not parts.get(p)
        ]
    return out


def orphans() -> list[Path]:
    """Docs whose family no longer exists."""
    keys = {f.key for f in _inv.FAMILIES}
    return sorted(
        p for p in DOCS_DIR.glob("*.md")
        if p.name not in INDEX_FILES and p.stem not in keys
    )


def all_problems() -> dict[str, list[str]]:
    report = {f.key: problems(f) for f in _inv.FAMILIES}
    for p in orphans():
        report[p.stem] = [f"{p.relative_to(ROOT)} has no family in inventory.py"]
    return {k: v for k, v in report.items() if v}


def is_current(fam: _inv.Family) -> bool:
    return not problems(fam)


def stamp(key: str, note: str, today: date | None = None) -> Path:
    """Refresh the generated block and add a newest-first changelog entry."""
    note = " ".join(note.split())
    if not note:
        raise ValueError("a changelog note is required: say what changed and why")
    fam = family(key)
    path = doc_path(key)
    text = path.read_text(encoding="utf-8")
    if not _GENERATED_RE.search(text):
        raise ValueError(f"{path.relative_to(ROOT)} has no generated block to refresh")
    text = _GENERATED_RE.sub(lambda _m: f"{_BEGIN}\n{generated_block(fam)}\n{_END}", text, count=1)
    entry = f"- {(today or date.today()).isoformat()} · `{fingerprint(fam)}` · {note}"
    head = re.search(r"^## Changelog[ \t]*\n+", text, re.M)
    if head is None:
        text = text.rstrip("\n") + f"\n\n## Changelog\n\n{entry}\n"
    else:
        text = text[: head.end()] + entry + "\n" + text[head.end():]
    path.write_text(text, encoding="utf-8")
    return path


def scaffold(key: str, today: date | None = None) -> str:
    """A new doc with every required section and one changelog entry."""
    fam = family(key)
    day = (today or date.today()).isoformat()
    return (
        f"# {fam.label}\n\n"
        f"{_BEGIN}\n{generated_block(fam)}\n{_END}\n\n"
        "## What it does\n\n"
        f"{fam.what}\n\n"
        "## How it was formed\n\n"
        "Who proposed it, when, and what research or result led to it.\n\n"
        "## How it works\n\n"
        "Inputs, the calculation, the bet or quote it produces.\n\n"
        "## Sources\n\n"
        "- Papers, datasets, scripts, pre-registrations, issues.\n\n"
        "## Changelog\n\n"
        f"- {day} · `{fingerprint(fam)}` · Doc created.\n"
    )
