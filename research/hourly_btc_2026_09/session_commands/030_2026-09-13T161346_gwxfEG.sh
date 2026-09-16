B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import json, numpy as np, pandas as pd
df = pd.read_csv("$B/btc_1h_oos.csv")
df["absmove"] = (df.close - df.open).abs()
idx = {t:i for i,t in enumerate(df.open_time)}
rows = [json.loads(l) for l in open("$B/backtest_results.jsonl")]
recs = []
for r in rows:
    i = idx[r["target_open_time"]]
    lo, hi = r["pred_close_p10_p90"]
    recs.append(dict(actual=abs(r["real_close"]-r["real_open"]), kronos=hi-lo,
        trail24=df.absmove.iloc[i-24:i].mean(), trail168=df.absmove.iloc[i-168:i].mean(),
        last1=df.absmove.iloc[i-1]))
d = pd.DataFrame(recs)
print("n =", len(d))
print("rank correlation with next hour's actual size of move:")
for c in ["kronos","trail24","trail168","last1"]: print(f"  {c:9s} {d[c].corr(d.actual, method='spearman'):+.3f}")
r = d.rank()
def r2(cols):
    X = np.column_stack([np.ones(len(r))]+[r[c] for c in cols]); y = r.actual
    b = np.linalg.lstsq(X,y,rcond=None)[0]; res = y - X@b
    return 1 - res.var()/y.var()
base, both = r2(["trail24"]), r2(["trail24","kronos"])
print(f"explained (ranked): trailing-24h alone {base:.3f} | + kronos spread {both:.3f} | gain {both-base:+.3f}")
rng = np.random.default_rng(0); gains=[]
for _ in range(2000):
    s = rng.integers(0,len(r),len(r)); rr = r.iloc[s]
    X1 = np.column_stack([np.ones(len(rr)), rr.trail24]); X2 = np.column_stack([X1, rr.kronos]); y = rr.actual
    g = [1-(y-X@np.linalg.lstsq(X,y,rcond=None)[0]).var()/y.var() for X in (X1,X2)]
    gains.append(g[1]-g[0])
print("gain 95% range:", np.percentile(gains,[2.5,97.5]).round(3))
EOF
