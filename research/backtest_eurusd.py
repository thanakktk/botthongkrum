"""
EURUSD backtest (the robust roster on a NON-gold instrument)
======================================================================
The edge study + the long-history validation were all on XAUUSD (the real
target). This pulls EURUSD M5 from the live MT5 terminal and runs the EXACT live
robust config through the same pipeline, plus the production DD-throttle, to see
whether the trend/breakout core travels to FX at all.

There is NO EURUSD CSV in backtest/ (only gold), so the data comes from MT5's
terminal cache — which is SHORT and may be STALE (the feed froze mid-June). Treat
this as a smoke test on a different instrument, NOT a validation: a few weeks in
one regime proves nothing (the same lesson the gold study hammered).

    ./env/Scripts/python.exe research/backtest_eurusd.py [SYMBOL] [M5_COUNT]
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import sys
import time
from datetime import datetime, timezone, timedelta
from statistics import mean, stdev
import math

import MetaTrader5 as mt5
from mt5_broker import Mt5Broker
from backtester import Backtester, BacktestConfig
from arbitration import Arbitrator, ArbitrationConfig
from strategies import select_strategies, ROBUST_TREND_IDS
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase

# live robust config (run_xau_robust.ps1) — held identical so only the INSTRUMENT changes
TF_WEIGHTS = {"M30": 1.0, "H1": 1.6, "H4": 2.4}
RISK = 0.003
GATES = dict(min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0)
MGMT = dict(manage=True, tp1_r=2.0, partial_pct=0.5, trail_r=1.0, be_trigger_r=0.0)
DD_STD = ((0.03, 0.5), (0.06, 0.25))            # the OOS-validated "S3" throttle

# EURUSD all-in cost: tiny raw spread + commission folded into one conservative
# number (FTMO FX is commission-based; ~1 pip all-in is a safe over-estimate).
EUR_COST = dict(spread=0.00010, slippage=0.00002, commission_per_lot=0.0)


def run(name, m5, specs, cost, dd_levels=()):
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M30", "H1", "H4"), tf_weights=TF_WEIGHTS, risk_pct=RISK,
        tp1_r=2.0, tp2_r=2.5, dd_throttle_levels=dd_levels, **GATES))
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies(ROBUST_TREND_IDS), arb,
        BacktestConfig(risk_pct=RISK, **cost, **MGMT), specs=specs)
    res = bt.run(sym, m5)
    rs = [t.r_mult for t in res.trades]
    avgR = mean(rs) if rs else 0.0
    se = (stdev(rs) / math.sqrt(len(rs))) if len(rs) > 1 else 0.0
    print(f"{name:26s} {res.summary()}  avgR={avgR:+.3f}±{se:.3f}")
    return res


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "EURUSD"
    # MT5 copy_rates_from_pos rejects count >= ~100k ("Invalid params"); 20k M5
    # (~2.3 months) is the most it reliably serves from the terminal cache.
    count = min(int(sys.argv[2]) if len(sys.argv) > 2 else 20_000, 20_000)

    with Mt5Broker() as b:
        info = mt5.symbol_info(sym)
        if info is None:
            print(f"{sym}: not in Market Watch", file=sys.stderr); sys.exit(1)
        vpp = float(info.trade_contract_size)        # value per 1.0 price move / lot
        # COLD-CACHE WARMUP: a fresh MT5 connection returns 0 bars until the
        # terminal streams the symbol's history. copy_rates_range forces that
        # download; then from_pos returns the cached bars. Retry with backoff.
        mt5.symbol_select(sym, True)
        now = datetime.now(timezone.utc)
        m5 = []
        for _ in range(6):
            mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5,
                                 now - timedelta(days=400), now)
            m5 = b.get_bars(sym, "M5", count)
            if m5 and len(m5) > 500:
                break
            time.sleep(1.5)
    if not m5 or len(m5) < 500:
        print(f"{sym}: only {len(m5) if m5 else 0} M5 bars — too few to backtest.",
              file=sys.stderr)
        sys.exit(1)

    specs = {sym: vpp}
    days = (m5[-1].time - m5[0].time).days
    print(f"=== EURUSD BACKTEST (robust roster, live config): {sym} ===")
    print(f"M5 {len(m5):,} bars  {m5[0].time:%Y-%m-%d}->{m5[-1].time:%Y-%m-%d} "
          f"(~{days}d)  contract={vpp:,.0f}  TFs M30/H1/H4  risk {RISK*100:.1f}%")
    print(f"cost: spread={EUR_COST['spread']}  slippage={EUR_COST['slippage']}\n")

    base = run("baseline (robust)", m5, specs, EUR_COST)
    dd = run("+DD-throttle (S3)", m5, specs, EUR_COST, dd_levels=DD_STD)

    print(f"\nTrades: {len(base.trades)}  |  net XAU-study expectation does NOT carry "
          f"over — EURUSD is FX, the roster was tuned on gold.")
    if len(base.trades) < 30:
        print("⚠️  <30 trades and one short, possibly-stale window — this is a SMOKE "
              "TEST, not evidence of an edge (or its absence) on EURUSD.")
