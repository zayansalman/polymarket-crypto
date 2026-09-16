B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; cd $B && python3 - <<'EOF'
# Post-hoc robustness checks on R5 (labelled as such; not used to change the pre-registered verdict)
import math, runpy, io, contextlib
import numpy as np, pandas as pd
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    g = runpy.run_path("walkforward_oi.py")
df, masks, months, w = g["df"], g["masks"], g["months"], g["w"]
VAL = pd.Timestamp("2025-10-18 14:00")
idx = lambda k: masks[k][0].append(masks[k][1:])
A = df.loc[idx("R1")]
print("1) Dose-response inside Tier A by OI change during H-1 (z):")
for lo, hi in [(-99, -1.0), (-1.0, -0.3), (-0.3, 0.3), (0.3, 1.0), (1.0, 99)]:
    s = A[(A.doi_now > lo) & (A.doi_now <= hi)]
    print(f"   z in ({lo:>5},{hi:>5}]: {w(s.win)[4]}")
push_mask = []
for m in months:
    hist, cur = df[df.month < m], df[df.month == m]
    push_mask.append(cur.index[cur.fz_1 > hist.fz_1.quantile(0.8)])
P = df.loc[push_mask[0].append(push_mask[1:])]
print("2) Same split on the broader flow-push signal (bigger n):")
for lo, hi in [(-99, -1.0), (-1.0, -0.3), (-0.3, 0.3), (0.3, 1.0), (1.0, 99)]:
    s = P[(P.doi_now > lo) & (P.doi_now <= hi)]
    print(f"   z in ({lo:>5},{hi:>5}]: {w(s.win)[4]}   val: {w(s[s.ts>=VAL].win)[4]}")
R5 = df.loc[idx("R5")]
print("3) R5 by bet side:")
print("   bet Down (after up hour):", w(R5[R5.dir_1 > 0].win)[4])
print("   bet Up   (after down hour):", w(R5[R5.dir_1 < 0].win)[4])
print("4) R5 half-years:", " ".join(f"{h}:{g.win.mean():.0%}/{len(g)}" for h, g in R5.groupby(R5.ts.dt.year.astype(str) + np.where(R5.ts.dt.month <= 6, 'H1', 'H2'))))
print("5) R5 frequency: %.1f bets/week" % (len(R5) / ((len(months) * 30.4) / 7)))
EOF
