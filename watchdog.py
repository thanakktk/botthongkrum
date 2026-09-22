"""
Watchdog / Kill-Switch (Pillar 5)  — runs as a SEPARATE process
======================================================================
The last-resort safety net. It does NOT trust the main loop: it keeps its own
broker connection and its own Compliance Engine, recomputes the floors from the
persisted baseline, and trips the shared kill switch when either:

  (a) the main loop goes SILENT (no heartbeat for > max_silence) — a hung or
      dead loop can't manage open positions, so we flatten and halt; or
  (b) equity independently breaches a floor (soft -> halt_new, hard -> flatten).

Modes (escalate only; never auto-downgrade — a human must reset):
  running -> halt_new -> close_all_halt

The kill_switch row is the shared signal: the watchdog WRITES it (and, for
close_all, flattens directly via its own broker so it works even if the main
loop is frozen); the main loop READS it every tick and obeys.

Run (live, separate terminal):  ./env/Scripts/python.exe watchdog.py
Reset after a trip:             ./env/Scripts/python.exe watchdog.py --reset
Self-test (paper, no MT5):      ./env/Scripts/python.exe watchdog.py --selftest
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from ftmo_compliance_engine import (
    FtmoComplianceEngine, AccountProfile, AccountSnapshot, Action,
    Variant, Path, Phase, FTMO_TZ,
    config_from_env, apply_env_loss_limits, rules_summary,
)

load_dotenv()

_SEVERITY = {"running": 0, "halt_new": 1, "close_all_halt": 2}


@dataclass
class WatchdogConfig:
    max_silence_secs: float = 90.0     # ~3x a 30s loop -> presumed hung
    startup_grace_secs: float = 120.0  # ignore "no heartbeat yet" right after boot
    interval: float = 10.0             # watchdog ticks faster than the main loop


@dataclass
class WatchdogDecision:
    mode: str
    reason: str
    equity: float
    heartbeat_age: float | None
    acted: bool


class Watchdog:
    def __init__(self, broker, store, cfg: WatchdogConfig | None = None,
                 boot_time: datetime | None = None):
        self.broker = broker
        self.store = store
        self.cfg = cfg or WatchdogConfig()
        self.boot_time = boot_time or datetime.now(timezone.utc)

    # ----- build an INDEPENDENT engine from persisted state ---------------- #
    def _engine(self, now: datetime) -> FtmoComplianceEngine:
        prof = self.store.account_profile()
        if prof is None:
            raise RuntimeError("no account_profile in DB; cannot police floors")
        profile = apply_env_loss_limits(AccountProfile(
            variant=Variant(prof["variant"]), path=Path(prof["path"]),
            phase=Phase(prof["phase"]), initial_capital=prof["initial_capital"],
        ))
        # Same rulebook as the main loop (RULES_MODE in .env) — otherwise the
        # watchdog would police FTMO floors on a non-FTMO account.
        engine = FtmoComplianceEngine(profile, config_from_env())
        cet_today = engine.cet_date(now)
        today_mb, highest_mb = self.store.risk_anchors(cet_today)
        # If today's anchor is missing, fall back to the current balance — a
        # tighter (safer) floor, never a looser one.
        if today_mb is None:
            today_mb = self.broker.balance()
        engine.roll_daily_baseline(now, today_mb)
        if highest_mb is not None:
            engine.highest_midnight_balance = max(engine.highest_midnight_balance,
                                                  highest_mb)
        engine.mark_reconciled()
        return engine

    # ----- one evaluation -------------------------------------------------- #
    def evaluate_once(self, now: datetime | None = None) -> WatchdogDecision:
        now = now or datetime.now(timezone.utc)
        engine = self._engine(now)
        snap = AccountSnapshot(self.broker.balance(), self.broker.equity(),
                               self.broker.open_risk_to_sl())
        verdict = engine.evaluate(snap, now)

        age = self.store.last_heartbeat_age_secs(now)
        boot_age = (now - self.boot_time).total_seconds()
        silent = (age is not None and age > self.cfg.max_silence_secs) or \
                 (age is None and boot_age > self.cfg.startup_grace_secs)

        # desired action from the independent checks
        if verdict.action == Action.FLATTEN_ALL:
            desired, reason = "close_all_halt", verdict.reason.value
        elif silent:
            desired, reason = "close_all_halt", \
                (f"heartbeat_silence:{age:.0f}s" if age is not None
                 else "no_heartbeat_since_boot")
        elif verdict.action == Action.HALT_NEW:
            desired, reason = "halt_new", verdict.reason.value
        else:
            desired, reason = "running", "ok"

        current, _ = self.store.kill_switch()
        # escalate only
        new_mode = desired if _SEVERITY[desired] > _SEVERITY[current] else current
        acted = False

        if new_mode == "close_all_halt":
            # Re-assert flatten while anything is open, even if already tripped —
            # the main loop may be frozen and unable to do it.
            open_now = list(self.broker.open_positions())
            if open_now or _SEVERITY[current] < _SEVERITY["close_all_halt"]:
                closed = self.broker.flatten_all()
                for t in closed:
                    self.store.mark_position_closed(t)
                self.store.set_kill_switch("close_all_halt", reason, "watchdog")
                self.store.write_audit("kill", "close_all_halt", reason,
                                       {"closed": closed, "equity": snap.equity,
                                        "heartbeat_age": age})
                acted = True
        elif new_mode == "halt_new" and _SEVERITY[current] < _SEVERITY["halt_new"]:
            self.store.set_kill_switch("halt_new", reason, "watchdog")
            self.store.write_audit("kill", "halt_new", reason,
                                   {"equity": snap.equity, "heartbeat_age": age})
            acted = True

        return WatchdogDecision(new_mode, reason, snap.equity, age, acted)

    # ----- loop ------------------------------------------------------------ #
    def run(self) -> None:
        print(f"[watchdog] up. max_silence={self.cfg.max_silence_secs}s "
              f"interval={self.cfg.interval}s")
        while True:
            try:
                d = self.evaluate_once()
                cet = datetime.now(FTMO_TZ)
                hb = f"{d.heartbeat_age:.0f}s" if d.heartbeat_age is not None else "—"
                flag = " <ACTED>" if d.acted else ""
                print(f"[{cet:%H:%M:%S %Z}] mode={d.mode} reason={d.reason} "
                      f"eq={d.equity:,.2f} hb_age={hb}{flag}")
                if d.mode == "close_all_halt":
                    print("  [watchdog] account is in close_all_halt — "
                          "manual --reset required to resume.")
            except Exception as e:
                print(f"[watchdog] ERROR: {e}")
            time.sleep(self.cfg.interval)


# --------------------------------------------------------------------------- #
def _reset(store) -> None:
    store.set_kill_switch("running", reason="manual_reset", source="manual")
    store.write_audit("kill", "reset", "manual_reset", {})
    print("[watchdog] kill switch reset to 'running'.")


def _selftest() -> None:
    from paper_broker import PaperBroker
    from pg_state_store import PgStateStore

    now0 = datetime.now(timezone.utc)
    broker = PaperBroker(initial_balance=100_000.0)
    broker.set_price("XAUUSD", 2000.0)

    with PgStateStore() as store:
        today = now0.astimezone(FTMO_TZ).date()
        store.roll_baseline(today, 100_000.0, "live")
        store.set_kill_switch("running", "selftest_init", "manual")
        store.write_audit("heartbeat", "normal", "ok", {"equity": 100_000.0})

        wd = Watchdog(broker, store, WatchdogConfig(max_silence_secs=60),
                      boot_time=now0)

        # (1) healthy
        d = wd.evaluate_once(now0)
        print(f"(1) healthy        -> mode={d.mode} acted={d.acted}")
        assert d.mode == "running" and not d.acted

        # (2) equity breaches hard floor -> flatten + close_all_halt
        broker.place_market(symbol="XAUUSD", side="buy", volume=1.0,
                            sl=1950.0, tp=2100.0, client_order_id="wd-1")
        broker.set_price("XAUUSD", 1949.0)              # equity ~94,900 < 95,500
        d = wd.evaluate_once(now0)
        print(f"(2) hard breach    -> mode={d.mode} acted={d.acted} "
              f"eq={d.equity:,.2f} open={len(broker.open_positions())}")
        assert d.mode == "close_all_halt" and d.acted
        assert not broker.open_positions()
        assert store.kill_switch()[0] == "close_all_halt"

        # (3) SILENCE alone trips it. Use a FRESH broker (healthy 100k equity, no
        #     positions) so equity is NOT a trigger — only the stale heartbeat is.
        #     Evaluate far in the future so the last heartbeat looks long-silent.
        store.set_kill_switch("running", "reset", "manual")
        healthy = PaperBroker(initial_balance=100_000.0)
        healthy.set_price("XAUUSD", 2000.0)
        wd2 = Watchdog(healthy, store, WatchdogConfig(max_silence_secs=60),
                       boot_time=now0)
        future = now0 + timedelta(seconds=10_000)
        d = wd2.evaluate_once(future)
        print(f"(3) silence        -> mode={d.mode} reason={d.reason} acted={d.acted}")
        assert d.mode == "close_all_halt" and "silence" in d.reason

        # cleanup
        store.set_kill_switch("running", "selftest_done", "manual")
        print("\nWatchdog self-test OK (kill switch reset to running).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="reset kill switch to running")
    ap.add_argument("--selftest", action="store_true", help="run paper self-test")
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--max-silence", type=float, default=90.0)
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return 0

    from pg_state_store import PgStateStore
    from mt5_broker import Mt5Broker

    if args.reset:
        with PgStateStore() as store:
            _reset(store)
        return 0

    with Mt5Broker() as broker, PgStateStore() as store:
        cfg = WatchdogConfig(interval=args.interval, max_silence_secs=args.max_silence)
        Watchdog(broker, store, cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
