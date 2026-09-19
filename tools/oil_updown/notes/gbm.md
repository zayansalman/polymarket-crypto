# GBM / tree-ensemble candidates for daily WTI/Brent direction

Compiled 2026-09-14. Read-only search: GitHub (repo, topic, and code search), Hugging Face (models, datasets, Spaces), Kaggle (web), arXiv (alphaXiv), journals and SSRN (web). READMEs and result files were read directly. Nothing was run and no weights were downloaded.

Target use: Polymarket daily "WTI Up or Down", which compares the 5pm-ET close to the prior 5pm-ET close. Candidates are judged on whether they can be re-scored on a next-day up/down label with inputs known as of the prior 5pm ET.

Already known, not repeated below: nyanko1999/oil-price-model, NavnoorBawa/WTI-Crude-Oil-Futures, occasional-thoughts/multimodal-futures-research, Dai et al. arXiv 2603.11408.

## Bottom line

- No GBM or tree model for oil direction has published weights on Hugging Face. No Kaggle competition targets daily WTI direction. Hugging Face oil Spaces are LSTM or price-level demos.
- **Honest results cluster at 52-55% accuracy and 0.53-0.56 AUC for next-day direction.** Single holdouts reach 55-61%. Walk-forward or purged testing pulls results back toward 50-54%.
- Anything claiming 60% or more on daily direction had a clear leak:
  - features from the same day as the target
  - overlapping or smoothed targets
  - price-level "direction"
  - decomposition fit on the full series
  - a feature set chosen while looking at the test period
- The most useful finds are repos with **saved out-of-sample predictions or saved models plus leak-safe feature pipelines** (EIA release timing, OVX regimes, GDELT, FRED). They serve as baselines and feature templates, not as proven edges.

---

## A. Test-worthy candidates (ranked)

### 1. SelinaPhan0205/OilPricePrediction
https://github.com/SelinaPhan0205/OilPricePrediction

- **Target:** next-day oil return direction, `oil_return_fwd1 > 0`.
- **Model:** LGBMClassifier with 13 features. The saved model is `ml/classification/results_daily_selected_model_v1/selected_daily_model.joblib`. Processed datasets and the feature list are committed.
- **Features:**
  - VIX return, oil return, S&P return lag 1, 5-day mean return, 10-day momentum, MACD signal
  - GDELT tone lag 1 and volume lag 1
  - yield spread and 10y-3y change lag 1
  - EIA net imports change
  - OVX high-regime flag lag 1 and oil high-volatility regime flag lag 1
  - The extended set also has MOVE, gold, DGS3 and DGS10.
- **Results:**
  - Fixed test from 2023-01 (840 days): accuracy 0.555, macro-F1 0.544, AUC 0.570.
  - Yearly walk-forward, 6 folds, 2020-2025: mean accuracy 0.547, AUC 0.549. Majority baseline 0.512, persistence 0.494.
  - Longer 2007-2026 history, 11 folds: accuracy 0.524, AUC 0.540. Adding old regimes hurt.
  - OVX high-regime slice: accuracy 0.58.
  - Confidence filter at 0.56 with 80% coverage: 0.558.
- **Red flags:**
  - The 13 features were picked after many experiments that could see the 2023+ test, so the holdout is inflated.
  - The authors dropped several macro and GDELT stress columns for leakage themselves.
  - No license.
- **Why test:** it is the cleanest documented daily LGBM, and the saved model makes a baseline quick. The OVX-regime result is worth checking.

### 2. trungdangtapcode/Oil-Prediction-Data-Mining-ML (sibling of #1, same course pipeline)
https://github.com/trungdangtapcode/Oil-Prediction-Data-Mining-ML

- **Target:** next-day direction. Data runs 2015-2026: train to 2022-12, test 2023-01 to 2026-03 (840 rows).
- **Features:** ACLED conflict data, GDELT tone and Goldstein scores, Yahoo (USD, S&P, VIX), FRED macro, EIA inventory z-score, production and net imports.
- **Results:** best LGBM accuracy 0.5452; best XGBoost AUC 0.5586.
- **Extra:** `docs/improve/oil_direction_research_benchmark_brief.md` sets realistic bands: accuracy 0.53-0.56 "acceptable", above 0.63 "audit".
- **Red flags:**
  - One holdout only.
  - Same-day `vix_close`, `usd_close` and `sp500_close` were flagged for leakage by the authors.
  - ACLED requires a manual export.
- **Why test:** it is the fullest geopolitical and fundamental feature pipeline with a daily target.

### 3. Ddhamani123/wti-direction-ml (NYU VIP, spring 2026)
https://github.com/Ddhamani123/wti-direction-ml

- **Target:** `sign(P_t - P_{t-1})` for the next day.
- **Models:** XGBoost, Elastic Net, MLP, LSTM and VAR.
- **Features:**
  - 30 price lags
  - 31 FRED-MD macro series, lagged one month
  - EIA Cushing and total crude stocks: week-over-week change, 4-week moving average, and a "surprise" defined as the change minus its 4-week mean
  - EIA data shifted one trading day after the Wednesday release
- **Splits:** train to 2014, validation 2015-2024, test 2025. TimeSeriesSplit inside train.
- **Results:**
  - XGBoost validation accuracy 0.534 (2,635 days).
  - Test 2025 (216 days): accuracy 0.49-0.50, AUC 0.50-0.52.
- **Assets:** saved out-of-sample predictions in `predictions/` (xgb, xgb_eia, xgb_baseline, lstm, mlpen, var; validation and test CSVs) and `report/Final_Report.pdf`.
- **Red flags:** 2025 test is small. Monthly FRED-MD adds little at a daily horizon. No license.
- **Why test:** the ready-made 2015-2025 prediction CSVs can be scored against the Polymarket label without retraining. The EIA timing is done correctly.

### 4. ziqianz360/oil-price-prediction (NYU VIP)
https://github.com/ziqianz360/oil-price-prediction

- **Target:** next-day direction.
- **Models:** LightGBM, CNN, RNN and BiLSTM. Features: 30-day price and FRED-MD windows, with and without FinBERT oil-news sentiment. A volatility-targeted strategy is included.
- **Results:** `results/four_model_sentiment_results/metrics_by_model.csv`, test around 386 days:
  - LightGBM price+macro: accuracy 0.554, balanced accuracy 0.549, AUC 0.558.
  - Adding sentiment: accuracy 0.518, AUC 0.539.
  - The neural nets collapsed to predicting "up".
  - An earlier run (`summary_tables/model_comparison.csv`, test 2025-01 to 2026-01, 277 days) had LightGBM at 0.498 accuracy and 0.467 AUC.
- **Red flags:** results change a lot between runs, the test is small, the strategy Sharpe was negative, and trained binaries and raw data are not included.
- **Why test:** it is a second independent daily LightGBM and includes McNemar and bootstrap code.

### 5. RobinDreh/Meta-Trading-Strategy-Energy-Futures
https://github.com/RobinDreh/Meta-Trading-Strategy-Energy-Futures

- **Setup:** López de Prado meta-labeling. A primary daily long/short/flat signal is filtered by a meta-model ensemble weighted LightGBM 38.7%, XGBoost 33.0%, RandomForest 11.5%.
- **Labels:** 10-day triple barrier, pooled across CL, HO, RB and NG, trained on 2020-2021 events.
- **Features:**
  - candlestick shape and volume
  - forward-curve slope
  - HMM regime probabilities
  - population-weighted heating and cooling degree days
  - Caldara-Iacoviello GPR split into acts and threats (acts showed a monotonic relation with next crude returns)
  - COT and weather
- **Results:** walk-forward CV with a 10-day embargo gives LightGBM AUC 0.559 (std 0.062). **Combinatorial purged CV gives AUC 0.495.** Validation window is Jan-Jun 2022.
- **Red flags:**
  - Relies on `bloomberg_daily_panel.csv`, which is not free.
  - 10-day horizon, not next-day.
  - Barrier geometry was grid-searched, though with a neighbourhood-mean criterion.
  - Tiny validation window.
- **Why test:** it is the best template for purged CV and embargo, and the CPCV figure is an honest reality check. Its GPR acts/threats and curve-slope features carry over directly.

### 6. PeterLP123/wti-return-forecasting
https://github.com/PeterLP123/wti-return-forecasting

- **Target:** next-day WTI log return. Panel 2017-01 to 2026-03; test 2024-07 to 2026-03 (348 obs).
- **Models:** probabilistic LSTM, XGBoost, AR, naive and GARCH.
- **Leak controls:** `docs/FEATURE_AVAILABILITY.md` maps EIA events to release dates, lags GDELT and Fed features one row, and fits imputation, PCA and scaling inside each fold. Leakage regression tests are included.
- **Results:** best is the LSTM at 54.60% direction (p=0.096, not significant). RMSE gain over zero return is not significant (p=0.622). All volatility-scaled strategies are negative. The XGBoost benchmark is in the notebooks and report but not in the README table.
- **Red flags:** small test, and the headline model is an LSTM, not a GBM.
- **Why test:** it is the most careful publication-timing contract found. Reuse its XGBoost benchmark and availability rules.

### 7. NamHyunSeung/Team_project_oil_risk
https://github.com/NamHyunSeung/Team_project_oil_risk

- **Target:** WTI next-day direction via an XGBoost classifier, alongside SARIMAX price, an XGBoost-HAR volatility model, stacking and a Streamlit dashboard.
- **Features:** include the daily Caldara-Iacoviello GPR (`data_gpr_daily_recent.xls`) and sentiment.
- **Results:** hold-out direction accuracy 61.1% versus **walk-forward 53.6%**.
- **Red flags:**
  - The README documents a fixed bug where a forecast was compared against the wrong day's target.
  - The 61% holdout is not credible given the walk-forward figure.
  - No license.
- **Why test:** it is a daily GPR-augmented XGBoost with walk-forward code, and a realistic 53-54% reference.

### 8. buschevapoly-del/wti-prediction-diploma-paper (master's thesis)
https://github.com/buschevapoly-del/wti-prediction-diploma-paper, with companion repo buschevapoly-del/wti-prediction-crude-oil_final_project (`wti_optuna_ensemble.py`)

- **Target:** 5-day WTI direction with a ±0.3% neutral zone. Expanding walk-forward.
- **Results:**
  - model_05, LightGBM + chain-of-thought GPT news sentiment + CNN Fear & Greed: post-2020 accuracy 57.67%, Sharpe 0.53, **215 trades**.
  - Price-only shapelet (SIMPC/JISC-Net) + LightGBM: 55.56% on 288 trades.
  - Pure Fear & Greed contrarian rule: 54.5%.
- **Red flags:**
  - GPT sentiment on 2020-2024 news risks LLM look-ahead from training-data knowledge.
  - 5-day horizon.
  - Selective trading means accuracy is on a subset of days.
  - The neutral-zone label is not comparable to raw up/down.
- **Why test:** drop the GPT features and test the Fear & Greed contrarian and price-shapelet LightGBM on a 1-day label. It is cheap.

### Also worth a quick look

- **danmurray2020/commodities (`crude_oil/`)** — https://github.com/danmurray2020/commodities
  - Saved `production_classifier.joblib`: LightGBM, CL=F, 10-day horizon, purge gap 10. An ensemble of 5-day and 10-day XGBoost/LightGBM is described in `ensemble_metadata.json`.
  - Fold-average accuracies: 5-day XGBoost direction 0.477 and classifier 0.569; LightGBM 0.569 and 0.500; "promoted" 10-day LightGBM 0.708.
  - Red flags: few folds, best-of selection, 10-day horizon, and an LLM "agent" layer.
- **jameelhamdan/event-horizon** — https://github.com/jameelhamdan/event-horizon (GPL-3.0)
  - Pooled LightGBM classifier and regressor at 1-day and 5-day horizons across CL=F, GC=F, SPY, EURUSD and BTC.
  - Features: geolocated news event clusters plus price, as of time t with an automated leak self-check.
  - Walk-forward with Brier score and reliability curves. Live accuracy is logged at `/api/forecasts/accuracy`.
  - No published numbers in the docs.
  - Useful as a live news-event feature pipeline with calibration built in.
- **wasil202/WTI-Systematic-Forecasting-with-Technical-and-News-Based-Signals** — https://github.com/wasil202/WTI-Systematic-Forecasting-with-Technical-and-News-Based-Signals
  - Next-day WTI return with Ridge and XGBoost. Features: technical indicators, FinBERT sentiment, and PCA of FinBERT embeddings fit on train only.
  - Expanding walk-forward, 2014-2026 price-only and 2024-2026 with news.
  - Result: basis-point MAE differences versus zero, i.e. no usable edge. An honest negative template.
- **IshanBanerjee2003/Crude-Oil-Forecasting-and-Trading-Strategy-with-Integrated-Risk-Management** (MIT)
  - RandomForest regressor on lags 1-3 of CL=F, DXY and ^TNX returns. Train 2018-2021, test 2022+.
  - Test R² −0.62, yet the long/short strategy shows +99.5% and Sharpe 0.76.
  - The strategy return is almost certainly regime luck. Cheap baseline only.
- **ayushchinchane99-collab/crude-oil-predictor** (MIT)
  - XGBoost or RF next-day direction, 5-fold rolling walk-forward, cost-aware backtest in Streamlit. No numbers in the README.
- **manikanta-nitw/commodity-pulse-ai**
  - XGBoost 5-day direction plus FinBERT, intermarket macro and volatility targeting, TimeSeriesSplit. No numbers.
- **rithikrachcha/energy-ml-strategies**
  - Ridge, Lasso and XGBoost (depth 3) on 5-day forward WTI log return. Non-overlapping 5-day predictions, annual expanding walk-forward 2019+, a `verify_no_lookahead()` check.
  - The results table is still "X.XX" placeholders. A good leak-audit template only.
- **Lek0007/multifractal-brent-oil-forecasting**
  - RF, XGBoost, LightGBM and CatBoost on next-day Brent return. Features: Brent lag 1, Baltic Dirty Tanker Index lag 1, MF-DFA spectrum width Δα lag 1. CatBoost reported best.
  - MSE only. Red flags: BDTI is not free, and it is unclear whether Δα uses a trailing window.
- **Saumitra2315/Crude-Oil-Forecasting-Engine**
  - XGBoost, LightGBM and CatBoost walk-forward on return, volatility and jump targets with six news "channels" (e.g. OPEC policy, demand/macro).
  - Jump classifier PR-AUC 0.75. Return reported as RMSE only. The jump target might transfer to Polymarket tails.
- **GaiskaSalomon/climate-commodity-alpha-lab**
  - XGBoost and LightGBM walk-forward (500-day train, 21-day test) on 7 commodity ETFs including energy, with 1/5/10/20-day direction and regression targets.
  - Energy: a Gulf-hurricane proxy ranks top-5 by gain. Portfolio net Sharpe 0.82 (per-asset energy figures not in the README).

---

## B. Rejected or red-flag items (for reference; do not test as-is)

| Item | Claim | Why rejected |
|---|---|---|
| MDPI *Mach. Learn. Knowl. Extr.* 7(4):127 (2025), "A Comprehensive Study on Short-Term Oil Price Forecasting Using Econometric and ML Techniques" https://doi.org/10.3390/make7040127 | RF+GB+SVR meta-learner, R² 0.532, **71-80% daily directional accuracy** 2020-2024; top features VIX, OVX, MOVE | R² 0.53 on daily oil means same-day volatility-index changes are almost certainly used (contemporaneous leak). Paper not read in full (403). |
| MDPI *JRFM* 18(7):351 (2025), "Sustainable Factor Augmented ML Models for Crude Oil Return Forecasting" https://doi.org/10.3390/jrfm18070351 | XGBoost 76% direction accuracy | Frequency and split unverified (403); 76% is implausible for daily data. |
| Sadorsky-style tree classifiers (e.g. gold/silver, *JRFM* 14(5):198, 2021) | RF and bagging 75-80% at 5 days, 85-90% at 20 days | Overlapping multi-day labels plus technical indicators and non-purged CV. Classic inflation pattern. |
| hpelnaggar/DEPI-Final-Project---Gold-and-Oil-Predictions | Brent LightGBM directional accuracy 77.3% | Target = `rolling(return,5).mean().shift(-1)` already contains four known returns. Leak. |
| t2wqr27-tech/oil-price-prediction | ARIMA+XGBoost "61.2% direction accuracy" | Hybrid regression on price level; direction derived from levels. |
| bku941025-arch/oil-price-prediction-model | LightGBM 63.8% direction | Montreal retail gasoline, not WTI; retail prices are sticky and autocorrelated. |
| CEEMDAN-XGBoost (Zhou et al., *Complexity* 2019) https://onlinelibrary.wiley.com/doi/10.1155/2019/4392785; LSTM-feature+XGBoost regressor (*Energy* 2024, S0360544224028779) | Low RMSE on WTI price | Price-level targets; decomposition fit on the full series. |
| Roro06s/ml-project | XGB/LGBM/RF, "OOS AUC 0.659" | Walk-forward AUC only 0.559; the OOS figure looks cherry-picked. Pooled WTI + ETH with triple-barrier labels. |
| bwang008/CL_Analyst | Hourly LightGBM on CL, holdout Sharpe 2.5, 70.8% win rate | Intraday, 51 trades, 100-trial Optuna search: heavy selection. |
| RebeccaLustbergPreston/Pristine-RKHS | CL "Sharpe 1.56" | Its own postmortem says the figure was produced by bugs (row-number misalignment and a zero target). A shuffled target scored better. A useful cautionary tale. |
| tanyadiwedi78-boop/commodity-price-forecaster- | LightGBM+SARIMA crude MAPE 3.4% | Directional accuracy 49.8%. Recursive 30-day level forecast. |
| Wei (2026, *PLOS One*) "Forecasting crude oil futures price with energy uncertainty" | RF best at 1 month | Monthly Brent price level. |
| Stevens Hanlon FSC blog (Hinphy) | RF/KNN/LSTM WTI direction | One-year test (2022), no clean numbers. |
| inigo-diez/oil-market-stress-gdelt | XGB/LGBM AUC 0.615 | Target is next-day "high-stress day", not direction. GPR/GDELT signal marginal over OVX lags. Useful as a volatility-side feature study. |
| SelyanChicco/ovx-predictability | RF OVX-spike AUC 0.573 | Not direction. Shows EIA inventories add nothing for next-day OVX. Strong baseline discipline worth copying. |
| divij5267/Cot-Signal-Lab | Crude 1-week-return OOS R² 0.16 (Lasso); XGBoost worse | Weekly target, LSEG DataStream prices. The R² looks high for weekly returns. |

Also scanned with nothing usable: Hugging Face oil Spaces (sonobit, Uday007, ikhwannt/wti_price_prediction, faozi LSTM and others); HF datasets (only price dumps and news snapshots: MaxPrestige/CRUDE_OIL_PRICES, newsdata01/crude-oil-and-petroleum-market-news-dataset); Kaggle "Predicting Oil Price" competition (no solution writeups); Kaggle MITSUI Commodity Prediction Challenge (2025; LME, JPX, US stocks and FX return spreads over 1-4 day lags; LightGBM common, but no crude oil target and no final solutions found).

---

## C. Features for oil direction: free daily history

Timing caution for the Polymarket label (5pm ET to 5pm ET): anything published during the target window cannot be a feature for that day. For example, the EIA report at 10:30 ET Wednesday falls inside the Tuesday 5pm to Wednesday 5pm window; use it only for Thursday's prediction. The scheduled-release flag itself is fine. CME CL settles at 2:30pm ET, not 5pm, so settlement-based features lag the label close.

| Feature | Free source | Frequency and history | Notes |
|---|---|---|---|
| WTI/Brent spot | FRED `DCOILWTICO`, `DCOILBRENTEU` (EIA) | daily from 1986/1987 | Published with a few days' lag. Use for history; live CL=F from the exchange or broker. |
| **Term structure CL1-CL2 (and CL1-CL4)** | EIA API NYMEX futures contracts 1-4 (`RCLC1`-`RCLC4`) | daily from 1983 | Free roll-yield and backwardation signal. benyudolevich/wti and RobinDreh use curve slope. |
| **OVX** | FRED `OVXCLS` (CBOE) | daily from 2007-05 | Top feature in SelinaPhan (OVX high-regime lag). CBOE also posts CSVs. |
| VIX, MOVE | FRED `VIXCLS`; MOVE via Yahoo `^MOVE` | daily | VIX, OVX and MOVE were "most influential" in the MDPI paper, but beware using same-day values. |
| **Crack spreads** | EIA spot/futures: NY Harbor RBOB, ULSD/heating oil, Gulf Coast gasoline | daily | 3-2-1 crack = (2×RBOB + 1×HO) − 3×CL. Also Yahoo RB=F, HO=F. |
| Dollar | FRED `DTWEXBGS` (broad; weekly refresh); Yahoo `DX-Y.NYB` | daily | |
| Rates and inflation expectations | FRED `DGS2`, `DGS10`, `T10YIE`, `T5YIFR`, `T10Y2Y` | daily | 10y-3y change was one of SelinaPhan's 13 features. |
| Equities and energy sector | FRED `SP500`; Yahoo XLE, XOP, OIH | daily | Lagged S&P return is a common feature. |
| **EIA weekly inventories** | EIA WPSR API: `WCESTUS1` (commercial crude), `W_EPC0_SAX_YCUOK_MBBL` (Cushing), gasoline `WGTSTUS1`, distillate `WDISTUS1`, refinery utilization, production, net imports | weekly from 1982 (Cushing from 2004) | Surprise needs a consensus; Reuters/WSJ/Platts surveys are not free historically. Proxy: change minus 4-week mean or seasonal 5-year average (Ddhamani123). Lag one trading day. |
| API weekly inventories | API | weekly | **Not free** (subscription). Only news headlines. Skip. |
| **CFTC COT** (managed money / non-commercial net, open interest) | CFTC Socrata API publicreporting.cftc.gov; disaggregated from 2006, legacy from 1986 | weekly: Tuesday data, released Friday 3:30pm ET | Usable from the next session. Forward-fill with a release-date lag. |
| Rig counts | Baker Hughes (bakerhughesrigcount.gcs-web.com, xlsx history) | weekly, Friday 1pm ET | Slow-moving; low daily value. |
| **Geopolitical risk (GPR)** | Caldara-Iacoviello daily GPR plus acts/threats sub-indices (matteoiacoviello.com/gpr.htm) | daily from 1985 | Used by RobinDreh (acts predictive), NamHyunSeung, inigo-diez, axelcohen75. An oil-specific "GPR_OIL"/AI-GPR daily file appears in benyudolevich/wti (source unverified). Check update lag. |
| **Economic policy uncertainty** | FRED `USEPUINDXD` (daily news EPU); `WLEMUINDXD` (equity market volatility tracker) | daily from 1985 | |
| GDELT tone, Goldstein, event volume | GDELT 2.0 (BigQuery or raw files) | 15-minute updates from 2015 (1.0 from 1979) | Weak daily signal (SelinaPhan, inigo-diez); needs one-row lag. |
| Fear & Greed | CNN Fear & Greed (unofficial endpoint) | daily from about 2011 | buschevapoly: contrarian rule 54.5% at 5 days. History scraping is fragile. |
| Freight | Baltic Dirty Tanker Index is **not free**. Proxies: Yahoo BWET (tanker freight ETF, 2023+), FRO, DHT, STNG, INSW | daily | Lek0007 uses BDTI. |
| Google Trends | pytrends (unofficial) | daily only in windows of 270 days or less; needs stitching; rate-limited | Low priority; revisions and sampling noise. |
| OPEC basket price | opec.org | daily | Mostly redundant with Brent. |
| Calendar and events | EIA Wednesday flag, OPEC+ meeting dates, CL expiry and roll days, holidays | n/a | Cheap and leak-free. |

Takeaway from the repos: price/volatility-regime features (OVX level and regime, realized volatility, momentum) plus term structure usually carry the most weight. Fundamentals and news add little at a one-day horizon. Inventories and GPR help more for volatility or stress targets than for direction.

---

## D. Pretrained tabular foundation models

| Model | Link | License | Limits and notes |
|---|---|---|---|
| TabPFN v2 (Nature 2025) | https://hf.co/Prior-Labs/TabPFN-v2-clf | "other" (Prior Labs license) | Designed for 10k rows or fewer and 500 features or fewer. In-context learning, no gradient training, calibrated probabilities (useful for Polymarket pricing). |
| TabPFN-2.5 | https://hf.co/Prior-Labs/tabpfn_2_5 (arXiv 2511.08667) | "other" (not Apache; check terms before any commercial use) | Larger context (about 50k rows). A TabPFN-3 technical report exists (arXiv 2605.13986), not reviewed. |
| TabPFN-TS | https://github.com/PriorLabs/tabpfn-time-series (arXiv 2501.02945) | open (GitHub) | Univariate forecasting from calendar and time-index features. Built for smooth or seasonal series, not low-SNR return sign. Probably unsuitable except as a sanity baseline. |
| TabICL | https://hf.co/jingang/TabICL-clf (arXiv 2502.05564) | BSD-3-Clause | Scales to much larger context than TabPFN v2. Permissive license. |
| Mitra (Amazon/AutoGluon) | https://hf.co/autogluon/mitra-classifier (arXiv 2510.21204) | Apache-2.0 | Runs inside AutoGluon, which makes rolling-window evaluation easy. |
| TabDPT (Layer6) | https://hf.co/Layer6/TabDPT (arXiv 2410.18164) | Apache-2.0 | Retrieval-based context, handles larger tables. |
| CARTE | soda-inria/carte | open | Built for heterogeneous tables with string entries (graph plus language embeddings). **Not a fit** for numeric daily market features. |

**Using these on time series:**
- They assume i.i.d. rows and have no notion of time. Use the last N days as the in-context training set and predict day t+1. That is a natural rolling walk-forward, and the context limit acts as the window size. Never let context rows overlap the label horizon; with a 1-day label, just exclude day t's own label.
- They are strongest on small, low-dimensional, noisy tables, which describes daily oil data (about 2,500-5,000 rows, 10-50 features). Probability calibration beats raw GBM output, which matters for Polymarket edge-versus-price decisions.
- There is no published evidence of beating a GBM on commodity direction. A related study, arXiv 2606.27100, found gains over a random walk "small and sparse". Note it tested time-series foundation models (TimesFM, Chronos, Moirai) on equities at a 20-day horizon, not TabPFN.
- A FinPFN variant (TabPFN fine-tuned for regime-aware stock returns) is referenced in web results; not verified.
- Only oil use found: skang10/oil-signalyst, a platform using a hosted TabPFN API for regime classification, EIA inventory forecasting and return distributions. No metrics published.

---

## E. Reference numbers for honest daily direction (to calibrate expectations)

| Source | Model | Evaluation | Accuracy / AUC |
|---|---|---|---|
| SelinaPhan0205 | LightGBM, 13 features | yearly walk-forward 2020-2025, 6 folds | 0.547 / 0.549 |
| SelinaPhan0205 | LightGBM | walk-forward 2007-2026, 11 folds | 0.524 / 0.540 |
| trungdangtapcode | LightGBM / XGBoost | holdout 2023-2026 (840 days) | 0.545 / 0.559 |
| ziqianz360 | LightGBM | holdout about 386 days | 0.554 / 0.558 (another run 0.498 / 0.467) |
| Ddhamani123 | XGBoost | validation 2015-2024; test 2025 | 0.534; 0.49-0.50 / about 0.51 |
| NamHyunSeung | XGBoost classifier | walk-forward | 0.536 (holdout 0.611) |
| PeterLP123 | LSTM (XGBoost benchmark in report) | test 2024-07 to 2026-03 (348) | 0.546, p=0.096 |
| RobinDreh (10-day triple barrier) | LightGBM | walk-forward CV / combinatorial purged CV | AUC 0.559 / **0.495** |
| NavnoorBawa (known, 5-day) | XGBoost/LightGBM | purged | 0.48-0.52 |
| occasional-thoughts (known) | XGBoost | walk-forward | balanced accuracy 0.47-0.49 |
| tanyadiwedi78-boop | LightGBM+SARIMA | holdout | 0.498 |

Consensus: about 0.53-0.55 accuracy and 0.54-0.56 AUC is the honest ceiling so far. A realistic "good" target is 0.56 or better on purged walk-forward, ideally concentrated in high-OVX regimes or with a confidence filter.
