"""
Edge Validator (Phase 2 — robustness gate)
======================================================================
analyze_edge.py ranks strategies on ONE history window — that is in-sample, and
the winners are partly survivors of THAT window. Before we prune for real, we
split the data: train on the first 70% (IS), test on the last 30% (OOS). An edge
we can trust stays positive in BOTH halves. Anything positive only in-sample is
curve-fit and must be treated as noise.

    ./env/Scripts/python.exe validate_edge.py [SYMBOL] [M5_COUNT] [IS_FRAC]
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import sys
from collections import defaultdict

from mt5_broker import Mt5Broker
from backtester import resample, Trade
from arbitration import FAMILY
from strategies import DEFAULT_STRATEGIES
from analyze_edge import run_config, agg, TFS, MIN_N


def _split(bars, frac):
    k = int(len(bars) * frac)
    return bars[:k], bars[k:]


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    m5_count = int(sys.argv[2]) if len(sys.argv) > 2 else 40000
    is_frac = float(sys.argv[3]) if len(sys.argv) > 3 else 0.70

    with Mt5Broker() as b:
        m5 = b.get_bars(symbol, "M5", m5_count)
    if not m5:
        print(f"No M5 data for {symbol}.", file=sys.stderr)
        sys.exit(1)

    cut = int(len(m5) * is_frac)
    print(f"=== EDGE VALIDATION (IS/OOS): {symbol} ===")
    print(f"M5 bars {len(m5):,}  IS={m5[0].time:%Y-%m-%d}->{m5[cut].time:%Y-%m-%d}"
          f"  OOS={m5[cut].time:%Y-%m-%d}->{m5[-1].time:%Y-%m-%d}\n")

    is_by_strat: dict[str, list[Trade]] = defaultdict(list)
    oos_by_strat: dict[str, list[Trade]] = defaultdict(list)
    for st in DEFAULT_STRATEGIES:
        for tf, factor in TFS.items():
            bars = resample(m5, factor)
            is_bars, oos_bars = _split(bars, is_frac)
            if len(is_bars) < 60 or len(oos_bars) < 60:
                continue
            is_by_strat[st.id].extend(run_config(symbol, st, is_bars))
            oos_by_strat[st.id].extend(run_config(symbol, st, oos_bars))

    print("--- PER STRATEGY: in-sample vs out-of-sample (pooled M30/H1/H4) ---")
    print(f"{'strategy':22s}{'IS_n':>6s}{'IS_avgR':>9s}{'OOS_n':>7s}"
          f"{'OOS_avgR':>10s}   robust?")
    verdicts = {}
    for sid in sorted(is_by_strat, key=lambda s: -(agg(is_by_strat[s]) or {"avgR": -9})["avgR"]):
        ai = agg(is_by_strat[sid])
        ao = agg(oos_by_strat.get(sid, []))
        if not ai or not ao:
            continue
        # ROBUST: positive expectancy in BOTH halves with a usable OOS sample
        robust = ai["avgR"] > 0 and ao["avgR"] > 0 and ao["n"] >= MIN_N
        tag = "ROBUST" if robust else (
            "fragile(OOS<=0)" if ai["avgR"] > 0 and ao["avgR"] <= 0 else
            "thin-OOS" if ao["n"] < MIN_N else "weak")
        verdicts[sid] = robust
        print(f"{sid[:21]:22s}{ai['n']:6d}{ai['avgR']:9.2f}{ao['n']:7d}"
              f"{ao['avgR']:10.2f}   {tag}")

    survivors = [s for s, ok in verdicts.items() if ok]
    print(f"\n=== ROBUST SURVIVORS ({len(survivors)}): "
          f"{', '.join(survivors) or '— none held up out-of-sample'} ===")
    print("(positive expectancy in BOTH the train and the unseen test window)")


if __name__ == "__main__":
    main()
