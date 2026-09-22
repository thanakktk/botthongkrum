"""
LIVE order plumbing test (open -> confirm -> close immediately)
======================================================================
Validates the ONE path never exercised live: Mt5Broker.place_market /
close_position, the DB order/position lifecycle, and the audit trail. It
opens the SMALLEST lot, confirms the fill + DB rows, then closes it RIGHT
AWAY — nothing is held (so no weekend-hold concern on the Standard account).

Safety:
  * aborts unless Algo Trading is ON and the symbol has a live quote;
  * uses a deliberately tiny lot (default 0.01) with a protective SL/TP;
  * a finally-block force-flattens anything this test opened;
  * uses a SWING compliance profile FOR THIS TEST ONLY, so the (correct, for a
    real Standard account) weekend veto doesn't block a deliberate plumbing run.
    This does NOT change the real account profile in the DB.

Run (after enabling Algo Trading):
  ./env/Scripts/python.exe live_order_test.py            # BTCUSD 0.01
  ./env/Scripts/python.exe live_order_test.py --symbol ETHUSD --lot 0.01
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
import MetaTrader5 as mt5

# Windows consoles default to a legacy codepage (e.g. cp874 on Thai locale) that
# can't encode some characters; force UTF-8 so prints never crash.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from ftmo_compliance_engine import (
    FtmoComplianceEngine, AccountProfile, AccountSnapshot,
    Variant, Path, Phase,
)
from state_reconciliation import Reconciler
from mt5_broker import Mt5Broker
from pg_state_store import PgStateStore
from execution import ExecutionEngine, OrderRequest

load_dotenv()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSD")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--sl-pct", type=float, default=2.0, help="protective SL distance %")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    with Mt5Broker() as broker, PgStateStore() as store:
        # --- preflight safety checks ------------------------------------- #
        term = mt5.terminal_info()
        print(f"Algo Trading (trade_allowed): {term.trade_allowed}")
        if not term.trade_allowed:
            print("[ABORT] Algo Trading is OFF in the terminal. Enable it "
                  "(toolbar 'Algo Trading' / Ctrl+E) and re-run.")
            return 2

        if not mt5.symbol_select(args.symbol, True):
            print(f"[ABORT] cannot select {args.symbol}")
            return 2
        info = mt5.symbol_info(args.symbol)
        tick = mt5.symbol_info_tick(args.symbol)
        if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            print(f"[ABORT] {args.symbol} trade_mode={info.trade_mode} (not FULL)")
            return 2
        if not tick or tick.ask <= 0 or tick.bid <= 0:
            print(f"[ABORT] {args.symbol} has no live quote (market closed?). "
                  f"tick={tick}")
            return 2
        lot = max(info.volume_min, args.lot)
        entry = tick.ask
        sl = round(entry * (1 - args.sl_pct / 100), int(info.digits))
        tp = round(entry * (1 + args.sl_pct / 100), int(info.digits))
        risk = broker.estimate_risk(args.symbol, "buy", lot, entry, sl)
        print(f"Symbol {args.symbol}: bid={tick.bid} ask={tick.ask} "
              f"digits={info.digits}")
        print(f"Plan: BUY {lot} @ ~{entry}  SL {sl}  TP {tp}  "
              f"worst-case risk ~ ${risk:,.2f}")

        if not args.yes:
            print("\n[DRY] re-run with --yes to actually send the order.")
            return 0

        # --- independent reconcile so the engine has a real baseline ----- #
        # SWING profile FOR THIS TEST so the weekend gate doesn't veto.
        engine = FtmoComplianceEngine(AccountProfile(
            Variant.SWING, Path.TWO_STEP, Phase.CHALLENGE,
            float(os.getenv("ACCOUNT_INITIAL_CAPITAL", "100000"))))
        Reconciler(broker, store, engine).run(now)
        ex = ExecutionEngine(broker, store, engine)

        snap = AccountSnapshot(broker.balance(), broker.equity(),
                               broker.open_risk_to_sl())
        req = OrderRequest("live-test", args.symbol, "buy", lot, sl, tp, risk)

        ticket = None
        try:
            print("\n>>> submitting...")
            res = ex.submit(req, snap, now)
            if not res.submitted:
                print(f"[FAIL] not submitted: {res.verdict.reason.value} / {res.detail}")
                return 1
            ticket = res.ticket
            print(f"[OK] filled ticket #{ticket} coid={res.client_order_id}")

            orow = store.conn.execute(
                "SELECT status, broker_ticket FROM orders WHERE client_order_id=%s",
                (res.client_order_id,)).fetchone()
            prow = store.conn.execute(
                "SELECT status, symbol, volume FROM positions WHERE broker_ticket=%s",
                (ticket,)).fetchone()
            print(f"     DB order={orow}  position={prow}")

            print(">>> closing immediately...")
            pnl = ex.close(ticket, reason="live_test_close")
            print(f"[OK] closed; realized pnl ~ {pnl:,.2f}")
            prow2 = store.conn.execute(
                "SELECT status FROM positions WHERE broker_ticket=%s",
                (ticket,)).fetchone()
            print(f"     DB position now={prow2}")
            ticket = None
            print("\n[DONE] live order path verified end-to-end.")
            return 0
        finally:
            # safety net: never leave this test's position open
            if ticket is not None:
                print(f"[CLEANUP] force-closing leftover #{ticket}")
                try:
                    broker.close_position(ticket)
                    store.mark_position_closed(ticket)
                except Exception as e:
                    print(f"[CLEANUP-ERROR] {e} — CHECK THE TERMINAL MANUALLY")
            # tidy this test's DB rows (incl. any dangling 'submitted' order)
            store.conn.execute(
                "DELETE FROM positions WHERE client_order_id IN "
                "(SELECT client_order_id FROM orders WHERE strategy_id='live-test')")
            store.conn.execute("DELETE FROM orders WHERE strategy_id='live-test'")


if __name__ == "__main__":
    raise SystemExit(main())
