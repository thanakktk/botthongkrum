"""
DB / account health snapshot
======================================================================
Read-only. Prints the live state the operator needs before/after a
restart or an account switch: active account profile, kill-switch mode,
open positions, non-terminal orders, recent daily baselines, and the
kept "learning" tables (strategy_league / shadow).

Run:  ./env/Scripts/python.exe tools/db_state.py
"""

from __future__ import annotations

import sys
import pathlib

# allow importing project-root modules when run from this subfolder
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

import db

load_dotenv(str(pathlib.Path(__file__).resolve().parent.parent / ".env"))


def main() -> int:
    with db.connect() as c:
        prof = c.execute(
            "select login, variant, path, phase, initial_capital, updated_at "
            "from account_profile where id=1"
        ).fetchone()
        ks = c.execute(
            "select mode, reason, source, updated_at from kill_switch where id=1"
        ).fetchone()
        open_pos = c.execute(
            "select broker_ticket, symbol, side, volume, opened_at "
            "from positions where status='open' order by opened_at"
        ).fetchall()
        live_orders = c.execute(
            "select status, count(*) from orders "
            "where status in ('created','submitted','unknown') group by status"
        ).fetchall()
        baselines = c.execute(
            "select cet_date, midnight_balance, source "
            "from day_baseline order by cet_date desc limit 5"
        ).fetchall()
        league = c.execute("select count(*) from strategy_league").fetchone()[0]
        shadow = c.execute("select count(*) from shadow_positions").fetchone()[0]

    print("=== account_profile ===")
    if prof:
        print(f"  login={prof[0]}  {prof[1]}/{prof[2]}/{prof[3]}  "
              f"cap={float(prof[4]):,.0f}  updated={prof[5]}")
    else:
        print("  (none seeded — run db_setup.py)")

    print("\n=== kill_switch ===")
    flag = "  <-- TRIPPED, reset before trading" if ks and ks[0] != "running" else ""
    print(f"  mode={ks[0]}  reason={ks[1]}  source={ks[2]}  ({ks[3]}){flag}")

    print(f"\n=== open positions: {len(open_pos)} ===")
    for p in open_pos:
        print(f"  #{p[0]} {p[1]} {p[2]} {p[3]} opened={p[4]}")

    print("\n=== non-terminal orders ===")
    print(f"  {dict(live_orders) if live_orders else 'none'}")

    print("\n=== day_baseline (last 5) ===")
    for b in baselines:
        print(f"  {b[0]}  {float(b[1]):,.2f}  ({b[2]})")
    if not baselines:
        print("  (empty — will re-anchor on next loop startup)")

    print(f"\n=== learning kept ===  strategy_league={league}  shadow_positions={shadow}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
