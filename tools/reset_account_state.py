"""
Reset account-tied state (for an account switch or a clean restart)
======================================================================
Wipes the per-account trading state so a NEW (or freshly-reset) FTMO
account starts clean, and clears a tripped kill-switch:

    DELETE  positions, orders, day_baseline
    RESET   kill_switch -> 'running'
    KEEP    strategy_league + shadow_*  (cross-account strategy learning)

`day_baseline` MUST be wiped on an account switch: today's row keeps the
OLD account's midnight balance (source='live' is never overwritten), so
the compliance engine would anchor the new account's daily floor to the
wrong number. The loop re-anchors from the live balance on next startup.

SAFETY: dry-run by default. Pass --yes to actually execute.

Run:
  ./env/Scripts/python.exe tools/reset_account_state.py            # dry run
  ./env/Scripts/python.exe tools/reset_account_state.py --yes      # execute (keep learning)
  ./env/Scripts/python.exe tools/reset_account_state.py --yes --wipe-learning
"""

from __future__ import annotations

import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

import db

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(str(ROOT / ".env"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset account-tied DB state.")
    ap.add_argument("--yes", action="store_true",
                    help="actually execute (otherwise dry-run preview only)")
    ap.add_argument("--wipe-learning", action="store_true",
                    help="ALSO clear strategy_league + shadow_* (full clean slate)")
    args = ap.parse_args()

    with db.connect() as c:
        n_pos = c.execute("select count(*) from positions").fetchone()[0]
        n_ord = c.execute("select count(*) from orders").fetchone()[0]
        n_base = c.execute("select count(*) from day_baseline").fetchone()[0]
        ks = c.execute("select mode from kill_switch where id=1").fetchone()
        login = c.execute("select login from account_profile where id=1").fetchone()

        print(f"account_profile login = {login[0] if login else '(none)'}")
        print(f"would delete: positions={n_pos}, orders={n_ord}, day_baseline={n_base}")
        print(f"kill_switch: {ks[0] if ks else '(none)'} -> running")
        print(f"strategy_league/shadow: "
              f"{'WIPED' if args.wipe_learning else 'KEPT'}")

        if not args.yes:
            print("\n[dry-run] nothing changed. Re-run with --yes to execute.")
            return 0

        # positions FK-references orders -> delete positions first
        c.execute("DELETE FROM positions")
        c.execute("DELETE FROM orders")
        c.execute("DELETE FROM day_baseline")
        c.execute(
            "UPDATE kill_switch SET mode='running', reason=NULL, source='manual', "
            "tripped_at=NULL, updated_at=now() WHERE id=1"
        )
        if args.wipe_learning:
            c.execute("DELETE FROM shadow_positions")
            c.execute("DELETE FROM shadow_trades")
            c.execute("DELETE FROM strategy_league")
        c.execute(
            "INSERT INTO audit_log(event_type, decision, reason_code, payload) "
            "VALUES ('account_switch','reset','reset_account_state', "
            "jsonb_build_object('wipe_learning', %s::boolean))",
            (args.wipe_learning,),
        )
        c.commit()

    print("\n[done] state reset. kill_switch=running. "
          "Next: confirm Algo Trading is ON, then relaunch the bots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
