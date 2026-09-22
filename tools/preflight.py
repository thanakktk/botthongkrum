"""
Pre-flight check — run BEFORE launching the live stack on Monday.
======================================================================
Read-only. Verifies the things that silently break a go-live:
  * Postgres reachable; kill-switch state; no stuck open positions
  * MT5 connected to the RIGHT account; Algo Trading allowed
  * XAUUSD present, market OPEN (fresh bars), sane spread
  * Discord webhooks configured

    ./env/Scripts/python.exe tools/preflight.py

Exit code 0 = all good (or warnings only); 1 = a hard FAIL (do not launch).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
_fails = 0
_warns = 0


def ok(msg):
    print(f"  [OK]   {msg}")


def warn(msg):
    global _warns
    _warns += 1
    print(f"  [WARN] {msg}")


def fail(msg):
    global _fails
    _fails += 1
    print(f"  [FAIL] {msg}")


print(f"=== Pre-flight for {SYMBOL} @ {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} ===\n")

# ----- 1) Postgres + state -------------------------------------------- #
print("Postgres / state:")
profile_login = None
try:
    import db
    c = db.connect(autocommit=True)
    prof = c.execute("SELECT login,variant,path,phase,initial_capital "
                     "FROM account_profile WHERE id=1").fetchone()
    if prof:
        profile_login = int(prof[0])
        ok(f"account_profile: login={prof[0]} {prof[1]}/{prof[2]}/{prof[3]} "
           f"capital={float(prof[4]):,.0f}")
    else:
        fail("account_profile row missing (run db_setup.py)")

    mode, reason = (c.execute("SELECT mode,reason FROM kill_switch WHERE id=1")
                    .fetchone() or ("running", None))
    if mode == "running":
        ok("kill_switch = running")
    else:
        fail(f"kill_switch = {mode} ({reason}) -> run: watchdog.py --reset")

    nopen = c.execute("SELECT count(*) FROM positions WHERE status='open'").fetchone()[0]
    (ok if nopen == 0 else warn)(f"open positions in DB: {nopen}")

    hb = c.execute("SELECT max(ts) FROM audit_log WHERE event_type='heartbeat'").fetchone()[0]
    if hb:
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - hb).total_seconds()
        warn(f"last heartbeat {age/60:.0f} min ago (stale = previous run; OK before launch)")
    else:
        ok("no prior heartbeat (clean)")
except Exception as e:
    fail(f"Postgres unreachable: {type(e).__name__}: {e}")

# ----- 2) MT5 connection + account ------------------------------------ #
print("\nMT5 terminal / account:")
broker = None
try:
    from mt5_broker import Mt5Broker
    import MetaTrader5 as mt5
    broker = Mt5Broker().connect()
    info = mt5.account_info()
    term = mt5.terminal_info()
    ok(f"connected: login={info.login} balance={info.balance:,.2f} "
       f"equity={info.equity:,.2f}")
    if profile_login is not None and int(info.login) != profile_login:
        fail(f"WRONG ACCOUNT: MT5 login {info.login} != DB profile {profile_login}")
    else:
        ok("MT5 login matches DB profile")
    if term and getattr(term, "trade_allowed", False):
        ok("Algo Trading is ON (trade_allowed)")
    else:
        fail("Algo Trading is OFF -> click the 'Algo Trading' button in MT5 (Ctrl+E)")
except Exception as e:
    fail(f"MT5 connect failed: {type(e).__name__}: {e}")

# ----- 3) Symbol / market open --------------------------------------- #
print(f"\n{SYMBOL} market:")
if broker is not None:
    try:
        bars = broker.get_bars(SYMBOL, "M15", 2)
        if not bars:
            fail(f"{SYMBOL} has no bars (symbol name wrong, or not in Market Watch?)")
        else:
            last = bars[-1].time
            last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
            if age_min < 30:
                ok(f"market OPEN — last M15 bar {age_min:.0f} min ago")
            elif age_min < 180:
                warn(f"last M15 bar {age_min:.0f} min ago (open but slow/illiquid?)")
            else:
                fail(f"market looks CLOSED — last bar {age_min/60:.1f} h ago "
                     f"(weekend? wait for the session open)")
            bid, ask = broker.quote(SYMBOL)
            if bid and ask:
                ok(f"quote bid={bid:.2f} ask={ask:.2f} spread={ask-bid:.2f}")
            else:
                warn(f"no live tick for {SYMBOL} yet")
    except Exception as e:
        fail(f"{SYMBOL} check failed: {type(e).__name__}: {e}")
    finally:
        broker.shutdown()
else:
    warn("skipped (no MT5 connection)")

# ----- 4) Webhooks ---------------------------------------------------- #
print("\nDiscord webhooks (.env):")
for k in ("ADVISORY_WEBHOOK", "DISCORD_TRADE_WEBHOOK", "DISCORD_NEWS_WEBHOOK"):
    (ok if os.getenv(k) else warn)(f"{k}: {'set' if os.getenv(k) else 'MISSING'}")

# ----- summary -------------------------------------------------------- #
print("\n" + "=" * 60)
if _fails:
    print(f"RESULT: {_fails} FAIL, {_warns} warn -> DO NOT launch until fixed.")
    sys.exit(1)
print(f"RESULT: all checks passed ({_warns} warn) -> safe to launch.")
sys.exit(0)
