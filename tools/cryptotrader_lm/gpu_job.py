"""GPU job: tag headline sentiment, render prompts, run CryptoTrader-LM with vLLM.

Runs inside the `vllm/vllm-openai` image on Hugging Face Jobs, not locally. Inputs
(inputs.jsonl, prompts.py) come from a private dataset repo; decisions are
uploaded back to the same repo after every chunk so a stopped job keeps its work.
Base weights are bf16 with the LoRA applied at run time, matching how it was trained.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

BASE_MODEL = "mistralai/Ministral-8B-Instruct-2410"
BASE_REVISION = "2f494a194c5b980dfb9772cb92d26cbb671fce5a"
ADAPTER = "agarkovv/CryptoTrader-LM"
ADAPTER_REVISION = "09a36894351d032b7c83dfbc56b6bf9adcae9db8"
SENTIMENT_MODEL = "ProsusAI/finbert"
BASE_FILES = ["config.json", "generation_config.json", "model-*.safetensors",
              "model.safetensors.index.json", "tokenizer.json", "tokenizer_config.json",
              "special_tokens_map.json"]
WORK = Path("/tmp/ctlm")
os.environ.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--sentiment-io", nargs=2, metavar=("TEXTS_JSON", "LABELS_JSON"),
                   help="internal: tag texts with FinBERT on the GPU and exit")
    p.add_argument("--repo")
    p.add_argument("--run")
    p.add_argument("--variants", default="finmem,teacher")
    p.add_argument("--wrappers", default="card")
    p.add_argument("--limit", type=int, default=0, help="evenly spaced sample of rows; 0 = all")
    p.add_argument("--finmem-news", type=int, default=5)
    p.add_argument("--teacher-news", type=int, default=10)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--teacher-max-tokens", type=int, default=0, help="0 = same as --max-tokens")
    p.add_argument("--max-model-len", type=int, default=8192)
    p.add_argument("--gpu-mem", type=float, default=0.90)
    p.add_argument("--base-check", type=int, default=0, help="rows also run WITHOUT the adapter")
    p.add_argument("--chunk", type=int, default=256)
    return p.parse_args()


def _sample(rows: list[dict], limit: int) -> list[dict]:
    if not limit or limit >= len(rows):
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


def _item_text(item: dict) -> str:
    """Title immediately followed by its summary, as in the challenge's news data."""
    return item["title"] + item["summary"]


def _sentiment_worker(texts_path: str, labels_path: str) -> None:
    from transformers import pipeline

    texts = json.loads(Path(texts_path).read_text())
    clf = pipeline("text-classification", model=SENTIMENT_MODEL, device=0, truncation=True)
    labels = {t: r["label"].lower() for t, r in zip(texts, clf(texts, batch_size=128))}
    Path(labels_path).write_text(json.dumps(labels))


def _tag_sentiment(texts: list[str]) -> dict[str, str]:
    """FinBERT in a child process, so its GPU memory is fully released before vLLM starts."""
    texts_path, labels_path = WORK / "texts.json", WORK / "labels.json"
    texts_path.write_text(json.dumps(sorted(set(texts))))
    subprocess.run([sys.executable, __file__, "--sentiment-io", str(texts_path), str(labels_path)],
                   check=True)
    return json.loads(labels_path.read_text())


def main() -> None:
    args = _args()
    if args.sentiment_io:
        _sentiment_worker(*args.sentiment_io)
        return
    from huggingface_hub import HfApi, hf_hub_download, snapshot_download

    api = HfApi()
    WORK.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location(
        "prompts", hf_hub_download(args.repo, "code/prompts.py", repo_type="dataset"))
    prompts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prompts)
    inputs_path = hf_hub_download(args.repo, "inputs.jsonl", repo_type="dataset")
    rows = [json.loads(line) for line in Path(inputs_path).read_text().splitlines()]
    rows = _sample([r for r in rows if r["price"] is not None], args.limit)

    t0 = time.time()
    used = max(args.finmem_news, args.teacher_news)
    sentiment = _tag_sentiment([_item_text(n) for r in rows for n in r["news"][:used]])
    t_sentiment = time.time() - t0

    base_dir = snapshot_download(BASE_MODEL, revision=BASE_REVISION, allow_patterns=BASE_FILES)
    adapter_dir = WORK / "adapter"
    snapshot_download(ADAPTER, revision=ADAPTER_REVISION, local_dir=adapter_dir,
                      allow_patterns=["adapter_config.json", "adapter_model.safetensors"])
    cfg = json.loads((adapter_dir / "adapter_config.json").read_text())
    cfg.pop("model_type", None)
    (adapter_dir / "adapter_config.json").write_text(json.dumps(cfg))

    import torch
    import transformers
    import vllm
    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest

    tokenizer = AutoTokenizer.from_pretrained(base_dir)
    llm = LLM(model=base_dir, tokenizer=base_dir, dtype="bfloat16", enable_lora=True,
              max_lora_rank=8, max_loras=1, max_model_len=args.max_model_len,
              gpu_memory_utilization=args.gpu_mem, seed=0)
    lora = LoRARequest("cryptotrader-lm", 1, str(adapter_dir))
    max_tokens = {"finmem": args.max_tokens,
                  "teacher": args.teacher_max_tokens or args.max_tokens}

    def render(row: dict, variant: str) -> str:
        k = args.finmem_news if variant == "finmem" else args.teacher_news
        news = [(_item_text(n), sentiment[_item_text(n)]) for n in row["news"][:k]]
        if variant == "finmem":
            return prompts.finmem_prompt(row["asset"], row["decision_date_et"], news,
                                         row["momentum_3d"])
        return prompts.teacher_prompt(row["asset"], news, row["price"], row["daily_returns_30d"])

    def run(name: str, subset: list[dict], variant: str, wrapper: str, use_lora: bool) -> None:
        out_path = WORK / f"{name}.jsonl"
        out_path.write_text("")
        sampling = SamplingParams(temperature=0.0, max_tokens=max_tokens[variant], seed=0)
        for i in range(0, len(subset), args.chunk):
            chunk = subset[i:i + args.chunk]
            texts = [prompts.wrap(render(r, variant), wrapper) for r in chunk]
            ids = [tokenizer(t, add_special_tokens=True)["input_ids"] for t in texts]
            fits = [len(x) + max_tokens[variant] <= args.max_model_len for x in ids]
            outs = llm.generate([{"prompt_token_ids": x} for x, ok in zip(ids, fits) if ok],
                                sampling, lora_request=lora if use_lora else None)
            outs_iter = iter(outs)
            with out_path.open("a") as fh:
                for row, x, ok in zip(chunk, ids, fits):
                    rec = {"asset": row["asset"], "market_date": row["market_date"],
                           "variant": variant, "wrapper": wrapper, "adapter": use_lora,
                           "prompt_tokens": len(x),
                           "sentiments": [sentiment[_item_text(n)] for n in row["news"][:used]]}
                    if not ok:
                        rec.update(decision=None, parse="prompt_too_long", text="",
                                   output_tokens=0, finish_reason=None)
                    else:
                        o = next(outs_iter).outputs[0]
                        decision, method = prompts.parse_decision(o.text)
                        rec.update(decision=decision, parse=method, text=o.text,
                                   output_tokens=len(o.token_ids), finish_reason=o.finish_reason)
                    fh.write(json.dumps(rec) + "\n")
            api.upload_file(path_or_fileobj=str(out_path), repo_id=args.repo, repo_type="dataset",
                            path_in_repo=f"runs/{args.run}/{name}.jsonl")
            print(f"{name}: {min(i + args.chunk, len(subset))}/{len(subset)}", flush=True)

    timings = {"sentiment_s": round(t_sentiment, 1)}
    for variant in args.variants.split(","):
        for wrapper in args.wrappers.split(","):
            t = time.time()
            run(f"{variant}_{wrapper}", rows, variant, wrapper, use_lora=True)
            timings[f"{variant}_{wrapper}_s"] = round(time.time() - t, 1)
    if args.base_check:
        variant, wrapper = args.variants.split(",")[0], args.wrappers.split(",")[0]
        run(f"{variant}_{wrapper}_no_adapter", rows[:args.base_check], variant, wrapper,
            use_lora=False)

    meta = {"args": vars(args), "rows": len(rows), "timings": timings,
            "base": [BASE_MODEL, BASE_REVISION], "adapter": [ADAPTER, ADAPTER_REVISION],
            "sentiment_model": SENTIMENT_MODEL, "gpu": torch.cuda.get_device_name(),
            "vllm": vllm.__version__, "transformers": transformers.__version__,
            "python": platform.python_version(), "job_id": os.environ.get("JOB_ID")}
    api.upload_file(path_or_fileobj=json.dumps(meta, indent=2).encode(), repo_id=args.repo,
                    repo_type="dataset", path_in_repo=f"runs/{args.run}/meta.json")
    print(json.dumps(meta), flush=True)


if __name__ == "__main__":
    main()
