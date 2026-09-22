"""
Buy-only gold grid "with belts" — can a cash-flow grid deliver ~2%/month
REALISED profit with floating drawdown capped at 60%?
======================================================================
Base = the calibrated Grandma/KZM grid (buy a level as price crosses it, TP at
the next level or a tight trail, re-buy freely). Belts swept:

  spacing   : fixed $7 / $14, or 0.5x / 1.0x the D1 ATR(14)  (volatility-aware)
  trend     : none | D1 close > SMA200 | D1 EMA50 > EMA200  -> no NEW buys when
              the filter is off (existing levels keep their TP/trail)
  max_levels: cap on simultaneously open levels
  hard stop : equity <= 40% of the cycle's starting equity -> close all,
              restart the grid sized on what is left (the "belt")
  sizing    : lots = scale x equity/1000, recomputed whenever the grid is flat

Metrics per config (start $10,000, VT 1:2000, cost .25+.05, swap -6.7% pa):
  realised month = balance change (what you could withdraw)
  pct months with realised >= +2%, >= 0; avg realised %/mo; max EQUITY DD
  (open positions included); hard-stop events.

    ./env/Scripts/python.exe research/grid_belt.py [--workers 11] [--quick]
Writes reports/grid_belt.txt / .json
"""
from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

import argparse
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool

import numpy as np

import grid_lab as G
from grid_lab import Account, CONTRACT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
G.LEVERAGE = 2000
HARD_STOP = 0.60

WINDOWS = {"2004-26": 2004, "2015-26": 2015, "2023-26": 2023, "vt24-26": 2024}   # vt24-26 = VT H1 incl. the 2026 crash
SPACINGS = {"fix7": ("fix", 7.0), "fix14": ("fix", 14.0), "atr0.5": ("atr", 0.5), "atr1.0": ("atr", 1.0)}
TRENDS = ("none", "sma200", "ema50x200")
SCALES = (0.0002, 0.0004, 0.0008)
MAXLEV = (60, 30)


# --------------------------------------------------------------------------- #
_CACHE: dict = {}


def m15_rows(start_year: int, window: str = ""):
    key = ("rows", start_year, window)
    if key not in _CACHE:
        if window == "vt24-26":
            import csv
            path = os.path.join(ROOT, "backtest", "XAUUSD_ECN_H1_vt.csv")
            rows = [(int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
                    for r in csv.DictReader(open(path))]
            _CACHE[key] = rows                              # H1 bars, 2024-02 -> now
        else:
            from histdata import load_m15
            b = load_m15(start_year=start_year - 1)          # 1 extra year for D1 warm-up
            _CACHE[key] = [(int(x.time.timestamp()), x.open, x.high, x.low, x.close) for x in b]
    return _CACHE[key]


def daily_context(rows):
    """Per-M15-row: D1 ATR14, SMA200 and EMA50/EMA200 of D1 closes, as of the
    PREVIOUS completed day (no look-ahead)."""
    days = defaultdict(list)
    for t, o, h, l, c in rows:
        days[datetime.fromtimestamp(t, timezone.utc).date()].append((o, h, l, c))
    dkeys = sorted(days)
    d_o = np.array([days[k][0][0] for k in dkeys]); d_h = np.array([max(x[1] for x in days[k]) for k in dkeys])
    d_l = np.array([min(x[2] for x in days[k]) for k in dkeys]); d_c = np.array([days[k][-1][3] for k in dkeys])
    n = len(dkeys)
    tr = np.maximum(d_h - d_l, np.maximum(abs(d_h - np.roll(d_c, 1)), abs(d_l - np.roll(d_c, 1)))); tr[0] = d_h[0] - d_l[0]
    atr = np.full(n, np.nan)
    for i in range(14, n):
        atr[i] = tr[i - 13:i + 1].mean()
    sma200 = np.full(n, np.nan)
    cs = np.cumsum(np.insert(d_c, 0, 0.0))
    sma200[199:] = (cs[200:] - cs[:-200]) / 200
    def ema(x, k):
        out = np.empty(len(x)); a = 2 / (k + 1); out[0] = x[0]
        for i in range(1, len(x)): out[i] = a * x[i] + (1 - a) * out[i - 1]
        return out
    e50, e200 = ema(d_c, 50), ema(d_c, 200)
    idx = {k: i for i, k in enumerate(dkeys)}
    ctx = {}
    for k in dkeys:
        i = idx[k] - 1                                    # previous completed day
        ctx[k] = (atr[i] if i >= 14 else np.nan, sma200[i] if i >= 199 else np.nan,
                  (e50[i] > e200[i]) if i >= 200 else True, d_c[i] if i >= 0 else np.nan)
    return ctx


# --------------------------------------------------------------------------- #
class BeltGrid:
    def __init__(self, scale, spacing_mode, spacing_val, trend, max_levels):
        self.scale, self.mode, self.val, self.trend, self.maxlev = scale, spacing_mode, spacing_val, trend, max_levels
        self.sp = spacing_val if spacing_mode == "fix" else 10.0
        self.lot = 0.0
        self.trail_arm, self.trail_frac = 1.5, 1.4

    def new_day(self, ctx_today, equity):
        atr, sma, ema_up, dclose = ctx_today
        if self.mode == "atr" and not np.isnan(atr):
            self.sp = max(3.0, round(self.val * atr, 1))
        self.allow = True
        if self.trend == "sma200" and not np.isnan(sma):
            self.allow = dclose > sma
        elif self.trend == "ema50x200":
            self.allow = bool(ema_up)

    def on_bar(self, acc: Account, o, h, l, c):
        if not acc.pos:                                   # flat -> re-size to current equity
            self.lot = max(0.0001, round(self.scale * acc.balance / 1000, 4))
        for p in list(acc.pos):
            tp = p[2] + p[3][0]                           # extra = (spacing at entry, highest)
            if h >= tp:
                acc.close(p, tp); continue
            p[3][1] = max(p[3][1], h)
            if p[3][1] - p[2] >= self.trail_arm and l <= p[3][1] - self.trail_frac:
                acc.close(p, max(p[3][1] - self.trail_frac, l))
        if not self.allow or len(acc.pos) >= self.maxlev:
            return
        sp = self.sp
        open_levels = {math.floor(p[2] / sp + 1e-9) for p in acc.pos}
        lo, hi = math.floor(l / sp) + 1, math.floor(h / sp)
        n = 0
        for k in range(lo, hi + 1):
            if k not in open_levels and n < 3 and len(acc.pos) < self.maxlev:
                px = k * sp
                acc.open(+1, self.lot, px, [sp, px]); open_levels.add(k); n += 1


def simulate(spacing_key, trend, scale, maxlev, window):
    start_year = WINDOWS[window]
    rows = m15_rows(start_year, window)
    ctx = daily_context(rows)
    mode, val = SPACINGS[spacing_key]
    acc = Account(G.CAPITAL)
    g = BeltGrid(scale, mode, val, trend, maxlev)
    day = None; mkey = None
    month_bal0 = G.CAPITAL; months = []                   # (key, realised %, equity %)
    month_eq0 = G.CAPITAL
    peak = G.CAPITAL; maxdd = 0.0; stops = 0; cycle_start = G.CAPITAL; maxpos = 0
    t0 = datetime(start_year, 7 if window == 'vt24-26' else 1, 1, tzinfo=timezone.utc).timestamp()
    for t, o, h, l, c in rows:
        if t < t0:
            continue
        d = datetime.fromtimestamp(t, timezone.utc)
        if day != d.date():
            if day is not None:
                acc.swap(c)
            day = d.date()
            g.new_day(ctx[day], acc.equity(o))
        key = (d.year, d.month)
        if mkey is None:
            mkey = key
        if key != mkey:
            eq = acc.equity(o)
            months.append((mkey, acc.balance / month_bal0 - 1, eq / month_eq0 - 1))
            month_bal0, month_eq0, mkey = acc.balance, eq, key
        # hard stop on the cycle: equity <= 40% of what the cycle started with
        eq_low = acc.balance + sum(p[0] * (l - p[2]) * p[1] * CONTRACT for p in acc.pos)
        if acc.pos and eq_low <= cycle_start * (1 - HARD_STOP):
            acc.close_all(l); stops += 1; cycle_start = acc.balance
            if acc.balance < 200:
                break
        if acc.check_stop_out(l, h, t):
            stops += 1; cycle_start = acc.balance
            if acc.balance < 200:
                break
        if not acc.pos:
            cycle_start = max(cycle_start, acc.balance) if stops == 0 else acc.balance
        g.on_bar(acc, o, h, l, c)
        maxpos = max(maxpos, len(acc.pos))
        eq = acc.equity(c); peak = max(peak, eq); maxdd = max(maxdd, 1 - eq / peak)
    eq = acc.equity(rows[-1][4])
    months.append((mkey, acc.balance / month_bal0 - 1, eq / month_eq0 - 1))
    real = np.array([m[1] for m in months]); eqm = np.array([m[2] for m in months])
    years = len(months) / 12
    return {
        "spacing": spacing_key, "trend": trend, "scale": scale, "max_levels": maxlev, "window": window,
        "months": len(months),
        "real_avg_mo_pct": round(float(real.mean()) * 100, 2),
        "real_ge2_pct": round(float((real >= 0.02).mean()) * 100),
        "real_pos_pct": round(float((real > 0).mean()) * 100),
        "eq_avg_mo_pct": round(float(eqm.mean()) * 100, 2),
        "eq_pos_pct": round(float((eqm > 0).mean()) * 100),
        "worst_eq_mo_pct": round(float(eqm.min()) * 100, 1),
        "final_equity_pct": round((eq / G.CAPITAL - 1) * 100, 1),
        "cagr_pct": round(((eq / G.CAPITAL) ** (1 / max(years, 1e-9)) - 1) * 100, 1) if eq > 0 else -100,
        "max_dd_pct": round(maxdd * 100, 1), "max_open": maxpos, "hard_stops": stops, "trades": acc.n_trades,
    }


def _run(job):
    t0 = time.time()
    try:
        r = simulate(*job)
    except Exception as e:
        r = {"spacing": job[0], "trend": job[1], "scale": job[2], "max_levels": job[3], "window": job[4], "error": repr(e)}
    r["secs"] = round(time.time() - t0)
    print(f"  {job[4]:<8s} {job[0]:<7s} {job[1]:<9s} {job[2]:<7} L{job[3]:<3d} "
          f"real={r.get('real_avg_mo_pct','?'):>+6}%/mo >=2%:{r.get('real_ge2_pct','?'):>3}% DD={r.get('max_dd_pct','?'):>5}% "
          f"stops={r.get('hard_stops','?')} {r['secs']}s", flush=True)
    return r


def fmt(rows):
    out = []
    for w in WINDOWS:
        out.append(f"=== {w} ===  (realised = balance change; DD = equity incl. open positions; hard stop 60%)")
        out.append(f"{'spacing':<8s}{'trend':<10s}{'scale':>7s}{'lev':>5s}{'real/mo':>8s}{'>=2%':>6s}{'>0':>5s}{'eq/mo':>7s}"
                   f"{'worst':>7s}{'CAGR':>7s}{'maxDD':>7s}{'maxpos':>7s}{'stops':>6s}{'trades':>7s}")
        rs = [r for r in rows if r["window"] == w and "error" not in r]
        rs.sort(key=lambda r: (r["hard_stops"] == 0, r["max_dd_pct"] <= 60, r["real_ge2_pct"], r["real_avg_mo_pct"]), reverse=True)
        for r in rs:
            out.append(f"{r['spacing']:<8s}{r['trend']:<10s}{r['scale']:>7}{r['max_levels']:>5d}{r['real_avg_mo_pct']:>+8.2f}"
                       f"{r['real_ge2_pct']:>5d}%{r['real_pos_pct']:>4d}%{r['eq_avg_mo_pct']:>+7.2f}{r['worst_eq_mo_pct']:>+7.1f}"
                       f"{r['cagr_pct']:>+7.1f}{r['max_dd_pct']:>6.1f}%{r['max_open']:>7d}{r['hard_stops']:>6d}{r['trades']:>7d}")
        out.append("")
    errs = [r for r in rows if "error" in r]
    if errs:
        out.append("ERRORS: " + "; ".join(str(r) for r in errs[:5]))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    jobs = [(sp, tr, sc, lev, w) for w in WINDOWS for sp in SPACINGS for tr in TRENDS for sc in SCALES for lev in MAXLEV]
    if args.quick:
        jobs = [j for j in jobs if j[4] == "2023-26"]
    jobs.sort(key=lambda j: WINDOWS[j[4]])
    print(f"{len(jobs)} jobs on {args.workers} workers", flush=True)
    t0 = time.time()
    with Pool(args.workers) as pool:
        rows = pool.map(_run, jobs, chunksize=1)
    print(f"done in {time.time() - t0:.0f}s")
    txt = fmt(rows)
    print("\n" + txt)
    with open(os.path.join(REPORTS, "grid_belt.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    with open(os.path.join(REPORTS, "grid_belt.json"), "w") as f:
        json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
