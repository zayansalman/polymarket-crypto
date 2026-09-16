import json
import math
import sys
from pathlib import Path

import numpy as np

path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "backtest_results.jsonl")
rows = [json.loads(line) for line in path.open()]
n = len(rows)
up = np.array([r["up"] for r in rows])
prev_up = np.array([r["prev_up"] for r in rows])
N = rows[0]["paths"]


def wilson(k, n, z=1.96):
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def report(name, p_raw):
    p = (p_raw * N + 0.5) / (N + 1)
    call = p_raw > 0.5
    decided = p_raw != 0.5
    k = int((call[decided] == up[decided].astype(bool)).sum())
    m = int(decided.sum())
    lo, hi = wilson(k, m)
    brier = float(((p - up) ** 2).mean())
    ll = float(-(up * np.log(p) + (1 - up) * np.log(1 - p)).mean())
    print(f"\n== {name}")
    print(f"  hit rate {k}/{m} = {k / m:.1%}  (95% range {lo:.1%}-{hi:.1%}; {n - m} hours at exactly 50% skipped)")
    print(f"  Brier {brier:.4f} vs coin flip 0.2500 | log loss {ll:.4f} vs coin flip 0.6931")
    print(f"  average P(up) {p_raw.mean():.1%} vs actual up rate {up.mean():.1%}")
    print("  calibration (model said -> actually up):")
    for a, b in [(0, 0.2), (0.2, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.8), (0.8, 1.01)]:
        s = (p_raw >= a) & (p_raw < b)
        if s.sum():
            print(f"    {a:.0%}-{min(b, 1):.0%}: {int(s.sum()):>3} hours, said {p_raw[s].mean():.0%}, up {up[s].mean():.0%}")
    for t in (0.6, 0.7):
        s = (p_raw >= t) | (p_raw <= 1 - t)
        if s.sum():
            kk = int((call[s] == up[s].astype(bool)).sum())
            l2, h2 = wilson(kk, int(s.sum()))
            print(f"  confident only (>= {t:.0%} either way): {kk}/{int(s.sum())} = {kk / s.sum():.1%} ({l2:.1%}-{h2:.1%})")


print(f"hours tested: {n} | {rows[0]['target_ts']} -> {rows[-1]['target_ts']} | paths/hour {N} | top_p {rows[0]['top_p']}")
print(f"actual up rate: {up.mean():.1%}")
k = int((prev_up == up).sum())
lo, hi = wilson(k, n)
print(f"baseline 'repeat last hour': {k}/{n} = {k / n:.1%} ({lo:.1%}-{hi:.1%})")

report("P(up) = paths closing above the REAL open", np.array([r["p_up_vs_real_open"] for r in rows]))
report("P(up) = paths closing above their OWN predicted open", np.array([r["p_up_within_path"] for r in rows]))

off = np.array([r["pred_open_median"] - r["real_open"] for r in rows])
print("\n== blurry prices")
print(f"  model's predicted open vs real open: median offset ${np.median(off):+.0f}, typical size ${np.median(np.abs(off)):.0f}")
print(f"  tokenizer round-trip, close price error: typical ${np.median([r['rec_close_mae'] for r in rows]):.0f}")
print(f"  tokenizer round-trip, hourly move error: typical ${np.median([r['rec_move_mae'] for r in rows]):.0f} vs typical real hourly move ${np.median([r['real_move_mae'] for r in rows]):.0f}")
print(f"  forecast time per hour: {np.median([r['forecast_s'] for r in rows]):.1f}s")
