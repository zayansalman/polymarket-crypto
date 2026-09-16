B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import math, json, numpy as np, pandas as pd
SPLIT = pd.Timestamp("2025-10-18 14:00")
df = pd.read_csv("$B/btc_1h_flow.csv", parse_dates=["ts"])
df["up"] = (df.close > df.open).astype(int)
imb = 2*df.taker_buy_base/df.volume - 1
z = (imb - imb.rolling(168).mean())/imb.rolling(168).std()
mv = df.close - df.open
df["fz_raw"] = z*np.sign(mv); df["z_raw"] = z; df["dir"] = np.sign(mv)
df = df.iloc[200:].reset_index(drop=True)
disc = df[df.ts < SPLIT]
q5 = disc.fz_raw.quantile(0.8); qz = disc.z_raw.abs().quantile(0.8)
def w(k, n):
    if n == 0: return "n=0"
    p=k/n; zc=1.96; d=1+zc*zc/n; c=(p+zc*zc/(2*n))/d; h=zc*math.sqrt(p*(1-p)/n+zc*zc/(4*n*n))/d
    return f"{p:5.1%} [{c-h:4.1%},{c+h:4.1%}] n={n}"
# signal at hour H from hour H-1
df["sig"] = (df.fz_raw.shift(1) > q5) & (df.dir.shift(1) != 0)
df["bet_up"] = (df.dir.shift(1) < 0).astype(int)
df["win"] = (df.up == df.bet_up).astype(int)
df["against_flow_sig"] = df.z_raw.shift(1).abs() > qz
df["against_flow_win"] = (df.up == (df.z_raw.shift(1) < 0).astype(int)).astype(int)

print("1) DECAY OVER TIME - flow-pushed move -> bet reversal, by half-year")
df["half"] = df.ts.dt.year.astype(str) + np.where(df.ts.dt.month <= 6, "-H1", "-H2")
rows = []
for h, g in df.groupby("half"):
    s = g[g.sig]; a = g[g.against_flow_sig]
    rows.append((h, s.win.mean(), len(s), g.ts.mean()))
    print(f"   {h}: flow-pushed reversal {w(int(s.win.sum()), len(s))} | against strong flow {w(int(a.against_flow_win.sum()), len(a))} | all-hours reversal {((g.up != g.dir.shift(1).gt(0).astype(int)).mean()):.1%}")
s = df[df.sig].copy(); t = (s.ts - s.ts.min()).dt.days.values/365.25; y = s.win.values.astype(float)
X = np.column_stack([np.ones_like(t), t]); b = np.linalg.lstsq(X, y, rcond=None)[0]
res = y - X@b; se = math.sqrt(res.var()*np.linalg.inv(X.T@X)[1,1])
now_t = (df.ts.max() - s.ts.min()).days/365.25
print(f"   linear trend in win rate: {b[1]*100:+.2f} pts per year (95% CI {(b[1]-1.96*se)*100:+.2f} to {(b[1]+1.96*se)*100:+.2f})")
for yrs in [0, 0.5, 1.0]:
    print(f"   trend-implied win rate {'now' if yrs==0 else f'in {yrs:g} yr'}: {(b[0]+b[1]*(now_t+yrs)):.1%}")

print("\n2) DECAY OVER HOURS - flow-pushed move at hour H-k, does hour H go the other way?")
for k in range(1, 7):
    sig = (df.fz_raw.shift(k) > q5) & (df.dir.shift(k) != 0)
    win = (df.up == (df.dir.shift(k) < 0).astype(int))
    for name, part in [("2023-25", df.ts < SPLIT), ("2025-26", df.ts >= SPLIT)]:
        m = sig & part
        print(f"   k={k} {name}: {w(int(win[m].sum()), int(m.sum()))}", end=" |" if name=="2023-25" else "\n")

print("\n3) UP vs DOWN - which side carries it")
for name, part in [("2023-25", df.ts < SPLIT), ("2025-26 unseen", df.ts >= SPLIT)]:
    g = df[part]; base_up = g.up.mean()
    after_down = g[g.sig & (g.dir.shift(1) < 0)]; after_up = g[g.sig & (g.dir.shift(1) > 0)]
    plain_down = g[(g.dir.shift(1) < 0) & ~g.sig]; plain_up = g[(g.dir.shift(1) > 0) & ~g.sig]
    print(f"   {name}: base P(up) {base_up:.1%}")
    print(f"      after flow-pushed DOWN hour -> bet UP:   win {w(int(after_down.up.sum()), len(after_down))} | other down hours P(up) {plain_down.up.mean():.1%}")
    print(f"      after flow-pushed UP hour   -> bet DOWN: win {w(int((1-after_up.up).sum()), len(after_up))} | other up hours P(down) {(1-plain_up.up).mean():.1%}")

print("\n4) WITH KRONOS (500 unseen hours; small samples)")
rows = [json.loads(l) for l in open("$B/backtest_results.jsonl")]
k = pd.DataFrame({"ts": pd.to_datetime([r["target_ts"] for r in rows]), "p": [r["p_up_within_path"] for r in rows]}).merge(df[["ts","sig","bet_up","win","up"]], on="ts")
kk = k[k.sig & (k.p != 0.5)]
agree = kk[(kk.p > 0.5).astype(int) == kk.bet_up]; dis = kk[(kk.p > 0.5).astype(int) != kk.bet_up]
print(f"   flow signal fired on {int(k.sig.sum())} of {len(k)} hours; flow bet overall {w(int(k[k.sig].win.sum()), int(k.sig.sum()))}")
print(f"   ...Kronos agrees   {w(int(agree.win.sum()), len(agree))}")
print(f"   ...Kronos disagrees {w(int(dis.win.sum()), len(dis))}")
EOF
