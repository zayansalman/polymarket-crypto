B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import pandas as pd
cols = ["open","high","low","close","volume","amount"]
h = pd.read_csv("$B/repo/data/BTCUSDT_1h_20251018_220012.csv", parse_dates=["timestamp"]).rename(columns={"timestamp":"ts"})
o = pd.read_csv("$B/btc_1h_oos.csv", parse_dates=["ts"])
m = h.merge(o, on="ts", suffixes=("_h","_o"))
print("overlap rows", len(m), {c: round(float((m[c+"_h"]-m[c+"_o"]).abs().max()), 6) for c in cols})
full = pd.concat([h[["ts"]+cols], o[o.ts > h.ts.iloc[-1]][["ts"]+cols]]).sort_values("ts").reset_index(drop=True)
print("rows", len(full), full.ts.iloc[0], "->", full.ts.iloc[-1], "| gaps", int(full.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()), "| up rate", round(float((full.close > full.open).mean()), 4))
full.to_csv("$B/btc_1h_full.csv", index=False)
EOF
