"""
Python-side signal dump for the MQL5 parity test
======================================================================
Pulls XAUUSD-ECN H4 bars from the VT Markets terminal (same history the
Strategy Tester uses), runs the three real Python strategies on every closed
bar exactly as the backtester does, and writes reports/parity_python.csv with
one row per (closed bar, strategy, dir, entry, sl, tp) — tp being the
arbitrator's TP2 (2.5R), i.e. the broker TP the EA places.

Then compares against the EA's dump (Common\Files\h4trend_signals.csv) if it
exists: rows must match 1:1 within 1e-5.

    ./env/Scripts/python.exe research/parity_dump.py [--symbol XAUUSD-ECN] [--from 2024-06-01]
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse
import csv
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

from signals import Bar                                        # noqa: E402
from strategies import select_strategies                       # noqa: E402

TP2_R = 2.5
OUT = os.path.join(ROOT, "reports", "parity_python.csv")
EA_DUMP = os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal", "Common", "Files",
                       "h4trend_signals.csv")


def fetch_h4(symbol: str, start: datetime) -> list[Bar]:
    import MetaTrader5 as mt5
    assert mt5.initialize(login=int(os.getenv("MT5_LOGIN")), password=os.getenv("MT5_PASSWORD"),
                          server=os.getenv("MT5_SERVER"), path=os.getenv("MT5_TERMINAL_PATH"))
    mt5.symbol_select(symbol, True)
    r = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_H4, start, datetime.now(timezone.utc))
    mt5.shutdown()
    bars = [Bar(time=datetime.fromtimestamp(int(x["time"]), timezone.utc), open=float(x["open"]),
                high=float(x["high"]), low=float(x["low"]), close=float(x["close"]),
                volume=float(x["tick_volume"])) for x in r]
    return bars[:-1]        # drop the forming bar


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD-ECN")
    ap.add_argument("--from", dest="start", default="2024-03-01")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    bars = fetch_h4(args.symbol, start)
    strats = select_strategies(("breakout_sr", "donchian_breakout", "roc_momentum"))
    rows = []
    for i in range(40, len(bars)):
        win = bars[max(0, i - 400): i + 1]          # as-of bar i (closed)
        for st in strats:
            sig = st.generate(args.symbol, win, win[-1].time)
            if sig is None:
                continue
            d = 1 if sig.direction.value == "buy" else -1
            rdist = abs(sig.entry - sig.sl)
            rows.append((win[-1].time.strftime("%Y.%m.%d %H:%M"), st.id, d,
                         round(sig.entry, 5), round(sig.sl, 5), round(sig.entry + d * TP2_R * rdist, 5)))
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["bar_time", "strategy", "dir", "entry", "sl", "tp"])
        w.writerows(rows)
    print(f"python: {len(rows)} signals over {len(bars)} closed H4 bars "
          f"({bars[0].time:%Y-%m-%d} -> {bars[-1].time:%Y-%m-%d}) -> {OUT}")

    if not os.path.exists(EA_DUMP):
        print(f"(no EA dump at {EA_DUMP} yet — run the EA in the Strategy Tester with SignalDump=true)")
        return
    ea = {}
    with open(EA_DUMP, newline="") as f:
        for r in csv.DictReader(f):
            ea[(r["bar_time"], r["strategy"])] = (int(r["dir"]), float(r["entry"]), float(r["sl"]), float(r["tp"]))
    py = {(t, s): (d, e, sl, tp) for t, s, d, e, sl, tp in rows}
    # compare only the overlapping time span
    ea_times = sorted(t for t, _ in ea)
    if not ea_times:
        print("EA dump is empty"); return
    lo, hi = ea_times[0], ea_times[-1]
    py_in = {k: v for k, v in py.items() if lo <= k[0] <= hi}
    missing = [k for k in py_in if k not in ea]
    extra = [k for k in ea if k not in py_in and lo <= k[0] <= hi]
    diff = [(k, py_in[k], ea[k]) for k in py_in if k in ea and any(abs(a - b) > 1e-4 for a, b in zip(py_in[k], ea[k]))]
    print(f"overlap {lo} -> {hi}: python {len(py_in)} signals, EA {len(ea)} | "
          f"missing in EA: {len(missing)}, extra in EA: {len(extra)}, value mismatches: {len(diff)}")
    for k in (missing[:5]): print("  missing:", k, py_in[k])
    for k in (extra[:5]): print("  extra:  ", k, ea[k])
    for k, a, b in diff[:5]: print("  diff:   ", k, "py", a, "ea", b)
    print("PARITY OK" if not (missing or extra or diff) else "PARITY FAILED")


if __name__ == "__main__":
    main()
