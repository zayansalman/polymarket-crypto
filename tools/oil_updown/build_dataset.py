"""Daily WTI table: targets + features known at the prior close (decision time = after D-1 close).

Targets
  y_settle : CL=F settle(D) > settle(D-1)          2000-2026 (long history proxy)
  y_5pm    : 5pm-ET close(D) > 5pm close(D-1)       2024-04 onward (matches Polymarket 96%)
Every feature in row D uses data up to and including D-1 only.
"""
import numpy as np
import pandas as pd

ohlc = pd.read_parquet("market_ohlcv.parquet")
wti = pd.read_parquet("wti_cl.parquet")
closes = pd.read_parquet("market_closes.parquet")
h = pd.read_parquet("wti_1h.parquet")

idx = wti.index
df = pd.DataFrame(index=idx)
c = wti["Close"]
df["settle"] = c
df["y_settle"] = np.where(c.diff() > 0, 1.0, np.where(c.diff() < 0, 0.0, np.nan))

# 5pm ET close from hourly bars (last bar starting before 17:00 ET)
h = h.copy()
h["date"] = pd.to_datetime(h.index.date)
c5 = h[h.index.hour < 17].groupby("date")["Close"].last()
df["c5"] = c5.reindex(idx)
d5 = df["c5"].diff()
df["y_5pm"] = np.where(d5 > 0, 1.0, np.where(d5 < 0, 0.0, np.nan))
df.loc[df["c5"].isna() | df["c5"].shift(1).isna(), "y_5pm"] = np.nan


def safe_ret(s):
    prev = s.shift(1)
    r = (s - prev) / prev.abs()
    r[prev <= 0.5] = np.nan
    return r.clip(-0.5, 0.5)


r = safe_ret(c)
feat = pd.DataFrame(index=idx)
for k in range(1, 11):
    feat[f"ret_l{k}"] = r.shift(k)
for w in (3, 5, 10, 20, 60):
    feat[f"mom_{w}"] = r.rolling(w).sum().shift(1)
for w in (5, 20, 60):
    feat[f"vol_{w}"] = r.rolling(w).std().shift(1)
feat["vol_ratio"] = feat["vol_5"] / feat["vol_60"]
ma20 = c.rolling(20).mean()
feat["dist_ma20"] = ((c - ma20) / ma20.abs()).shift(1)
up = r.clip(lower=0).rolling(14).mean()
dn = (-r.clip(upper=0)).rolling(14).mean()
feat["rsi14"] = (100 - 100 / (1 + up / dn)).shift(1)
hi, lo, op = wti["High"], wti["Low"], wti["Open"]
feat["range_l1"] = ((hi - lo) / c.abs()).shift(1)
feat["clv_l1"] = ((2 * c - hi - lo) / (hi - lo).replace(0, np.nan)).shift(1)
feat["body_l1"] = ((c - op) / c.abs()).shift(1)
vol = wti["Volume"].replace(0, np.nan)
feat["volume_z"] = ((np.log(vol) - np.log(vol).rolling(60).mean()) / np.log(vol).rolling(60).std()).shift(1)
streak = np.sign(r).groupby((np.sign(r) != np.sign(r).shift()).cumsum()).cumcount() + 1
feat["streak_l1"] = (streak * np.sign(r)).shift(1)

# cross-asset (forward-filled onto WTI dates, lagged one day)
cx = closes.reindex(idx.union(closes.index)).sort_index().ffill().reindex(idx)
for n in ["brent", "dxy", "spx", "gold", "natgas", "gasoline", "heating", "xle", "uso", "t10"]:
    feat[f"{n}_ret_l1"] = safe_ret(cx[n]).shift(1)
    feat[f"{n}_mom5"] = safe_ret(cx[n]).rolling(5).sum().shift(1)
feat["vix_l1"] = cx["vix"].shift(1)
feat["vix_chg_l1"] = cx["vix"].diff().shift(1)
feat["brent_wti_spread"] = (cx["brent"] - c).shift(1)
feat["brent_wti_spread_chg"] = (cx["brent"] - c).diff().shift(1)
feat["crack_321"] = ((2 * cx["gasoline"] * 42 + cx["heating"] * 42 - 3 * c) / 3).shift(1)
feat["crack_chg5"] = feat["crack_321"].diff(5)

# calendar for day D (known in advance)
feat["dow"] = idx.dayofweek
feat["is_wed_eia"] = (idx.dayofweek == 2).astype(float)
feat["month"] = idx.month
feat["dom"] = idx.day
# CL expiry ~ 3 business days before the 25th: roll pressure days
feat["days_to_25"] = idx.day - 25

out = pd.concat([df, feat], axis=1)
out = out[out.index >= "2001-01-01"]
out.to_parquet("dataset.parquet")
print(out.shape, "features:", feat.shape[1])
print("y_settle rows", out.y_settle.notna().sum(), "up rate", round(out.y_settle.mean(), 4))
print("y_5pm rows", out.y_5pm.notna().sum(), "up rate", round(out.y_5pm.mean(), 4),
      out.y_5pm.dropna().index.min().date(), out.y_5pm.dropna().index.max().date())
print("settle vs 5pm direction agreement:",
      round((out.y_5pm == out.y_settle)[out.y_5pm.notna() & out.y_settle.notna()].mean(), 3))
