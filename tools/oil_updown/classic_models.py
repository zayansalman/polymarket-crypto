"""Walk-forward baselines, linear and GBM models. Writes preds/<model>.parquet (index=date, p_up).

Pre-registered protocol (fixed before looking at any test result):
  - expanding window, refit once per calendar year; test years 2012..2026
  - train target: y_settle (settle-to-settle direction); regression models use next-day return
  - a model's call for day D uses only rows < D's year start for fitting, and features known at D-1
  - no hyperparameter search; settings below are fixed defaults
"""
import os
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
os.makedirs("preds", exist_ok=True)

d = pd.read_parquet("dataset.parquet")
d = d[d.index < "2026-09-14"]  # today's row is incomplete
c = d["settle"]
prev = c.shift(1)
d["ret"] = ((c - prev) / prev.abs()).where(prev > 0.5).clip(-0.5, 0.5)

TARGETS = {"settle", "c5", "y_settle", "y_5pm", "ret"}
ALL = [col for col in d.columns if col not in TARGETS]
LAGS = [f"ret_l{k}" for k in range(1, 11)]
PRICE_ONLY = LAGS + [f"mom_{w}" for w in (3, 5, 10, 20, 60)] + ["vol_5", "vol_20", "vol_60", "vol_ratio",
              "dist_ma20", "rsi14", "range_l1", "clv_l1", "body_l1", "volume_z", "streak_l1"]
CAL = ["dow", "is_wed_eia", "month", "dom", "days_to_25"]
YEARS = range(2012, 2027)


def save(name, s):
    s = s.dropna().rename("p_up").to_frame()
    s.to_parquet(f"preds/{name}.parquet")
    print(f"{name:22s} n={len(s)}")


# ---- rule baselines (no fitting) ----
save("rule_always_up", pd.Series(0.51, index=d.index[d.index >= "2012-01-01"]))
save("rule_persist_1d", (d["ret_l1"] > 0).astype(float).where(d["ret_l1"].notna())[d.index >= "2012-01-01"])
save("rule_reversal_1d", (d["ret_l1"] < 0).astype(float).where(d["ret_l1"].notna())[d.index >= "2012-01-01"])
save("rule_mom5", (d["mom_5"] > 0).astype(float).where(d["mom_5"].notna())[d.index >= "2012-01-01"])
save("rule_mom20", (d["mom_20"] > 0).astype(float).where(d["mom_20"].notna())[d.index >= "2012-01-01"])
save("rule_brent_lead", (d["brent_ret_l1"] - d["ret_l1"] > 0).astype(float)[d.index >= "2012-01-01"])


def walk_forward(make_model, cols, kind="clf", window_years=None):
    out = []
    for y in YEARS:
        start = y - window_years if window_years else 2001
        tr = d[(d.index.year >= start) & (d.index.year < y)]
        te = d[d.index.year == y]
        if te.empty:
            continue
        target = "y_settle" if kind == "clf" else "ret"
        tr = tr[tr[target].notna()]
        m = make_model()
        m.fit(tr[cols], tr[target])
        if kind == "clf":
            p = m.predict_proba(te[cols])[:, 1]
        else:  # regression: map predicted return to pseudo-probability via training residual scale
            pred = m.predict(te[cols])
            scale = np.std(tr[target] - m.predict(tr[cols]))
            from scipy.stats import norm
            p = norm.cdf(pred / scale)
        out.append(pd.Series(p, index=te.index))
    return pd.concat(out)


def lin_pipe(model):
    return lambda: make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), model)


save("linear_ar10_ols", walk_forward(lin_pipe(LinearRegression()), LAGS, kind="reg"))
save("linear_ols_all", walk_forward(lin_pipe(LinearRegression()), ALL, kind="reg"))
save("logit_lags", walk_forward(lin_pipe(LogisticRegression(C=0.1, max_iter=2000)), LAGS))
save("logit_price", walk_forward(lin_pipe(LogisticRegression(C=0.1, max_iter=2000)), PRICE_ONLY))
save("logit_all", walk_forward(lin_pipe(LogisticRegression(C=0.1, max_iter=2000)), ALL))
save("logit_all_l1", walk_forward(lin_pipe(LogisticRegression(C=0.05, penalty="l1", solver="liblinear", max_iter=2000)), ALL))
save("logit_all_recent5y", walk_forward(lin_pipe(LogisticRegression(C=0.1, max_iter=2000)), ALL, window_years=5))


def lgbm():
    return lgb.LGBMClassifier(n_estimators=300, learning_rate=0.02, num_leaves=15, min_child_samples=80,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0, verbose=-1)


save("gbm_lgbm_price", walk_forward(lgbm, PRICE_ONLY + CAL))
save("gbm_lgbm_all", walk_forward(lgbm, ALL))
save("gbm_lgbm_all_recent5y", walk_forward(lgbm, ALL, window_years=5))
save("gbm_lgbm_reg_all", walk_forward(lambda: lgb.LGBMRegressor(n_estimators=300, learning_rate=0.02, num_leaves=15,
                                                                 min_child_samples=80, subsample=0.8, subsample_freq=1,
                                                                 colsample_bytree=0.7, reg_lambda=5.0, verbose=-1),
                                      ALL, kind="reg"))
save("rf_depth3_price", walk_forward(lambda: make_pipeline(SimpleImputer(strategy="median"),
                                     RandomForestClassifier(n_estimators=300, max_depth=3, class_weight="balanced",
                                                            random_state=0, n_jobs=-1)), PRICE_ONLY))
