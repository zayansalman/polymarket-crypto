# Kronos model code (vendored, unchanged)

- Source: https://github.com/shiyu-coder/Kronos, folder `model/`, commit 67b630e67f6a18c9e9be918d9b4337c960db1e9a (2026-04-13), MIT licence (LICENSE in this folder).
- Paper: Shi et al., "Kronos: A Foundation Model for the Language of Financial Markets", arXiv 2508.02739 (Tsinghua University).
- Why this and not github.com/shiyu-coder/Kronos-demo: the demo repo (whose forecast recipe Tsinghua-Kronos BTC 24h follows) has no licence file, so its code is not vendored. The recipe is reproduced with this MIT code: `KronosPredictor.predict_batch` over N identical copies with `sample_count=1` returns N independent sampled paths, the same as the demo's per-path `predict(sample_count=N)`. Checked against the Kronos team's published forecasts with `tools/check_tsinghua_kronos_btc_24h_against_published.py` (Claude, 2026-09-16).
- Used only by `polymarket_bot/kronos_forecast/worker.py`, which runs in its own process. The app process never imports it.
- Do not edit these files. To update, re-vendor a new commit into a new folder and update `KRONOS_CODE_COMMIT` / `CODE_DIR` in `polymarket_bot/kronos_forecast/client.py`.
