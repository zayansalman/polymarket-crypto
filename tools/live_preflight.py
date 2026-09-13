"""Live-launch preflight: verify the .env wallet config end to end (issue #32).

Read-only against the account: runs the boot gate, builds the CLOB client,
derives API credentials, checks reachability, and reports the funder's
balance/allowance as the CLOB sees it. Places NO orders.

Usage:
    .venv/bin/python tools/live_preflight.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config as _config  # noqa: E402  (loads .env)
from polymarket_exec.execution.live import (  # noqa: E402
    LiveBootRefused,
    assert_live_boot_allowed,
)


def main() -> int:
    print("=== boot gate ===")
    try:
        assert_live_boot_allowed()
    except LiveBootRefused as e:
        print(f"REFUSED: {e}")
        return 1
    print("gate OK "
          f"(signature_type={_config.POLYMARKET_SIGNATURE_TYPE}, "
          f"funder={_config.POLYMARKET_FUNDER})")

    from eth_account import Account

    eoa = Account.from_key(_config.POLYMARKET_PRIVATE_KEY).address
    print(f"signer EOA: {eoa}")

    from py_clob_client_v2 import (
        AssetType,
        BalanceAllowanceParams,
        ClobClient,
    )

    print("\n=== CLOB ===")
    client = ClobClient(
        _config.POLYMARKET_CLOB_API,
        chain_id=_config.POLYMARKET_CHAIN_ID,
        key=_config.POLYMARKET_PRIVATE_KEY,
        signature_type=_config.POLYMARKET_SIGNATURE_TYPE,
        funder=_config.POLYMARKET_FUNDER or None,
    )
    print("reachability:", client.get_ok())
    creds = client.create_or_derive_api_key()
    if creds is None:
        print("FAIL: could not derive API credentials")
        return 1
    client.set_api_creds(creds)
    print("API credentials derived OK")

    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=_config.POLYMARKET_SIGNATURE_TYPE,
    )
    client.update_balance_allowance(params)
    bal = client.get_balance_allowance(params)
    print(f"funder balance/allowance: {bal}")

    balance = float((bal or {}).get("balance", 0)) / 1e6  # USDC 6dp
    print(f"\nusable balance: ${balance:.2f}")
    if balance <= 0:
        print("NO-GO: deposit USDC into your Polymarket account (MetaMask), then re-run.")
        return 1
    print("GO: config verified — launch with the dashboard Start button.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
