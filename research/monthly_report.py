"""
Monthly performance report (one continuous, compounding backtest)
======================================================================
Runs the deployed config (run_xau_robust.ps1 = backtest_history "live" preset)
ONCE over the whole XAUUSD M15 history instead of resetting every year, then
breaks it down by calendar month. Month return is mark-to-market: equity at the
month's last bar vs the previous month's last bar, so an open trade's floating
P/L is counted in the month it happened. Trades are bucketed by CLOSE month.

    ./env/Scripts/python.exe research/monthly_report.py [START_YEAR] [END_YEAR]
        [--rules env|ftmo|none] [--spread 0.44]

Writes reports/monthly_<rules>_<start>_<end>.csv and prints a year x month grid.
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import argparse
import csv
import os
from collections import defaultdict

from dotenv import load_dotenv

from histdata import load_m15
from backtest_history import PRESETS, NO_RULES, run_year, COST
from ftmo_compliance_engine import EngineConfig, config_from_env, rules_summary, \
    AccountProfile, Variant, Path, Phase

MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports")


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("start", nargs="?", type=int, default=2015)
    ap.add_argument("end", nargs="?", type=int, default=2026)
    ap.add_argument("--rules", choices=["env", "ftmo", "none"], default="env")
    ap.add_argument("--spread", type=float, default=COST["spread"],
                    help="full spread in $ (FTMO measured 0.44; set VT's real one)")
    args = ap.parse_args()
    COST["spread"] = args.spread

    engine_cfg = {"ftmo": EngineConfig(), "none": NO_RULES}.get(args.rules) \
        or config_from_env()
    preset = PRESETS["live"]
    bars = load_m15(start_year=args.start, end_year=args.end)
    res = run_year(bars, preset["regime"], preset, engine_cfg)

    # equity_curve has one point per bar from the warmup onward
    warm = len(bars) - len(res.equity_curve)
    month_end_eq: dict[tuple, float] = {}
    month_peak: dict[tuple, float] = {}
    month_dd: dict[tuple, float] = defaultdict(float)
    for b, eq in zip(bars[warm:], res.equity_curve):
        k = (b.time.year, b.time.month)
        month_end_eq[k] = eq
        pk = max(month_peak.get(k, eq), eq)
        month_peak[k] = pk
        month_dd[k] = max(month_dd[k], (pk - eq) / pk)

    trades = defaultdict(list)
    for t in res.trades:
        trades[(t.closed_at.year, t.closed_at.month)].append(t)

    rows, prev = [], 100_000.0
    for k in sorted(month_end_eq):
        eq, ts = month_end_eq[k], trades.get(k, [])
        wins = sum(1 for t in ts if t.pnl > 0)
        rows.append({
            "month": f"{k[0]}-{k[1]:02d}", "trades": len(ts),
            "win_rate": round(wins / len(ts) * 100, 1) if ts else "",
            "pnl": round(eq - prev, 2), "return_pct": round((eq / prev - 1) * 100, 2),
            "sum_r": round(sum(t.r_mult for t in ts), 2),
            "max_dd_in_month_pct": round(month_dd[k] * 100, 2),
            "end_equity": round(eq, 2),
        })
        prev = eq

    tag = args.rules if args.rules != "env" else f"env-{os.getenv('RULES_MODE', 'ftmo')}"
    out = os.path.join(REPORTS, f"monthly_{tag}_{args.start}_{args.end}.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # ----- console: year x month grid of % returns -------------------------- #
    prof = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    print(f"=== MONTHLY: XAUUSD {rows[0]['month']} -> {rows[-1]['month']} | "
          f"live preset, risk 0.3%/trade, spread {args.spread}, compounding ===")
    print("Rules: " + rules_summary(prof, engine_cfg).split("-> ", 1)[-1] + "\n")
    by = {r["month"]: r for r in rows}
    print("year " + "".join(f"{m:>7s}" for m in MONTHS) + "    YEAR  trades")
    for y in sorted({int(r["month"][:4]) for r in rows}):
        cells, yr, n = [], 1.0, 0
        for mi in range(1, 13):
            r = by.get(f"{y}-{mi:02d}")
            cells.append(f"{r['return_pct']:>+7.2f}" if r else f"{'':>7s}")
            if r:
                yr *= 1 + r["return_pct"] / 100
                n += r["trades"]
        print(f"{y} " + "".join(cells) + f"  {(yr - 1) * 100:>+6.2f}  {n:>6d}")

    rets = [r["return_pct"] for r in rows]
    pos = sum(1 for x in rets if x > 0)
    streak = worst_streak = 0
    for x in rets:
        streak = streak + 1 if x < 0 else 0
        worst_streak = max(worst_streak, streak)
    best, worst = max(rows, key=lambda r: r["return_pct"]), min(rows, key=lambda r: r["return_pct"])
    total = (rows[-1]["end_equity"] / 100_000 - 1) * 100
    print(f"\nmonths={len(rows)}  positive={pos} ({pos / len(rows) * 100:.0f}%)  "
          f"avg={sum(rets) / len(rets):+.2f}%/month")
    print(f"best {best['month']} {best['return_pct']:+.2f}%  |  "
          f"worst {worst['month']} {worst['return_pct']:+.2f}%  |  "
          f"longest losing streak {worst_streak} months")
    print(f"total {total:+.2f}%  (100,000 -> {rows[-1]['end_equity']:,.2f})  |  "
          f"max drawdown {res.max_drawdown_pct * 100:.2f}%  |  "
          f"{len(res.trades)} trades, WR {res.win_rate * 100:.0f}%, PF {res.profit_factor:.2f}")
    print(f"\nCSV -> {out}")


if __name__ == "__main__":
    main()
