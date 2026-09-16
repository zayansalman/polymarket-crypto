B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import json, time, urllib.request, pandas as pd
start = int(pd.Timestamp("2023-10-12").timestamp()*1000); end_all = int(time.time()*1000); rows = []
step = 200*3600*1000
s = start
while s < end_all:
    e = min(s + step - 1, end_all)
    url = f"https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1h&startTime={s}&endTime={e}&limit=200"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for a in range(4):
        try:
            d = json.load(urllib.request.urlopen(req, timeout=30)); break
        except Exception as ex:
            time.sleep(2 + 2*a)
    else:
        raise SystemExit(f"failed {s}")
    lst = d.get("result", {}).get("list", [])
    rows += [(int(x["timestamp"]), float(x["openInterest"])) for x in lst]
    s = e + 1; time.sleep(0.15)
df = pd.DataFrame(rows, columns=["ts_ms","oi"]).drop_duplicates("ts_ms").sort_values("ts_ms")
df["ts"] = pd.to_datetime(df.ts_ms, unit="ms")
df.to_csv("$B/bybit_oi_1h.csv", index=False)
gaps = df.ts.diff().dropna().ne(pd.Timedelta(hours=1)).sum()
print("rows", len(df), df.ts.iloc[0], "->", df.ts.iloc[-1], "gaps", int(gaps))
EOF
