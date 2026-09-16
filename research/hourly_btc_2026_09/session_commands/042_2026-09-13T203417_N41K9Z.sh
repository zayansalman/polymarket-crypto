B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import json, time, urllib.request, pandas as pd
def get(url):
    return json.load(urllib.request.urlopen(url, timeout=30))
now = int(time.time()*1000)
start = int(pd.Timestamp("2023-10-12 00:00").timestamp()*1000); rows = []
while start < now:
    b = get(f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&startTime={start}&limit=1500")
    if not b: break
    rows += b; start = b[-1][0] + 3600000; time.sleep(0.2)
k = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","amount","trades","taker_buy_base","taker_buy_quote","ignore"])
k = k[k.close_time < now].drop_duplicates("open_time").sort_values("open_time")
for c in ["open","high","low","close","volume","amount","taker_buy_base","taker_buy_quote"]: k[c] = k[c].astype(float)
k["ts"] = pd.to_datetime(k.open_time, unit="ms")
k[["ts","open","high","low","close","volume","amount","trades","taker_buy_base","taker_buy_quote"]].to_csv("$B/btc_perp_1h_flow.csv", index=False)
print("perp klines", len(k), k.ts.iloc[0], "->", k.ts.iloc[-1], "| gaps", int(k.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()))
start = int(pd.Timestamp("2023-10-01").timestamp()*1000); fr = []
while start < now:
    b = get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&startTime={start}&limit=1000")
    if not b: break
    fr += b; start = b[-1]["fundingTime"] + 1; time.sleep(0.2)
    if len(b) < 1000: break
f = pd.DataFrame(fr); f["ts"] = pd.to_datetime(f.fundingTime, unit="ms"); f["rate"] = f.fundingRate.astype(float)
f[["ts","rate"]].drop_duplicates("ts").to_csv("$B/btc_funding.csv", index=False)
print("funding", len(f), f.ts.iloc[0], "->", f.ts.iloc[-1], "| rate p5/p50/p95", f.rate.quantile([.05,.5,.95]).round(6).tolist())
t = get("https://api.kraken.com/0/public/Trades?pair=XBTUSD&count=1000")
tr = list(t["result"].values())[0]
span = float(tr[-1][2]) - float(tr[0][2])
print("kraken trades sample: 1000 trades span", round(span/60,1), "min | fields", tr[0])
o = get("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60")
oh = list(o["result"].values())[0]
print("kraken OHLC 60m rows", len(oh), "first", pd.to_datetime(oh[0][0], unit="s"), "| fields", oh[0])
EOF
