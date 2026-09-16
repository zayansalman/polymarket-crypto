B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import pandas as pd, numpy as np, json, time, urllib.request
h = pd.read_csv("$B/repo/data/BTCUSDT_1h_20251018_220012.csv", parse_dates=["timestamp"]).rename(columns={"timestamp":"ts"})
o = pd.read_csv("$B/btc_1h_oos.csv", parse_dates=["ts"]).drop(columns=["open_time"])
m = h.merge(o, on="ts", suffixes=("_h","_o"))
cols = ["open","high","low","close","volume","amount"]
diff = {c: float((m[c+"_h"]-m[c+"_o"]).abs().max()) for c in cols}
print("overlap rows", len(m), "max abs diff per column", diff)
# extend to the latest closed candle
last = int(o.ts.iloc[-1].timestamp()*1000) + 3600000
now = int(time.time()*1000); rows=[]
while last < now:
    b = json.load(urllib.request.urlopen(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&startTime={last}&limit=1000", timeout=30))
    if not b: break
    rows += [r for r in b if r[6] < now]; last = b[-1][0] + 3600000
n = pd.DataFrame([[pd.to_datetime(r[0], unit="ms"), *map(float, (r[1],r[2],r[3],r[4],r[5],r[7]))] for r in rows], columns=["ts"]+cols)
full = pd.concat([h[["ts"]+cols], o[o.ts > h.ts.iloc[-1]][["ts"]+cols], n]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
gaps = full.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()
print("full", len(full), full.ts.iloc[0], "->", full.ts.iloc[-1], "| gaps:", int(gaps), "| up rate all:", round(float((full.close>full.open).mean()),4))
full.to_csv("$B/btc_1h_full.csv", index=False)
EOF
