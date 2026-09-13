"""Find your MetaMask Polymarket wallet and write it into .env (#34).

A MetaMask-connected Polymarket account trades from a Gnosis Safe that your
MetaMask key owns (signature type 2) — NOT from the MetaMask address itself.
This derives that Safe address from the key, checks its on-chain collateral
(pUSD) balance on Polygon, and writes POLYMARKET_FUNDER +
POLYMARKET_SIGNATURE_TYPE=2 into .env.

Prereq: put your MetaMask private key in .env as POLYMARKET_PRIVATE_KEY
(MetaMask: Account details -> Show private key). The key is read locally and
never printed. Then:

    python3 tools/live_detect_wallet.py
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from dotenv import load_dotenv  # noqa: E402

ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(ENV_PATH)

# Keys this script manages in .env.
_LIVE_KEYS = (
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_FUNDER",
    "POLYMARKET_SIGNATURE_TYPE",
    "TRADE_MAX_USD",
    "PAPER_MIN_TRADE_USD",
    "PAPER_MAX_TRADE_USD",
)


def _merge_env(text: str, updates: dict[str, str]) -> str:
    """Update existing KEY= lines in place; append any new keys. Comments kept."""
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key in _LIVE_KEYS:
        if key in updates and key not in seen:
            out.append(f"{key}={updates[key]}")
    return "\n".join(out).rstrip("\n") + "\n"


def _write_0600(path: Path, text: str) -> None:
    """Write a secret file created 0600 from the start (no world-readable window)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # enforce 0600 even if pre-existing


def _write_env_secure(updates: dict[str, str]) -> None:
    """Write updates into .env (backing up first). Both files are 0600 — a
    backup of a key file is itself a secret and must not be world-readable."""
    existing = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    if existing:
        _write_0600(PROJECT_ROOT / ".env.bak", existing)
    _write_0600(ENV_PATH, _merge_env(existing, updates))



# Public Polygon RPCs (no key), tried in order — endpoints rotate auth/limits.
POLYGON_RPCS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
)
COLLATERAL_TOKEN = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD, Polygon
DECIMALS = 6


def _balance_of(address: str) -> float:
    """On-chain collateral balance of an address (human units).

    Tries each public RPC until one answers; raises if they all fail so a
    transient RPC outage is never mistaken for a zero balance.
    """
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": COLLATERAL_TOKEN, "data": data}, "latest"],
    }
    last_err: Exception | None = None
    for rpc in POLYGON_RPCS:
        try:
            r = httpx.post(rpc, json=payload, timeout=15)
            r.raise_for_status()
            result = r.json().get("result")
            if result is None:
                raise ValueError(r.json())
            return int(result, 16) / (10**DECIMALS)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"all Polygon RPCs failed: {last_err}")


def main() -> int:
    key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    if not key:
        print("Set POLYMARKET_PRIVATE_KEY in .env first "
              "(MetaMask: Account details -> Show private key).")
        return 1
    if not key.startswith("0x"):
        key = "0x" + key

    from eth_account import Account
    from polymarket._internal.environment import PRODUCTION_CONFIG
    from polymarket._internal.wallet import derive_safe_wallet_address

    signer = Account.from_key(key).address
    funder = derive_safe_wallet_address(signer, PRODUCTION_CONFIG.wallet_derivation)

    print(f"MetaMask address (public): {signer}")
    print(f"Polymarket wallet (public): {funder}")
    try:
        bal = _balance_of(funder)
    except RuntimeError as e:
        print(f"\nCould not read balance: {e}\nRe-run in a moment; .env unchanged.")
        return 1
    print(f"  balance = ${bal:.2f}")
    if bal <= 0:
        print("\nThis Polymarket wallet holds no USDC. Deposit on polymarket.com, or check "
              "this is the MetaMask account you use there. .env unchanged.")
        return 1

    _write_env_secure({
        "POLYMARKET_PRIVATE_KEY": key,
        "POLYMARKET_FUNDER": funder,
        "POLYMARKET_SIGNATURE_TYPE": "2",
        "TRADE_MAX_USD": "5",
        "PAPER_MIN_TRADE_USD": "5",
        "PAPER_MAX_TRADE_USD": "5",
    })
    print("\n.env updated (funder + signature type written; key untouched; 0600).")
    print("\nNEXT:")
    print("  1. Verify: python3 tools/live_preflight.py")
    print("  2. Restart the dashboard, click LIVE, then press Start.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
