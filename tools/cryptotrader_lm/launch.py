"""Upload inputs, launch the CryptoTrader-LM GPU job on Hugging Face Jobs, fetch results.

    python3 -m tools.cryptotrader_lm.launch upload
    python3 -m tools.cryptotrader_lm.launch run --run smoke --limit 12 --wrappers card,tutorial,system --base-check 6
    python3 -m tools.cryptotrader_lm.launch fetch --run smoke

Needs a local `hf auth login` and Hugging Face credits. Jobs bill per minute of GPU time.
"""
from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from huggingface_hub import HfApi, get_token, run_job, snapshot_download

from tools.cryptotrader_lm.collect import DEFAULT_OUT

IMAGE = "vllm/vllm-openai:v0.29.0"
REPO_NAME = "cryptotrader-lm-backtest"
CODE_DIR = Path(__file__).parent


def _repo(api: HfApi) -> str:
    return f"{api.whoami()['name']}/{REPO_NAME}"


def cmd_upload(api: HfApi, args: argparse.Namespace) -> None:
    repo = _repo(api)
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    inputs = args.inputs or args.out / "inputs.jsonl"
    api.upload_file(path_or_fileobj=str(inputs), path_in_repo="inputs.jsonl",
                    repo_id=repo, repo_type="dataset")
    for name in ("prompts.py", "gpu_job.py"):
        api.upload_file(path_or_fileobj=str(CODE_DIR / name), path_in_repo=f"code/{name}",
                        repo_id=repo, repo_type="dataset")
    print(f"uploaded to private dataset {repo}")


def cmd_run(api: HfApi, args: argparse.Namespace) -> None:
    repo = _repo(api)
    job_args = ["--repo", repo, "--run", args.run, "--variants", args.variants,
                "--wrappers", args.wrappers, "--limit", str(args.limit),
                "--max-tokens", str(args.max_tokens), "--base-check", str(args.base_check),
                "--teacher-max-tokens", str(args.teacher_max_tokens),
                "--max-model-len", str(args.max_model_len), "--gpu-mem", str(args.gpu_mem)]
    fetch = ("from huggingface_hub import hf_hub_download as d; import shutil; "
             f"shutil.copy(d({repo!r}, 'code/gpu_job.py', repo_type='dataset'), '/tmp/gpu_job.py')")
    script = f"python3 -c {shlex.quote(fetch)} && python3 /tmp/gpu_job.py {shlex.join(job_args)}"
    job = run_job(image=IMAGE, command=["bash", "-c", script], flavor=args.flavor,
                  timeout=args.timeout, secrets={"HF_TOKEN": get_token()})
    print(f"job {job.id} ({args.flavor}) -> {job.url}")


def cmd_fetch(api: HfApi, args: argparse.Namespace) -> None:
    local = snapshot_download(_repo(api), repo_type="dataset", allow_patterns=[f"runs/{args.run}/*"],
                              local_dir=args.out)
    print(f"results in {Path(local) / 'runs' / args.run}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=["upload", "run", "fetch"])
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--inputs", type=Path, default=None, help="upload: inputs file (default out/inputs.jsonl)")
    p.add_argument("--run", default="full")
    p.add_argument("--variants", default="finmem,teacher")
    p.add_argument("--wrappers", default="card")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--teacher-max-tokens", type=int, default=0)
    p.add_argument("--base-check", type=int, default=0)
    p.add_argument("--max-model-len", type=int, default=8192)
    p.add_argument("--gpu-mem", type=float, default=0.90)
    p.add_argument("--flavor", default="l40sx1")
    p.add_argument("--timeout", default="3h")
    args = p.parse_args()
    api = HfApi()
    {"upload": cmd_upload, "run": cmd_run, "fetch": cmd_fetch}[args.step](api, args)


if __name__ == "__main__":
    main()
