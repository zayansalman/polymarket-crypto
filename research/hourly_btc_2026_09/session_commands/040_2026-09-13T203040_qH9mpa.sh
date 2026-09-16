B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import json, time, urllib.request, pandas as pd
start = int(pd.Timestamp("2023-10-12 00:00").timestamp()*1000); now = int(time.time()*1000); rows = []
while start < now:
    b = json.load(urllib.request.urlopen(f"https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1h&startTime={start}&limit=1000", timeout=30))
    if not b: break
    rows += b; start = b[-1][0] + 3600000; time.sleep(0.15)
df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","amount","trades","taker_buy_base","taker_buy_quote","ignore"])
df = df[df.close_time < now].drop_duplicates("open_time").sort_values("open_time")
for c in ["open","high","low","close","volume","amount","taker_buy_base","taker_buy_quote"]: df[c] = df[c].astype(float)
df["ts"] = pd.to_datetime(df.open_time, unit="ms")
df = df[["ts","open","high","low","close","volume","amount","trades","taker_buy_base","taker_buy_quote"]].reset_index(drop=True)
print("ETH rows", len(df), df.ts.iloc[0], "->", df.ts.iloc[-1], "| gaps", int(df.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()))
df.to_csv("$B/eth_1h_flow.csv", index=False)
EOF
