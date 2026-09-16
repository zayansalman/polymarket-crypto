B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; cat > $B/PREREG_tierA_factors.md <<'EOF'
# Pre-registered 2026-09-14 (before running): Tier A x consistent factors
Tier A = spot fz > 1.20 (discovery p80), perp fz <= 1.24, CLV beyond +-0.8 in H-1 move direction; bet against H-1.
Splits (fixed): (1) Tier A & BTC/ETH H-1 same direction; (2) Tier A & hour != 08 UTC; (3) Tier A & both.
Bar unchanged: validation point >= 60%, lower 95% >= 55%, n >= 150. Report all three with discovery and validation.
EOF
python3 - <<EOF
import math, numpy as np, pandas as pd
B="$B"; SPLIT=pd.Timestamp("2025-10-18 14:00")
def hourly(n):
    d=pd.read_csv(f"{B}/{n}", parse_dates=["ts"]); imb=2*d.taker_buy_base/d.volume-1
    d["z"]=(imb-imb.rolling(168).mean())/imb.rolling(168).std(); d["dir"]=np.sign(d.close-d.open); d["fz"]=d.z*d.dir; return d
s=hourly("btc_1h_flow.csv"); p=hourly("btc_perp_1h_flow.csv")[["ts","fz"]].rename(columns={"fz":"p_fz"}); e=hourly("eth_1h_flow.csv")[["ts","dir"]].rename(columns={"dir":"e_dir"})
df=s.merge(p,on="ts").merge(e,on="ts"); df["up"]=(df.close>df.open).astype(int)
rng=(df.high-df.low).replace(0,np.nan); df["clv"]=(2*df.close-df.high-df.low)/rng
for c in ["fz","p_fz","dir","clv","e_dir"]: df[c+"_1"]=df[c].shift(1)
df=df.iloc[300:].dropna(subset=["fz_1","p_fz_1","dir_1","clv_1","e_dir_1"]); df=df[df.dir_1!=0]
tierA=(df.fz_1>1.20)&(df.p_fz_1<=1.24)&(((df.dir_1>0)&(df.clv_1>0.8))|((df.dir_1<0)&(df.clv_1<-0.8)))
win=(df.up==(df.dir_1<0).astype(int)).astype(int); disc=df.ts<SPLIT
def w(x):
    n=len(x); k=int(x.sum()); p=k/n if n else float('nan'); z=1.96; d=1+z*z/n; c=(p+z*z/(2*n))/d; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return f"{p:5.1%} [{c-h:4.1%},{c+h:4.1%}] n={n}", (p, c-h, n)
same=df.e_dir_1==df.dir_1; not08=df.ts.dt.hour!=8
for name,m in [("Tier A (reference)",tierA),("(1) Tier A & ETH same dir",tierA&same),("(2) Tier A & not 08 UTC",tierA&not08),("(3) Tier A & both",tierA&same&not08)]:
    ds,_=w(win[m&disc]); vs,(vp,vlo,vn)=w(win[m&~disc])
    print(f"{name:28s} | disc {ds:30s} | val {vs:30s} | bar met: {vn>=150 and vp>=0.60 and vlo>=0.55}")
EOF
