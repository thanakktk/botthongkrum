"""
Filter sweep (regime + session) on the robust-4 — make the edge sharper
======================================================================
The 4 survivors are trend/breakout techniques, so they should pay best in a
TREND regime and during liquid sessions. This sweeps HARD regime + session gates
through the REAL live pipeline (full MTF confluence + the live trade management:
early-BE@0.8 + TP1 partial + trail) on XAUUSD, so we keep only the filters the
data actually rewards — not ones that just sound right.

    ./env/Scripts/python.exe test_filters.py [SYMBOL] [M5_COUNT]
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
# live trade management (matches the deployed config)
MGMT = dict(manage=True, tp1_r=1.0, partial_pct=0.5, trail_r=1.0, be_trigger_r=0.8)


def run(name: str, m5, *, require_regime=(), sessions=()):
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M30", "H1", "H4"), tf_weights=TF_WEIGHTS, risk_pct=0.003,
        tp1_r=1.0, tp2_r=2.5, require_regime=require_regime,
        allowed_sessions=sessions, **GATES))
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies(ROBUST_TREND_IDS), arb,
        BacktestConfig(risk_pct=0.003, **COST, **MGMT), specs={"XAUUSD": 100.0})
    res = bt.run("XAUUSD", m5)
    t = res.trades
    avgR = mean(x.r_mult for x in t) if t else 0.0
    print(f"{name:30s} trades={len(t):3d} WR={res.win_rate*100:4.0f}% "
          f"PF={res.profit_factor:4.2f} ret={res.return_pct*100:+6.2f}% "
          f"avgR={avgR:+.3f} maxDD={res.max_drawdown_pct*100:4.1f}%")
    return res


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    m5_count = int(sys.argv[2]) if len(sys.argv) > 2 else 30000
    with Mt5Broker() as b:
        m5 = b.get_bars(symbol, "M5", m5_count)
    if not m5:
        print(f"No M5 data for {symbol}.", file=sys.stderr)
        sys.exit(1)
    print(f"=== FILTER SWEEP (robust-4, full pipeline + mgmt): {symbol} ===")
    print(f"M5 {len(m5):,}  {m5[0].time:%Y-%m-%d}->{m5[-1].time:%Y-%m-%d} | risk 0.3%\n")

    print("-- baseline --")
    run("baseline (no filter)", m5)
    print("-- REGIME gate --")
    run("trend only", m5, require_regime=("trend",))
    run("trend + unknown", m5, require_regime=("trend", "unknown"))
    run("range only (sanity check)", m5, require_regime=("range",))
    print("-- SESSION gate --")
    run("london+ny", m5, sessions=("london", "ny"))
    run("ny only", m5, sessions=("ny",))
    print("-- COMBINED --")
    run("trend+unknown & london+ny", m5,
        require_regime=("trend", "unknown"), sessions=("london", "ny"))


if __name__ == "__main__":
    main()
