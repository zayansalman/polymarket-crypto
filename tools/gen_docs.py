"""Generate the machine-derived sections of the agent docs.

Owns the facts that rot: module inventory + roles, wired-vs-dead status,
env-knob table, test count. Writes docs/FILE_MAP.md in full and replaces
fenced GENERATED blocks in AGENTS.md and docs/CODE_MAP.md.

Deterministic: stable sort, no timestamps, so `git diff --exit-code` is meaningful.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NO_DOCSTRING = "(needs docstring)"

SOURCE_ROOTS = ["ems", "tools"]
TOPLEVEL_MODULES = ["main.py"]
CONFIG_PATH = "ems/config.py"
# Entrypoints / foundation: never flagged DEAD even with zero importers.
WIRED_ALLOWLIST = {"main.py", "ems/config.py", "ems/db.py", "ems/logging_setup.py"}

# Virtualenv/build/VCS dirs: never walk into these. Third-party packages have
# modules/symbols with short generic names (`main`, `config`) that collide with
# our top-level modules and would inflate importer counts. `*.egg-info` is
# handled separately (suffix match). `.claude` holds full repo copies under
# `.claude/worktrees/<name>/`; walking them would count every copy as an importer.
EXCLUDE_DIRS = {".venv", "venv", "env", "__pycache__", ".git", ".pytest_cache",
                "build", "dist", "node_modules", ".mypy_cache", ".ruff_cache",
                ".claude"}


def _iter_py_files(root: Path):
    """Yield `*.py` files under `root`, skipping virtualenv/build/VCS dirs.

    Deterministic (sorted) and stdlib-only. Excludes any path with a part in
    `EXCLUDE_DIRS` or a part ending in `.egg-info`. Only parts below `root` are
    checked, so a root that itself lives in `.claude/worktrees/<name>/` is
    still scanned.
    """
    for p in sorted(root.rglob("*.py")):
        parts = p.relative_to(root).parts
        if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in parts):
            continue
        yield p


@dataclass
class Module:
    path: str               # repo-relative posix path
    role: str               # first line of top docstring, or NO_DOCSTRING
    importers: int = 0      # count of non-test modules importing this one
    has_main: bool = False  # has an `if __name__ == "__main__":` guard

    @property
    def status(self) -> str:
        """Resolve status by precedence: pkg → cli → WIRED → DEAD?.

        - `pkg`:   package marker (`__init__.py`); imported via the package,
                   not by dotted path, so a zero importer count is expected.
        - `cli`:   entrypoint script (under `tools/` or carries a `__main__`
                   guard); run directly, never imported, so also not alarming.
        - `WIRED`: in the allowlist or has at least one non-test importer.
        - `DEAD?`: built but no importers found — investigate before relying on it.
        """
        name = self.path.rsplit("/", 1)[-1]
        if name == "__init__.py":
            return "pkg"
        if self.path.startswith("tools/") or self.has_main:
            return "cli"
        if self.path in WIRED_ALLOWLIST or self.importers > 0:
            return "WIRED"
        return "DEAD?"


def _role_from_source(text: str) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return NO_DOCSTRING
    doc = ast.get_docstring(tree)
    if not doc:
        return NO_DOCSTRING
    return doc.strip().splitlines()[0].strip()


def _has_main_guard(text: str) -> bool:
    """True if the source has an `if __name__ == "__main__":` guard (AST-based)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Fall back to a substring probe so unparsable files still classify.
        return 'if __name__ == "__main__"' in text
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        left = test.left
        comparators = test.comparators
        names = []
        if isinstance(left, ast.Name):
            names.append(left.id)
        for c in comparators:
            if isinstance(c, ast.Constant) and isinstance(c.value, str):
                names.append(c.value)
        if "__name__" in names and "__main__" in names:
            return True
    return False


def _read_text(p: Path) -> str:
    """Read a file tolerantly so odd bytes can't crash doc generation."""
    return p.read_text(encoding="utf-8", errors="replace")


def _make_module(root: Path, p: Path, rel: str) -> Module:
    text = _read_text(p)
    return Module(
        path=rel,
        role=_role_from_source(text),
        has_main=_has_main_guard(text),
    )


def collect_modules(root: Path, source_roots=SOURCE_ROOTS, toplevel=TOPLEVEL_MODULES):
    mods: list[Module] = []
    for rel in toplevel:
        p = root / rel
        if p.exists():
            mods.append(_make_module(root, p, rel))
    for sr in source_roots:
        base = root / sr
        if not base.exists():
            continue
        for p in _iter_py_files(base):
            rel = p.relative_to(root).as_posix()
            mods.append(_make_module(root, p, rel))
    return sorted(mods, key=lambda m: m.path)


def _dotted_name(rel_path: str) -> str:
    parts = rel_path[:-3].split("/")  # strip .py
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_targets(text: str, pkg: str = "") -> set[str]:
    """Dotted names this module references via import / from-import.

    ``pkg`` is the importing module's own package (dotted); it resolves
    relative imports (``from . import x``, ``from ..y import z``) to absolute
    names so they count as importers like any other.
    """
    targets: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return targets
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                targets.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: anchor on the importer's package
                base_parts = pkg.split(".") if pkg else []
                if node.level > 1:
                    base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                base = ".".join(base_parts)
                module = ".".join(p for p in (base, node.module) if p)
            else:
                module = node.module or ""
            if not module:
                continue
            targets.add(module)
            for a in node.names:  # `from pkg import mod` → pkg.mod is a candidate
                targets.add(f"{module}.{a.name}")
    return targets


def _package_of(rel_path: str) -> str:
    """Dotted package that relative imports inside *rel_path* resolve against."""
    dotted = _dotted_name(rel_path)
    if rel_path.endswith("/__init__.py"):
        return dotted
    return dotted.rsplit(".", 1)[0] if "." in dotted else ""


def annotate_importers(root: Path, mods, test_dirs=("tests",)) -> None:
    known = {_dotted_name(m.path): m for m in mods}
    for src in _iter_py_files(root):
        rel = src.relative_to(root).as_posix()
        if any(rel.startswith(td + "/") or rel == td for td in test_dirs):
            continue  # test importers don't count toward "wired"
        for tgt in _imported_targets(_read_text(src), _package_of(rel)):
            mod = known.get(tgt)
            if mod is not None and mod.path != rel:
                mod.importers += 1


def count_tests(root: Path) -> int:
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
             "--continue-on-collection-errors"],
            cwd=root, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return 0  # fail soft — a hung collection must not break doc generation
    # Exit code is nonzero whenever ANY module fails to collect (e.g. missing
    # optional local deps like py_clob_client_v2/h2), even though
    # --continue-on-collection-errors still collects and reports everything
    # else. Discarding the whole count on that made this silently report 0
    # real tests instead of ~800 — do not gate on returncode here; the
    # trailing-line parser below already returns 0 for a genuinely empty or
    # malformed run.
    # pytest prints a trailing summary line like "488 tests collected in 1.2s"
    # (or "822 tests collected, 3 errors in 0.5s" with errors present).
    for line in reversed(out.stdout.splitlines()):
        line = line.strip()
        if "test" in line and line.split() and line.split()[0].isdigit():
            return int(line.split()[0])
    return 0


def entrypoint_ok(root: Path) -> bool:
    try:
        res = subprocess.run(
            [sys.executable, "-c", "import ems.dashboard.app"],
            cwd=root, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False  # fail soft — treat a hung import as "not ok"
    return res.returncode == 0


def collect_env_knobs(root: Path, config_rel: str = CONFIG_PATH):
    """Parse config.py for * knob names + their deprecated aliases.

    Returns sorted list of (canonical, default, deprecated_alias|''). Best-effort:
    reads the literal os.environ.get / _env_* string args via AST. A knob name
    is an ALL_CAPS, multi-word (underscore-joined) constant — this excludes
    bare single-word env vars like ``PATH`` that aren't project knobs.
    """
    cfg = _read_text(root / config_rel)
    tree = ast.parse(cfg)
    knobs: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if "_" in v and v.isupper():
                knobs.setdefault(v, "")
    return sorted(knobs)


GEN_HEADER = "<!-- GENERATED by tools/gen_docs.py — DO NOT EDIT BY HAND -->"


def replace_block(doc: str, name: str, new_body: str) -> str:
    begin = f"<!-- BEGIN GENERATED:{name} -->"
    end = f"<!-- END GENERATED:{name} -->"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    repl = f"{begin}\n{new_body.rstrip()}\n{end}"
    if not pattern.search(doc):
        raise ValueError(f"marker block '{name}' not found")
    return pattern.sub(repl, doc)


def _table(mods) -> str:
    rows = ["| Module | Status | Importers | Role |", "|---|---|---|---|"]
    for m in mods:
        rows.append(f"| `{m.path}` | {m.status} | {m.importers} | {m.role} |")
    return "\n".join(rows)


FILE_MAP_LEGEND = (
    "_Status: `WIRED` = has non-test importers; `DEAD?` = no importers found "
    "(investigate); `pkg` = package marker; `cli` = entrypoint script "
    "(run directly)._"
)


def render_file_map(root: Path) -> str:
    mods = collect_modules(root)
    annotate_importers(root, mods)
    return f"{GEN_HEADER}\n\n# File Map\n\n{FILE_MAP_LEGEND}\n\n{_table(mods)}\n"


PLACEHOLDER_TEST_COUNT = "(see FILE_MAP)"
_TEST_COUNT_RE = re.compile(r"- \*\*Tests:\*\* (\d+)\.")


def _existing_test_count(doc_text: str) -> str | None:
    """Extract the digits from an existing `- **Tests:** N.` line, else None.

    Used by the `--fast` path to preserve a committed test count rather than
    recomputing it (slow) or emitting the `(see FILE_MAP)` placeholder.
    """
    m = _TEST_COUNT_RE.search(doc_text)
    return m.group(1) if m else None


def render_summary(
    root: Path,
    with_test_count: bool = True,
    test_count: str | None = None,
    source_roots=SOURCE_ROOTS,
    toplevel=TOPLEVEL_MODULES,
) -> str:
    """Render the orientation summary block.

    Test-count resolution, in order:
    - explicit `test_count` (string) → used verbatim in the Tests bullet;
    - else `with_test_count=True` → compute the real count via `count_tests`;
    - else → the `(see FILE_MAP)` placeholder.
    """
    mods = collect_modules(root, source_roots=source_roots, toplevel=toplevel)
    annotate_importers(root, mods)
    dead = [m.path for m in mods if m.status == "DEAD?"]
    if test_count is not None:
        n = test_count
    elif with_test_count:
        n = count_tests(root)
    else:
        n = PLACEHOLDER_TEST_COUNT
    lines = [
        "- **Layout:** one package, `ems/`: `fade_1h_momentum_15m/` and `kelly_horse_race/` = the strategies, `execution/` = the execution layer every strategy shares (resting-order venues, risk gate, fill model, tape and result reads, mode, kill switch), `marketdata/` = the WebSocket market-data hub it reads, `connectors/updown_quote.py` = live top-of-book for the hub, `dashboard/` = the FastAPI operator UI, `strategies.py` / `inventory.py` / `runtime_knobs.py` / `strategy_docs.py` = switches, inventory, knobs and docs, `config.py` / `db.py` / `logging_setup.py` = foundation.",
        "- **Entry:** `python main.py` → FastAPI `ems/dashboard/app.py`; its lifespan starts the hub, then the strategies.",
        f"- **Tests:** {n}.",
        f"- **Built-but-dead (do not edit expecting runtime effect):** {', '.join(f'`{d}`' for d in dead) or 'none'}.",
    ]
    return "\n".join(lines)


def _table_section(root: Path) -> str:
    mods = collect_modules(root)
    annotate_importers(root, mods)
    return _table(mods)


DEFAULT_TARGETS = (
    ("AGENTS.md", ("summary",)),
    ("docs/CODE_MAP.md", ("summary", "inventory")),
)


def _write_generated(
    root: Path,
    fast: bool,
    source_roots=SOURCE_ROOTS,
    toplevel=TOPLEVEL_MODULES,
    targets=DEFAULT_TARGETS,
) -> None:
    (root / "docs" / "FILE_MAP.md").write_text(render_file_map(root))
    inv = _table_section(root)
    # Non-fast: compute the real count once and inject it into ALL targets, so
    # AGENTS.md and CODE_MAP.md get the SAME number (and `count_tests` — slow —
    # runs only once). Fast: leave None and resolve per-target from its own doc.
    full_count = None if fast else str(count_tests(root))
    for rel, block_names in targets:
        p = root / rel
        if not p.exists():
            continue
        doc = _read_text(p)
        if fast:
            # Preserve the committed count; fall back to placeholder if absent.
            count = _existing_test_count(doc) or PLACEHOLDER_TEST_COUNT
        else:
            count = full_count
        summary = render_summary(
            root, test_count=count, source_roots=source_roots, toplevel=toplevel
        )
        bodies = {"summary": summary, "inventory": inv}
        for name in block_names:
            try:
                doc = replace_block(doc, name, bodies[name])
            except ValueError:
                pass  # block not present in this doc
        p.write_text(doc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate agent docs.")
    ap.add_argument("--check", action="store_true", help="fail if generation would change committed docs")
    ap.add_argument("--fast", action="store_true", help="skip test-count (for hooks)")
    ap.add_argument("--print-summary", action="store_true", help="print the orientation summary and exit")
    args = ap.parse_args(argv)

    if args.print_summary:
        print(render_summary(REPO, with_test_count=False))
        return 0

    if args.check:
        before = {p: _read_text(REPO / p) for p in ["docs/FILE_MAP.md", "AGENTS.md", "docs/CODE_MAP.md"] if (REPO / p).exists()}
        _write_generated(REPO, fast=False)
        changed = [p for p, txt in before.items() if _read_text(REPO / p) != txt]
        if changed:
            print("DOC DRIFT — regenerate with `python tools/gen_docs.py`:\n  " + "\n  ".join(changed), file=sys.stderr)
            return 1
        return 0

    _write_generated(REPO, fast=args.fast)
    if not entrypoint_ok(REPO):
        print("WARNING: ems.dashboard.app failed to import — main.py cannot boot the dashboard.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
