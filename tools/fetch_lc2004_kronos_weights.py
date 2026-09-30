"""Download the pinned lc2004 Kronos BTCUSDT 1h model and tokenizer weights (HF cache).

Sources: huggingface.co/lc2004/kronos_base_model_BTCUSDT_1h_finetune and
huggingface.co/lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune, at the revisions pinned in
ems/kronos_forecast/client.py (LC2004_BTCUSDT_1H), about 425 MB in all. Only config.json and
model.safetensors are fetched; safetensors files never run code when loaded. They land in the
standard Hugging Face cache (HF_HUB_CACHE, else $HF_HOME/hub, else ~/.cache/huggingface/hub),
where the lc2004-Kronos BTC 24h forecast worker reads them offline. Needs huggingface_hub
(pyproject extra "kronos"). One-time operator setup (Claude, 2026-09-22; ems imports,
Claude, 2026-09-29).

Usage::

    python3 tools/fetch_lc2004_kronos_weights.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import snapshot_download  # noqa: E402

from ems.kronos_forecast import client as kc  # noqa: E402


def main() -> int:
    spec = kc.LC2004_BTCUSDT_1H
    # The same folder the forecast client reads, so the two can never disagree.
    cache = kc.hf_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    for repo, revision in ((spec.model_repo, spec.model_revision),
                           (spec.tokenizer_repo, spec.tokenizer_revision)):
        path = snapshot_download(repo_id=repo, revision=revision, cache_dir=str(cache),
                                 allow_patterns=["config.json", "model.safetensors"])
        print(f"{repo}@{revision} -> {path}")
    missing = kc.missing_weights(spec)
    print("lc2004 Kronos weights ready." if missing is None else missing)
    return 0 if missing is None else 1


if __name__ == "__main__":
    sys.exit(main())
