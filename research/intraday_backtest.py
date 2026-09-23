"""
Intraday (day-trading) bot backtest with a DAILY TARGET / DAILY STOP
======================================================================
Runs the live `intraday_momentum` / `intraday_pullback` classes (strategies.py)
on every closed M15 bar with a 200-bar window (= what main_loop hands over),
fills on the next M15 bars (stop checked before target inside a bar), cost
charged at entry, one trade at a time, risk `--risk` of equity per trade,
compounding. A DailyGoal manager mirrors main_loop's --daily-target-pct /
--daily-stop-pct: once the day's P/L (realised + floating) reaches +target
the bot locks it in (closes the open trade) and stops for the day; at
-stop it closes and stops too. Open trades are flattened at the day's last
bar (no overnight).

    ./env/Scripts/python.exe research/intraday_backtest.py [--start 2015] [--end 2026]
          [--cost 0.25] [--risk 0.005] [--sweep] [--goals] [--detail <label>]

Reports: trades/day, entries per hour of day, % days target hit / stop hit /
positive, WR, W/L, PF, yearly + monthly tables, max DD.
Writes reports/intraday.txt (+ _trades.csv, _daily.csv).
"""
from __future__ import annotations

import sys, os, argparse, csv, time
from collections import defaultdict, Counter
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15
from strategies import IntradayMomentum, IntradayPullback, SessionRangeBreakout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
WINDOW = 200
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
WD = "Mon Tue Wed Thu Fri Sat Sun".split()


class DailyGoal:
    """target/stop as fractions of the day's starting equity (0 = off)."""
    def __init__(self, target=0.0, stop=0.0, eod_flat=True):
        self.target, self.stop, self.eod_flat = target, stop, eod_flat

    def state(self, day_pl_frac):
        if self.target and day_pl_frac >= self.target:
            return "target"
        if self.stop and day_pl_frac <= -self.stop:
            return "stop"
        return None


def _ema(x, n):
    k = 2.0 / (n + 1); out = np.empty_like(x); out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def simulate(st, bars, t, o, h, l, c, day, cost, risk, goal: DailyGoal):
    n = len(t)
    eq_curve = np.empty(n); eq = 1.0
    trades = []
    # cheap vectorised pre-filter so the (slow) class is only called where it could fire
    if isinstance(st, SessionRangeBreakout):
        hrs = np.array([datetime.fromtimestamp(int(x), timezone.utc).hour for x in t])
        cand = np.zeros(n, dtype=bool)
        for op in st.opens:          # bars from range close to window end
            for off in range(st.range_bars, st.range_bars + st.window_bars):
                cand |= np.roll(hrs == op, off) & (np.roll(t % 3600 == 0, off))
    elif isinstance(st, IntradayMomentum):
        k = st.n
        rmax = np.full(n, np.inf); rmin = np.full(n, -np.inf)
        for i in range(k, n):
            rmax[i] = h[i - k:i].max(); rmin[i] = l[i - k:i].min()
        cand = (c > rmax) | (c < rmin)
    else:
        ef = _ema(c, st.fast)
        cand = (l <= ef * 1.0005) | (h >= ef * 0.9995)
    day_start_eq = eq; cur_day = day[WINDOW]; blocked = None
    day_rows = []                       # (day, trades, pl_frac, state)
    day_trades = 0
    i = WINDOW
    pos = None                          # dict when in a trade
    while i < n:
        if day[i] != cur_day:           # new day: record + reset
            day_rows.append((cur_day, day_trades, eq / day_start_eq - 1, blocked))
            cur_day, day_start_eq, blocked, day_trades = day[i], eq, None, 0
        last_of_day = (i == n - 1) or (day[i + 1] != cur_day)
        if pos is None:
            eq_curve[i] = eq
            if blocked is None and cand[i] and not last_of_day:
                win = bars[i - WINDOW + 1:i + 1]
                sig = st.generate("XAUUSD", win, win[-1].time)
                if sig is not None:
                    d = 1 if sig.direction.value == "buy" else -1
                    e = sig.entry + d * cost
                    rd = abs(e - sig.sl)
                    if rd > 0:
                        pos = dict(d=d, e=e, sl=sig.sl, tp=sig.tp, rd=rd, j=i + 1,
                                   hold=0, open_t=t[i + 1] if i + 1 < n else t[i])
                        day_trades += 1
            i += 1
            continue
        # ---- in a trade: evaluate this bar ------------------------------
        d, e, sl, tp, rd = pos["d"], pos["e"], pos["sl"], pos["tp"], pos["rd"]
        res = None; reason = None
        if d > 0:
            if l[i] <= sl: res, reason = sl, "sl"
            elif h[i] >= tp: res, reason = tp, "tp"
        else:
            if h[i] >= sl: res, reason = sl, "sl"
            elif l[i] <= tp: res, reason = tp, "tp"
        pos["hold"] += 1
        if res is None:
            mtm = eq * (1 + risk * d * (c[i] - e) / rd)
            gs = goal.state(mtm / day_start_eq - 1)
            if gs is not None:
                res, reason = c[i], "lock_" + gs
            elif pos["hold"] >= st.max_hold_bars:
                res, reason = c[i], "time"
            elif goal.eod_flat and last_of_day:
                res, reason = c[i], "eod"
        if res is None:
            eq_curve[i] = eq * (1 + risk * d * (c[i] - e) / rd)
            i += 1
            continue
        r = d * (res - e) / rd
        eq *= 1 + risk * r
        eq_curve[i] = eq
        trades.append(dict(open_t=pos["open_t"], close_t=t[i], d=d, e=e, sl=sl, tp=tp,
                           exit=res, r=r, reason=reason, hold=pos["hold"]))
        pos = None
        gs = goal.state(eq / day_start_eq - 1)
        if gs is not None:
            blocked = gs
        i += 1
    day_rows.append((cur_day, day_trades, eq / day_start_eq - 1, blocked))
    eq_curve[:WINDOW] = 1.0
    return trades, eq_curve, day_rows


def stats(trs, risk):
    if not trs:
        return dict(n=0, wr=0, wl=0, pf=0, avg_r=0, maxl=0)
    rs = np.array([x["r"] for x in trs]); w = rs[rs > 0]; lo = rs[rs <= 0]
    maxl = cl = 0
    for r in rs:
        cl = cl + 1 if r <= 0 else 0; maxl = max(maxl, cl)
    aw = w.mean() if len(w) else 0; al = -lo.mean() if len(lo) else 0
    return dict(n=len(rs), wr=len(w) / len(rs), wl=aw / al if al else float("inf"),
                pf=w.sum() / max(-lo.sum(), 1e-9), avg_r=rs.mean(), maxl=maxl)


def max_dd(x):
    return float((1 - x / np.maximum.accumulate(x)).max())


def summary_line(label, trades, eq, day_rows, years, risk):
    s = stats(trades, risk)
    nd = max(len(day_rows), 1)
    tg = sum(1 for r in day_rows if r[3] == "target"); sp = sum(1 for r in day_rows if r[3] == "stop")
    posd = sum(1 for r in day_rows if r[2] > 0); actd = sum(1 for r in day_rows if r[1] > 0)
    tpd = np.mean([r[1] for r in day_rows]) if day_rows else 0
    return (f"{label:<34s} n={s['n']:5d} ({tpd:4.1f}/day) WR={s['wr']*100:5.1f}% W/L={s['wl']:4.2f} PF={s['pf']:4.2f} "
            f"avgR={s['avg_r']:+.3f} CAGR={(eq[-1]**(1/years)-1)*100:+6.1f}% DD={max_dd(eq)*100:5.1f}% "
            f"| days: target {tg/nd*100:4.1f}% stop {sp/nd*100:4.1f}% positive {posd/max(actd,1)*100:4.1f}% of active")


def detail(label, trades, eq, day_rows, t, risk, out):
    P = lambda *a: print(*a, file=out)
    s = stats(trades, risk)
    t0 = datetime.fromtimestamp(int(t[0]), timezone.utc); t1 = datetime.fromtimestamp(int(t[-1]), timezone.utc)
    years = (t1 - t0).days / 365.25
    P(f"\n{'='*100}\nDETAIL: {label}  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}  risk {risk*100:.2f}%/trade\n{'='*100}")
    P(f"trades {s['n']}  win-rate {s['wr']*100:.1f}%  W/L {s['wl']:.2f}  PF {s['pf']:.2f}  expectancy {s['avg_r']:+.3f}R  "
      f"max consecutive losses {s['maxl']}  avg hold {np.mean([x['hold'] for x in trades])*15:.0f} min")
    P(f"total {(eq[-1]-1)*100:+.1f}%  CAGR {(eq[-1]**(1/years)-1)*100:+.1f}%  max DD {max_dd(eq)*100:.1f}%")
    ex = Counter(x["reason"] for x in trades)
    P("exits: " + ", ".join(f"{k} {v} ({v/len(trades)*100:.0f}%)" for k, v in ex.most_common()))
    for nm, dd in (("BUY", 1), ("SELL", -1)):
        ss = stats([x for x in trades if x["d"] == dd], risk)
        P(f"  {nm:<4s} n={ss['n']:5d} WR={ss['wr']*100:5.1f}% PF={ss['pf']:4.2f} avgR={ss['avg_r']:+.3f}")
    # ---- days
    nd = len(day_rows); act = [r for r in day_rows if r[1] > 0]
    tg = [r for r in day_rows if r[3] == "target"]; sp = [r for r in day_rows if r[3] == "stop"]
    pl = np.array([r[2] for r in day_rows])
    P(f"\n--- DAILY --- {nd} trading days, {len(act)} with trades ({np.mean([r[1] for r in day_rows]):.1f} trades/day avg, "
      f"max {max(r[1] for r in day_rows)}/day)")
    P(f"days target hit {len(tg)} ({len(tg)/nd*100:.1f}%)   days stopped out {len(sp)} ({len(sp)/nd*100:.1f}%)   "
      f"positive days {sum(1 for r in act if r[2] > 0)/max(len(act),1)*100:.1f}% of active   "
      f"avg day {pl.mean()*100:+.3f}%  best {pl.max()*100:+.2f}%  worst {pl.min()*100:+.2f}%")
    cl = maxcl = 0
    for v in pl:
        cl = cl + 1 if v < 0 else (0 if v > 0 else cl); maxcl = max(maxcl, cl)
    P(f"max consecutive losing days {maxcl}")
    # entries per hour
    hrs = Counter(datetime.fromtimestamp(int(x["open_t"]), timezone.utc).hour for x in trades)
    P("entries per hour of day (server time) and WR:")
    line = []
    for hr in range(24):
        tt = [x for x in trades if datetime.fromtimestamp(int(x["open_t"]), timezone.utc).hour == hr]
        if tt:
            ss = stats(tt, risk)
            line.append(f"  {hr:02d}h n={ss['n']:4d} WR={ss['wr']*100:4.0f}% avgR={ss['avg_r']:+.2f}")
    P("\n".join(line))
    P("by weekday:")
    for wd in range(5):
        tt = [x for x in trades if datetime.fromtimestamp(int(x["open_t"]), timezone.utc).weekday() == wd]
        if tt:
            ss = stats(tt, risk)
            P(f"  {WD[wd]} n={ss['n']:5d} WR={ss['wr']*100:5.1f}% PF={ss['pf']:4.2f} avgR={ss['avg_r']:+.3f}")
    # ---- yearly / monthly
    years_ = sorted({datetime.fromtimestamp(int(x), timezone.utc).year for x in t})
    yr_of = np.array([datetime.fromtimestamp(int(x), timezone.utc).year for x in t])
    P(f"\n--- YEARLY ---\n{'year':<6s}{'trades':>7s}{'/day':>6s}{'WR%':>7s}{'W/L':>6s}{'PF':>6s}{'avgR':>8s}{'return%':>9s}{'maxDD%':>8s}{'tgt%':>6s}{'stop%':>6s}")
    for y in years_:
        m = yr_of == y; seg = eq[m]; p0 = np.where(m)[0][0] - 1; base = eq[p0] if p0 >= 0 else 1.0
        tt = [x for x in trades if datetime.fromtimestamp(int(x["close_t"]), timezone.utc).year == y]
        dr = [r for r in day_rows if datetime.fromtimestamp(int(r[0]) * 86400, timezone.utc).year == y]
        ss = stats(tt, risk)
        P(f"{y:<6d}{ss['n']:>7d}{(ss['n']/max(len(dr),1)):>6.1f}{ss['wr']*100:>7.1f}{ss['wl']:>6.2f}{ss['pf']:>6.2f}{ss['avg_r']:>+8.3f}"
          f"{(seg[-1]/base-1)*100:>+9.1f}{max_dd(np.concatenate([[base], seg]))*100:>8.1f}"
          f"{sum(1 for r in dr if r[3]=='target')/max(len(dr),1)*100:>6.0f}{sum(1 for r in dr if r[3]=='stop')/max(len(dr),1)*100:>6.0f}")
    ym = np.array([datetime.fromtimestamp(int(x), timezone.utc).year * 100 + datetime.fromtimestamp(int(x), timezone.utc).month for x in t])
    mret = {}; prev = 1.0
    for key in sorted(set(ym)):
        last = eq[ym == key][-1]; mret[key] = last / prev - 1; prev = last
    pos = sum(1 for v in mret.values() if v > 0)
    P(f"\n--- MONTHLY --- {pos}/{len(mret)} months positive ({pos/len(mret)*100:.0f}%), avg {np.mean(list(mret.values()))*100:+.2f}%, "
      f"best {max(mret.values())*100:+.1f}%, worst {min(mret.values())*100:+.1f}%")
    P(f"{'year':<6s}" + "".join(f"{m:>7s}" for m in MONTHS) + f"{'YEAR':>8s}")
    for y in years_:
        cells = [f"{mret[y*100+mo]*100:>+7.1f}" if (y * 100 + mo) in mret else f"{'':>7s}" for mo in range(1, 13)]
        yr = np.prod([1 + mret[k] for k in mret if k // 100 == y]) - 1
        P(f"{y:<6d}" + "".join(cells) + f"{yr*100:>+8.1f}")
    with open(os.path.join(REPORTS, "intraday_trades.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["open", "close", "dir", "entry", "sl", "tp", "exit", "r", "reason", "hold_min"])
        for x in trades:
            w.writerow([datetime.fromtimestamp(int(x["open_t"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(int(x["close_t"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "buy" if x["d"] > 0 else "sell", f"{x['e']:.2f}", f"{x['sl']:.2f}", f"{x['tp']:.2f}",
                        f"{x['exit']:.2f}", f"{x['r']:.3f}", x["reason"], x["hold"] * 15])
    with open(os.path.join(REPORTS, "intraday_daily.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "trades", "pl_pct", "state"])
        for r in day_rows:
            w.writerow([datetime.fromtimestamp(int(r[0]) * 86400, timezone.utc).date(), r[1], f"{r[2]*100:.3f}", r[3] or ""])


def variants(args):
    M, Pb = IntradayMomentum, IntradayPullback
    g = DailyGoal(args.target, args.stop)
    if args.session:
        S = SessionRangeBreakout
        return [
            ("orb_lon9+ny16_r4_w12_rr1.5",     S(), g),
            ("orb_lon9+ny16_rr1.0",            S(rr=1.0), g),
            ("orb_lon9+ny16_rr2.0",            S(rr=2.0), g),
            ("orb_lon9+ny16_r2_w8",            S(range_bars=2, window_bars=8), g),
            ("orb_lon9+ny16_r8_w16",           S(range_bars=8, window_bars=16), g),
            ("orb_lon9_only",                  S(opens=(9,)), g),
            ("orb_ny16_only",                  S(opens=(16,)), g),
            ("orb_asia1+lon9+ny16",            S(opens=(1, 9, 16)), g),
            ("orb_lon9+ny16_buy",              S(side="buy"), g),
            ("orb_lon9+ny16_nogoal",           S(), DailyGoal(0, 0)),
            ("orb_lon9+ny16_sl1atr",           S(max_sl_atr=1.0), g),
        ]
    if args.goals:      # daily-goal sweep on the base rule
        out = []
        for tg, sp in ((0, 0), (0.005, 0.01), (0.01, 0.01), (0.01, 0.02), (0.02, 0.02), (0.005, 0.005), (0.015, 0.015)):
            out.append((f"mom_n8_rr1.5 tgt{tg*100:.1f}% stop{sp*100:.1f}%", M(), DailyGoal(tg, sp)))
        return out
    if args.sweep:
        out = [
            ("mom_n8_rr1.5 (base)",            M(), g),
            ("mom_n4_rr1.5 (hourly pace)",     M(n=4), g),
            ("mom_n12_rr2",                    M(n=12, rr=2.0), g),
            ("mom_n8_rr1.0",                   M(rr=1.0), g),
            ("mom_n8_rr2.5",                   M(rr=2.5), g),
            ("mom_n8_sl1.5_rr1.5",             M(sl_atr=1.5), g),
            ("mom_n8_sl0.7_rr2",               M(sl_atr=0.7, rr=2.0), g),
            ("mom_n8_rr1.5_buy",               M(side="buy"), g),
            ("mom_n8_rr1.5_lon+ny(8-21h)",     M(sessions=((8, 21),)), g),
            ("mom_n8_rr1.5_ema30/150",         M(fast=30, slow=150), g),   # slow must fit the 200-bar window
            ("mom_n8_rr1.5_hold32",            M(max_hold_bars=32), g),
            ("mom_n8_rr1.5_minatr0.05%",       M(min_atr_frac=0.0005), g),
            ("pull_rr1.2 (base)",              Pb(), g),
            ("pull_rr1.0",                     Pb(rr=1.0), g),
            ("pull_rr2.0",                     Pb(rr=2.0), g),
            ("pull_rr1.2_buy",                 Pb(side="buy"), g),
            ("pull_rr1.2_lon+ny(8-21h)",       Pb(sessions=((8, 21),)), g),
            ("pull_sl1.5_rr1.2",               Pb(sl_atr=1.5), g),
        ]
        return out
    return [("intraday_momentum (default)", M(), g), ("intraday_pullback (default)", Pb(), g)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2015); ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--cost", type=float, default=0.25, help="$/oz round trip charged at entry")
    ap.add_argument("--risk", type=float, default=0.005, help="fraction of equity per trade")
    ap.add_argument("--target", type=float, default=0.01, help="daily target (fraction), 0 = off")
    ap.add_argument("--stop", type=float, default=0.01, help="daily stop (fraction), 0 = off")
    ap.add_argument("--sweep", action="store_true"); ap.add_argument("--goals", action="store_true")
    ap.add_argument("--session", action="store_true", help="opening-range breakout variants")
    ap.add_argument("--detail", default="intraday_momentum (default)")
    ap.add_argument("--out", default="intraday.txt")
    args = ap.parse_args()
    t_start = time.time()
    bars = load_m15(start_year=args.start, end_year=args.end)
    t = np.array([int(b.time.timestamp()) for b in bars]); o = np.array([b.open for b in bars])
    h = np.array([b.high for b in bars]); l = np.array([b.low for b in bars]); c = np.array([b.close for b in bars])
    day = t // 86400
    years = (t[-1] - t[0]) / 86400 / 365.25
    out = open(os.path.join(REPORTS, args.out), "w", encoding="utf-8")

    class Tee:
        def write(self, s): sys.stdout.write(s); out.write(s)
        def flush(self): sys.stdout.flush(); out.flush()
    tee = Tee(); P = lambda *a: print(*a, file=tee)
    P(f"Intraday bot — XAUUSD M15 {bars[0].time:%Y-%m-%d} -> {bars[-1].time:%Y-%m-%d} ({len(bars)} bars, {years:.1f} yr), "
      f"cost ${args.cost}/oz, risk {args.risk*100:.2f}%/trade, daily target {args.target*100:.1f}% / stop {args.stop*100:.1f}%, "
      f"flat at day end, {WINDOW}-bar window (= live)\n")
    results = {}
    for label, st, goal in variants(args):
        trades, eq, days = simulate(st, bars, t, o, h, l, c, day, args.cost, args.risk, goal)
        results[label] = (trades, eq, days)
        P(summary_line(label, trades, eq, days, years, args.risk)); tee.flush()
    if args.detail in results:
        detail(args.detail, *results[args.detail], t, args.risk, tee)
    P(f"\ndone in {time.time()-t_start:.0f}s")
    out.close()


if __name__ == "__main__":
    main()
