# Polymarket EMS Rebrand & Visual Revamp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the dashboard brand from "Pricing EMS" to "Polymarket EMS" (user-facing text and internal identifiers) and replace its dark "Bloomberg-EMS" theme with the approved light-mode institutional OMS/EMS grid design, including EMSX-style live cell-flash on genuine SSE value changes.

**Architecture:** No architectural change. Same FastAPI + Jinja2Templates + Python-generated-HTML-per-panel + vanilla JS/SSE stack. This is (a) a mechanical rename of one module + its call sites, (b) a full CSS token/rule rewrite in `static/style.css` that most of the visual transformation rides on, (c) small, targeted Python edits to three panels to add `data-flash` markers, and (d) new diff-and-flash logic in `static/dashboard.js`.

**Tech Stack:** FastAPI, Jinja2, vanilla JS, Server-Sent Events. No new dependencies, no build step, no framework.

**Spec:** [docs/superpowers/specs/2026-08-30-ems-rebrand-and-visual-revamp-design.md](../specs/2026-08-30-ems-rebrand-and-visual-revamp-design.md)

## Global Constraints

- No dark mode ships. No light/dark toggle in the product.
- No layout/information-architecture change — same 9 panel modules + `daily_altcoin`, same grid arrangement, same secondary section.
- No frontend framework, bundler, or npm dependency of any kind.
- `border-radius: 0` and no `box-shadow` anywhere in the revamped CSS.
- Color is reserved for BUY/SELL side, PnL sign, and guardrail/order status. Everything else (chrome, borders, labels, headers, brand text) is grayscale.
- Fonts: `--font-ui: "Helvetica Neue", Helvetica, Arial, "Segoe UI", sans-serif;` `--font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace;` — no imported/curated web fonts.
- `panels/_shared.py` is **live code** (8 of 10 panels import it) — a prior investigation confirmed the "0 importers / DEAD?" flag in `docs/CODE_MAP.md`/`docs/FILE_MAP.md` is a false positive caused by `tools/gen_docs.py` not recognizing bare relative imports (`from . import _shared as s`). Do not delete it.
- `app.py`'s `_kpi_card`/`_position_cards` helpers (and the `_overview_html`/`_paper_html` functions that call them) are unreachable from any live route or template. **Out of scope for this plan** — do not reskin or rename anything inside them. (Worth a separate future cleanup issue; not this one.)
- Every task that changes `static/style.css` or a panel's HTML output must keep these existing tests green unless the task itself is the one updating that specific assertion (see Task 16): `tests/unit/test_dashboard.py`, `tests/e2e/test_dashboard_flow.py`.

---

## File structure

| File | Change |
|---|---|
| `polymarket_exec/ops/dashboard/ems.py` | Rename → `execution_view.py`; `ems_html()` → `execution_view_html()` |
| `polymarket_exec/ops/dashboard/app.py` | Update import, `_ems_safe`→`_execution_view_safe`, JSON key `"ems"`→`"execution_view"` (3 sites) |
| `polymarket_exec/ops/dashboard/templates/base.html` | Rename brand/title, drop stale `.sub` tagline, restyle topbar |
| `polymarket_exec/ops/dashboard/templates/dashboard.html` | `ems-content` id → `execution-content`, `{{ ems }}` → `{{ execution_view }}`, comment update |
| `polymarket_exec/ops/dashboard/static/style.css` | Full token + rule rewrite (light institutional theme, flash keyframes) |
| `polymarket_exec/ops/dashboard/static/dashboard.js` | Rename references; add `captureFlashValues`/`applyFlashes` diffing |
| `polymarket_exec/ops/dashboard/panels/_shared.py` | Recolor constants + SVG generators; extend `stat()` with optional `flash` param; docstring/comment updates |
| `polymarket_exec/ops/dashboard/panels/__init__.py` | Fix stale docstring (list all 10 panels) |
| `polymarket_exec/ops/dashboard/panels/ribbon.py` | Rename brand text; wire `flash="pnl"` into its PnL `stat()` call |
| `polymarket_exec/ops/dashboard/panels/guardrails.py` | Add `data-flash="guardrail"` to the daily-loss-halt value cell; preserve `onclick` handlers verbatim |
| `polymarket_exec/ops/dashboard/panels/blotter.py` | Add `data-flash="price"` to each row's price cell |
| `polymarket_exec/ops/dashboard/panels/controls.py`, `strategy.py`, `market.py`, `decision_engine.py`, `performance.py`, `tca.py`, `daily_altcoin.py` | Terminology-only comment cleanup ("EMS grid"/"the EMS" references); no markup change — visual change comes entirely from the CSS rewrite in Task 2 |
| `tests/unit/test_dashboard.py`, `tests/e2e/test_dashboard_flow.py` | Rename assertions; replace `TestVisualContract.test_dark_palette` with light-palette assertions; add flash-diffing assertions |
| `docs/CODE_MAP.md`, `docs/FILE_MAP.md`, `AGENTS.md`, `CHANGELOG.md` | Regenerate via `tools/gen_docs.py`; add a CHANGELOG entry |

---

### Task 1: Rename the orchestrator module and its call sites

**Files:**
- Create (rename): `polymarket_exec/ops/dashboard/execution_view.py` (from `ems.py`)
- Modify: `polymarket_exec/ops/dashboard/app.py:67,541-547,579,825,841`
- Modify: `polymarket_exec/ops/dashboard/templates/dashboard.html:5-7`
- Modify: `polymarket_exec/ops/dashboard/static/dashboard.js:262-266`
- Test: `tests/unit/test_dashboard.py:122-131`, `tests/e2e/test_dashboard_flow.py:84-90`

**Interfaces:**
- Produces: `async def execution_view_html() -> str` (same body/contract as the old `ems_html()`, just renamed — still returns `"<div class='ems'>..."` markup for now; the class-name rewrite happens in Task 2). `app.py`'s JSON payloads now carry key `"execution_view"` instead of `"ems"` in `/`, `/api/data`, and `/api/stream`.

- [ ] **Step 1: Rename the module**

```bash
git mv polymarket_exec/ops/dashboard/ems.py polymarket_exec/ops/dashboard/execution_view.py
```

- [ ] **Step 2: Rename the function inside it**

In `polymarket_exec/ops/dashboard/execution_view.py`, change:
```python
async def ems_html() -> str:
```
to:
```python
async def execution_view_html() -> str:
```
Update the docstring's first line from `"""EMS view orchestrator (#37).` to `"""Execution view orchestrator (#37).` — leave the rest of the docstring and the function body untouched (the `<div class='ems'>`/`<div class='ems-grid'>` class names inside the returned string are handled in Task 2, not here).

- [ ] **Step 3: Update `app.py`'s import and internal references**

Change line 67:
```python
from polymarket_exec.ops.dashboard.ems import ems_html  # type: ignore[import-untyped]
```
to:
```python
from polymarket_exec.ops.dashboard.execution_view import execution_view_html  # type: ignore[import-untyped]
```

Change lines 541–547 from:
```python
async def _ems_safe() -> str:
    """Render the EMS view; never let a dashboard error touch the trading loop."""
    try:
        return await ems_html()
    except Exception as e:  # noqa: BLE001
        log.warning("ems_render_failed", error=str(e))
        return f"<div class='ems'><div class='card'>EMS view error: {escape(str(e))}</div></div>"
```
to:
```python
async def _execution_view_safe() -> str:
    """Render the execution view; never let a dashboard error touch the trading loop."""
    try:
        return await execution_view_html()
    except Exception as e:  # noqa: BLE001
        log.warning("execution_view_render_failed", error=str(e))
        return f"<div class='ems'><div class='card'>Execution view error: {escape(str(e))}</div></div>"
```
(the `class='ems'` literal stays for now — Task 2 renames it everywhere in one pass so it isn't half-renamed mid-plan.)

Change line 579 (`dashboard()` route context dict) from `"ems": await _ems_safe(),` to `"execution_view": await _execution_view_safe(),`.

Change line 825 (`api_data()`) from `"ems": await _ems_safe(),` to `"execution_view": await _execution_view_safe(),`.

Change line 841 (`api_stream()`'s `event_generator()`) from `"ems": await _ems_safe(),` to `"execution_view": await _execution_view_safe(),`.

- [ ] **Step 4: Update `templates/dashboard.html`**

Change lines 5–7 from:
```html
{# ───────── EMS main view (status ribbon + strategy/market/perf/TCA/blotter) ───────── #}
<div id="ems-content">
  {{ ems | safe }}
</div>
```
to:
```html
{# ───────── Execution view (status ribbon + strategy/market/perf/TCA/blotter) ───────── #}
<div id="execution-content">
  {{ execution_view | safe }}
</div>
```

- [ ] **Step 5: Update `static/dashboard.js`'s `updateDashboard()`**

Change lines 262–266 from:
```js
  // EMS main view (status ribbon + strategy/market/perf/TCA/blotter)
  if (data.ems) {
    var ems = document.getElementById('ems-content');
    if (ems) ems.innerHTML = data.ems || '';
  }
```
to:
```js
  // Execution view (status ribbon + strategy/market/perf/TCA/blotter)
  if (data.execution_view) {
    var execEl = document.getElementById('execution-content');
    if (execEl) execEl.innerHTML = data.execution_view || '';
  }
```
(This will be replaced again in Task 15 with the diffing version — keep it a plain swap for now so this task is independently testable.)

- [ ] **Step 6: Update the rename-affected test assertions**

In `tests/unit/test_dashboard.py`, change lines 122–131 from:
```python
    def test_api_data_has_expected_keys(self, client: TestClient):
        data = client.get("/api/data").json()
        assert "ems" in data
        assert "activity" in data
        assert "backtest" in data

    def test_api_data_ems_is_rendered_html(self, client: TestClient):
        ems = client.get("/api/data").json()["ems"]
        assert isinstance(ems, str) and len(ems) > 200
        assert "ribbon" in ems
```
to:
```python
    def test_api_data_has_expected_keys(self, client: TestClient):
        data = client.get("/api/data").json()
        assert "execution_view" in data
        assert "activity" in data
        assert "backtest" in data

    def test_api_data_execution_view_is_rendered_html(self, client: TestClient):
        execution_view = client.get("/api/data").json()["execution_view"]
        assert isinstance(execution_view, str) and len(execution_view) > 200
        assert "ribbon" in execution_view
```

In `tests/e2e/test_dashboard_flow.py`, change lines 84–90 from:
```python
    def test_data_after_start_stop_cycle(self, client: TestClient):
        client.post("/api/start")
        client.post("/api/stop")
        data = client.get("/api/data").json()
        assert "ems" in data
        assert "activity" in data
        assert "backtest" in data
```
to:
```python
    def test_data_after_start_stop_cycle(self, client: TestClient):
        client.post("/api/start")
        client.post("/api/stop")
        data = client.get("/api/data").json()
        assert "execution_view" in data
        assert "activity" in data
        assert "backtest" in data
```

Leave every other assertion in both files untouched for now (the `ems-content`/`ems-grid` CSS-selector assertions are updated in Task 16, once Task 2/4 have actually renamed those classes/ids — renaming the test ahead of the code would just make it fail for the wrong reason).

- [ ] **Step 7: Run the full test suite**

```bash
pytest tests/unit/test_dashboard.py tests/e2e/test_dashboard_flow.py -v
```
Expected: all tests pass **except** `test_has_ems_panels` (still asserts old `"ems-grid"` string — untouched until Task 16), `test_css_has_ems_components`, `test_js_swaps_ems_content`, `test_ems_panels_present`, `test_ems_content_container`, `test_css_complete`, `test_dark_palette`, `test_pnl_color_classes` — these fail *at this point in the plan* because their subject (CSS classes/ids/palette) hasn't been renamed yet. Confirm the failures are exactly these named tests and nothing else; anything else failing means this task introduced a real bug.

- [ ] **Step 8: Commit**

```bash
git add polymarket_exec/ops/dashboard/execution_view.py polymarket_exec/ops/dashboard/app.py \
  polymarket_exec/ops/dashboard/templates/dashboard.html polymarket_exec/ops/dashboard/static/dashboard.js \
  tests/unit/test_dashboard.py tests/e2e/test_dashboard_flow.py
git status --short  # confirm ems.py shows as deleted (renamed)
git commit -m "rename(#206): ems.py -> execution_view.py, JSON key ems -> execution_view"
```

---

### Task 2: New CSS token system and full stylesheet rewrite

**Files:**
- Modify: `polymarket_exec/ops/dashboard/static/style.css` (all 347 lines)
- Test: `tests/e2e/test_dashboard_flow.py:111-122` (`TestVisualContract`)

**Interfaces:**
- Produces: new CSS custom properties `--bg`, `--bg-alt`, `--bg-page`, `--border`, `--border-strong`, `--text`, `--muted`, `--label`, `--pos`, `--neg`, `--warn`, `--font-ui`, `--font-mono` on `:root`, replacing the old `--panel`/`--panel-2`/`--line`/`--line-soft`/`--dim`/`--faint`/`--accent`/`--accent-dim`/`--green`/`--red`/`--amber`/`--blue`/`--mono`/`--sans` set. Adds `.flash-pos`/`.flash-neg`/`.flash-warn` animation classes consumed by Task 15's JS. `.ems`/`.ems-grid` class names are renamed to `.execution-view`/`.execution-grid` here (this is the one place those two literal strings live in CSS — `ems.py`/`app.py`'s `class='ems'` string from Task 1 is also updated in this task, see Step 6).

- [ ] **Step 1: Replace the `:root` token block**

Replace lines 2–20 of `static/style.css`:
```css
:root {
  /* Bloomberg-EMS: dark slate (not pure black), amber accent, convention colors */
  --bg: #0a0d13;
  --panel: #11151e;
  --panel-2: #0d111a;
  --line: #222a38;
  --line-soft: #1a212c;
  --text: #c9d1de;
  --dim: #6b7689;
  --faint: #475063;
  --accent: #ffa53c;        /* Bloomberg amber */
  --accent-dim: #b8732a;
  --green: #34d399;
  --red: #ff5d6c;
  --amber: #ffb454;
  --blue: #4aa8ff;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, "Inter", "Segoe UI", system-ui, sans-serif;
}
```
with:
```css
:root {
  /* Institutional OMS/EMS: light theme, hairline grids, color reserved for signal */
  --bg: #ffffff;
  --bg-alt: #fafafa;
  --bg-page: #eceef0;
  --border: #d7dade;
  --border-strong: #b9bec4;
  --text: #14171c;
  --muted: #6b7078;
  --label: #8a8f97;
  --pos: #1b7a43;
  --neg: #b3261e;
  --warn: #9a6b00;
  --font-ui: "Helvetica Neue", Helvetica, Arial, "Segoe UI", sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, monospace;
}
```

- [ ] **Step 2: Apply the variable-name mapping across the rest of the file**

Every other rule in the file (originally lines 22–347: `.app`, `.topbar`, `.brand`, `.mode-toggle`, `.btn`, `.ribbon`, `.pill`, `.feed`, `.stat`, `.ems-grid`, `.card`, `.kv`, `.gauge`, `.book`, `.decision`, `.de-*`, `.gr-*`, `.ctl-*`, `.share-min`, `.equity`, `.spark`, `.statrow`, `.perf-*`, `.calib*`, `.blotter`, `.mono`, `.tag`, `.secondary`, `.feed-wrap`, `.backtest-report`, `.sse-*`, `.toast*`, `.perf-recon*`, `.open-pos*`) references the old variable names. Read the full current file, then replace every occurrence per this table (old → new):

| Old | New |
|---|---|
| `var(--bg)` | `var(--bg-page)` |
| `var(--panel)` | `var(--bg)` |
| `var(--panel-2)` | `var(--bg-alt)` |
| `var(--line)` | `var(--border-strong)` |
| `var(--line-soft)` | `var(--border)` |
| `var(--text)` | `var(--text)` (unchanged name) |
| `var(--dim)` | `var(--muted)` |
| `var(--faint)` | `var(--label)` |
| `var(--accent)` / `var(--accent-dim)` | remove — replace the property with a neutral value: background/border uses drop to `var(--border)`/`var(--border-strong)`, text-color uses drop to `var(--text)` or `var(--muted)` depending on whether the original rule was emphasizing state (emphasize → `var(--text)` + `font-weight:600`) or just decorative (drop the color override entirely, inherit) |
| `var(--green)` | `var(--pos)` |
| `var(--red)` | `var(--neg)` |
| `var(--amber)` | `var(--warn)` |
| `var(--blue)` | inspect each call site: if it's coloring genuinely neutral/informational text (e.g. a "connecting" state), use `var(--muted)`; if it's actually encoding a directional/signed value, use `var(--pos)`/`var(--neg)` per that value's sign |
| `var(--mono)` | `var(--font-mono)` |
| `var(--sans)` | `var(--font-ui)` |

Also, file-wide:
- Delete every `border-radius` declaration that is not `0` (or set it to `0`).
- Delete every `box-shadow` declaration.
- Rename every literal `.ems-grid` selector/reference to `.execution-grid`, and every literal `.ems` selector/reference to `.execution-view` (there are exactly two spots: the grid rule at old line 113, and nowhere else per the earlier investigation — the bare `.ems` class itself has no CSS rule and needs none added, since it was already unstyled chrome).
- Table/grid conventions (spec §2): headers (`th` or column-label elements) get `font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--label);`. Any table (`.gr-tail`, `.de-tail-tbl`, `.blotter`) gets `border-collapse: collapse;` with `td { border-bottom: 1px solid var(--border); }` and `tbody tr:nth-child(even) { background: var(--bg-alt); }` if not already present.

- [ ] **Step 3: Add the flash-tick keyframes**

Append to the end of the file:
```css
@keyframes flash-pos { 0% { background-color: color-mix(in srgb, var(--pos) 40%, transparent); } 100% { background-color: transparent; } }
@keyframes flash-neg { 0% { background-color: color-mix(in srgb, var(--neg) 40%, transparent); } 100% { background-color: transparent; } }
@keyframes flash-warn { 0% { background-color: color-mix(in srgb, var(--warn) 40%, transparent); } 100% { background-color: transparent; } }
.flash-pos, .flash-neg, .flash-warn { animation-duration: 700ms; animation-timing-function: ease-out; }
@media (prefers-reduced-motion: reduce) {
  .flash-pos, .flash-neg, .flash-warn { animation: none; }
}
```

- [ ] **Step 4: Update the visual-contract test**

In `tests/e2e/test_dashboard_flow.py`, replace lines 111–122:
```python
class TestVisualContract:
    """Trading-terminal dark theme."""

    def test_dark_palette(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert "#0a0d13" in css       # --bg dark slate
        assert "#ffa53c" in css       # --accent Bloomberg amber

    def test_pnl_color_classes(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert ".up" in css and ".down" in css
        assert "--green:" in css and "--red:" in css
```
with:
```python
class TestVisualContract:
    """Institutional light theme — hairline grids, color reserved for signal."""

    def test_light_palette(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert "#ffffff" in css      # --bg
        assert "#1b7a43" in css      # --pos
        assert "#b3261e" in css      # --neg
        assert "#ffa53c" not in css  # old Bloomberg amber accent must be gone

    def test_pnl_color_classes(self, client: TestClient):
        css = client.get("/static/style.css").text
        assert ".up" in css and ".down" in css
        assert "--pos:" in css and "--neg:" in css

    def test_no_rounded_corners_or_shadows(self, client: TestClient):
        css = client.get("/static/style.css").text
        import re
        radii = re.findall(r"border-radius:\s*([^;]+);", css)
        assert all(r.strip() in ("0", "0px", "0 0 0 0") for r in radii), radii
        assert "box-shadow" not in css
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/e2e/test_dashboard_flow.py::TestVisualContract -v
```
Expected: PASS (all three).

- [ ] **Step 6: Commit**

```bash
git add polymarket_exec/ops/dashboard/static/style.css tests/e2e/test_dashboard_flow.py
git commit -m "feat(#206): institutional light-mode theme, drop dark Bloomberg-EMS palette"
```

---

### Task 3: `panels/_shared.py` recoloring and `stat()` flash support

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/_shared.py`
- Modify: `polymarket_exec/ops/dashboard/panels/__init__.py`
- Test: `tests/unit/test_dashboard.py` (new test, appended)

**Interfaces:**
- Consumes: nothing new.
- Produces: `stat(label: str, value: str, *, flash: str | None = None, ...)` — existing signature plus one new optional keyword-only parameter. When `flash` is given, the emitted value element carries `data-flash="{flash}"`. Every existing caller that omits `flash` gets byte-identical output to before (aside from the color/token changes below). `ACCENT`/`GREEN`/`RED`/`DIM` constants keep their names but new values; `svg_equity`/`svg_calibration` use the new constants instead of the old hardcoded hex.

- [ ] **Step 1: Read the current file in full**

```bash
sed -n '1,190p' polymarket_exec/ops/dashboard/panels/_shared.py
```
Locate: the `ACCENT`/`GREEN`/`RED`/`DIM` constant block (~lines 13–17), the `stat()` function (among lines 25–117), and `svg_equity`/`svg_calibration` (lines 125, 156).

- [ ] **Step 2: Update the module docstring and palette constants**

Change the docstring/comment that currently reads `"Bloomberg-EMS palette: amber accent, convention green/red, dim slate."` to `"Institutional light palette: color reserved for signal (buy/sell, PnL, breach); everything else grayscale."` and update the constant values to match Task 2's tokens:
```python
ACCENT = "#14171c"  # was Bloomberg amber; no decorative accent in the new palette — this now means "primary text/ink"
GREEN = "#1b7a43"
RED = "#b3261e"
DIM = "#6b7078"
```
(If `ACCENT` is only ever used as a "primary ink" color for chart strokes/labels, keep the name but treat it as `--text`'s hex twin so `svg_equity`/`svg_calibration` render legibly on the new white background — grep every use of `ACCENT` in this file first and confirm none of them assume a colored/amber accent stroke; if one does, replace that specific stroke with `GREEN`/`RED` per its actual sign, not `ACCENT`.)

- [ ] **Step 3: Add `flash` support to `stat()`**

Add a keyword-only `flash: str | None = None` parameter to `stat()`'s signature. In the function body, wherever it currently builds the value element's opening tag (e.g. `<div class='stat-v'>` or `<span class='stat-v'>` — read the actual current tag first), conditionally add the attribute:
```python
flash_attr = f" data-flash='{flash}'" if flash else ""
```
and splice `{flash_attr}` into that opening tag right after the class attribute, before the value text. Every other line of `stat()` stays as it currently is.

- [ ] **Step 4: Update `panels/__init__.py`'s stale docstring**

Replace the docstring's panel list (currently omits `daily_altcoin` and `controls`) so it lists all 10: `ribbon, guardrails, controls, strategy, market, decision_engine, performance, tca, blotter, daily_altcoin`.

- [ ] **Step 5: Write a test for the new `flash` parameter**

Append to `tests/unit/test_dashboard.py`:
```python
def test_stat_helper_emits_data_flash_attribute():
    from polymarket_exec.ops.dashboard.panels._shared import stat
    html_with_flash = stat("Session P&L", "+$1,284.50", flash="pnl")
    assert "data-flash='pnl'" in html_with_flash
    html_without_flash = stat("Win rate", "61.0%")
    assert "data-flash" not in html_without_flash
```

- [ ] **Step 6: Run the test**

```bash
pytest tests/unit/test_dashboard.py::test_stat_helper_emits_data_flash_attribute -v
```
Expected: PASS. If it fails because `stat()`'s actual parameter order/tag structure differs from what Step 3 assumed, adjust Step 3's edit to match the real code you read in Step 1 — the assertions above (`data-flash='pnl'` present/absent) are the actual contract, not the exact line numbers.

- [ ] **Step 7: Run the full existing panel test suite to confirm no regression**

```bash
pytest tests/unit/test_dashboard.py -v
```
Expected: same pass/fail set as the end of Task 1 (still-pending-rename tests fail identically; nothing new breaks).

- [ ] **Step 8: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/_shared.py polymarket_exec/ops/dashboard/panels/__init__.py tests/unit/test_dashboard.py
git commit -m "feat(#206): recolor _shared.py for institutional palette, add stat() flash support"
```

---

### Task 4: `base.html` / `dashboard.html` rebrand

**Files:**
- Modify: `polymarket_exec/ops/dashboard/templates/base.html:6,13`

**Interfaces:**
- Consumes: nothing new.
- Produces: page `<title>` and topbar brand now read "Polymarket EMS"; stale `.sub` tagline removed.

- [ ] **Step 1: Update the title and brand**

Change line 6:
```html
  <title>{% block title %}Pricing EMS · Polymarket Crypto{% endblock %}</title>
```
to:
```html
  <title>{% block title %}Polymarket EMS{% endblock %}</title>
```

Change line 13:
```html
      <div class="brand">PRICING <b>EMS</b><span class="sub">Polymarket · BTC Up/Down 5m</span></div>
```
to:
```html
      <div class="brand">POLYMARKET <b>EMS</b></div>
```

- [ ] **Step 2: Remove the now-orphaned `.sub` CSS rule, if any**

Grep `static/style.css` for `.sub` — if a rule targets `.brand .sub` or `.sub` alone and nothing else in the templates uses that class, delete the rule (it has no markup left to style after Step 1).

```bash
grep -n "\.sub" polymarket_exec/ops/dashboard/static/style.css
```

- [ ] **Step 3: Run the dashboard route test**

```bash
pytest tests/unit/test_dashboard.py -k "title or brand" -v
```
(If no test currently names the title/brand directly, instead run the broader smoke test: `pytest tests/unit/test_dashboard.py::TestDashboardRoute -v` or equivalent class covering `GET /` — check the file for the actual class name covering the root route and use it.)
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add polymarket_exec/ops/dashboard/templates/base.html polymarket_exec/ops/dashboard/static/style.css
git commit -m "rebrand(#206): Pricing EMS -> Polymarket EMS in topbar/title, drop stale BTC 5m tagline"
```

---

### Task 5: `panels/ribbon.py` — brand rename + PnL flash wiring

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/ribbon.py:97` and its session-PnL `s.stat(...)` call site

**Interfaces:**
- Consumes: `stat(..., flash: str | None = None)` from Task 3.
- Produces: no change to `render()`'s signature; output HTML's brand text and PnL stat's `data-flash` attribute change.

- [ ] **Step 1: Read the current file in full**

```bash
sed -n '1,140p' polymarket_exec/ops/dashboard/panels/ribbon.py
```

- [ ] **Step 2: Rename the brand line**

Change line 97 from:
```python
            f"<div class='ribbon-id'>BTC·5M PRICING <b>EMS</b>"
```
to:
```python
            f"<div class='ribbon-id'>POLYMARKET <b>EMS</b>"
```

- [ ] **Step 3: Wire `flash="pnl"` into the session PnL stat**

Find the `s.stat(...)` call that renders the session/day PnL figure (built from the `day_pnl` parameter). Add `flash="pnl"` to that call's keyword arguments. Do not add `flash` to any other `stat()` call in this file — only the one live PnL figure should tick.

- [ ] **Step 4: Run the panel test**

```bash
pytest tests/unit/test_dashboard.py -k ribbon -v
```
Expected: PASS. Also manually confirm via:
```bash
python -c "
from polymarket_exec.ops.dashboard.panels import ribbon
html = ribbon.render(mode='paper', state='running', session_start=None, paused=False,
    pause_reason='', live_pnl=0.0, paper_pnl=1284.50, day_pnl=1284.50, open_pos=[],
    closed_session=[], tick=None, last_live_at=None)
assert 'POLYMARKET' in html and 'BTC·5M' not in html
assert \"data-flash='pnl'\" in html
print('OK')
"
```
Expected: prints `OK`.

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/ribbon.py
git commit -m "rebrand(#206): ribbon brand text -> Polymarket EMS, wire PnL flash marker"
```

---

### Task 6: `panels/guardrails.py` — flash wiring, preserve onclick handlers

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/guardrails.py`

**Interfaces:**
- Produces: the daily-loss-halt value cell carries `data-flash="guardrail"`. No other output changes.

- [ ] **Step 1: Read the current file in full**

```bash
sed -n '1,260p' polymarket_exec/ops/dashboard/panels/guardrails.py
```
Note the exact `onclick="..."` attribute strings on the bypass/reset buttons (they call `fetch('/api/loss_halt/bypass', ...)` / `fetch('/api/loss_halt/reset', ...)`) — copy them verbatim; this task must not change a single character inside those attribute values.

- [ ] **Step 2: Add the flash marker to the loss-halt value cell**

Locate the `.de-kv`/`.de-h` pair (or table cell) that renders the daily-loss-halt value (built from `day_pnl` and `loss_halt_usd`). Add `data-flash='guardrail'` as an attribute on that value element's opening tag — same technique as Task 3 Step 3 (a small conditional or unconditional attribute string spliced into the existing f-string), but here it's unconditional (this cell should always be flash-eligible, not parameterized).

- [ ] **Step 3: Run the panel test**

```bash
pytest tests/unit/test_dashboard.py -k guardrails -v
```
Expected: PASS.

- [ ] **Step 4: Manually verify the onclick handlers are untouched**

```bash
git diff polymarket_exec/ops/dashboard/panels/guardrails.py | grep -i onclick
```
Expected: no lines starting with `-` (removed) that also start with `+` (added) showing a *different* onclick value — i.e. confirm the diff shows zero changes to any `onclick=` attribute content. If any onclick line appears changed, revert and redo Step 2 more surgically.

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/guardrails.py
git commit -m "feat(#206): wire guardrail loss-halt flash marker, preserve control handlers"
```

---

### Task 7: `panels/blotter.py` — per-row price flash wiring

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/blotter.py`

**Interfaces:**
- Produces: each blotter row's price cell carries `data-flash="price"`.

- [ ] **Step 1: Read the current file in full**

```bash
sed -n '1,120p' polymarket_exec/ops/dashboard/panels/blotter.py
```

- [ ] **Step 2: Add the flash marker to each row's price cell**

In the `<table class='blotter'>` row-building loop (covers both the prepended `<tr class='live-row'>` open-position rows and the closed rows), add `data-flash='price'` to the `<td>` (or the cell containing the entry/exit price value) for every row. Since `dashboard.js`'s diffing (Task 15) keys purely on `[data-flash]` type + document position (not a per-row business id — the blotter's row count/order can change tick to tick), this is intentionally simple: every price cell just gets the same `data-flash='price'` marker, regardless of row.

- [ ] **Step 3: Run the panel test**

```bash
pytest tests/unit/test_dashboard.py -k blotter -v
```
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/blotter.py
git commit -m "feat(#206): wire blotter price-cell flash markers"
```

---

### Task 8: Terminology cleanup in the remaining seven panels

**Files:**
- Modify: `polymarket_exec/ops/dashboard/panels/controls.py`, `strategy.py`, `market.py`, `decision_engine.py`, `performance.py`, `tca.py`, `daily_altcoin.py`

**Interfaces:**
- Produces: no functional or markup change — only comments/docstrings referencing "EMS grid"/"the EMS" are reworded. Visual appearance changes purely because these files' output is styled by Task 2's rewritten CSS, not because these files themselves change structurally.

- [ ] **Step 1: Find every stale comment**

```bash
grep -n -i "ems grid\|the ems\b" polymarket_exec/ops/dashboard/panels/controls.py \
  polymarket_exec/ops/dashboard/panels/strategy.py \
  polymarket_exec/ops/dashboard/panels/market.py \
  polymarket_exec/ops/dashboard/panels/decision_engine.py \
  polymarket_exec/ops/dashboard/panels/performance.py \
  polymarket_exec/ops/dashboard/panels/tca.py \
  polymarket_exec/ops/dashboard/panels/daily_altcoin.py
```

- [ ] **Step 2: Reword each hit**

For each match, reword "EMS grid" → "execution grid" and "the EMS" → "the execution view", keeping the rest of the comment/docstring's meaning intact. **Do not touch `market.py`'s "always emits `card wide` — do not revert to a bare `card`" constraint comment beyond this wording pass** — that constraint itself stays in force (grid-column parity requirement is unrelated to the rename).

- [ ] **Step 3: Confirm no markup changed**

```bash
git diff --stat polymarket_exec/ops/dashboard/panels/controls.py polymarket_exec/ops/dashboard/panels/strategy.py \
  polymarket_exec/ops/dashboard/panels/market.py polymarket_exec/ops/dashboard/panels/decision_engine.py \
  polymarket_exec/ops/dashboard/panels/performance.py polymarket_exec/ops/dashboard/panels/tca.py \
  polymarket_exec/ops/dashboard/panels/daily_altcoin.py
```
Expected: only comment/docstring lines changed (spot-check the diff — no `<div`/`<table`/`class=` lines should appear in the diff).

- [ ] **Step 4: Run the full panel test suite**

```bash
pytest tests/unit/test_dashboard.py -v
```
Expected: same pass/fail set as after Task 3.

- [ ] **Step 5: Commit**

```bash
git add polymarket_exec/ops/dashboard/panels/controls.py polymarket_exec/ops/dashboard/panels/strategy.py \
  polymarket_exec/ops/dashboard/panels/market.py polymarket_exec/ops/dashboard/panels/decision_engine.py \
  polymarket_exec/ops/dashboard/panels/performance.py polymarket_exec/ops/dashboard/panels/tca.py \
  polymarket_exec/ops/dashboard/panels/daily_altcoin.py
git commit -m "chore(#206): reword stale EMS-grid terminology in panel comments"
```

---

### Task 9: `dashboard.js` — flash-diffing logic

**Files:**
- Modify: `polymarket_exec/ops/dashboard/static/dashboard.js:262-266` (the block Task 1 Step 5 left as a plain swap)

**Interfaces:**
- Consumes: `[data-flash]` attributes produced by Tasks 5, 6, 7, and the `.flash-pos`/`.flash-neg`/`.flash-warn` CSS classes from Task 2.
- Produces: `captureFlashValues(container)`, `applyFlashes(container, oldVals)`, `flashClassFor(kind, oldText, newText)` — new module-level functions in `dashboard.js`.

- [ ] **Step 1: Add the diffing helper functions**

Add near the top of the DOM-patching section (just above `refreshAll()`, i.e. before line 228):
```js
var FLASH_SELECTOR = '[data-flash]';

function captureFlashValues(container) {
  var map = {};
  var els = container.querySelectorAll(FLASH_SELECTOR);
  for (var i = 0; i < els.length; i++) {
    var key = els[i].getAttribute('data-flash') + ':' + i;
    map[key] = els[i].textContent;
  }
  return map;
}

function flashClassFor(kind, oldText, newText) {
  if (kind === 'guardrail') return 'flash-warn';
  var oldNum = parseFloat(oldText);
  var newNum = parseFloat(newText);
  if (!isNaN(oldNum) && !isNaN(newNum)) {
    return newNum >= oldNum ? 'flash-pos' : 'flash-neg';
  }
  return 'flash-pos';
}

function applyFlashes(container, oldVals) {
  var els = container.querySelectorAll(FLASH_SELECTOR);
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    var kind = el.getAttribute('data-flash');
    var key = kind + ':' + i;
    var oldText = oldVals[key];
    var newText = el.textContent;
    if (oldText === undefined || oldText === newText) continue;
    var cls = flashClassFor(kind, oldText, newText);
    el.classList.remove('flash-pos', 'flash-neg', 'flash-warn');
    void el.offsetWidth;
    el.classList.add(cls);
  }
}
```

- [ ] **Step 2: Replace the plain-swap block with the diffing version**

Change (the version Task 1 Step 5 produced):
```js
  // Execution view (status ribbon + strategy/market/perf/TCA/blotter)
  if (data.execution_view) {
    var execEl = document.getElementById('execution-content');
    if (execEl) execEl.innerHTML = data.execution_view || '';
  }
```
to:
```js
  // Execution view (status ribbon + strategy/market/perf/TCA/blotter)
  if (data.execution_view) {
    var execEl = document.getElementById('execution-content');
    if (execEl) {
      var oldVals = captureFlashValues(execEl);
      execEl.innerHTML = data.execution_view || '';
      applyFlashes(execEl, oldVals);
    }
  }
```

- [ ] **Step 3: Write a test asserting the diffing markers are served**

Append to `tests/unit/test_dashboard.py`:
```python
def test_js_has_flash_diffing_logic(client):
    js = client.get("/static/dashboard.js").text
    assert "captureFlashValues" in js
    assert "applyFlashes" in js
    assert "data-flash" in js
    # guard against reverting to a blanket-swap-with-no-diff implementation
    assert "if (oldText === undefined || oldText === newText) continue;" in js
```
(Use whatever fixture name the existing tests in this file use for the `TestClient` — e.g. `client` — matching the surrounding tests' style; check the top of the file for the actual fixture/parameter convention and match it exactly.)

- [ ] **Step 4: Run the test**

```bash
pytest tests/unit/test_dashboard.py -k flash_diffing -v
```
Expected: PASS.

- [ ] **Step 5: Manual smoke check in a browser**

Start the dashboard locally, open `http://localhost:<port>` (per `AGENTS.md`'s entry-point instructions), open devtools console, and run:
```js
document.querySelectorAll('[data-flash]').length
```
Expected: a number greater than 0 (at least the ribbon PnL stat, the guardrail loss-halt cell, and however many blotter rows are currently rendered). Watch the page for ~10 seconds (the SSE stream ticks every 5s per `docs/CODE_MAP.md`) and confirm a cell flashes if its underlying value actually changed between ticks (this requires the bot to be running with live data; if nothing changes for 10s, that's expected — the flash only fires on a genuine value change, which is the point).

- [ ] **Step 6: Commit**

```bash
git add polymarket_exec/ops/dashboard/static/dashboard.js tests/unit/test_dashboard.py
git commit -m "feat(#206): EMSX-style flash-on-change for price/PnL/guardrail cells"
```

---

### Task 10: Remaining rename-dependent test updates

**Files:**
- Modify: `tests/unit/test_dashboard.py:53-57,88-91,104-105`
- Modify: `tests/e2e/test_dashboard_flow.py:41-51,93-101`

**Interfaces:**
- Consumes: the renamed classes/ids from Tasks 1, 2, 4 and the flash markers from Tasks 5–7.
- Produces: no production code change — this task only finishes updating the tests that were deliberately left failing since Task 1 Step 7.

- [ ] **Step 1: Update `test_has_ems_panels`**

In `tests/unit/test_dashboard.py`, change lines 53–57:
```python
    def test_has_ems_panels(self, client: TestClient):
        text = client.get("/").text
        for panel in ("ribbon", "ems-grid", "STRATEGY", "LIVE MARKET",
                      "PERFORMANCE / ALPHA", "TCA", "TRADE BLOTTER"):
            assert panel in text, f"missing EMS panel: {panel}"
```
to:
```python
    def test_has_execution_view_panels(self, client: TestClient):
        text = client.get("/").text
        for panel in ("ribbon", "execution-grid", "STRATEGY", "LIVE MARKET",
                      "PERFORMANCE / ALPHA", "TCA", "TRADE BLOTTER"):
            assert panel in text, f"missing execution-view panel: {panel}"
```

- [ ] **Step 2: Update `test_css_has_ems_components`**

Change lines 88–91:
```python
    def test_css_has_ems_components(self, client: TestClient):
        css = client.get("/static/style.css").text
        for sel in (".ribbon", ".card", ".ems-grid", ".blotter", ".stat", ".pill", ".tag"):
            assert sel in css, f"missing {sel}"
```
to:
```python
    def test_css_has_execution_view_components(self, client: TestClient):
        css = client.get("/static/style.css").text
        for sel in (".ribbon", ".card", ".execution-grid", ".blotter", ".stat", ".pill", ".tag"):
            assert sel in css, f"missing {sel}"
```

- [ ] **Step 3: Update `test_js_swaps_ems_content`**

Change lines 104–105:
```python
    def test_js_swaps_ems_content(self, client: TestClient):
        assert "ems-content" in client.get("/static/dashboard.js").text
```
to:
```python
    def test_js_swaps_execution_content(self, client: TestClient):
        assert "execution-content" in client.get("/static/dashboard.js").text
```

- [ ] **Step 4: Update `test_ems_panels_present` and `test_ems_content_container`**

In `tests/e2e/test_dashboard_flow.py`, change lines 41–45:
```python
    def test_ems_panels_present(self, client: TestClient):
        text = client.get("/").text
        for panel in ("STRATEGY", "LIVE MARKET", "PERFORMANCE / ALPHA",
                      "TCA", "TRADE BLOTTER", "ems-grid", "ribbon"):
            assert panel in text
```
to:
```python
    def test_execution_view_panels_present(self, client: TestClient):
        text = client.get("/").text
        for panel in ("STRATEGY", "LIVE MARKET", "PERFORMANCE / ALPHA",
                      "TCA", "TRADE BLOTTER", "execution-grid", "ribbon"):
            assert panel in text
```
Change lines 47–51:
```python
    def test_ems_content_container(self, client: TestClient):
        text = client.get("/").text
        assert "ems-content" in text
        assert "activity-content" in text
        assert "backtest-content" in text
```
to:
```python
    def test_execution_content_container(self, client: TestClient):
        text = client.get("/").text
        assert "execution-content" in text
        assert "activity-content" in text
        assert "backtest-content" in text
```

- [ ] **Step 5: Update `test_css_complete`**

Change lines 93–101:
```python
    def test_css_complete(self, client: TestClient):
        css = client.get("/static/style.css").text
        selectors = [
            ":root", "body", ".topbar", ".ribbon", ".ems-grid", ".card",
            ".card-h", ".stat", ".pill", ".gauge", ".book", ".spark",
            ".calib", ".blotter", ".tag", ".btn", ".sse-indicator", ".toast",
        ]
        for sel in selectors:
            assert sel in css, f"missing CSS selector: {sel}"
```
to:
```python
    def test_css_complete(self, client: TestClient):
        css = client.get("/static/style.css").text
        selectors = [
            ":root", "body", ".topbar", ".ribbon", ".execution-grid", ".card",
            ".card-h", ".stat", ".pill", ".gauge", ".book", ".spark",
            ".calib", ".blotter", ".tag", ".btn", ".sse-indicator", ".toast",
        ]
        for sel in selectors:
            assert sel in css, f"missing CSS selector: {sel}"
```

- [ ] **Step 6: Run the full suite**

```bash
pytest tests/unit/test_dashboard.py tests/e2e/test_dashboard_flow.py -v
```
Expected: **all tests pass**, zero failures.

- [ ] **Step 7: Commit**

```bash
git add tests/unit/test_dashboard.py tests/e2e/test_dashboard_flow.py
git commit -m "test(#206): finish renaming ems-grid/ems-content assertions to execution-view equivalents"
```

---

### Task 11: Docs regeneration and changelog

**Files:**
- Regenerate: `docs/CODE_MAP.md`, `docs/FILE_MAP.md`, `AGENTS.md` (generated blocks only)
- Modify: `CHANGELOG.md`

**Interfaces:** none (docs/changelog only).

- [ ] **Step 1: Regenerate the generated docs**

```bash
python tools/gen_docs.py
```

- [ ] **Step 2: Verify no drift remains**

```bash
python tools/gen_docs.py --check
```
Expected: exit code 0, no "DOC DRIFT" message.

- [ ] **Step 3: Add a CHANGELOG entry**

Add a new top entry to `CHANGELOG.md` following the file's existing entry format (check the most recent entries for the exact heading/style convention used, e.g. `## vX.Y.Z — <title>`), summarizing: rebrand "Pricing EMS" → "Polymarket EMS" (module rename `ems.py`→`execution_view.py`, JSON key `ems`→`execution_view`); new institutional light-mode visual theme replacing the dark "Bloomberg-EMS" palette; EMSX-style flash-on-change for price/PnL/guardrail cells.

- [ ] **Step 4: Run the full test suite one final time**

```bash
pytest tests/unit/test_dashboard.py tests/e2e/test_dashboard_flow.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add docs/CODE_MAP.md docs/FILE_MAP.md AGENTS.md CHANGELOG.md
git commit -m "docs(#206): regenerate CODE_MAP/FILE_MAP, changelog entry for EMS rebrand + visual revamp"
```

---

## Plan self-review notes

- **Spec coverage:** §1 (rename, all files) → Tasks 1, 4, 5, 8, 10. §2 (visual system) → Task 2 (CSS) + Task 3 (`_shared.py` chart/constant colors) + Task 4 (topbar). §3 (flash ticking, real SSE-diff not fake interval) → Tasks 5, 6, 7 (server-side markers) + Task 9 (client diffing). §4 (tech constraints) → enforced throughout (no new deps introduced in any task). §5 (scope: all panels) → Tasks 5–8 cover all 10 panel files. §6 (testing) → Tasks 1, 2, 3, 9, 10.
- **Correction versus the spec:** the spec's §5 said to also touch `app.py`'s `_kpi_card`/`_position_cards`; this plan explicitly excludes them (Global Constraints) because they're unreachable dead code — reskinning unreachable code isn't testable and isn't real scope. The spec's §5 also flagged `panels/_shared.py` as possibly-dead; this plan corrects that (it's live, Task 3 modifies it directly).
- **Type/interface consistency:** `stat(..., flash: str | None = None)` defined in Task 3 is consumed identically in Task 5 (ribbon.py passes `flash="pnl"`); `data-flash` attribute values (`"pnl"`, `"guardrail"`, `"price"`) are produced in Tasks 5/6/7 and consumed by the exact same three string literals in Task 9's `flashClassFor`.

## Execution options

Plan complete and saved to `docs/superpowers/plans/2026-09-13-ems-rebrand-and-visual-revamp-plan.md`. Two ways to execute:

1. **Subagent-driven (recommended)** — a fresh subagent per task, with a review gate between each.
2. **Inline execution** — run tasks in this session in batches with checkpoints.

Which approach?
