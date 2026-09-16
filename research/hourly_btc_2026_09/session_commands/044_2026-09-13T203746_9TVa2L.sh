B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import json, time, urllib.request, numpy as np, pandas as pd
start = int(pd.Timestamp("2023-10-12 00:00").timestamp()*1000); now = int(time.time()*1000)
cols = []; n = 0
while start < now:
    for attempt in range(5):
        try:
            b = json.load(urllib.request.urlopen(f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={start}&limit=1000", timeout=30)); break
        except Exception as e:
            time.sleep(2 + attempt*3)
    else:
        raise SystemExit(f"failed at {start}")
    if not b: break
    a = np.array([[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[9]), r[6]] for r in b])
    cols.append(a); start = int(b[-1][0]) + 60000; n += 1
    if n % 200 == 0: print("calls", n, pd.to_datetime(start, unit="ms"), flush=True)
    time.sleep(0.12)
a = np.vstack(cols); a = a[a[:,7] < now]
df = pd.DataFrame(a[:, :7], columns=["open_time","open","high","low","close","volume","taker_buy_base"])
df = df.drop_duplicates("open_time").sort_values("open_time")
df["open_time"] = df.open_time.astype("int64")
df.to_parquet("$B/btc_1m_spot.parquet") if False else df.to_csv("$B/btc_1m_spot.csv", index=False)
gaps = int((df.open_time.diff().dropna() != 60000).sum())
print("DONE rows", len(df), pd.to_datetime(df.open_time.iloc[0], unit="ms"), "->", pd.to_datetime(df.open_time.iloc[-1], unit="ms"), "gaps", gaps)
EOF
