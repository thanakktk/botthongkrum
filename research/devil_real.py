"""
Gold Devil, calibrated from its Myfxbook record (members/Chin156/hero-gold-devil)
======================================================================
Rules read off the live trade list + stats (2026-09-22):
  * base lot 0.01 cent = 0.0001 std lot on a $1,160 deposit (1:2000 leverage)
  * direction = short-term trend (EMA on M15); one basket at a time
  * adds every `step` $ against the basket; ladder multipliers
    [1,1,1,2,2,4,4,8,8,16...] (same lot for the first levels, then doubling —
    the Jun-11 basket reached ~10-25x base)
  * whole basket closes when net floating >= tp_usd_per_base_lot x base lot
    (observed ~USC 1.6-1.9 per 0.01 cent lot)
  * no stop loss anywhere
Runs it on the live window (to check DD vs Myfxbook's 34%) and on 2015-26 /
2004-26 at the live scale and 2x / 5x.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_lab as G
from grid_lab import Account, CONTRACT
from multiprocessing import Pool
import numpy as np

G.LEVERAGE = 2000
LADDER = [1, 1, 1, 2, 2, 4, 4, 8, 8, 16, 16, 32, 32]


class DevilReal:
    def __init__(self, lot, step=4.0, tp_per_base=1.6, cap=None):
        self.lot, self.step, self.tp = lot, step, tp_per_base * lot * CONTRACT
        self.cap = cap or len(LADDER)
        self.ef = self.es = None
        self.side = 0
        self.deepest = 0

    def on_bar(self, acc, o, h, l, c, i):
        af, as_ = 2 / 81, 2 / 201
        self.ef = c if self.ef is None else af * c + (1 - af) * self.ef
        self.es = c if self.es is None else as_ * c + (1 - as_) * self.es
        if acc.pos:
            side = self.side
            # basket TP: floating at the favourable extreme
            fav = h if side > 0 else l
            if acc.floating(fav) >= self.tp:
                # fill at the price where floating == tp (linear), bounded by extreme
                lots = sum(p[1] for p in acc.pos)
                be = sum(p[2] * p[1] for p in acc.pos) / lots
                px = be + side * self.tp / (lots * CONTRACT)
                acc.close_all(px)
                return
            n = len(acc.pos)
            worst = min(p[2] for p in acc.pos) if side > 0 else max(p[2] for p in acc.pos)
            adverse = (worst - l) if side > 0 else (h - worst)
            if adverse >= self.step and n < self.cap:
                mult = LADDER[min(n, len(LADDER) - 1)]
                acc.open(side, self.lot * mult, worst - side * self.step)
                self.deepest = max(self.deepest, n + 1)
            return
        if i < 200:
            return
        self.side = +1 if self.ef > self.es else -1
        acc.open(self.side, self.lot, c)


def run(args):
    label, start, scale, step = args
    G.WINDOWS[label] = start
    G.SYSTEMS["devil_real"] = lambda lot: DevilReal(lot, step)
    r = G.simulate("devil_real", scale, label)
    r["step"] = step
    print(f"  {label:<8s} scale={scale:<7} step={step}  final={r['final_pct']:+.1f}% avg_mo={r['avg_month_pct']:+.2f}% "
          f"mo>0={r['months_pos_pct']}% DD={r['max_dd_pct']}% maxpos={r['max_open_positions']} ruin={r['ruined'] or '-'}", flush=True)
    return r


if __name__ == "__main__":
    live_scale = 0.0001 / 1.16      # 0.0001 lot per $1,160 -> lots per $1k
    jobs = []
    for step in (4.0, 8.0):
        for label, start in (("live25", 2025), ("2015-26", 2015), ("2004-26", 2004)):
            for k in (1, 2, 5):
                jobs.append((label, start, round(live_scale * k, 6), step))
    with Pool(11) as p:
        rows = p.map(run, jobs, chunksize=1)
    with open(os.path.join(G.REPORTS, "devil_real.json"), "w") as f:
        json.dump(rows, f, indent=1)
