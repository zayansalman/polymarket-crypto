"""Download the pinned Kronos-mini and Kronos-Tokenizer-2k weights (Tsinghua-Kronos BTC 24h).

Sources: huggingface.co/NeoQuasar/Kronos-mini and huggingface.co/NeoQuasar/Kronos-Tokenizer-2k
(MIT), at the revisions pinned in polymarket_bot/kronos_forecast/client.py. Only config.json
and model.safetensors are fetched; safetensors files never run code when loaded.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import snapshot_download  # noqa: E402

from polymarket_bot.kronos_forecast import client as kc  # noqa: E402


def main() -> int:
    spec = kc.KRONOS_MINI_WITH_TOKENIZER_2K
    kc.models_dir().mkdir(parents=True, exist_ok=True)
    for repo, revision in ((spec.model_repo, spec.model_revision),
                           (spec.tokenizer_repo, spec.tokenizer_revision)):
        path = snapshot_download(repo_id=repo, revision=revision, cache_dir=str(kc.models_dir()),
                                 allow_patterns=["config.json", "model.safetensors"])
        print(f"{repo}@{revision} -> {path}")
    missing = kc.missing_weights(spec)
    print("Kronos weights ready." if missing is None else missing)
    return 0 if missing is None else 1


if __name__ == "__main__":
    sys.exit(main())
