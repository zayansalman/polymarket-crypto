# Weather forecasting models — survey notes (parked 2026-09-19)

Backlog issue: #253

Desk research only. **No code, no data, no backtest.** Nothing here has been measured against
any market. Parked at this point so it is recoverable later.

Scope of the search: what open weather models exist on Hugging Face, and what the recent
literature says about using them for forecasts at a *specific location* (a station), which is the
only form a temperature/precipitation market would settle on.

## Open models on Hugging Face

Global forecast models, trained on reanalysis (ERA5 / MERRA-2 / HRES analysis):

| Model | Repo | Notes |
| --- | --- | --- |
| ECMWF AIFS | `ecmwf/aifs-single-2.0`, `ecmwf/aifs-ens-2.0` | ENS variant gives a real ensemble; `anemoi` runtime |
| Prithvi WxC (IBM/NASA) | `ibm-nasa-geospatial/Prithvi-WxC-1.0-2300M` | 2.3B, MERRA-2, `terratorch`; most-used of the set |
| Aurora (Microsoft) | `microsoft/aurora` | weather + air quality + waves + cyclone tracks |
| FourCastNet 3 (NVIDIA) | `nvidia/fourcastnet3` | also `nvidia/fourcastnet1` |
| ACE2 (AI2) | `allenai/ACE2-ERA5` | built for long climate rollouts, not day-ahead skill |
| GraphCast (DeepMind) | `shermansiu/dm_graphcast` | third-party copy, **CC-BY-NC-SA — non-commercial** |
| WeatherNext 2 (DeepMind) | `kashif/weathernext2` (+ `-mini`, `-cyclones`) | third-party port, CC-BY-4.0 |
| ClimaX, FuXi | `microsoft/ClimaX`, `tpys/fuxi-2.1` | |

Regional / higher-resolution: `nvidia/stormcast-v1-era5-hrrr` (US storm scale),
`nvidia/corrdiff-cmip6-era5` and `nvidia/corrdiff-cosmo-era5` (downscaling).

Everything else returned by a "weather" search on the Hub is either a chatbot fine-tune or a
photo classifier — not trained on weather data.

## What the literature says (alphaXiv)

**RealBench — arXiv 2605.24945** (code: github.com/lixruize-del/NWP-Benchmark). Benchmarks AIFS,
Aurora, GraphCast, Pangu, FuXi, FengWu, Stormer, NeuralGCM under *operational* conditions
(2025 out-of-sample year, operational analysis as initial state, verified against 11,539 GHCNh
stations) instead of the usual ERA5-vs-ERA5 setup.
- FengWu and FuXi strongest overall. T2M WRMSE at day 10: FengWu 2.40 K (operational init).
- **Station verification is far worse than gridded.** Day-1 T2M WRMSE goes from ~0.7–0.9 K on the
  grid to 2.28–2.67 K at stations. Budget roughly 1.5–2× published ERA5 numbers.
- Extremes are systematically flattened: negative Tmax bias on heatwaves, positive Tmin bias on
  cold snaps, both growing with lead time. Heatwave CSI falls below ~0.17 for most models by day 10.
- Models fine-tuned on IFS/operational data do better from live initial conditions (Aurora day-5
  cyclone track error 427.6 km → 338.1 km with IFS init).
- Inference cost (whole benchmark): FuXi 19.5 h / 5.8 GB, FengWu 20.8 h / 17.5 GB,
  NeuralGCM 92.9 h / 73.8 GB.

**Station-based evaluation in Northern Norway — arXiv 2609.10564**
(code: anonymous.4open.science/r/mlwp-evaluation-28AF). **10 m wind speed only — no temperature.**
FourCastNet3 2.96 m/s RMSE, GraphCast 2.94, ECMWF HRES 2.89 against 12 stations, 2016–2022; only
the FCN3–HRES gap was significant. All three under-forecast (−0.85 to −1.26 m/s). High-wind cases
(≥10.8 m/s) roughly double the error and under-forecast by 37–41%; regression slopes as low as
0.24 at some stations. Bilinear interpolation from a 0.25° grid to a point is called out as a known
weak spot; a 2.5 km regional reanalysis fit the stations much better (2.41 vs 2.71 m/s RMSE).

**WeatherNext 3 — arXiv 2609.03582** (DeepMind, Sept 2026; weights not on the Hub). Directionally the
most relevant design: hourly initialisation from geostationary satellite data, 0.1° output, and a
*station head* that predicts 2 m temperature and dewpoint at arbitrary coordinates. It cuts 2 m
temperature CRPS ~30% vs WeatherNext 2 and ~40% vs ECMWF ENS at short lead times, generalises to
held-out stations, and needs no lapse-rate correction. Takeaway: the win comes from training on
station observations directly, not from post-processing a gridded forecast.

**AdaWeather — arXiv 2606.02663** (no code released). Online mixing of several probabilistic models
(FCN3, FGN, GenCast, IFS-ENS, GEM) with per-pixel weights from a pre-trained U-Net plus an online
exponential-weights update on cumulative CRPS. India 2024–25, 2 m temperature: CRPS 0.503 K vs
0.583 K for the best single model and 0.699 K for equal weighting — about 14% better than the best
member. Update rule: `dμ_{t+1}/dμ_t(p) = exp(−η · CRPS(F_t^(p), y_t))`, `η = 2/(b−a)`, no tuning;
1000 Monte-Carlo mixture samples in production. Cheap to run on top of forecasts you already have.

**HRRR error correction with LSTMs — arXiv 2512.14898**
(code: github.com/shmaronshmevans/inference_ai2es_forecast_err). One LSTM per station per variable
predicts HRRR's error at hours 1–18, trained on NY and Oklahoma mesonets 2018–2023.
**Temperature was the one variable where it did not beat raw HRRR in New York** (74% of points within
±2 °C); wind and precipitation improved. Worth knowing as a negative result: naive per-station error
correction is not free skill.

**Not yet read** (abstracts only): conformal calibration of AI ensembles (arXiv 2606.19642) — claims
raw GenCast/NeuralGCM/AIFS-ENS coverage is off, especially on extremes, and fixes it with an online
conformal update at no CRPS cost. This is the piece that matters most for turning an ensemble into a
probability, and it is the one gap in these notes.

## If this is picked up again

1. The question to answer first is data, not models: what is the settlement source, and can its own
   history be pulled point-in-time? Every paper above says station truth ≠ gridded forecast.
2. Prefer a model that outputs an ensemble (`ecmwf/aifs-ens-2.0`) over a single deterministic run —
   a market needs a probability, not a point forecast.
3. Assume raw ensemble probabilities are miscalibrated, and that extremes are flattened. Read
   arXiv 2606.19642 before trusting any threshold probability.
4. Check licences before anything real: the GraphCast copy is non-commercial.
5. Compute: local box is 8 GB RAM — none of the global models run here. This is an HF-compute job.
