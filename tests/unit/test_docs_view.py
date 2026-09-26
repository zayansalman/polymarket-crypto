"""The dashboard's /strategy-docs pages: every strategy readable, maths intact, nothing else served."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from polymarket_bot import inventory as _inv
from polymarket_exec.ops.dashboard import docs_view


def _client() -> TestClient:
    app = FastAPI()
    docs_view.register(app)
    return TestClient(app)


def test_inline_and_display_maths_survive_markdown() -> None:
    html = docs_view.render_markdown(
        "Drift $\\hat\\mu_H$ and $a^*$ then $b^*$.\n\n$$\n\\sigma\\sqrt{h}\\;\\Phi^{-1}(b)\n$$\n"
    )
    assert "\\(\\hat\\mu_H\\)" in html
    assert "\\(a^*\\)" in html and "\\(b^*\\)" in html
    assert "<em>" not in html
    assert '<div class="math-display">\\[\\sigma\\sqrt{h}\\;\\Phi^{-1}(b)\\]</div>' in html
    assert "<p><div" not in html


def test_dollar_amounts_are_not_maths() -> None:
    html = docs_view.render_markdown("Lost -$10.04 on $19.37 staked; clips of $1-5.")
    assert "\\(" not in html
    assert "$10.04" in html and "$19.37" in html


def test_dollars_inside_code_are_left_alone() -> None:
    html = docs_view.render_markdown("Run `echo $HOME $PATH` first.")
    assert "\\(" not in html
    assert "<code>echo $HOME $PATH</code>" in html


def test_maths_is_html_escaped() -> None:
    assert "\\(a &lt; b\\)" in docs_view.render_markdown("Where $a < b$ holds.")


def test_marker_comments_are_not_printed_and_raw_html_is_escaped() -> None:
    html = docs_view.render_markdown(
        "<!-- BEGIN GENERATED:strategy -->\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        "<!-- END GENERATED:strategy -->\n\n<script>alert(1)</script>\n"
    )
    assert "GENERATED" not in html
    assert "<table>" in html
    assert "<script>" not in html


def test_tables_render() -> None:
    assert "<table>" in docs_view.render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n")


def test_index_lists_every_strategy() -> None:
    r = _client().get("/strategy-docs")
    assert r.status_code == 200
    for fam in _inv.FAMILIES:
        assert f'href="/strategy-docs/{fam.key}"' in r.text


def test_every_strategy_doc_page_renders() -> None:
    client = _client()
    for fam in _inv.FAMILIES:
        r = client.get(f"/strategy-docs/{fam.key}")
        assert r.status_code == 200, fam.key
        assert "out of date with the code" not in r.text, fam.key


def test_relative_md_links_between_docs_resolve() -> None:
    assert _client().get("/strategy-docs/fade_1h_momentum_15m.md").status_code == 200


def test_unknown_or_traversing_keys_are_404() -> None:
    client = _client()
    assert client.get("/strategy-docs/nope").status_code == 404
    assert client.get("/strategy-docs/..%2F..%2Fconfig").status_code == 404


def test_the_dashboard_serves_the_docs_pages() -> None:
    # Outside a `with` block TestClient does not run the lifespan, so no
    # background loops start.
    from polymarket_exec.ops.dashboard.app import app

    client = TestClient(app)
    assert client.get("/strategy-docs").status_code == 200
    assert client.get("/strategy-docs/fade_1h_momentum_15m").status_code == 200
