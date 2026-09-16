import json, time, urllib.request
from pathlib import Path
import pandas as pd
B = Path(__file__).parent
START = int(pd.Timestamp("2023-10-12").timestamp() * 1000)
NOW = int(time.time() * 1000)

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for a in range(5):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception:
            time.sleep(2 + 3 * a)
    raise RuntimeError(url)

def klines(base, path, symbol, limit, out):
    rows, s = [], START
    while s < NOW:
        b = get(f"{base}{path}?symbol={symbol}&interval=1h&startTime={s}&limit={limit}")
        if not b:
            break
        rows += b; s = b[-1][0] + 3600000; time.sleep(0.12)
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","amount","trades","taker_buy_base","taker_buy_quote","ignore"])
    df = df[df.close_time < NOW].drop_duplicates("open_time").sort_values("open_time")
    for c in ["open","high","low","close","volume","amount","taker_buy_base","taker_buy_quote"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df.open_time, unit="ms")
    df[["ts","open","high","low","close","volume","amount","trades","taker_buy_base","taker_buy_quote"]].to_csv(B / out, index=False)
    print(out, len(df), df.ts.iloc[0], "->", df.ts.iloc[-1], "gaps", int(df.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()), flush=True)

def bybit_oi(symbol, out):
    rows, s, step = [], START, 200 * 3600 * 1000
    while s < NOW:
        e = min(s + step - 1, NOW)
        d = get(f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol={symbol}&intervalTime=1h&startTime={s}&endTime={e}&limit=200")
        rows += [(int(x["timestamp"]), float(x["openInterest"])) for x in d.get("result", {}).get("list", [])]
        s = e + 1; time.sleep(0.12)
    df = pd.DataFrame(rows, columns=["ts_ms","oi"]).drop_duplicates("ts_ms").sort_values("ts_ms")
    df["ts"] = pd.to_datetime(df.ts_ms, unit="ms")
    df.to_csv(B / out, index=False)
    print(out, len(df), df.ts.iloc[0], "->", df.ts.iloc[-1], "gaps", int(df.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()), flush=True)

for sym in ["SOLUSDT", "XRPUSDT"]:
    klines("https://api.binance.com", "/api/v3/klines", sym, 1000, f"{sym.lower()[:3]}_1h_flow.csv")
for sym in ["ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    klines("https://fapi.binance.com", "/fapi/v1/klines", sym, 1500, f"{sym.lower()[:3]}_perp_1h_flow.csv")
    bybit_oi(sym, f"bybit_oi_{sym.lower()[:3]}_1h.csv")
print("DONE", flush=True)
