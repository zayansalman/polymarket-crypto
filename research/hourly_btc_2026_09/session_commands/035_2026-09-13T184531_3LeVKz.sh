B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 - <<EOF
import pandas as pd, numpy as np, json, math
df = pd.read_csv("$B/btc_1h_full.csv", parse_dates=["ts"])
df["up"] = (df.close > df.open).astype(int)
d = df.close.diff(); g = d.clip(lower=0); l = (-d).clip(lower=0)
rs = g.ewm(alpha=1/14, adjust=False).mean() / l.ewm(alpha=1/14, adjust=False).mean()
df["rsi_prev"] = (100 - 100/(1+rs)).shift(1)
df["ret1"] = ((df.close - df.open)/df.open).shift(1)
df["ret4"] = (df.close.shift(1)/df.close.shift(5) - 1)
df["ret24"] = (df.close.shift(1)/df.close.shift(25) - 1)
df["absmove_prev24"] = ((df.close-df.open).abs()).shift(1).rolling(24).mean()
rows = [json.loads(l) for l in open("$B/backtest_results.jsonl")]
k = pd.DataFrame([{ "ts": pd.Timestamp(r["target_ts"]), "p_own": r["p_up_within_path"], "p_real": r["p_up_vs_real_open"],
  "pred_move": r["pred_close_median"] - r["pred_open_median"], "spread": r["pred_close_p10_p90"][1]-r["pred_close_p10_p90"][0],
  "real_open": r["real_open"], "real_close": r["real_close"], "up": r["up"]} for r in rows]).merge(df[["ts","rsi_prev","ret1","ret4","ret24","absmove_prev24"]], on="ts")
k["actual_move"] = k.real_close - k.real_open
def w(kk, n):
    if n == 0: return "n=0"
    p=kk/n; z=1.96; dd=1+z*z/n; c=(p+z*z/(2*n))/dd; h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/dd
    return f"{p:5.1%} ({c-h:4.1%}-{c+h:4.1%}) n={n}"
sp = lambda a,b: pd.Series(a).corr(pd.Series(b), method="spearman")
print("1) WHAT KRONOS'S CALLS FOLLOW (rank correlation of its P(up) with each input)")
for c in ["ret1","ret4","ret24","rsi_prev"]:
    print(f"   {c:9s} own-open P(up) {sp(k.p_own,k[c]):+.3f} | real-open P(up) {sp(k.p_real,k[c]):+.3f}")
dec = k[k.p_own != 0.5]
cont = (np.sign(dec.p_own-0.5) == np.sign(dec.ret1)).mean()
print(f"   calls that CONTINUE last hour's direction: {cont:.1%}")
print(f"   its predicted move vs actual move, rank corr: {sp(k.pred_move, k.actual_move):+.3f}")
print(f"   average predicted move \${k.pred_move.mean():+.0f} vs average actual \${k.actual_move.mean():+.0f}")

print("\n2) REVERSAL BET (bet against last hour) ON THESE 500 HOURS, SPLIT BY KRONOS")
k["rev_call"] = np.where(k.ret1 > 0, 0, 1)
k["rev_hit"] = (k.rev_call == k.up).astype(int)
k["kr_call"] = (k.p_own > 0.5).astype(int)
m = k[k.p_own != 0.5]
print("   reversal bet, all hours          ", w(int(k.rev_hit.sum()), len(k)))
print("   Kronos AGREES with reversal      ", w(int(m[m.kr_call==m.rev_call].rev_hit.sum()), int((m.kr_call==m.rev_call).sum())))
print("   Kronos DISAGREES with reversal   ", w(int(m[m.kr_call!=m.rev_call].rev_hit.sum()), int((m.kr_call!=m.rev_call).sum())))

print("\n3) OVERREACTION GAUGE: last hour's move relative to Kronos's expected range")
k["stretch"] = k.ret1.abs()*k.real_open / k.spread
q = k.stretch.quantile([1/3, 2/3]).values
for name, s in [("small vs expected (bottom third)", k.stretch <= q[0]), ("middle third", (k.stretch>q[0])&(k.stretch<=q[1])), ("large vs expected (top third)", k.stretch > q[1])]:
    print(f"   reversal bet when last move was {name:34s} {w(int(k[s].rev_hit.sum()), int(s.sum()))}")
k["stretch24"] = k.ret1.abs()*k.real_open / k.absmove_prev24
q2 = k.stretch24.quantile([2/3]).values[0]
print(f"   same split using plain 24h-average move instead (top third): {w(int(k[k.stretch24>q2].rev_hit.sum()), int((k.stretch24>q2).sum()))}")

print("\n4) DOES KRONOS'S EXPECTED MOVE SIZE CHANGE THE REVERSAL?")
qs = k.spread.quantile([0.5]).values[0]
for name, s in [("Kronos expects a calm hour (below-median range)", k.spread <= qs), ("Kronos expects a big hour (above-median range)", k.spread > qs)]:
    print(f"   {name:48s} {w(int(k[s].rev_hit.sum()), int(s.sum()))}")

print("\n5) RSI EXTREMES + KRONOS")
for name, s in [("RSI<30 or >70, bet reversal", (k.rsi_prev<30)|(k.rsi_prev>70))]:
    kk = k[s].copy(); kk["rsi_call"] = (kk.rsi_prev<30).astype(int); kk["hit"] = (kk.rsi_call==kk.up).astype(int)
    print(f"   {name:34s} {w(int(kk.hit.sum()), len(kk))}")
    a = kk[(kk.p_own!=0.5) & (kk.kr_call==kk.rsi_call)]; b = kk[(kk.p_own!=0.5) & (kk.kr_call!=kk.rsi_call)]
    print(f"   ...Kronos agrees                  {w(int(a.hit.sum()), len(a))}")
    print(f"   ...Kronos disagrees               {w(int(b.hit.sum()), len(b))}")
EOF
