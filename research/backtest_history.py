"""
Multi-year edge validation on the long history (Phase 2 — the real OOS)
======================================================================
MetaTrader's cache only gave ~7 months. The backtest/ folder has ~22 years of
gold. This runs the DEPLOYED config — robust-4 + full MTF confluence + the trend
regime gate + live trade-management (early-BE@0.8) — independently on EACH YEAR
2015->2026, so we can see whether the edge is consistent across many market
cycles (2018 chop, 2020 covid spike, 2022 hikes, 2024-26 bull) or just a recent
fluke. A genuine edge is positive in MOST years, not one lucky stretch.

    ./env/Scripts/python.exe backtest_history.py [START_YEAR] [END_YEAR] [baseline]
        [--preset legacy|live] [--rules env|ftmo|none]

--preset live   = the exact run_xau_robust.ps1 config (tp1 2.0R, no early BE,
                  no regime gate). legacy (default) = the original study config.
--rules env     = RULES_MODE from .env, same rulebook as the live bot (default);
        ftmo    = force the full FTMO ruleset; none = no FTMO rules.
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse
from statistics import mean

from dotenv import load_dotenv

from histdata import load_m15, M15_TF_FACTOR
from backtester import Backtester, BacktestConfig
from arbitration import Arbitrator, ArbitrationConfig
from strategies import select_strategies, ROBUST_TREND_IDS
from ftmo_compliance_engine import (
    AccountProfile, Variant, Path, Phase, EngineConfig, config_from_env,
    rules_summary,
)

TF_WEIGHTS = {"M30": 1.0, "H1": 1.6, "H4": 2.4}
COST = dict(spread=0.44, slippage=0.05, commission_per_lot=0.0)
GATES = dict(min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0)
PRESETS = {
    # the original multi-year study config
    "legacy": dict(tp1_r=1.0, be_trigger_r=0.8, regime=("trend", "unknown")),
    # what run_xau_robust.ps1 actually deploys
    "live": dict(tp1_r=2.0, be_trigger_r=0.0, regime=()),
}
NO_RULES = EngineConfig(
    enforce_daily_loss=False, enforce_overall_loss=False, enforce_consistency=False,
    enforce_weekend_flatten=False, enforce_news_blackout=False)


def run_year(bars, require_regime, preset, engine_cfg):
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M30", "H1", "H4"), tf_weights=TF_WEIGHTS, risk_pct=0.003,
        tp1_r=preset["tp1_r"], tp2_r=2.5, require_regime=require_regime, **GATES))
    mgmt = dict(manage=True, tp1_r=preset["tp1_r"], partial_pct=0.5, trail_r=1.0,
                be_trigger_r=preset["be_trigger_r"])
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies(ROBUST_TREND_IDS), arb,
        BacktestConfig(risk_pct=0.003, tf_factor=M15_TF_FACTOR, **COST, **mgmt),
        specs={"XAUUSD": 100.0}, engine_cfg=engine_cfg)
    return bt.run("XAUUSD", bars)


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("start", nargs="?", type=int, default=2015)
    ap.add_argument("end", nargs="?", type=int, default=2026)
    # "baseline" drops the regime gate, to test whether the filter actually
    # added value over the FULL multi-year sample (not just the recent months).
    ap.add_argument("mode", nargs="?", default="")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="legacy")
    ap.add_argument("--rules", choices=["env", "ftmo", "none"], default="env")
    args = ap.parse_args()
    start, end = args.start, args.end
    preset = PRESETS[args.preset]
    regime = () if args.mode == "baseline" else preset["regime"]
    engine_cfg = {"ftmo": EngineConfig(), "none": NO_RULES}.get(args.rules) \
        or config_from_env()
    m15 = load_m15(start_year=start, end_year=end)
    print(f"=== MULTI-YEAR VALIDATION: XAUUSD M15 {start}-{end} "
          f"({len(m15):,} bars) ===")
    print(f"Config: preset={args.preset} robust-4 + confluence + "
          f"regime={regime or 'ANY'} tp1={preset['tp1_r']}R "
          f"early-BE={preset['be_trigger_r'] or 'off'} | risk 0.3%")
    prof = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    print("Rules: " + rules_summary(prof, engine_cfg).split("-> ", 1)[-1] + "\n")
    print(f"{'year':6s}{'bars':>8s}{'trades':>8s}{'WR%':>6s}{'PF':>6s}"
          f"{'ret%':>8s}{'avgR':>8s}{'maxDD%':>8s}{'breach':>8s}")

    all_r: list[float] = []
    pos_years = 0
    n_years = 0
    for y in range(start, end + 1):
        bars = [b for b in m15 if b.time.year == y]
        if len(bars) < 2000:
            continue
        res = run_year(bars, regime, preset, engine_cfg)
        t = res.trades
        avgR = mean(x.r_mult for x in t) if t else 0.0
        all_r.extend(x.r_mult for x in t)
        n_years += 1
        if res.return_pct > 0:
            pos_years += 1
        pf = "inf" if res.profit_factor == float("inf") else f"{res.profit_factor:.2f}"
        print(f"{y:<6d}{len(bars):>8d}{len(t):>8d}{res.win_rate*100:>6.0f}"
              f"{pf:>6s}{res.return_pct*100:>+8.2f}{avgR:>+8.3f}"
              f"{res.max_drawdown_pct*100:>8.1f}{'YES' if res.floor_breached else 'no':>8s}")

    if all_r:
        wins = sum(1 for r in all_r if r > 0)
        print(f"\nOVERALL: {len(all_r)} trades across {n_years} years | "
              f"WR={wins/len(all_r)*100:.0f}% | avgR={mean(all_r):+.3f} | "
              f"positive years={pos_years}/{n_years}")


if __name__ == "__main__":
    main()
