"""
Shadow ledger backfill (Phase A helper)
======================================================================
Seeds the shadow_trades table from RECENT history so the dashboard shows where
the benched 9 stand right now, instead of waiting weeks for live paper-trades to
accumulate. For each benched (non-roster) technique it replays the last stretch
of bars STANDALONE (same engine as analyze_edge) and records each paper outcome
(2.5R target / 1R stop) with its real timestamp. The live ShadowTracker then
appends new outcomes on top going forward.

    ./env/Scripts/python.exe shadow_backfill.py [SYMBOL] [M5_COUNT] [TF]
    ./env/Scripts/python.exe shadow_backfill.py XAUUSD 12000 H1
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import sys

from mt5_broker import Mt5Broker
from backtester import resample
from pg_state_store import PgStateStore
from analyze_edge import run_config, TFS
from strategies import DEFAULT_STRATEGIES, ROBUST_TREND_IDS


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    m5_count = int(sys.argv[2]) if len(sys.argv) > 2 else 12000
    tf = sys.argv[3] if len(sys.argv) > 3 else "H1"
    factor = TFS.get(tf, 12)

    benched = [s for s in DEFAULT_STRATEGIES if s.id not in set(ROBUST_TREND_IDS)]
    with Mt5Broker() as b:
        m5 = b.get_bars(symbol, "M5", m5_count)
    if not m5:
        print(f"No M5 data for {symbol}.", file=sys.stderr)
        sys.exit(1)
    bars = resample(m5, factor)
    print(f"Backfilling shadow ledger: {symbol} {tf} "
          f"({bars[0].time:%Y-%m-%d}->{bars[-1].time:%Y-%m-%d}) "
          f"for {len(benched)} benched techniques…")

    with PgStateStore() as store:
        store.shadow_clear(symbol)            # idempotent: reseed cleanly
        total = 0
        for st in benched:
            trades = run_config(symbol, st, bars)
            for t in trades:
                store.shadow_record(st.id, symbol, t.side, t.entry, t.exit,
                                    t.r_mult, t.reason, t.opened_at, t.closed_at)
            total += len(trades)
            print(f"  {st.id:24s} {len(trades):4d} paper-trades")
        print(f"done — {total} paper-trades recorded for {symbol}.")
        # show the resulting standings
        for sid, s in sorted(store.shadow_stats().items(),
                             key=lambda kv: -kv[1]["avgR"]):
            print(f"   {sid:24s} n={s['n']:3d} WR={s['wr']*100:3.0f}% "
                  f"avgR={s['avgR']:+.2f}")


if __name__ == "__main__":
    main()
