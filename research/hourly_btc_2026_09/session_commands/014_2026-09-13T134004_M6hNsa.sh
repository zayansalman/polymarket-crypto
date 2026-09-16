BASE=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test
python3 - <<EOF
from huggingface_hub import snapshot_download
for repo, sub in [("lc2004/kronos_tokenizer_base_BTCUSDT_1h_finetune", "tokenizer"), ("lc2004/kronos_base_model_BTCUSDT_1h_finetune", "model")]:
    p = snapshot_download(repo, local_dir="$BASE/weights/" + sub, allow_patterns=["config.json", "model.safetensors"])
    print("ok", repo, p)
EOF
du -sh "$BASE/weights"/*; cat "$BASE/weights/model/config.json"; echo; cat "$BASE/weights/tokenizer/config.json"
