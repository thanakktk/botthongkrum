"""
Break-Even / trade-management A/B test (Phase 2, item 2)
======================================================================
The MFE study said losing trades peak ~0.71R before reversing into the full -1R
stop. The hypothesis: an EARLY break-even (move SL to entry once price reaches
~0.6-0.8R) turns many of those -1R losses into ~0R, lifting expectancy. But it
has a cost — a trade that dips back to entry after tagging the trigger is closed
flat instead of being allowed to run. So we TEST it on the robust-4 roster with
the real live pipeline rather than assume.

Variants (full MTF confluence, robust roster, XAUUSD):
  A raw            — SL / TP2 only (no management)
  B TP1+BE@1R      — current live: bank 50% + break-even at 1R, then trail
  C + early-BE@0.8 — B, plus move SL→entry as soon as price tags 0.8R
  D + early-BE@0.6 — B, plus the early break-even at 0.6R

    ./env/Scripts/python.exe test_breakeven.py [SYMBOL] [M5_COUNT]
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import sys
from statistics import mean

from mt5_broker import Mt5Broker
from backtester import Backtester, BacktestConfig
from arbitration import Arbitrator, ArbitrationConfig
from strategies import select_strategies, ROBUST_TREND_IDS
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase

TF_WEIGHTS = {"M30": 1.0, "H1": 1.6, "H4": 2.4}
COST = dict(spread=0.44, slippage=0.05, commission_per_lot=0.0)
GATES = dict(min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0)


def run(name: str, m5, mgmt: dict):
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M30", "H1", "H4"), tf_weights=TF_WEIGHTS,
        risk_pct=0.003, tp1_r=1.0, tp2_r=2.5, **GATES))
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies(ROBUST_TREND_IDS), arb,
        BacktestConfig(risk_pct=0.003, **COST, **mgmt), specs={"XAUUSD": 100.0})
    res = bt.run("XAUUSD", m5)
    t = res.trades
    avgR = mean(x.r_mult for x in t) if t else 0.0
    # how many losers were "rescued" toward ~breakeven (small |R|)
    saved = sum(1 for x in t if -0.15 <= x.r_mult <= 0.05)
    print(f"{name:24s} trades={len(t):3d} WR={res.win_rate*100:4.0f}% "
          f"PF={res.profit_factor:4.2f} ret={res.return_pct*100:+6.2f}% "
          f"avgR={avgR:+.3f} maxDD={res.max_drawdown_pct*100:4.1f}% ~BE={saved}")
    return res


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    m5_count = int(sys.argv[2]) if len(sys.argv) > 2 else 30000
    with Mt5Broker() as b:
        m5 = b.get_bars(symbol, "M5", m5_count)
    if not m5:
        print(f"No M5 data for {symbol}.", file=sys.stderr)
        sys.exit(1)
    print(f"=== BREAK-EVEN A/B (robust-4, full confluence): {symbol} ===")
    print(f"M5 {len(m5):,}  {m5[0].time:%Y-%m-%d}->{m5[-1].time:%Y-%m-%d}  "
          f"| risk 0.3% | ~BE = losers rescued to about breakeven\n")

    run("A raw (no mgmt)", m5, dict(manage=False))
    run("B TP1+BE@1R (live)", m5,
        dict(manage=True, tp1_r=1.0, partial_pct=0.5, trail_r=1.0, be_trigger_r=0.0))
    run("C + early-BE@0.8R", m5,
        dict(manage=True, tp1_r=1.0, partial_pct=0.5, trail_r=1.0, be_trigger_r=0.8))
    run("D + early-BE@0.6R", m5,
        dict(manage=True, tp1_r=1.0, partial_pct=0.5, trail_r=1.0, be_trigger_r=0.6))


if __name__ == "__main__":
    main()
