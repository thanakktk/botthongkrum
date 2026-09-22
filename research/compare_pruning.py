"""
Pruning comparison (Phase 2 — does cutting dead strategies help the REAL system?)
======================================================================
The standalone analysis judged each technique alone. This runs the ACTUAL live
pipeline — full MTF confluence (M30/H1/H4), the same gates the bot uses — on real
XAUUSD, comparing the current all-13 roster against the 4 out-of-sample
survivors. If pruning is right, the survivor roster should trade better (higher
expectancy / PF / return) even though it trades less often.

    ./env/Scripts/python.exe compare_pruning.py [SYMBOL] [M5_COUNT]
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
from strategies import DEFAULT_STRATEGIES
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase

SURVIVORS = {"macd_trend", "roc_momentum", "donchian_breakout", "breakout_sr"}
TF_WEIGHTS = {"M30": 1.0, "H1": 1.6, "H4": 2.4}
COST = dict(spread=0.44, slippage=0.05, commission_per_lot=0.0)


def select(ids: set[str]):
    return [s for s in DEFAULT_STRATEGIES if s.id in ids]


def run(name: str, strategies, m5, **gate):
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M30", "H1", "H4"), tf_weights=TF_WEIGHTS,
        risk_pct=0.003, tp1_r=1.0, tp2_r=2.5, **gate))
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        strategies, arb, BacktestConfig(risk_pct=0.003, **COST),
        specs={"XAUUSD": 100.0})
    res = bt.run("XAUUSD", m5)
    avgR = mean(t.r_mult for t in res.trades) if res.trades else 0.0
    print(f"{name:34s} {res.summary()}  avgR={avgR:+.2f}")
    return res


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    m5_count = int(sys.argv[2]) if len(sys.argv) > 2 else 20000
    with Mt5Broker() as b:
        m5 = b.get_bars(symbol, "M5", m5_count)
    if not m5:
        print(f"No M5 data for {symbol}.", file=sys.stderr)
        sys.exit(1)
    print(f"=== PRUNING COMPARISON (full MTF confluence): {symbol} ===")
    print(f"M5 {len(m5):,}  {m5[0].time:%Y-%m-%d}->{m5[-1].time:%Y-%m-%d}  "
          f"| TFs M30/H1/H4 | risk 0.3%\n")

    run("ALL-13  (live cfg, agr0.65)", DEFAULT_STRATEGIES, m5,
        min_agree=3, min_families=2, min_agreement=0.65, min_conviction=1.5)
    run("PRUNED-4 (agr0.60, 2 tech)", select(SURVIVORS), m5,
        min_agree=2, min_families=2, min_agreement=0.60, min_conviction=1.0)
    run("PRUNED-4 (agr0.70, 2 tech)", select(SURVIVORS), m5,
        min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0)
    run("PRUNED-4 (agr0.60, 3 tech)", select(SURVIVORS), m5,
        min_agree=3, min_families=2, min_agreement=0.60, min_conviction=1.0)


if __name__ == "__main__":
    main()
