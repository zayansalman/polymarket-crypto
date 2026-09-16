B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; cd $B && python3 - <<'EOF'
import io, contextlib, runpy, pandas as pd
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    g = runpy.run_path("walkforward_oi.py")
df, masks, w = g["df"], g["masks"], g["w"]
A = df.loc[masks["R1"][0].append(masks["R1"][1:])]
VAL = pd.Timestamp("2025-10-18 14:00")
for name, s in [("walk-forward", A), ("validation window", A[A.ts >= VAL])]:
    print(name, "| bet Down (after up hour):", w(s[s.dir_1 > 0].win)[4], "| bet Up (after down hour):", w(s[s.dir_1 < 0].win)[4])
EOF
