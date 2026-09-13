"""Dashboard for the copy-trade shadow ledgers (#182).

Read-only. Serves a single auto-refreshing page over every ``copytrade_*.db``
in ``data/``, so each configuration being tested is visible side by side and the
operator can read the result directly rather than relying on a summary.

Design notes:

* **Read-only, separate process.** It opens the ledgers in SQLite read-only mode
  and never writes, so it cannot disturb a running shadow or lock its database.
* **No capture-% metric.** The ratio ``ours/theirs`` is meaningless when either
  side is negative — "131% captured" of a loss reads like success and is the
  opposite. Both PnLs are shown as absolute dollars, plus their difference.
* **Sample size is displayed as prominently as PnL**, because at these counts it
  is the number that decides whether anything can be concluded.

Usage::

    python tools/copytrade_dashboard.py           # http://127.0.0.1:7861
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="copy-trade shadow")
DATA_DIR = ROOT / "data"


def _read(db: Path) -> dict[str, Any] | None:
    """Summarise one ledger, or ``None`` if it has no settled rows yet."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = list(con.execute("SELECT * FROM copy_fills"))
    except sqlite3.Error:
        return None
    settled = [r for r in rows if r["settled_at"] is not None]
    if not rows:
        return None
    shares = sum(r["size"] or 0 for r in settled)
    theirs = sum(r["their_pnl"] or 0 for r in settled)
    ours = sum(r["our_pnl"] or 0 for r in settled)
    wins = sum(1 for r in settled if (r["our_pnl"] or 0) > 0)
    # equity curve, for the sparkline
    eq, run = [], 0.0
    for r in sorted(settled, key=lambda x: x["settled_at"] or 0):
        run += r["our_pnl"] or 0
        eq.append(run)
    peak = 0.0
    dd = 0.0
    for v in eq:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return {
        "name": db.stem,
        "settled": len(settled),
        "pending": len(rows) - len(settled),
        "shares": shares,
        "theirs": theirs,
        "ours": ours,
        "diff": ours - theirs,
        "winrate": (100 * wins / len(settled)) if settled else 0.0,
        "slip": (100 * sum((r["slippage"] or 0) * (r["size"] or 0) for r in settled) / shares)
        if shares
        else 0.0,
        "maxdd": dd,
        "eq": eq[-120:],
        "recent": [dict(r) for r in sorted(settled, key=lambda x: -(x["settled_at"] or 0))[:12]],
    }


def _spark(eq: list[float]) -> str:
    """Inline SVG equity curve — no external assets, CSP-safe."""
    if len(eq) < 2:
        return ""
    lo, hi = min(eq), max(eq)
    rng = (hi - lo) or 1.0
    pts = " ".join(
        f"{i * 260 / (len(eq) - 1):.1f},{40 - (v - lo) / rng * 36:.1f}"
        for i, v in enumerate(eq)
    )
    colour = "#3fb950" if eq[-1] >= 0 else "#f85149"
    zero = 40 - (0 - lo) / rng * 36 if lo <= 0 <= hi else None
    zline = (
        f'<line x1="0" y1="{zero:.1f}" x2="260" y2="{zero:.1f}" '
        f'stroke="#30363d" stroke-dasharray="2,2"/>'
        if zero is not None
        else ""
    )
    return (
        f'<svg viewBox="0 0 260 40" width="260" height="40">{zline}'
        f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.5"/></svg>'
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    ledgers = [d for d in sorted(DATA_DIR.glob("copytrade_*.db"))]
    cards = []
    for db in ledgers:
        s = _read(db)
        if not s:
            continue
        cls = "pos" if s["ours"] >= 0 else "neg"
        tcls = "pos" if s["theirs"] >= 0 else "neg"
        underpowered = (
            '<div class="warn">n is too small to conclude anything</div>'
            if s["settled"] < 200
            else ""
        )
        rows = "".join(
            f"<tr><td>{r['window_slug'] or ''}</td><td>{r['outcome'] or ''}</td>"
            f"<td class=num>{(r['their_price'] or 0):.3f}</td>"
            f"<td class=num>{((r['our_price'] or 0) + (r['fee'] or 0)):.3f}</td>"
            f"<td class='num {'pos' if (r['slippage'] or 0) < 0 else 'neg'}'>"
            f"{100 * (r['slippage'] or 0):+.1f}c</td>"
            f"<td class=num>{(r['size'] or 0):.0f}</td>"
            f"<td class='num {'pos' if (r['our_pnl'] or 0) >= 0 else 'neg'}'>"
            f"${(r['our_pnl'] or 0):+.2f}</td></tr>"
            for r in s["recent"]
        )
        cards.append(f"""
        <div class="card">
          <h2>{s['name']}</h2>
          <div class="grid">
            <div><span class="lbl">settled</span><span class="big">{s['settled']}</span>
                 <span class="sub">+{s['pending']} pending</span></div>
            <div><span class="lbl">target PnL</span>
                 <span class="big {tcls}">${s['theirs']:+.2f}</span></div>
            <div><span class="lbl">our PnL</span>
                 <span class="big {cls}">${s['ours']:+.2f}</span></div>
            <div><span class="lbl">difference</span>
                 <span class="big {'pos' if s['diff'] >= 0 else 'neg'}">${s['diff']:+.2f}</span></div>
            <div><span class="lbl">slippage</span><span class="big">{s['slip']:+.2f}c</span>
                 <span class="sub">per share</span></div>
            <div><span class="lbl">max drawdown</span><span class="big">${s['maxdd']:.2f}</span></div>
          </div>
          {underpowered}
          <div class="spark">{_spark(s['eq'])}</div>
          <table><thead><tr><th>window</th><th>side</th><th>them</th><th>us</th>
            <th>slip</th><th>sh</th><th>pnl</th></tr></thead><tbody>{rows}</tbody></table>
        </div>""")

    body = "".join(cards) or '<div class="card"><h2>no ledgers yet</h2></div>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>copy-trade shadow</title><meta http-equiv="refresh" content="10">
<style>
 :root{{color-scheme:dark}}
 body{{background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;padding:24px}}
 h1{{font-size:15px;letter-spacing:.14em;text-transform:uppercase;color:#8b949e;margin:0 0 4px}}
 .note{{color:#6e7681;margin-bottom:20px}}
 .card{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:18px;margin-bottom:18px}}
 h2{{font-size:14px;margin:0 0 14px;color:#58a6ff}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:14px;margin-bottom:12px}}
 .lbl{{display:block;color:#6e7681;font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
 .big{{font-size:20px;font-weight:600}} .sub{{color:#6e7681;font-size:11px;margin-left:6px}}
 .pos{{color:#3fb950}} .neg{{color:#f85149}}
 .warn{{background:#2d2211;border:1px solid #9e6a03;color:#d29922;padding:7px 10px;border-radius:5px;margin:10px 0;font-size:12px}}
 .spark{{margin:12px 0}}
 table{{width:100%;border-collapse:collapse;margin-top:10px}}
 th{{text-align:left;color:#6e7681;font-weight:400;font-size:11px;text-transform:uppercase;
    border-bottom:1px solid #30363d;padding:5px 8px}}
 td{{padding:4px 8px;border-bottom:1px solid #21262d}} .num{{text-align:right}}
</style></head><body>
<h1>Copy-trade shadow</h1>
<div class="note">read-only &middot; no orders are placed &middot; refreshes every 10s</div>
{body}</body></html>"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=7861)
    a = p.parse_args()
    import uvicorn

    print(f"copy-trade dashboard -> http://127.0.0.1:{a.port}")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
