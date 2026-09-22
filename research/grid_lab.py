"""
Grid / Martingale lab — the commercial "gold EA" archetypes on 22 years of gold
======================================================================
Re-creates, from their public descriptions, the three families of gold EAs
that are sold as "2-5%/month, DD 30-40%, backtested 3 years":

  grandma  BUY-ONLY GRID (KZM): a new buy every `spacing` $ of price travel,
           each closed at +spacing with a trailing stop; never sells, no SL.
  devil    TREND + DYNAMIC MARTINGALE: enter with the H1 EMA trend, take a
           tiny fixed profit (100 points = $1); when the market goes against
           the basket by `step` $, add a bigger lot (x`mult`); the whole basket
           closes at break-even + $1. No SL.
  slayer   HEDGE + MARTINGALE: buy AND sell at once, one side 2x bigger; if
           the basket is under water and price moves `step` $ against the big
           side, double that side; close the whole set the moment it is
           positive, then reopen with the big side flipped.

All are simulated on M15 bars (intrabar high/low touches) with a REAL account
model: fixed lots per `capital`, VT Markets 1:500 margin, 50% stop-out (the
broker force-closes everything = the account is effectively dead), swap
charged daily as a % of notional, spread $0.25 + slippage $0.05 per fill.

Nothing is compounded: lots are fixed for the run, so "return" is relative to
starting capital — how the vendors quote their scales ("$500 per 0.01 lot").
Lot scale is swept as std lots per $1,000 of capital; the vendor's cent-account
scale "$500 / 0.01 cent lot" = 0.0001 std lot = 0.0002 lots per $1,000.

Judged on THREE windows so the "3-year backtest" effect is visible:
  2023-2026 (the vendors' window: gold +170%, no deep pullback)
  2015-2026 (11 yrs incl. 2018-19 chop, 2020 crash, 2021-22 range)
  2004-2026 (22 yrs incl. the 2011-2015 bear: $1,920 -> $1,050, and Apr-2013)

    ./env/Scripts/python.exe research/grid_lab.py [--workers 11]
Writes reports/grid_lab.txt / .json
"""

from __future__ import annotations

import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

import argparse
import json
import os
import time
from datetime import datetime, timezone
from multiprocessing import Pool

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")

CAPITAL = 10_000.0
CONTRACT = 100.0                 # oz per std lot
LEVERAGE = 500
STOP_OUT = 0.50                  # margin level at which the broker closes all
SPREAD, SLIP = 0.25, 0.05
SWAP_LONG_PA, SWAP_SHORT_PA = -0.067, +0.029   # per year, % of notional (VT ECN)
WINDOWS = {"2023-26": 2023, "2015-26": 2015, "2004-26": 2004}
SCALES = (0.0002, 0.0005, 0.001, 0.002, 0.005, 0.01)   # std lots per $1,000


# --------------------------------------------------------------------------- #
# Account model                                                                #
# --------------------------------------------------------------------------- #
class Account:
    def __init__(self, capital: float):
        self.balance = capital
        self.pos: list[list] = []          # [side(+1/-1), lots, entry, extra]
        self.ruined_at: int | None = None
        self.n_trades = 0

    def open(self, side: int, lots: float, mid: float, extra=None):
        px = mid + side * (SPREAD / 2 + SLIP)
        self.pos.append([side, lots, px, extra])
        self.n_trades += 1

    def close(self, p, mid: float):
        px = mid - p[0] * (SPREAD / 2 + SLIP)
        self.balance += p[0] * (px - p[2]) * p[1] * CONTRACT
        self.pos.remove(p)

    def close_all(self, mid: float):
        for p in list(self.pos):
            self.close(p, mid)

    def floating(self, mid: float) -> float:
        return sum(p[0] * (mid - p[2]) * p[1] * CONTRACT for p in self.pos)

    def equity(self, mid: float) -> float:
        return self.balance + self.floating(mid)

    def margin(self, mid: float) -> float:
        # hedged lots net out on most brokers; charge the larger side
        b = sum(p[1] for p in self.pos if p[0] > 0)
        s = sum(p[1] for p in self.pos if p[0] < 0)
        return max(b, s) * CONTRACT * mid / LEVERAGE

    def swap(self, mid: float):
        for p in self.pos:
            pa = SWAP_LONG_PA if p[0] > 0 else SWAP_SHORT_PA
            self.balance += pa / 365 * p[1] * CONTRACT * mid

    def check_stop_out(self, low_mid: float, high_mid: float, t: int) -> bool:
        """Worst intrabar mark: longs at the low, shorts at the high."""
        if not self.pos:
            return False
        eq = self.balance + sum(p[0] * ((low_mid if p[0] > 0 else high_mid) - p[2])
                                * p[1] * CONTRACT for p in self.pos)
        m = self.margin(low_mid)
        if eq <= 0 or (m > 0 and eq / m <= STOP_OUT):
            worst = low_mid if any(p[0] > 0 for p in self.pos) else high_mid
            self.close_all(worst)
            self.balance = max(self.balance, 0.0)
            self.ruined_at = t
            return True
        return False


# --------------------------------------------------------------------------- #
# The three archetypes                                                         #
# --------------------------------------------------------------------------- #
class Grandma:
    """Buy-only grid with per-position take-profit + trailing."""
    def __init__(self, lot, spacing=10.0, trail_frac=0.5):
        self.lot, self.spacing, self.trail = lot, spacing, spacing * trail_frac
        self.last_level = None

    def on_bar(self, acc: Account, o, h, l, c, i):
        # exits first: TP at +spacing, or a trailing stop once past +spacing
        for p in list(acc.pos):
            hi = p[3] = max(p[3], h)                    # extra = highest seen
            if hi - p[2] >= self.spacing and l <= hi - self.trail:
                acc.close(p, max(hi - self.trail, l))
        # entries: a new buy each time price is `spacing` away from every open
        # entry and from the last fill level (market orders as price passes)
        if self.last_level is None:
            self.last_level = c
            acc.open(+1, self.lot, c, c)
            return
        if abs(c - self.last_level) >= self.spacing and all(
                abs(c - p[2]) >= self.spacing * 0.99 for p in acc.pos):
            self.last_level = c
            acc.open(+1, self.lot, c, c)


class Devil:
    """Trend entry, $1 take profit, martingale adds every `step` $ against."""
    def __init__(self, lot, step=5.0, mult=1.5, tp=1.0, ema_fast=20, ema_slow=50):
        self.lot, self.step, self.mult, self.tp = lot, step, mult, tp
        self.ef, self.es = ema_fast, ema_slow
        self.ema_f = self.ema_s = None
        self.basket_side = 0

    def on_bar(self, acc: Account, o, h, l, c, i):
        # H1 EMA trend approximated on M15 with 4x longer EMAs
        af, as_ = 2 / (self.ef * 4 + 1), 2 / (self.es * 4 + 1)
        self.ema_f = c if self.ema_f is None else af * c + (1 - af) * self.ema_f
        self.ema_s = c if self.ema_s is None else as_ * c + (1 - as_) * self.ema_s
        if acc.pos:
            side = self.basket_side
            lots = sum(p[1] for p in acc.pos)
            be = sum(p[2] * p[1] for p in acc.pos) / lots
            target = be + side * self.tp
            touched = h >= target if side > 0 else l <= target
            if touched:
                acc.close_all(target)
                return
            worst = min(p[2] for p in acc.pos) if side > 0 else max(p[2] for p in acc.pos)
            adverse = (worst - l) if side > 0 else (h - worst)
            if adverse >= self.step:
                acc.open(side, acc.pos[-1][1] * self.mult,
                         worst - side * self.step)
            return
        if i < self.es * 4:
            return
        side = +1 if self.ema_f > self.ema_s else -1
        self.basket_side = side
        acc.open(side, self.lot, c)


class Slayer:
    """Hedge pair, big side martingales, close the set when positive."""
    def __init__(self, lot, step=10.0, mult=2.0):
        self.lot, self.step, self.mult = lot, step, mult
        self.big = +1

    def on_bar(self, acc: Account, o, h, l, c, i):
        if not acc.pos:
            acc.open(self.big, self.lot * 2, c)
            acc.open(-self.big, self.lot, c)
            return
        # close the whole set as soon as it is positive (checked at the bar's
        # favourable extreme for the big side, conservatively at close)
        if acc.floating(c) > 0:
            acc.close_all(c)
            self.big = -self.big
            return
        bigs = [p for p in acc.pos if p[0] == self.big]
        worst = min(p[2] for p in bigs) if self.big > 0 else max(p[2] for p in bigs)
        adverse = (worst - l) if self.big > 0 else (h - worst)
        if adverse >= self.step:
            acc.open(self.big, bigs[-1][1] * self.mult, worst - self.big * self.step)


SYSTEMS = {
    "grandma_grid10_trail": lambda lot: Grandma(lot, 10.0),
    "grandma_grid20_trail": lambda lot: Grandma(lot, 20.0),
    "devil_step5_x1.5": lambda lot: Devil(lot, 5.0, 1.5),
    "devil_step10_x2.0": lambda lot: Devil(lot, 10.0, 2.0),
    "slayer_step10_x2": lambda lot: Slayer(lot, 10.0, 2.0),
}


# --------------------------------------------------------------------------- #
def load_arrays(start_year: int):
    from histdata import load_m15
    b = load_m15(start_year=start_year)
    return (np.array([int(x.time.timestamp()) for x in b]),
            np.array([x.open for x in b]), np.array([x.high for x in b]),
            np.array([x.low for x in b]), np.array([x.close for x in b]))


def simulate(system: str, scale: float, window: str) -> dict:
    t, o, h, l, c = load_arrays(WINDOWS[window])
    lot = round(scale * CAPITAL / 1000, 4)
    acc = Account(CAPITAL)
    sys_ = SYSTEMS[system](lot)
    day = None
    monthly, month_start, mkey = [], CAPITAL, None
    peak, maxdd, max_pos = CAPITAL, 0.0, 0
    for i in range(len(t)):
        d = datetime.fromtimestamp(t[i], timezone.utc)
        if day != d.date():
            if day is not None:
                acc.swap(c[i])
            day = d.date()
        key = (d.year, d.month)
        if mkey is None:
            mkey = key
        if key != mkey:
            eq = acc.equity(o[i])
            monthly.append((mkey, eq / month_start - 1))
            month_start, mkey = eq, key
        if acc.check_stop_out(l[i], h[i], t[i]):
            break
        sys_.on_bar(acc, o[i], h[i], l[i], c[i], i)
        max_pos = max(max_pos, len(acc.pos))
        eq = acc.equity(c[i])
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    if acc.ruined_at is None:
        eq = acc.equity(c[-1])
        monthly.append((mkey, eq / month_start - 1))
        acc.close_all(c[-1])
    rets = np.array([r for _, r in monthly]) if monthly else np.array([0.0])
    years = max(len(monthly) / 12, 1e-9)
    final = acc.balance
    return {
        "system": system, "scale": scale, "lot": lot, "window": window,
        "final_pct": round((final / CAPITAL - 1) * 100, 1),
        "avg_month_pct": round(float(rets.mean()) * 100, 2),
        "months_pos_pct": round(float((rets > 0).mean()) * 100),
        "worst_month_pct": round(float(rets.min()) * 100, 1),
        "max_dd_pct": round(maxdd * 100, 1),
        "max_open_positions": max_pos,
        "trades": acc.n_trades,
        "ruined": (datetime.fromtimestamp(acc.ruined_at, timezone.utc).strftime("%Y-%m")
                   if acc.ruined_at else ""),
        "years": round(years, 1),
    }


def _run(job):
    t0 = time.time()
    try:
        r = simulate(*job)
    except Exception as e:
        r = {"system": job[0], "scale": job[1], "window": job[2], "error": repr(e)}
    r["secs"] = round(time.time() - t0)
    print(f"  {r['system']:<22s} {r['scale']:<7} {r['window']}  "
          f"final={r.get('final_pct', '?')}% dd={r.get('max_dd_pct', '?')}% "
          f"ruin={r.get('ruined') or '-'}  {r['secs']}s", flush=True)
    return r


def fmt(rows):
    out = []
    for w in WINDOWS:
        out.append(f"=== window {w} (start ${CAPITAL:,.0f}, fixed lots, no compounding) ===")
        out.append(f"{'system':<22s}{'lots/$1k':>9s}{'lot':>8s}{'total%':>9s}{'avg mo%':>8s}"
                   f"{'mo>0':>6s}{'worst mo':>9s}{'maxDD%':>8s}{'max pos':>8s}{'trades':>8s}{'RUINED':>9s}")
        for r in rows:
            if r["window"] != w or "error" in r:
                continue
            out.append(f"{r['system']:<22s}{r['scale']:>9}{r['lot']:>8}{r['final_pct']:>+9.1f}"
                       f"{r['avg_month_pct']:>+8.2f}{r['months_pos_pct']:>5d}%{r['worst_month_pct']:>+9.1f}"
                       f"{r['max_dd_pct']:>8.1f}{r['max_open_positions']:>8d}{r['trades']:>8d}"
                       f"{r['ruined'] or '-':>9s}")
        out.append("")
    errs = [r for r in rows if "error" in r]
    if errs:
        out.append("ERRORS: " + "; ".join(f"{r['system']} {r['scale']} {r['window']}: {r['error']}" for r in errs))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--systems", default=",".join(SYSTEMS))
    args = ap.parse_args()
    jobs = [(s, sc, w) for w in WINDOWS for s in args.systems.split(",") for sc in SCALES]
    # longest windows first
    jobs.sort(key=lambda j: WINDOWS[j[2]])
    print(f"{len(jobs)} jobs on {args.workers} workers", flush=True)
    t0 = time.time()
    with Pool(args.workers) as pool:
        rows = pool.map(_run, jobs, chunksize=1)
    print(f"done in {time.time() - t0:.0f}s")
    rows.sort(key=lambda r: (list(WINDOWS).index(r["window"]), r["system"], r["scale"]))
    txt = ("GRID / MARTINGALE LAB — XAUUSD M15, VT 1:500, stop-out 50%, spread .25+.05, "
           f"swap {SWAP_LONG_PA:+.1%}/{SWAP_SHORT_PA:+.1%} pa\n"
           "lots/$1k = std lots per $1,000 capital (vendor '$500 per 0.01 cent lot' = 0.0002)\n\n"
           + fmt(rows))
    print("\n" + txt)
    with open(os.path.join(REPORTS, "grid_lab.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    with open(os.path.join(REPORTS, "grid_lab.json"), "w") as f:
        json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
