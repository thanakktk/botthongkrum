"""
Tune min_agreement on REAL MT5 history (multi-timeframe backtest)
======================================================================
Pulls real bars for a symbol, runs the FULL MTF strategy->arbitration pipeline
across a grid of `min_agreement` thresholds with realistic spread/commission,
and prints a comparison so the DATA chooses the threshold (not a guess). Then a
walk-forward IS/OOS check on the best value for robustness.

Run:  ./env/Scripts/python.exe tune.py --symbol BTCUSD --bars 1500
      ./env/Scripts/python.exe tune.py --symbol XAUUSD --bars 2000 --spread 0.44
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse

import MetaTrader5 as mt5

from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase
from mt5_broker import Mt5Broker
from arbitration import Arbitrator, ArbitrationConfig
from backtester import Backtester, BacktestConfig
from optimize import WalkForwardOptimizer, default_objective
from strategies import DEFAULT_STRATEGIES

TF_WEIGHTS = {"M5": 1.0, "M15": 1.6, "H1": 2.4}
TIMEFRAMES = ("M5", "M15", "H1")


def make_arb(min_agreement: float, risk_pct: float) -> Arbitrator:
    return Arbitrator(ArbitrationConfig(
        timeframes=TIMEFRAMES, tf_weights=TF_WEIGHTS, risk_pct=risk_pct,
        min_agreement=min_agreement, min_agree=2, min_families=1,
        min_conviction=0.5, max_notional_frac=0.5))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSD")
    ap.add_argument("--bars", type=int, default=1500)
    ap.add_argument("--spread", type=float, default=None, help="price units")
    ap.add_argument("--commission", type=float, default=None, help="$/lot/side")
    ap.add_argument("--risk-pct", type=float, default=0.002)
    ap.add_argument("--grid", default="0.50,0.60,0.65,0.70,0.75,0.80,0.85")
    args = ap.parse_args()
    grid = [float(x) for x in args.grid.split(",")]

    with Mt5Broker() as b:
        m5 = b.get_bars(args.symbol, "M5", args.bars)
        info = mt5.symbol_info(args.symbol)
        contract = float(info.trade_contract_size)
        live_spread = (mt5.symbol_info_tick(args.symbol).ask
                       - mt5.symbol_info_tick(args.symbol).bid)
    spread = args.spread if args.spread is not None else round(live_spread, 5)
    # commission: measured ~20.6 $/lot/side on BTCUSD; default per symbol
    commission = args.commission if args.commission is not None else (
        20.6 if "BTC" in args.symbol or "ETH" in args.symbol else 5.0)
    specs = {args.symbol: contract}     # value_per_price_per_lot == contract size

    print(f"{args.symbol}: {len(m5)} M5 bars  ({m5[0].time:%Y-%m-%d} -> "
          f"{m5[-1].time:%Y-%m-%d})  spread={spread} commission=${commission}/lot/side")
    print(f"TFs={TIMEFRAMES} weights={TF_WEIGHTS} risk={args.risk_pct:.2%}\n")

    profile = AccountProfile(Variant.SWING, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    btcfg = BacktestConfig(initial_balance=100_000, spread=spread, slippage=spread / 2,
                           commission_per_lot=commission, risk_pct=args.risk_pct)

    print(f"{'min_agr':>7} {'trades':>6} {'win%':>5} {'PF':>5} {'ret%':>7} "
          f"{'maxDD%':>6} {'maxDayLoss':>10} {'breach':>7}")
    rows = []
    for ma in grid:
        bt = Backtester(profile, DEFAULT_STRATEGIES, make_arb(ma, args.risk_pct),
                        btcfg, specs=specs)
        r = bt.run(args.symbol, m5)
        rows.append((ma, r))
        pf = r.profit_factor if r.profit_factor != float("inf") else 99.9
        print(f"{ma:>7.2f} {len(r.trades):>6} {r.win_rate*100:>5.0f} {pf:>5.2f} "
              f"{r.return_pct*100:>+7.2f} {r.max_drawdown_pct*100:>6.1f} "
              f"{r.max_daily_loss:>10,.0f} {'BREACH' if r.floor_breached else 'ok':>7}")

    # pick best by objective among rows that actually traded and didn't breach
    valid = [(ma, r) for ma, r in rows if r.trades and not r.floor_breached]
    if not valid:
        print("\nNo threshold produced safe trades on this sample.")
        return 0
    best_ma, best_r = max(valid, key=lambda mr: default_objective(mr[1]))
    print(f"\nBest by objective: min_agreement={best_ma:.2f} "
          f"({len(best_r.trades)} trades, win {best_r.win_rate*100:.0f}%, "
          f"PF {best_r.profit_factor:.2f}, ret {best_r.return_pct*100:+.2f}%)")

    # walk-forward IS/OOS robustness on the best value
    print("\nWalk-forward IS/OOS at the best threshold:")
    opt = WalkForwardOptimizer(
        profile, lambda p: (DEFAULT_STRATEGIES, make_arb(p["ma"], args.risk_pct), btcfg),
        specs=specs)
    for o in opt.walk_forward(args.symbol, m5, {"ma": [best_ma]}, splits=3):
        print(f"  split {o.split}: IS {o.is_result.summary()}")
        print(f"            OOS {o.oos_result.summary()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
