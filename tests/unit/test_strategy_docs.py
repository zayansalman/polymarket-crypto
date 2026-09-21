"""Every strategy has a doc, and the doc moves when the strategy does.

The first test is the one that bites in CI: change a strategy's code, name,
status or switch and it fails until the doc is re-stamped with a note —
``python tools/strategy_docs.py stamp <key> "what changed"``. The rest pin the
mechanism on a throwaway family so it cannot quietly stop working.
"""

from __future__ import annotations

from datetime import date

import pytest

from polymarket_bot import inventory as _inv
from polymarket_bot import strategy_docs as sd


def test_every_strategy_doc_is_in_step_with_its_code() -> None:
    report = sd.all_problems()
    lines = [f"{key}: {issue}" for key, issues in report.items() for issue in issues]
    assert not report, "strategy docs out of step with the code:\n" + "\n".join(lines)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A throwaway repo with one family and its code."""
    code = tmp_path / "strat.py"
    code.write_text("EDGE = 1\n")
    fam = _inv.Family(
        key="toy", label="Toy strategy", path="strat.py",
        what="Does one thing.", status=_inv.UNWIRED, record="never traded",
    )
    monkeypatch.setattr(sd, "ROOT", tmp_path)
    monkeypatch.setattr(sd, "DOCS_DIR", tmp_path / "docs")
    monkeypatch.setattr(sd._inv, "FAMILIES", (fam,))
    sd.DOCS_DIR.mkdir()
    sd.doc_path("toy").write_text(sd.scaffold("toy", date(2026, 9, 21)))
    return fam, code


def test_a_fresh_scaffold_passes(lab) -> None:
    fam, _ = lab
    assert sd.problems(fam) == []


def test_changing_the_code_makes_the_doc_stale(lab) -> None:
    fam, code = lab
    code.write_text("EDGE = 2\n")
    issues = sd.problems(fam)
    assert any("generated block is stale" in i for i in issues)
    assert any("newest changelog entry" in i for i in issues)


def test_stamping_with_a_note_brings_it_back_in_step(lab) -> None:
    fam, code = lab
    code.write_text("EDGE = 2\n")
    sd.stamp("toy", "raised the edge to 2 after the September test", date(2026, 9, 22))
    assert sd.problems(fam) == []
    doc = sd.parse(sd.doc_path("toy").read_text())
    assert doc.changelog[0].day == "2026-09-22"
    assert doc.changelog[0].note.startswith("raised the edge")
    assert len(doc.changelog) == 2


def test_stamp_refuses_an_empty_note(lab) -> None:
    with pytest.raises(ValueError):
        sd.stamp("toy", "   ")


def test_renaming_a_strategy_makes_its_doc_stale(lab, monkeypatch) -> None:
    fam, _ = lab
    renamed = _inv.Family(**{**fam.__dict__, "label": "Toy strategy v2"})
    monkeypatch.setattr(sd._inv, "FAMILIES", (renamed,))
    issues = sd.problems(renamed)
    assert any("title is" in i for i in issues)
    assert any("generated block is stale" in i for i in issues)


def test_a_doc_with_no_family_is_reported(lab) -> None:
    (sd.DOCS_DIR / "gone.md").write_text("# Gone\n")
    assert "gone" in sd.all_problems()


def test_missing_sections_are_named(lab) -> None:
    fam, _ = lab
    path = sd.doc_path("toy")
    path.write_text(path.read_text().replace("## Sources", "## Links"))
    assert "missing section: ## Sources" in sd.problems(fam)
