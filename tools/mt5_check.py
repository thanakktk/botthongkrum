"""
MT5 connection smoke test
======================================================================
Loads credentials from .env, connects to the FTMO terminal, and prints
account state + open positions. Read-only: it never sends an order.

Run:  ./env/Scripts/python.exe mt5_check.py
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import os
import sys

from dotenv import load_dotenv
import MetaTrader5 as mt5

load_dotenv()


def _env(key: str, required: bool = True) -> str | None:
    val = os.getenv(key)
    if required and not val:
        sys.exit(f"[fatal] missing {key} in .env")
    return val


def main() -> int:
    login = int(_env("MT5_LOGIN"))
    password = _env("MT5_PASSWORD")
    server = _env("MT5_SERVER")
    path = _env("MT5_TERMINAL_PATH", required=False)

    init_kwargs = {"login": login, "password": password, "server": server}
    if path:
        init_kwargs["path"] = path

    print(f"Connecting to {server} as {login} ...")
    if not mt5.initialize(**init_kwargs):
        code, msg = mt5.last_error()
        print(f"[fatal] initialize() failed: ({code}) {msg}")
        return 1

    try:
        term = mt5.terminal_info()
        acct = mt5.account_info()
        if acct is None:
            code, msg = mt5.last_error()
            print(f"[fatal] account_info() returned None: ({code}) {msg}")
            return 1

        print("\n--- Terminal ---")
        print(f"  build      : {getattr(term, 'build', '?')}")
        print(f"  connected  : {getattr(term, 'connected', '?')}")
        print(f"  trade_allowed (algo): {getattr(term, 'trade_allowed', '?')}")

        print("\n--- Account ---")
        print(f"  login      : {acct.login}")
        print(f"  name       : {acct.name}")
        print(f"  server     : {acct.server}")
        print(f"  company    : {acct.company}")
        print(f"  currency   : {acct.currency}")
        print(f"  leverage   : 1:{acct.leverage}")
        print(f"  balance    : {acct.balance:,.2f}")
        print(f"  equity     : {acct.equity:,.2f}")
        print(f"  margin_free: {acct.margin_free:,.2f}")
        print(f"  trade_mode : {acct.trade_mode}  (0=demo, 1=contest, 2=real)")

        positions = mt5.positions_get()
        positions = positions or ()
        print(f"\n--- Open positions: {len(positions)} ---")
        for p in positions:
            side = "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell"
            print(f"  #{p.ticket} {p.symbol} {side} {p.volume} @ {p.price_open} "
                  f"sl={p.sl} tp={p.tp} pnl={p.profit:,.2f}")

        print("\n[ok] MT5 connection verified (read-only, no orders sent).")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
