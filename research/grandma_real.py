"""
Hero Gold / Grandma (KZM buy-only grid), calibrated from its Myfxbook record
======================================================================
Rules read off the live trade list (2026-09-22):
  * price levels every `spacing` $ (currently $7; vendor says 1,000 pts, dynamic)
  * a BUY at market whenever price crosses a level that has no open position
    (a level is re-bought as soon as its previous position has closed)
  * exit: take-profit at the next level up (+spacing), or a tight trailing
    stop once the trade is in profit (tiny +0.06..+0.5 exits are common)
  * never sells, no stop loss; base lot 0.0001 std per ~$1,100-1,600
Windows: live (Jul-2024->), 2015-26, 2004-26 at live scale x1 / x2 / x5.
"""
import sys, os, json, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_lab as G
from multiprocessing import Pool

G.LEVERAGE = 2000


class GrandmaReal:
    def __init__(self, lot, spacing=7.0, trail_arm=1.5, trail=1.4):
        self.lot, self.sp, self.arm, self.trail = lot, spacing, trail_arm, trail
        self.prev = None
        self.deepest = 0

    def on_bar(self, acc, o, h, l, c, i):
        # exits: TP at entry+spacing (intrabar), else trailing once armed
        for p in list(acc.pos):
            tp = p[2] + self.sp
            if h >= tp:
                acc.close(p, tp)
                continue
            p[3] = max(p[3], h)
            if p[3] - p[2] >= self.arm and l <= p[3] - self.trail:
                acc.close(p, max(p[3] - self.trail, l))
        # entries: every level crossed within this bar that has no open position
        open_levels = {math.floor(p[2] / self.sp + 1e-9) for p in acc.pos}
        lo, hi = math.floor(l / self.sp) + 1, math.floor(h / self.sp)
        n = 0
        for k in range(lo, hi + 1):
            if k not in open_levels and n < 3:      # cap fills per bar (M15)
                px = k * self.sp
                acc.open(+1, self.lot, px, px)
                open_levels.add(k)
                n += 1
        self.deepest = max(self.deepest, len(acc.pos))


def run(args):
    label, start, scale, spacing = args
    G.WINDOWS[label] = start
    G.SYSTEMS["grandma_real"] = lambda lot: GrandmaReal(lot, spacing)
    r = G.simulate("grandma_real", scale, label)
    r["spacing"] = spacing
    print(f"  {label:<8s} scale={scale:<8} sp=${spacing:<3.0f} final={r['final_pct']:+7.1f}% avg_mo={r['avg_month_pct']:+.2f}% "
          f"mo>0={r['months_pos_pct']}% worst_mo={r['worst_month_pct']}% DD={r['max_dd_pct']}% maxpos={r['max_open_positions']} "
          f"trades/mo={r['trades']/max(r['years']*12,1):.0f} ruin={r['ruined'] or '-'}", flush=True)
    return r


if __name__ == "__main__":
    live_scale = 0.0001 / 1.3        # 0.0001 lot on ~$1,300 avg balance
    jobs = [(label, start, round(live_scale * k, 6), sp)
            for sp in (7.0, 14.0)
            for label, start in (("live24", 2024), ("2015-26", 2015), ("2004-26", 2004))
            for k in (1, 2, 5)]
    with Pool(11) as p:
        rows = p.map(run, jobs, chunksize=1)
    with open(os.path.join(G.REPORTS, "grandma_real.json"), "w") as f:
        json.dump(rows, f, indent=1)
