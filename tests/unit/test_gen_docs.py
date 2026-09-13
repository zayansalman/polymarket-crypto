# tests/unit/test_gen_docs.py
from pathlib import Path
import tools.gen_docs as gd


def _make_tree(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""Pkg root."""\n')
    (pkg / "alpha.py").write_text('"""Computes alpha."""\nX = 1\n')
    (pkg / "beta.py").write_text("X = 2\n")  # no docstring
    return tmp_path


def test_inventory_reads_roles(tmp_path):
    root = _make_tree(tmp_path)
    mods = gd.collect_modules(root, source_roots=["pkg"], toplevel=[])
    by_path = {m.path: m for m in mods}
    assert by_path["pkg/alpha.py"].role == "Computes alpha."
    assert by_path["pkg/beta.py"].role == gd.NO_DOCSTRING
    assert by_path["pkg/__init__.py"].role == "Pkg root."


def test_import_graph_flags_dead(tmp_path):
    root = tmp_path
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "alpha.py").write_text('"""A."""\nX = 1\n')
    (pkg / "beta.py").write_text('"""B."""\nfrom pkg.alpha import X\n')  # imports alpha
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("from pkg.beta import X\n")  # test-only importer

    mods = gd.collect_modules(root, source_roots=["pkg"], toplevel=[])
    gd.annotate_importers(root, mods, test_dirs=["tests"])
    by = {m.path: m for m in mods}
    assert by["pkg/alpha.py"].status == "WIRED"   # imported by beta (non-test)
    assert by["pkg/beta.py"].status == "DEAD?"     # only a test imports it


def test_annotate_importers_excludes_venv(tmp_path):
    """Import graph must ignore .venv/build dirs whose modules collide with ours.

    Third-party files in a virtualenv often reference short generic names
    (`main`, `config`) that collide with our top-level modules. They must not
    inflate importer counts.
    """
    root = tmp_path
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""Pkg root."""\n')
    (pkg / "alpha.py").write_text('"""A."""\n')
    (pkg / "beta.py").write_text("from pkg.alpha import *\n")  # real source importer
    (root / "main.py").write_text('"""M."""\n')  # top-level collision target
    venv_lib = root / ".venv" / "lib"
    venv_lib.mkdir(parents=True)
    # Third-party file referencing OUR names — must be excluded from the walk.
    (venv_lib / "junk.py").write_text(
        "import pkg\nfrom pkg import alpha\nimport main\n"
    )

    mods = gd.collect_modules(
        root, source_roots=["pkg"], toplevel=["main.py"]
    )
    gd.annotate_importers(root, mods, test_dirs=["tests"])
    by = {m.path: m for m in mods}
    # .venv reference excluded -> main.py has zero source importers.
    assert by["main.py"].importers == 0
    # Only pkg/beta.py (a real source file) counts toward pkg/alpha.py.
    assert by["pkg/alpha.py"].importers == 1


def test_status_taxonomy_pkg_cli_wired_dead(tmp_path):
    """pkg / cli / WIRED / DEAD? precedence resolves correctly."""
    root = tmp_path
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""Pkg root."""\n')  # -> pkg
    (pkg / "wired.py").write_text('"""Wired module."""\nX = 1\n')  # imported below -> WIRED
    (pkg / "user.py").write_text('"""Imports wired."""\nfrom pkg.wired import X\n')
    (pkg / "lonely.py").write_text('"""Nobody imports me."""\nY = 2\n')  # -> DEAD?
    tools = root / "tools"
    tools.mkdir()
    (tools / "script.py").write_text(
        '"""A CLI script."""\n\n\ndef main():\n    pass\n\n\nif __name__ == "__main__":\n    main()\n'
    )  # under tools/ and has __main__ guard -> cli

    mods = gd.collect_modules(
        root, source_roots=["pkg", "tools"], toplevel=[]
    )
    gd.annotate_importers(root, mods, test_dirs=["tests"])
    by = {m.path: m for m in mods}
    assert by["pkg/__init__.py"].status == "pkg"
    assert by["tools/script.py"].status == "cli"
    assert by["pkg/wired.py"].status == "WIRED"
    assert by["pkg/user.py"].status == "DEAD?"   # imports but nobody imports it
    assert by["pkg/lonely.py"].status == "DEAD?"


def test_main_guard_outside_tools_is_cli(tmp_path):
    """A __main__ guard alone (not under tools/) still yields cli."""
    root = tmp_path
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "runme.py").write_text(
        '"""Runnable."""\nif __name__ == "__main__":\n    print("hi")\n'
    )
    mods = gd.collect_modules(root, source_roots=["pkg"], toplevel=[])
    gd.annotate_importers(root, mods, test_dirs=["tests"])
    by = {m.path: m for m in mods}
    assert by["pkg/runme.py"].status == "cli"


def test_summary_dead_list_excludes_pkg_and_cli(tmp_path):
    """render_summary dead-list must contain only genuine DEAD? modules."""
    root = tmp_path
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""Pkg."""\n')  # pkg
    (pkg / "lonely.py").write_text('"""Dead."""\nY = 2\n')  # DEAD?
    tools = root / "tools"
    tools.mkdir()
    (tools / "script.py").write_text(
        '"""CLI."""\nif __name__ == "__main__":\n    pass\n'
    )  # cli
    mods = gd.collect_modules(root, source_roots=["pkg", "tools"], toplevel=[])
    gd.annotate_importers(root, mods, test_dirs=["tests"])
    dead = [m.path for m in mods if m.status == "DEAD?"]
    assert dead == ["pkg/lonely.py"]
    assert "pkg/__init__.py" not in dead
    assert "tools/script.py" not in dead


def test_collect_env_knobs_canonical_and_sorted(tmp_path):
    cfg = (
        '"""config."""\n'
        "import os\n"
        'A = os.environ.get("TRADE_MAX_USD", "5")\n'
        'B = os.environ.get("LIVE_MAX_USD", "10")\n'
        'NOT_A_KNOB = os.environ.get("PATH", "")\n'
        'lowercase = "btc_not_upper"\n'
    )
    (tmp_path / "config.py").write_text(cfg)
    knobs = gd.collect_env_knobs(tmp_path)
    assert "TRADE_MAX_USD" in knobs
    assert "LIVE_MAX_USD" in knobs
    assert "PATH" not in knobs
    assert "btc_not_upper" not in knobs
    assert knobs == sorted(knobs)


def test_real_tree_wiring_truth():
    """The load-bearing facts the docs must never get wrong."""
    mods = gd.collect_modules(gd.REPO)
    gd.annotate_importers(gd.REPO, mods)
    by = {m.path: m for m in mods}
    # The live loop and its signal math are WIRED.
    assert by["polymarket_bot/paper.py"].status == "WIRED"
    assert by["polymarket_bot/strategy.py"].status == "WIRED"
    # The live risk gate + executor are WIRED.
    assert by["polymarket_exec/execution/gate.py"].status == "WIRED"
    assert by["polymarket_exec/execution/live.py"].status == "WIRED"
    # Known dead-in-active-tree module is flagged.
    assert by["polymarket_exec/ops/controller.py"].status == "DEAD?"


def test_test_count_is_positive_int():
    n = gd.count_tests(gd.REPO)
    assert isinstance(n, int) and n > 300  # currently ~488


def test_entrypoint_importable():
    assert gd.entrypoint_ok(gd.REPO) is True  # polymarket_exec.ops.dashboard.app imports


def test_replace_block_is_idempotent():
    doc = "intro\n<!-- BEGIN GENERATED:x -->\nOLD\n<!-- END GENERATED:x -->\nrest\n"
    once = gd.replace_block(doc, "x", "NEW")
    twice = gd.replace_block(once, "x", "NEW")
    assert "NEW" in once and "OLD" not in once
    assert once == twice  # deterministic / idempotent


def test_render_file_map_deterministic():
    a = gd.render_file_map(gd.REPO)
    b = gd.render_file_map(gd.REPO)
    assert a == b and a.startswith("<!-- GENERATED by tools/gen_docs.py")


def test_fast_run_preserves_existing_test_count(tmp_path):
    """--fast must keep the committed `- **Tests:** N.` line, not clobber it.

    The PostToolUse hook runs `gen_docs.py --fast` on every edit; if it
    replaced the real count with the `(see FILE_MAP)` placeholder, `--check`
    (Stop hook + CI `docs-drift`) would report permanent drift.
    """
    root = tmp_path
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('"""Pkg."""\n')
    (pkg / "alpha.py").write_text('"""A."""\nX = 1\n')
    (root / "config.py").write_text('"""config."""\n')
    (root / "docs").mkdir()
    # A doc carrying a summary block with a real, committed test count.
    doc = (
        "# Map\n\n"
        "<!-- BEGIN GENERATED:summary -->\n"
        "- **Tests:** 515.\n"
        "<!-- END GENERATED:summary -->\n"
    )
    (root / "docs" / "CODE_MAP.md").write_text(doc)

    gd._write_generated(
        root,
        fast=True,
        source_roots=["pkg"],
        toplevel=["config.py"],
        targets=[("docs/CODE_MAP.md", ("summary",))],
    )

    out = (root / "docs" / "CODE_MAP.md").read_text()
    assert "- **Tests:** 515." in out
    assert "(see FILE_MAP)" not in out


def test_existing_test_count_extracts_digits():
    assert gd._existing_test_count("- **Tests:** 515.") == "515"
    assert gd._existing_test_count("blah\n- **Tests:** 42.\nmore") == "42"
    assert gd._existing_test_count("- **Tests:** (see FILE_MAP).") is None
    assert gd._existing_test_count("no count here") is None
