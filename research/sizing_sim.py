"""
Position-sizing simulation for the H4 Trend bot
======================================================================
Q: what risk-per-trade turns the H4 trend cluster into ">= 2%/month on
average" and what drawdown / losing streaks come with it?

1. Re-runs the three H4 strategies SOLO (breakout_sr, donchian_breakout,
   roc_momentum) through the real Backtester on the full XAUUSD history and
   keeps every trade (close time, R multiple).
2. Merges them into one portfolio in time order (each loop risks r% of the
   CURRENT equity; positions can overlap, so up to 3x exposure).
3. For each r: the historical path + a Monte Carlo (block bootstrap by MONTH,
   which keeps the within-month correlation / streaks) -> distribution of
   annual return, monthly return, max drawdown, P(max DD > 50%).
4. Same again with a drawdown throttle (risk /2 below 20% from the equity
   peak, /4 below 35%).

Costs: VT Markets XAUUSD-ECN spread ~$0.11 measured + ECN commission (not
exposed by the API; typical $6-7/lot round turn = ~$0.07/oz) -> we charge
$0.25 spread-equivalent + $0.05 slippage, i.e. conservative.

    ./env/Scripts/python.exe research/sizing_sim.py [--runs 2000] [--start 2004]
"""

from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
STRATS = ("breakout_sr", "donchian_breakout", "roc_momentum")
SPREAD, SLIP = 0.25, 0.05
RISKS = (0.003, 0.005, 0.0075, 0.010, 0.0125, 0.015, 0.020)
THROTTLE = ((0.20, 0.5), (0.35, 0.25))     # (drawdown from peak, risk multiplier)


def solo_trades(sid: str, start: int) -> list[tuple[int, float]]:
    from histdata import load_m15
    from backtester import Backtester, BacktestConfig, resample_clock
    from arbitration import Arbitrator, ArbitrationConfig
    from strategies import select_strategies
    from ftmo_compliance_engine import (AccountProfile, Variant, Path, Phase,
                                        EngineConfig)
    bars = resample_clock(load_m15(start_year=start), 14400)
    no_rules = EngineConfig(enforce_daily_loss=False, enforce_overall_loss=False,
                            enforce_consistency=False, enforce_weekend_flatten=False,
                            enforce_news_blackout=False)
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M5",), tf_weights={"M5": 1.0}, risk_pct=0.003,
        tp1_r=2.0, tp2_r=2.5, min_agreement=0.0, min_agree=1, min_families=1,
        min_conviction=0.0, signal_floor=0.0))
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies((sid,)), arb,
        BacktestConfig(risk_pct=0.003, tf_factor={"M5": 1}, spread=SPREAD,
                       slippage=SLIP, manage=True, tp1_r=2.0, partial_pct=0.5,
                       trail_r=1.0, be_trigger_r=0.0),
        specs={"XAUUSD": 100.0}, engine_cfg=no_rules)
    res = bt.run("XAUUSD", bars)
    return [(int(t.closed_at.timestamp()), float(t.r_mult)) for t in res.trades]


def _solo(args):
    return solo_trades(*args)


# --------------------------------------------------------------------------- #
def month_key(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.year * 12 + d.month - 1


def by_month(trades: list[tuple[int, float]]) -> tuple[list[int], list[list[float]]]:
    """Ordered months -> the R-multiples closed in each (empty months kept)."""
    m = defaultdict(list)
    for ts, r in trades:
        m[month_key(ts)].append(r)
    keys = list(range(min(m), max(m) + 1))
    return keys, [m.get(k, []) for k in keys]


def run_path(months: list[list[float]], risk: float, throttle: bool):
    """Compound a sequence of months; returns (monthly_returns, max_dd)."""
    eq = peak = 1.0
    dd = 0.0
    out = []
    for rs in months:
        start = eq
        for r in rs:
            mult = 1.0
            if throttle:
                cur_dd = 1 - eq / peak
                for lvl, k in THROTTLE:
                    if cur_dd >= lvl:
                        mult = k
            eq = max(eq * (1 + risk * mult * r), 1e-9)
            peak = max(peak, eq)
            dd = max(dd, 1 - eq / peak)
        out.append(eq / start - 1)
    return out, dd


def stats(monthly_paths, dds, years_per_path):
    mr = np.concatenate(monthly_paths)
    ann = np.array([np.prod(1 + np.array(p)) ** (1 / years_per_path) - 1 for p in monthly_paths])
    dds = np.array(dds)
    return {
        "median_cagr_pct": round(float(np.median(ann)) * 100, 1),
        "p10_cagr_pct": round(float(np.percentile(ann, 10)) * 100, 1),
        "mean_month_pct": round(float(mr.mean()) * 100, 2),
        "median_month_pct": round(float(np.median(mr)) * 100, 2),
        "months_positive_pct": round(float((mr > 0).mean()) * 100),
        "worst_month_p5_pct": round(float(np.percentile(mr, 5)) * 100, 1),
        "median_maxdd_pct": round(float(np.median(dds)) * 100, 1),
        "p90_maxdd_pct": round(float(np.percentile(dds, 90)) * 100, 1),
        "p_dd_over_50_pct": round(float((dds > 0.50).mean()) * 100, 1),
        "p_dd_over_60_pct": round(float((dds > 0.60).mean()) * 100, 1),
    }


def longest_losing(monthly: list[float]) -> int:
    best = cur = 0
    for x in monthly:
        cur = cur + 1 if x < 0 else 0
        best = max(best, cur)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2000)
    ap.add_argument("--start", type=int, default=2004)
    args = ap.parse_args()
    rng = np.random.default_rng(42)

    print("running the 3 solo H4 backtests ...", flush=True)
    with Pool(3) as pool:
        streams = pool.map(_solo, [(s, args.start) for s in STRATS])
    merged = sorted((t for s in streams for t in s), key=lambda x: x[0])
    keys, months = by_month(merged)
    years = len(keys) / 12
    n = len(merged)
    print(f"{n} trades, {len(keys)} months ({years:.1f} yrs), "
          f"{n / years:.0f} trades/yr, avgR {np.mean([r for _, r in merged]):+.3f}")
    for sid, s in zip(STRATS, streams):
        print(f"  {sid:<20s} n={len(s):>5d} avgR={np.mean([r for _, r in s]):+.3f}")

    with open(os.path.join(REPORTS, "h4_trend_trades.json"), "w") as f:
        json.dump({"strategies": STRATS, "spread": SPREAD, "slippage": SLIP,
                   "trades": merged}, f)

    results = {}
    lines = [f"H4 TREND PORTFOLIO SIZING — {STRATS}, XAUUSD {args.start}-2026, "
             f"cost {SPREAD}+{SLIP}, {n} trades, {args.runs} bootstrap runs\n"]
    for throttle in (False, True):
        lines.append(f"=== {'WITH dd-throttle (x0.5 @20%, x0.25 @35%)' if throttle else 'NO throttle'} ===")
        lines.append(f"{'risk':>6s}{'hist CAGR':>10s}{'hist mDD':>9s}{'hist worst mo':>14s}"
                     f"{'streak':>7s} | {'MC med CAGR':>11s}{'p10 CAGR':>9s}{'avg mo':>7s}"
                     f"{'mo>0':>5s}{'p5 mo':>7s}{'med DD':>7s}{'p90 DD':>7s}{'P(DD>50)':>9s}{'P(DD>60)':>9s}")
        for risk in RISKS:
            hist, hdd = run_path(months, risk, throttle)
            paths, dds = [], []
            for _ in range(args.runs):
                idx = rng.integers(0, len(months), len(months))
                p, d = run_path([months[i] for i in idx], risk, throttle)
                paths.append(p)
                dds.append(d)
            st = stats(paths, dds, years)
            hist_cagr = (np.prod(1 + np.array(hist)) ** (1 / years) - 1) * 100
            results[f"{risk}|{throttle}"] = {**st, "hist_cagr_pct": round(hist_cagr, 1),
                                             "hist_maxdd_pct": round(hdd * 100, 1)}
            lines.append(
                f"{risk * 100:>5.2f}%{hist_cagr:>+9.1f}%{hdd * 100:>8.1f}%"
                f"{min(hist) * 100:>+13.1f}%{longest_losing(hist):>7d} | "
                f"{st['median_cagr_pct']:>+10.1f}%{st['p10_cagr_pct']:>+8.1f}%"
                f"{st['mean_month_pct']:>+6.2f}%{st['months_positive_pct']:>4d}%"
                f"{st['worst_month_p5_pct']:>+6.1f}%{st['median_maxdd_pct']:>6.1f}%"
                f"{st['p90_maxdd_pct']:>6.1f}%{st['p_dd_over_50_pct']:>8.1f}%"
                f"{st['p_dd_over_60_pct']:>8.1f}%")
        lines.append("")

    # historical year-by-year at the candidate sizes, for the eyeball test
    lines.append("=== historical calendar years, NO throttle (% return) ===")
    yrs = sorted({k // 12 for k in keys})
    lines.append("risk  " + "".join(f"{y:>7d}" for y in yrs))
    for risk in (0.005, 0.010, 0.015):
        hist, _ = run_path(months, risk, False)
        yr = defaultdict(lambda: 1.0)
        for k, m in zip(keys, hist):
            yr[k // 12] *= 1 + m
        lines.append(f"{risk * 100:>4.1f}% " + "".join(f"{(yr[y] - 1) * 100:>+7.1f}" for y in yrs))

    out = "\n".join(lines)
    print("\n" + out)
    with open(os.path.join(REPORTS, "sizing_sim.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    with open(os.path.join(REPORTS, "sizing_sim.json"), "w") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
