B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import pandas as pd, numpy as np, json, math
df = pd.read_csv("$B/btc_1h_full.csv", parse_dates=["ts"])
df["up"] = (df.close > df.open).astype(int)
d = df.close.diff(); g = d.clip(lower=0); l = (-d).clip(lower=0)
rs = g.ewm(alpha=1/14, adjust=False).mean() / l.ewm(alpha=1/14, adjust=False).mean()
df["rsi_prev"] = (100 - 100/(1+rs)).shift(1)
df["last_up"] = df.up.shift(1)
df["mom4"] = np.sign(df.close.shift(1) - df.close.shift(5))
df["mom24"] = np.sign(df.close.shift(1) - df.close.shift(25))
mv = (df.close - df.open).abs() / df.open
df["big_last"] = (mv.shift(1) > mv.shift(1).rolling(720, min_periods=200).quantile(0.9)).astype(float)
df = df.iloc[30:]
def w(k, n):
    if n == 0: return "n=0"
    p = k/n; z = 1.96; dd = 1+z*z/n; c = (p+z*z/(2*n))/dd; h = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/dd
    return f"{p:5.1%} ({c-h:4.1%}-{c+h:4.1%}) n={n}"
periods = {"2023-10..2025-10": df[df.ts < "2025-10-18 14:00"], "2025-10..2026-09 (unseen)": df[df.ts >= "2025-10-18 14:00"]}
rules = {
  "all hours": lambda x: x.up.notna(),
  "RSI < 30 (oversold)": lambda x: x.rsi_prev < 30,
  "RSI 30-50": lambda x: (x.rsi_prev >= 30) & (x.rsi_prev < 50),
  "RSI 50-70": lambda x: (x.rsi_prev >= 50) & (x.rsi_prev < 70),
  "RSI > 70 (overbought)": lambda x: x.rsi_prev > 70,
  "last hour up": lambda x: x.last_up == 1,
  "last hour down": lambda x: x.last_up == 0,
  "4h momentum up": lambda x: x.mom4 > 0,
  "4h momentum down": lambda x: x.mom4 < 0,
  "24h momentum up": lambda x: x.mom24 > 0,
  "24h momentum down": lambda x: x.mom24 < 0,
  "big last hour, was up": lambda x: (x.big_last == 1) & (x.last_up == 1),
  "big last hour, was down": lambda x: (x.big_last == 1) & (x.last_up == 0),
}
print("share of hours closing UP after each condition")
print(f"{'condition':26s} | {list(periods)[0]:36s} | {list(periods)[1]}")
for name, f in rules.items():
    cells = [w(int(p[f(p)].up.sum()), int(f(p).sum())) for p in periods.values()]
    print(f"{name:26s} | {cells[0]:36s} | {cells[1]}")
rows = [json.loads(l) for l in open("$B/backtest_results.jsonl")]
k = pd.DataFrame({"ts": pd.to_datetime([r["target_ts"] for r in rows]), "p": [r["p_up_within_path"] for r in rows], "up": [r["up"] for r in rows]}).merge(df[["ts","rsi_prev","mom24","last_up"]], on="ts")
k = k[k.p != 0.5]; k["hit"] = ((k.p > 0.5).astype(int) == k.up).astype(int)
print("\nKronos hit rate (500 unseen hours) split by condition")
for name, f in {"all": lambda x: x.p.notna(), "RSI < 30": lambda x: x.rsi_prev < 30, "RSI > 70": lambda x: x.rsi_prev > 70, "RSI 30-70": lambda x: x.rsi_prev.between(30, 70), "agrees with 24h momentum": lambda x: np.sign(x.p - 0.5) == x.mom24, "fights 24h momentum": lambda x: np.sign(x.p - 0.5) == -x.mom24}.items():
    s = f(k); print(f"  {name:26s} {w(int(k[s].hit.sum()), int(s.sum()))}")
EOF
