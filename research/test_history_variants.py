"""
11-year variant sweep — re-validate the management/filter ideas on REAL history
======================================================================
After the trend filter turned out to be recent-window overfit, every tweak gets
judged on the full 2015-2026 sample, not a few months. This sweeps the user's
three experiments through the robust-4 + confluence pipeline, per year, and
reports the POOLED expectancy + how many years each variant was positive — the
honest test of whether an idea generalises.

  A (give it room)  : bank the partial + go break-even LATER -> sweep tp1_r 1.5/2.0
  B (bank early)    : bank the partial at 0.8R -> tp1_r 0.8
  C (strict trend)  : only trade when Efficiency Ratio >= min_er (0.3/0.4/0.5)

    ./env/Scripts/python.exe test_history_variants.py [START] [END]
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import sys
from statistics import mean

from histdata import load_m15, M15_TF_FACTOR
from backtester import Backtester, BacktestConfig
from arbitration import Arbitrator, ArbitrationConfig
from strategies import select_strategies, ROBUST_TREND_IDS
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase

TFW = {"M30": 1.0, "H1": 1.6, "H4": 2.4}
COST = dict(spread=0.44, slippage=0.05, commission_per_lot=0.0)
GATES = dict(min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0)
MB = dict(manage=True, partial_pct=0.5, trail_r=1.0)   # shared management base

# (label, BacktestConfig mgmt kwargs, min_er)
VARIANTS = [
    ("raw (no mgmt)",            dict(manage=False),                        0.0),
    ("tp1@1.0 + eBE@0.8 (live)", dict(**MB, tp1_r=1.0, be_trigger_r=0.8),   0.0),
    ("tp1@1.0 (no early-BE)",    dict(**MB, tp1_r=1.0, be_trigger_r=0.0),   0.0),
    ("B: tp1@0.8 bank-early",    dict(**MB, tp1_r=0.8, be_trigger_r=0.0),   0.0),
    ("A: tp1@1.5 give-room",     dict(**MB, tp1_r=1.5, be_trigger_r=0.0),   0.0),
    ("A: tp1@2.0 more-room",     dict(**MB, tp1_r=2.0, be_trigger_r=0.0),   0.0),
    ("C: min_er 0.3",            dict(**MB, tp1_r=1.0, be_trigger_r=0.0),   0.3),
    ("C: min_er 0.4",            dict(**MB, tp1_r=1.0, be_trigger_r=0.0),   0.4),
    ("C: min_er 0.5",            dict(**MB, tp1_r=1.0, be_trigger_r=0.0),   0.5),
]


def run_variant(m15, years, mgmt, min_er):
    tp1_r = mgmt.get("tp1_r", 1.0)
    pooled, pos, nyr, rets = [], 0, 0, []
    for y in years:
        bars = [b for b in m15 if b.time.year == y]
        if len(bars) < 2000:
            continue
        arb = Arbitrator(ArbitrationConfig(
            timeframes=("M30", "H1", "H4"), tf_weights=TFW, risk_pct=0.003,
            tp1_r=tp1_r, tp2_r=2.5, min_er=min_er, **GATES))
        bt = Backtester(
            AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
            select_strategies(ROBUST_TREND_IDS), arb,
            BacktestConfig(risk_pct=0.003, tf_factor=M15_TF_FACTOR, **COST, **mgmt),
            specs={"XAUUSD": 100.0})
        res = bt.run("XAUUSD", bars)
        pooled.extend(x.r_mult for x in res.trades)
        rets.append(res.return_pct)
        nyr += 1
        if res.return_pct > 0:
            pos += 1
    return pooled, pos, nyr, rets


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 2015
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    m15 = load_m15(start_year=start, end_year=end)
    years = list(range(start, end + 1))
    print(f"=== 11-YEAR VARIANT SWEEP: XAUUSD {start}-{end} ({len(m15):,} M15) ===")
    print("robust-4 + confluence | per-year, pooled | risk 0.3%")
    print(f"{'variant':28s}{'trades':>7s}{'WR%':>6s}{'avgR':>8s}"
          f"{'+yrs':>6s}{'meanRet%':>10s}")
    for label, mgmt, min_er in VARIANTS:
        pooled, pos, nyr, rets = run_variant(m15, years, mgmt, min_er)
        if not pooled:
            print(f"{label:28s}  (no trades)")
            continue
        wr = sum(1 for r in pooled if r > 0) / len(pooled) * 100
        print(f"{label:28s}{len(pooled):>7d}{wr:>6.0f}{mean(pooled):>+8.3f}"
              f"{f'{pos}/{nyr}':>6s}{mean(rets)*100:>+10.2f}", flush=True)


if __name__ == "__main__":
    main()
