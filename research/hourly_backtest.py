"""
Hourly multi-position / hedge bot backtest (the "a trade every hour" set)
======================================================================
Decisions at every closed H1 bar (built clock-aligned from M15), fills and
mark-to-market on the M15 bars, several positions open at once (one new
basket per hour, up to `max_pos`), each position: SL / TP / max hold
(default 4 h), optional hedge leg, optional daily target/stop lock, NO
forced end-of-day flat. Cost charged at entry. Risk per position `--risk`
of equity (default 0.25 % -> 4 concurrent = 1 % at risk).

Rules (all also implementable in an MT5 EA, see mql5/HourlySet.mq5):
  trend      every hour in the EMA20/50/200 (H1) direction
  momo       every hour if |close - close[4]| > k ATR, in that direction
  breakout   H1 close beyond the prior 4-bar range
  straddle   every hour BUY + SELL (hedge), symmetric SL/TP   [lock variant:
             when one leg hits TP the other is closed at market]
  recovery   trend leg; if it goes -0.5 ATR against, open the opposite
             hedge leg; close the basket at +0.2 ATR net or at max hold
  mr         fade |z| > 1.5 of the 24-bar mean, TP at the mean

    ./env/Scripts/python.exe research/hourly_backtest.py [--start 2015] [--end 2026]
          [--cost 0.25] [--risk 0.0025] [--target 0] [--stop 0] [--sweep] [--detail <label>]
Writes reports/hourly.txt (+ _trades.csv).
"""
from __future__ import annotations

import sys, os, argparse, csv, time
from collections import Counter
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15
from backtester import resample_clock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def ema(x, n):
    k = 2.0 / (n + 1); out = np.empty_like(x); out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def atr(h, l, c, n=14):
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    out = np.full(len(c), np.nan)
    for i in range(n, len(c)):
        out[i] = tr[i - n:i].mean()
    return out


class Rule:
    """Decide new orders at H1 index k. Returns list of (dir, sl_dist, tp_dist, tag)."""
    name = "base"
    max_hold_h = 4

    def __init__(self, sl_atr=1.0, rr=1.5, max_hold_h=4, max_pos=4, side="both", **kw):
        self.sl_atr, self.rr, self.max_hold_h, self.max_pos, self.side = sl_atr, rr, max_hold_h, max_pos, side
        for a, b in kw.items():
            setattr(self, a, b)

    def prep(self, H):
        self.e20, self.e50, self.e200 = ema(H["c"], 20), ema(H["c"], 50), ema(H["c"], 200)
        self.atr = atr(H["h"], H["l"], H["c"], 14)
        n = len(H["c"]); self.sma24 = np.full(n, np.nan); self.sd24 = np.full(n, np.nan)
        for i in range(24, n):
            w = H["c"][i - 24:i]; self.sma24[i] = w.mean(); self.sd24[i] = w.std()

    def ok_side(self, d):
        return self.side == "both" or (d > 0 and self.side == "buy") or (d < 0 and self.side == "sell")

    def orders(self, H, k, open_pos):
        raise NotImplementedError


class Trend(Rule):
    name = "trend"
    def orders(self, H, k, open_pos):
        c = H["c"][k]; a = self.atr[k]
        if np.isnan(a) or k < 200: return []
        d = 1 if (self.e20[k] > self.e50[k] and c > self.e200[k]) else (-1 if (self.e20[k] < self.e50[k] and c < self.e200[k]) else 0)
        if d == 0 or not self.ok_side(d): return []
        return [(d, self.sl_atr * a, self.sl_atr * a * self.rr, "trend")]


class Momo(Rule):
    name = "momo"
    k_atr = 0.5
    def orders(self, H, k, open_pos):
        a = self.atr[k]
        if np.isnan(a) or k < 20: return []
        mv = H["c"][k] - H["c"][k - 4]
        if abs(mv) < self.k_atr * a: return []
        d = 1 if mv > 0 else -1
        if not self.ok_side(d): return []
        return [(d, self.sl_atr * a, self.sl_atr * a * self.rr, "momo")]


class Breakout(Rule):
    name = "breakout"
    def orders(self, H, k, open_pos):
        a = self.atr[k]
        if np.isnan(a) or k < 20: return []
        c = H["c"][k]; hi = H["h"][k - 4:k].max(); lo = H["l"][k - 4:k].min()
        d = 1 if c > hi else (-1 if c < lo else 0)
        if d == 0 or not self.ok_side(d): return []
        return [(d, self.sl_atr * a, self.sl_atr * a * self.rr, "brk")]


class Straddle(Rule):
    name = "straddle"
    lock = False
    def orders(self, H, k, open_pos):
        a = self.atr[k]
        if np.isnan(a) or k < 20: return []
        return [(1, self.sl_atr * a, self.sl_atr * a * self.rr, "strad"), (-1, self.sl_atr * a, self.sl_atr * a * self.rr, "strad")]


class Recovery(Trend):
    name = "recovery"
    hedge_at = 0.5        # ATR adverse before the hedge leg opens
    basket_tp = 0.2       # ATR net gain to close the basket
    def orders(self, H, k, open_pos):
        return [(d, sd, td, "lead") for d, sd, td, _ in super().orders(H, k, open_pos)]


class MeanRev(Rule):
    name = "mr"
    z_in = 1.5
    def orders(self, H, k, open_pos):
        a = self.atr[k]; m = self.sma24[k]; s = self.sd24[k]
        if np.isnan(a) or np.isnan(m) or not s > 0: return []
        z = (H["c"][k] - m) / s
        if z < -self.z_in and self.ok_side(1):
            return [(1, self.sl_atr * a, max(m - H["c"][k], 0.2 * a), "mr")]
        if z > self.z_in and self.ok_side(-1):
            return [(-1, self.sl_atr * a, max(H["c"][k] - m, 0.2 * a), "mr")]
        return []


def simulate(rule: Rule, H, t15, h15, l15, c15, cost, risk, target, stop):
    n = len(t15)
    h1_end = H["t"] + 3600
    eq = 1.0; eq_curve = np.empty(n)
    pos = []              # dicts
    trades = []
    hours_total = hours_with_trade = 0
    conc = []
    day = t15 // 86400; cur_day = day[0]; day_start = 1.0; blocked = None
    day_rows = []; day_trades = 0
    kh = 0                # next H1 bar to decide on
    max_hold = rule.max_hold_h * 4
    basket_tp = getattr(rule, "basket_tp", None)
    hedge_at = getattr(rule, "hedge_at", None)
    lock = getattr(rule, "lock", False)
    set_real, set_size = {}, {}          # per set (= open hour): realised P/L, legs opened
    for i in range(n):
        if day[i] != cur_day:
            day_rows.append((cur_day, day_trades, eq / day_start - 1, blocked))
            cur_day, day_start, blocked, day_trades = day[i], eq, None, 0
        # ---- manage open positions on this bar ----
        closed_now = []
        for p in pos:
            d, e, sl, tp, rd = p["d"], p["e"], p["sl"], p["tp"], p["rd"]
            res = None; why = None
            if d > 0:
                if l15[i] <= sl: res, why = sl, "sl"
                elif h15[i] >= tp: res, why = tp, "tp"
            else:
                if h15[i] >= sl: res, why = sl, "sl"
                elif l15[i] <= tp: res, why = tp, "tp"
            p["hold"] += 1
            if res is None and p["hold"] >= max_hold:
                res, why = c15[i], "time"
            if res is not None:
                p["res"], p["why"] = res, why
                closed_now.append(p)
        # straddle lock: a TP on one leg closes its twin at market
        if lock and closed_now:
            for p in closed_now:
                if p["why"] == "tp":
                    for q in pos:
                        if q is not p and q not in closed_now and q["open_t"] == p["open_t"]:
                            q["res"], q["why"] = c15[i], "lock"; closed_now.append(q)
        # generic basket take-profit: every position opened in the same hour is
        # one "set"; when the set's net P/L (in ATR of the lead) >= set_tp -> close all
        # (realised P/L of legs already closed counts: set_real[set_id])
        set_tp = getattr(rule, "set_tp", None)
        if set_tp:
            by_open = {}
            for p in pos:
                if p not in closed_now:
                    by_open.setdefault(p["open_t"], []).append(p)
            for sid, grp in by_open.items():
                if set_size.get(sid, 1) < 2: continue
                net = set_real.get(sid, 0.0) + sum(p["d"] * (c15[i] - p["e"]) for p in grp)
                if net >= set_tp * grp[0]["atr"]:
                    for p in grp:
                        p["res"], p["why"] = c15[i], "set_tp"; closed_now.append(p)
        # recovery basket: hedge leg + net-basket exit
        if hedge_at is not None:
            leads = [p for p in pos if p["tag"] == "lead" and p not in closed_now]
            for p in leads:
                adverse = -p["d"] * (c15[i] - p["e"])
                if not p.get("hedged") and adverse >= hedge_at * p["atr"]:
                    p["hedged"] = True
                    pos.append(dict(d=-p["d"], e=c15[i] - p["d"] * cost, sl=c15[i] - p["d"] * cost + p["d"] * 2 * p["atr"],
                                    tp=c15[i] - p["d"] * cost - p["d"] * 2 * p["atr"], rd=2 * p["atr"], hold=0,
                                    open_t=t15[i], tag="hedge", twin=p, atr=p["atr"], risk_eq=eq))
                    trades_hedge = True
            # net basket check
            for p in leads:
                if p.get("hedged"):
                    q = next((x for x in pos if x.get("twin") is p and x not in closed_now), None)
                    if q is None: continue
                    net = p["d"] * (c15[i] - p["e"]) + q["d"] * (c15[i] - q["e"])
                    if net >= basket_tp * p["atr"] or p["hold"] >= max_hold:
                        for x in (p, q):
                            if x not in closed_now:
                                x["res"], x["why"] = c15[i], "basket"; closed_now.append(x)
        for p in closed_now:
            r = p["d"] * (p["res"] - p["e"]) / p["rd"]
            set_real[p["open_t"]] = set_real.get(p["open_t"], 0.0) + p["d"] * (p["res"] - p["e"])
            eq += p["risk_eq"] * risk * r
            trades.append(dict(open_t=p["open_t"], close_t=t15[i], d=p["d"], r=r, why=p["why"], tag=p["tag"], hold=p["hold"]))
            pos.remove(p)
        # ---- mark to market ----
        fl = sum(p["risk_eq"] * risk * p["d"] * (c15[i] - p["e"]) / p["rd"] for p in pos)
        mtm = eq + fl
        eq_curve[i] = mtm
        if (target or stop) and blocked is None:
            plf = mtm / day_start - 1
            if (target and plf >= target) or (stop and plf <= -stop):
                blocked = "target" if plf >= (target or 9e9) else "stop"
                for p in list(pos):
                    r = p["d"] * (c15[i] - p["e"]) / p["rd"]; eq += p["risk_eq"] * risk * r
                    trades.append(dict(open_t=p["open_t"], close_t=t15[i], d=p["d"], r=r, why="lock_" + blocked, tag=p["tag"], hold=p["hold"]))
                pos = []
                eq_curve[i] = eq
        # ---- H1 close decision ----
        while kh < len(h1_end) and h1_end[kh] <= t15[i] + 900:     # this M15 bar is the last of the hour
            if h1_end[kh] == t15[i] + 900 and kh >= 200:
                hours_total += 1
                conc.append(len(pos))
                if blocked is None and len(pos) < rule.max_pos:
                    made = False
                    for d, sd, td, tag in rule.orders(H, kh, pos):
                        if len(pos) >= rule.max_pos: break
                        e = c15[i] + d * cost
                        pos.append(dict(d=d, e=e, sl=e - d * sd, tp=e + d * td, rd=sd, hold=0, open_t=t15[i],
                                        tag=tag, atr=rule.atr[kh], risk_eq=eq))
                        set_size[t15[i]] = set_size.get(t15[i], 0) + 1
                        made = True; day_trades += 1
                    hours_with_trade += made
            kh += 1
    day_rows.append((cur_day, day_trades, eq / day_start - 1, blocked))
    return trades, eq_curve, dict(hours=hours_total, hours_traded=hours_with_trade, conc=conc, days=day_rows)


def stats(trs):
    if not trs: return dict(n=0, wr=0, wl=0, pf=0, avg_r=0, maxl=0)
    rs = np.array([x["r"] for x in trs]); w = rs[rs > 0]; lo = rs[rs <= 0]
    maxl = cl = 0
    for r in rs:
        cl = cl + 1 if r <= 0 else 0; maxl = max(maxl, cl)
    aw = w.mean() if len(w) else 0; al = -lo.mean() if len(lo) else 0
    return dict(n=len(rs), wr=len(w) / len(rs), wl=aw / al if al else float("inf"),
                pf=w.sum() / max(-lo.sum(), 1e-9), avg_r=rs.mean(), maxl=maxl)


def max_dd(x): return float((1 - x / np.maximum.accumulate(x)).max())


def line(label, trades, eq, meta, years):
    s = stats(trades); d = meta["days"]
    tg = sum(1 for r in d if r[3] == "target"); sp = sum(1 for r in d if r[3] == "stop")
    return (f"{label:<30s} n={s['n']:6d} ({s['n']/max(len(d),1):4.1f}/day) hours-with-entry={meta['hours_traded']/max(meta['hours'],1)*100:5.1f}% "
            f"avg-open={np.mean(meta['conc']):.2f} WR={s['wr']*100:5.1f}% W/L={s['wl']:4.2f} PF={s['pf']:4.2f} avgR={s['avg_r']:+.3f} "
            f"CAGR={(max(eq[-1],1e-9)**(1/years)-1)*100:+6.1f}% DD={max_dd(eq)*100:5.1f}%"
            + (f" | days tgt {tg/len(d)*100:.0f}% stop {sp/len(d)*100:.0f}%" if tg or sp else ""))


def detail(label, trades, eq, meta, t15, out):
    P = lambda *a: print(*a, file=out)
    s = stats(trades)
    t0 = datetime.fromtimestamp(int(t15[0]), timezone.utc); t1 = datetime.fromtimestamp(int(t15[-1]), timezone.utc)
    years = (t1 - t0).days / 365.25
    P(f"\n{'='*100}\nDETAIL: {label}  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}\n{'='*100}")
    P(f"trades {s['n']}  WR {s['wr']*100:.1f}%  W/L {s['wl']:.2f}  PF {s['pf']:.2f}  avgR {s['avg_r']:+.3f}  max consec losses {s['maxl']}  "
      f"avg hold {np.mean([x['hold'] for x in trades])*15/60:.1f} h  max open {max(meta['conc'])}  total {(eq[-1]-1)*100:+.1f}%  DD {max_dd(eq)*100:.1f}%")
    ex = Counter(x["why"] for x in trades); P("exits: " + ", ".join(f"{k} {v} ({v/len(trades)*100:.0f}%)" for k, v in ex.most_common()))
    for tg in sorted({x["tag"] for x in trades}):
        ss = stats([x for x in trades if x["tag"] == tg]); P(f"  tag {tg:<6s} n={ss['n']:6d} WR={ss['wr']*100:5.1f}% PF={ss['pf']:4.2f} avgR={ss['avg_r']:+.3f}")
    for nm, dd in (("BUY", 1), ("SELL", -1)):
        ss = stats([x for x in trades if x["d"] == dd]); P(f"  {nm:<4s} n={ss['n']:6d} WR={ss['wr']*100:5.1f}% PF={ss['pf']:4.2f} avgR={ss['avg_r']:+.3f}")
    d = meta["days"]; pl = np.array([r[2] for r in d]); act = [r for r in d if r[1] > 0]
    P(f"\n--- DAILY --- {len(d)} days, {np.mean([r[1] for r in d]):.1f} entries/day, positive days {sum(1 for r in act if r[2]>0)/max(len(act),1)*100:.1f}%, "
      f"avg {pl.mean()*100:+.3f}%, best {pl.max()*100:+.2f}%, worst {pl.min()*100:+.2f}%")
    yr = np.array([datetime.fromtimestamp(int(x), timezone.utc).year for x in t15])
    P(f"\n--- YEARLY ---\n{'year':<6s}{'trades':>7s}{'WR%':>7s}{'PF':>6s}{'avgR':>8s}{'return%':>9s}{'maxDD%':>8s}")
    for y in sorted(set(yr)):
        m = yr == y; seg = eq[m]; p0 = np.where(m)[0][0] - 1; base = eq[p0] if p0 >= 0 else 1.0
        tt = [x for x in trades if datetime.fromtimestamp(int(x["close_t"]), timezone.utc).year == y]; ss = stats(tt)
        P(f"{y:<6d}{ss['n']:>7d}{ss['wr']*100:>7.1f}{ss['pf']:>6.2f}{ss['avg_r']:>+8.3f}{(seg[-1]/base-1)*100:>+9.1f}{max_dd(np.concatenate([[base], seg]))*100:>8.1f}")
    ym = np.array([datetime.fromtimestamp(int(x), timezone.utc).year * 100 + datetime.fromtimestamp(int(x), timezone.utc).month for x in t15])
    mret = {}; prev = 1.0
    for key in sorted(set(ym)):
        last = eq[ym == key][-1]; mret[key] = last / prev - 1; prev = last
    pos_m = sum(1 for v in mret.values() if v > 0)
    P(f"\n--- MONTHLY --- {pos_m}/{len(mret)} positive ({pos_m/len(mret)*100:.0f}%), avg {np.mean(list(mret.values()))*100:+.2f}%")
    P(f"{'year':<6s}" + "".join(f"{m:>7s}" for m in MONTHS))
    for y in sorted(set(yr)):
        P(f"{y:<6d}" + "".join(f"{mret[y*100+mo]*100:>+7.1f}" if (y*100+mo) in mret else f"{'':>7s}" for mo in range(1, 13)))
    with open(os.path.join(REPORTS, "hourly_trades.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["open", "close", "dir", "tag", "r", "why", "hold_h"])
        for x in trades:
            w.writerow([datetime.fromtimestamp(int(x["open_t"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(int(x["close_t"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "buy" if x["d"] > 0 else "sell", x["tag"], f"{x['r']:.3f}", x["why"], f"{x['hold']/4:.2f}"])


def variants(sweep):
    if not sweep:
        return [("trend_sl1.5_rr1_h4_buy (EA default)", Trend(sl_atr=1.5, rr=1.0, side="buy")),
                ("straddle_sl2_rr3_set0.3", Straddle(sl_atr=2.0, rr=3.0, set_tp=0.3))]
    return [
        ("trend_sl1_rr1.5_h4",          Trend()),
        ("trend_sl1.5_rr1_h4",          Trend(sl_atr=1.5, rr=1.0)),
        ("trend_sl2_rr1_h4",            Trend(sl_atr=2.0, rr=1.0)),
        ("trend_sl1_rr2_h4",            Trend(rr=2.0)),
        ("trend_sl1.5_rr1_h8",          Trend(sl_atr=1.5, rr=1.0, max_hold_h=8, max_pos=8)),
        ("trend_sl1.5_rr1_h4_buy",      Trend(sl_atr=1.5, rr=1.0, side="buy")),
        ("momo0.5_sl1_rr1.5_h4",        Momo()),
        ("momo1.0_sl1.5_rr1_h4",        Momo(k_atr=1.0, sl_atr=1.5, rr=1.0)),
        ("breakout4_sl1_rr1.5_h4",      Breakout()),
        ("breakout4_sl1.5_rr1_h4",      Breakout(sl_atr=1.5, rr=1.0)),
        ("straddle_sl1_rr1_h4",         Straddle(rr=1.0)),
        ("straddle_sl1_rr1_h4_lock",    Straddle(rr=1.0, lock=True)),
        ("straddle_sl2_rr0.5_h4",       Straddle(sl_atr=2.0, rr=0.5)),
        ("straddle_sl1_rr2_h4_lock",    Straddle(rr=2.0, lock=True)),
        ("straddle_sl2_rr3_set0.3",     Straddle(sl_atr=2.0, rr=3.0, set_tp=0.3)),
        ("straddle_sl2_rr3_set0.5",     Straddle(sl_atr=2.0, rr=3.0, set_tp=0.5)),
        ("straddle_sl1_rr3_set0.3",     Straddle(sl_atr=1.0, rr=3.0, set_tp=0.3)),
        ("straddle_sl3_rr2_set0.5_h8",  Straddle(sl_atr=3.0, rr=2.0, set_tp=0.5, max_hold_h=8, max_pos=8)),
        ("recovery_sl1.5_rr1_h4",       Recovery(sl_atr=1.5, rr=1.0)),
        ("recovery_sl2_rr1_h4",         Recovery(sl_atr=2.0, rr=1.0)),
        ("mr1.5_sl1.5_h4",              MeanRev(sl_atr=1.5)),
        ("mr2.0_sl2_h4",                MeanRev(z_in=2.0, sl_atr=2.0)),
        ("mr1.5_sl1.5_h4_buy",          MeanRev(sl_atr=1.5, side="buy")),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2015); ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--cost", type=float, default=0.25); ap.add_argument("--risk", type=float, default=0.0025)
    ap.add_argument("--target", type=float, default=0.0); ap.add_argument("--stop", type=float, default=0.0)
    ap.add_argument("--sweep", action="store_true"); ap.add_argument("--detail", default="trend_sl1.5_rr1_h4_buy (EA default)")
    ap.add_argument("--out", default="hourly.txt")
    args = ap.parse_args()
    t_start = time.time()
    m15 = load_m15(start_year=args.start, end_year=args.end)
    h1 = resample_clock(m15, 3600)
    H = dict(t=np.array([int(b.time.timestamp()) for b in h1]), o=np.array([b.open for b in h1]),
             h=np.array([b.high for b in h1]), l=np.array([b.low for b in h1]), c=np.array([b.close for b in h1]))
    t15 = np.array([int(b.time.timestamp()) for b in m15]); h15 = np.array([b.high for b in m15])
    l15 = np.array([b.low for b in m15]); c15 = np.array([b.close for b in m15])
    years = (t15[-1] - t15[0]) / 86400 / 365.25
    out = open(os.path.join(REPORTS, args.out), "w", encoding="utf-8")

    class Tee:
        def write(self, s): sys.stdout.write(s); out.write(s)
        def flush(self): sys.stdout.flush(); out.flush()
    tee = Tee(); P = lambda *a: print(*a, file=tee)
    P(f"Hourly multi-position bot — XAUUSD {m15[0].time:%Y-%m-%d} -> {m15[-1].time:%Y-%m-%d} ({len(h1)} H1 bars, {years:.1f} yr), "
      f"cost ${args.cost}/oz, risk {args.risk*100:.2f}%/position, daily target {args.target*100:.1f}% / stop {args.stop*100:.1f}% (0 = off), no EOD flat\n")
    res = {}
    for label, rule in variants(args.sweep):
        rule.prep(H)
        trades, eq, meta = simulate(rule, H, t15, h15, l15, c15, args.cost, args.risk, args.target, args.stop)
        res[label] = (trades, eq, meta)
        P(line(label, trades, eq, meta, years)); tee.flush()
    if args.detail in res:
        detail(args.detail, *res[args.detail], t15, tee)
    P(f"\ndone in {time.time()-t_start:.0f}s")
    out.close()


if __name__ == "__main__":
    main()
