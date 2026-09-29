"""Live preflight: check the wallet config end to end before clicking LIVE. Places no orders.

Runs the same wallet checks the live venue makes (``ems.execution.clob.wallet_problems``),
signs in to the exchange the same way (``build_client``), and reports the USDC balance and
allowance the exchange sees for the funder. It never prints the private key or the API
credentials, and it sends no order.

    pip install -e ".[live]"
    python tools/live_preflight.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ems import config as _config  # noqa: E402
from ems.execution import clob  # noqa: E402


def main() -> int:
    print("=== wallet config ===")
    problems = clob.wallet_problems()
    if problems:
        for problem in problems:
            print(f"NO-GO: {problem}")
        return 1
    print(f"OK (signature type {_config.POLYMARKET_SIGNATURE_TYPE}, chain {_config.POLYMARKET_CHAIN_ID})")

    print("\n=== exchange sign-in ===")
    try:
        client = clob.build_client()
    except clob.LiveUnavailable as exc:
        print(f"NO-GO: {exc}")
        return 1
    print("signed in; API credentials derived")

    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                    signature_type=_config.POLYMARKET_SIGNATURE_TYPE)
    client.update_balance_allowance(params)
    reply = client.get_balance_allowance(params) or {}
    balance = float(reply.get("balance") or 0) / 1e6  # USDC has 6 decimals
    print(f"\nUSDC balance the exchange sees: ${balance:.2f}")
    if balance <= 0:
        print("NO-GO: deposit USDC into your Polymarket account, then run this again.")
        return 1
    print("GO: the wallet passes. In the dashboard: check the live Risk knobs in SETTINGS, "
          "turn Kelly horse-race's switch on, and click LIVE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
