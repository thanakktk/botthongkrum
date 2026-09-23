"""
Multi-timeframe (top-down) bot backtest: D1 / H4 / H1 give the direction,
M15 (or M5) gives the entry.
======================================================================
Bias per higher TF (as of its last CLOSED bar): bull if EMA20 > EMA50 and
close > EMA50, bear if the mirror, else neutral. A trade needs `need` of the
chosen TFs (default D1+H4+H1) bullish and none bearish (mirror for sells).
LTF entry, in the bias direction only, on the closed LTF bar:
  pullback   price touched the LTF EMA20 within the last K bars, now closes
             back above it AND above the previous bar's high (trigger bar);
             one entry per pullback
  breakout   close beyond the highest high / lowest low of the prior N bars
  ema_cross  EMA9 / EMA21 cross on the LTF
Stop: `swing` = beyond the K-bar swing low/high (clamped 0.5..2 ATR_LTF) or
`atr` = 1 ATR_LTF; TP = rr x stop, or `htf` = 1 ATR_H4. Optional max hold,
exit when the H1 bias flips, buy-only, several positions.
Fills on the LTF bars themselves (stop before target), cost at entry,
risk `--risk` per trade of equity, compounding.

    ./env/Scripts/python.exe research/mtf_backtest.py [--ltf M15|M5] [--start 2015] [--end 2026]
          [--cost 0.25] [--risk 0.005] [--sweep] [--detail <label>] [--oos 2021]
Writes reports/mtf_<ltf>.txt (+ _trades.csv on detail).
"""
from __future__ import annotations

import sys, os, argparse, csv, time
from collections import Counter
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15, load_m1_year
from backtester import resample_clock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
SECS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


def ema(x, n):
    k = 2.0 / (n + 1); out = np.empty_like(x); out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def atr(h, l, c, n=14):
    tr = np.empty(len(c)); tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    out = np.full(len(c), np.nan); cs = np.cumsum(tr)
    out[n:] = (cs[n:] - cs[:-n]) / n
    return out


def arrays(bars):
    return dict(t=np.array([int(b.time.timestamp()) for b in bars]), o=np.array([b.open for b in bars]),
                h=np.array([b.high for b in bars]), l=np.array([b.low for b in bars]), c=np.array([b.close for b in bars]))


def htf_bias(H, fast=20, slow=50):
    ef, es = ema(H["c"], fast), ema(H["c"], slow)
    b = np.zeros(len(H["c"]), dtype=int)
    b[(ef > es) & (H["c"] > es)] = 1
    b[(ef < es) & (H["c"] < es)] = -1
    b[:slow] = 0
    return b


def asof(H_t, secs, L_t_end):
    """index of the last HTF bar CLOSED at or before each LTF bar end (-1 = none)."""
    return np.searchsorted(H_t + secs, L_t_end, side="right") - 1


class Cfg:
    def __init__(self, **kw):
        self.entry = "pullback"; self.need = 3; self.tfs = ("D1", "H4", "H1")
        self.K = 24; self.N = 8; self.sl = "swing"; self.rr = 2.0; self.tp = "rr"
        self.hold_h = 24; self.side = "both"; self.exit_flip = False; self.max_pos = 1
        self.sl_usd = 5.0; self.tp_usd = 10.0          # for sl="fixed" / tp="fixed" ($ per oz; 500 points = $5)
        self.be_r = 0.0        # move SL to entry once +be_r R reached (0 = off)
        self.trail_r = 0.0     # trail SL trail_r R behind the best price once +trail_r R reached (0 = off)
        self.min_stop_atr = 0.5; self.max_stop_atr = 2.0
        for a, b in kw.items():
            setattr(self, a, b)


def simulate(cfg: Cfg, L, HT, cost, risk, cost_bps=0.0, feat=None, gate=None):
    # feat: optional {name: array over LTF bars} recorded on each trade at its entry bar
    # gate: optional boolean array over LTF bars, or {1: buy_gate, -1: sell_gate}; entries only where True
    """L: LTF arrays (+ precomputed e20,e9,e21,atr); HT: {tf: (arrays, bias, atr, asof_idx)}"""
    n = len(L["t"]); c, h, l = L["c"], L["h"], L["l"]
    e20, e9, e21, a = L["e20"], L["e9"], L["e21"], L["atr"]
    # --- bias per bar
    bull = np.zeros(n, dtype=int); bear = np.zeros(n, dtype=int)
    for tf in cfg.tfs:
        arr, bias, _, idx = HT[tf]
        bb = np.where(idx >= 0, bias[np.clip(idx, 0, None)], 0)
        bull += (bb == 1); bear += (bb == -1)
    if cfg.need == 0:
        dirv = np.zeros(n, dtype=int)            # baseline: LTF direction, no HTF gate
    else:
        dirv = np.where((bull >= cfg.need) & (bear == 0), 1, np.where((bear >= cfg.need) & (bull == 0), -1, 0))
    h1arr, h1bias, _, h1idx = HT["H1"]
    h1b = np.where(h1idx >= 0, h1bias[np.clip(h1idx, 0, None)], 0)
    h4arr, _, h4atr, h4idx = HT["H4"]
    h4a = np.where(h4idx >= 0, h4atr[np.clip(h4idx, 0, None)], np.nan)
    # --- LTF trigger candidates (vectorised)
    K, N = cfg.K, cfg.N
    prev_hi = np.roll(h, 1); prev_lo = np.roll(l, 1)
    if cfg.entry == "breakout":
        rmax = np.full(n, np.inf); rmin = np.full(n, -np.inf)
        for i in range(N, n):
            rmax[i] = h[i - N:i].max(); rmin[i] = l[i - N:i].min()
        trig_up = c > rmax; trig_dn = c < rmin
    elif cfg.entry == "ema_cross":
        trig_up = (e9 > e21) & (np.roll(e9, 1) <= np.roll(e21, 1))
        trig_dn = (e9 < e21) & (np.roll(e9, 1) >= np.roll(e21, 1))
    else:   # pullback: touch within K bars + trigger bar
        touch_up = np.zeros(n, dtype=bool); touch_dn = np.zeros(n, dtype=bool)
        tu = l <= e20; td = h >= e20
        cu = np.cumsum(tu); cd = np.cumsum(td)
        for i in range(K, n):
            touch_up[i] = cu[i - 1] - cu[i - K - 1 if i - K - 1 >= 0 else 0] > 0
            touch_dn[i] = cd[i - 1] - cd[i - K - 1 if i - K - 1 >= 0 else 0] > 0
        trig_up = touch_up & (c > e20) & (c > prev_hi)
        trig_dn = touch_dn & (c < e20) & (c < prev_lo)
    swing_lo = np.full(n, np.nan); swing_hi = np.full(n, np.nan)
    for i in range(K, n):
        swing_lo[i] = l[i - K:i + 1].min(); swing_hi[i] = h[i - K:i + 1].max()
    eq = 1.0; eq_curve = np.empty(n); pos = []; trades = []
    max_hold = int(cfg.hold_h * 3600 / (L["t"][1] - L["t"][0])) if cfg.hold_h else 0
    last_entry_i = -10 ** 9; last_touch_reset = True
    for i in range(1, n):
        # ---- manage
        closed = []
        for p in pos:
            d, e, sl, tp = p["d"], p["e"], p["sl"], p["tp"]
            res = why = None
            if d > 0:
                if l[i] <= sl: res, why = sl, "sl"
                elif h[i] >= tp: res, why = tp, "tp"
            else:
                if h[i] >= sl: res, why = sl, "sl"
                elif l[i] <= tp: res, why = tp, "tp"
            p["hold"] += 1
            if res is None and (cfg.be_r or cfg.trail_r):
                fav = (h[i] - e) if d > 0 else (e - l[i])
                p["best"] = max(p.get("best", 0.0), fav)
                if cfg.be_r and p["best"] >= cfg.be_r * p["rd"] and (sl - e) * d < 0:
                    sl = e + d * 0.05 * p["rd"]; p["sl"] = sl
                if cfg.trail_r and p["best"] >= cfg.trail_r * p["rd"]:
                    new_sl = (e + d * (p["best"] - cfg.trail_r * p["rd"]))
                    if (new_sl - sl) * d > 0: sl = new_sl; p["sl"] = sl
            if res is None and max_hold and p["hold"] >= max_hold: res, why = c[i], "time"
            if res is None and cfg.exit_flip and h1b[i] == -d: res, why = c[i], "flip"
            if res is not None:
                r = d * (res - e) / p["rd"]; eq += p["eq0"] * risk * r
                rec = dict(open_t=L["t"][p["i"]], close_t=L["t"][i], d=d, r=r, why=why, hold=p["hold"], e=e, sl=sl, tp=tp, exit=res, rd=p["rd"])
                if feat is not None:
                    for k, v in feat.items(): rec[k] = float(v[p["i"]])
                trades.append(rec)
                closed.append(p)
        for p in closed: pos.remove(p)
        eq_curve[i] = eq + sum(p["eq0"] * risk * p["d"] * (c[i] - p["e"]) / p["rd"] for p in pos)
        # ---- entry
        if len(pos) >= cfg.max_pos or np.isnan(a[i]) or a[i] <= 0 or np.isnan(swing_lo[i]):
            continue
        d = 0
        if cfg.need == 0:
            d = 1 if trig_up[i] else (-1 if trig_dn[i] else 0)
        else:
            if dirv[i] > 0 and trig_up[i]: d = 1
            elif dirv[i] < 0 and trig_dn[i]: d = -1
        if d == 0: continue
        if gate is not None:
            g = gate[d][i] if isinstance(gate, dict) else gate[i]
            if not g: continue
        if cfg.side == "buy" and d < 0: continue
        if cfg.side == "sell" and d > 0: continue
        if cfg.entry == "pullback":
            # one entry per pullback: require a fresh touch since the last entry
            if i - last_entry_i < K: continue
        e = c[i] + d * (cost if cost_bps == 0 else c[i] * cost_bps / 1e4)
        if cfg.sl == "swing":
            dist = (e - swing_lo[i]) if d > 0 else (swing_hi[i] - e)
            dist = min(max(dist + 0.1 * a[i], cfg.min_stop_atr * a[i]), cfg.max_stop_atr * a[i])
        elif cfg.sl == "fixed":
            dist = cfg.sl_usd
        else:
            dist = 1.0 * a[i]
        if cfg.tp == "htf" and not np.isnan(h4a[i]): tpd = 1.0 * h4a[i]
        elif cfg.tp == "fixed": tpd = cfg.tp_usd
        else: tpd = cfg.rr * dist
        pos.append(dict(d=d, e=e, sl=e - d * dist, tp=e + d * tpd, rd=dist, hold=0, i=i, eq0=eq))
        last_entry_i = i
    eq_curve[0] = 1.0
    return trades, eq_curve


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


def line(label, trades, eq, t, years, oos_ts):
    s = stats(trades); days = (t[-1] - t[0]) / 86400 * 5 / 7
    is_t = [x for x in trades if x["close_t"] < oos_ts]; oos_t = [x for x in trades if x["close_t"] >= oos_ts]
    si, so = stats(is_t), stats(oos_t)
    k = int(np.searchsorted(t, oos_ts))
    dd_is = max_dd(eq[:k]) if k > 1 else 0; dd_oos = max_dd(eq[k:] / eq[k - 1]) if 1 < k < len(eq) else 0
    return (f"{label:<32s} n={s['n']:5d} ({s['n']/max(days,1):4.2f}/day) WR={s['wr']*100:5.1f}% W/L={s['wl']:4.2f} PF={s['pf']:4.2f} avgR={s['avg_r']:+.3f} "
            f"CAGR={(max(eq[-1],1e-9)**(1/years)-1)*100:+6.1f}% DD={max_dd(eq)*100:5.1f}% "
            f"| IS n={si['n']:4d} PF={si['pf']:4.2f} avgR={si['avg_r']:+.3f} DD={dd_is*100:4.1f}% | OOS n={so['n']:4d} PF={so['pf']:4.2f} avgR={so['avg_r']:+.3f} DD={dd_oos*100:4.1f}%")


def detail(label, trades, eq, t, out, ltf):
    P = lambda *a: print(*a, file=out)
    s = stats(trades)
    t0 = datetime.fromtimestamp(int(t[0]), timezone.utc); t1 = datetime.fromtimestamp(int(t[-1]), timezone.utc)
    years = (t1 - t0).days / 365.25
    P(f"\n{'='*100}\nDETAIL: {label}  {t0:%Y-%m-%d} -> {t1:%Y-%m-%d}\n{'='*100}")
    P(f"trades {s['n']}  WR {s['wr']*100:.1f}%  W/L {s['wl']:.2f}  PF {s['pf']:.2f}  avgR {s['avg_r']:+.3f}  max consec losses {s['maxl']}  "
      f"avg hold {np.mean([x['hold'] for x in trades])*SECS[ltf]/3600:.1f} h  total {(eq[-1]-1)*100:+.1f}%  CAGR {(eq[-1]**(1/years)-1)*100:+.1f}%  DD {max_dd(eq)*100:.1f}%")
    ex = Counter(x["why"] for x in trades); P("exits: " + ", ".join(f"{k} {v} ({v/len(trades)*100:.0f}%)" for k, v in ex.most_common()))
    for nm, dd in (("BUY", 1), ("SELL", -1)):
        ss = stats([x for x in trades if x["d"] == dd]); P(f"  {nm:<4s} n={ss['n']:5d} WR={ss['wr']*100:5.1f}% PF={ss['pf']:4.2f} avgR={ss['avg_r']:+.3f}")
    yr = np.array([datetime.fromtimestamp(int(x), timezone.utc).year for x in t])
    P(f"\n--- YEARLY ---\n{'year':<6s}{'trades':>7s}{'WR%':>7s}{'W/L':>6s}{'PF':>6s}{'avgR':>8s}{'return%':>9s}{'maxDD%':>8s}")
    for y in sorted(set(yr)):
        m = yr == y; seg = eq[m]; p0 = np.where(m)[0][0] - 1; base = eq[p0] if p0 >= 0 else 1.0
        tt = [x for x in trades if datetime.fromtimestamp(int(x["close_t"]), timezone.utc).year == y]; ss = stats(tt)
        P(f"{y:<6d}{ss['n']:>7d}{ss['wr']*100:>7.1f}{ss['wl']:>6.2f}{ss['pf']:>6.2f}{ss['avg_r']:>+8.3f}{(seg[-1]/base-1)*100:>+9.1f}{max_dd(np.concatenate([[base], seg]))*100:>8.1f}")
    ym = np.array([datetime.fromtimestamp(int(x), timezone.utc).year * 100 + datetime.fromtimestamp(int(x), timezone.utc).month for x in t])
    mret = {}; prev = 1.0
    for key in sorted(set(ym)):
        last = eq[ym == key][-1]; mret[key] = last / prev - 1; prev = last
    pos_m = sum(1 for v in mret.values() if v > 0)
    P(f"\n--- MONTHLY --- {pos_m}/{len(mret)} positive ({pos_m/len(mret)*100:.0f}%), avg {np.mean(list(mret.values()))*100:+.2f}%, best {max(mret.values())*100:+.1f}%, worst {min(mret.values())*100:+.1f}%")
    P(f"{'year':<6s}" + "".join(f"{m:>7s}" for m in MONTHS))
    for y in sorted(set(yr)):
        P(f"{y:<6d}" + "".join(f"{mret[y*100+mo]*100:>+7.1f}" if (y*100+mo) in mret else f"{'':>7s}" for mo in range(1, 13)))
    hrs = Counter(datetime.fromtimestamp(int(x["open_t"]), timezone.utc).hour for x in trades)
    P("\nentries by hour (server): " + ", ".join(f"{h:02d}h {n}" for h, n in sorted(hrs.items())))
    with open(os.path.join(REPORTS, f"mtf_{ltf}_trades.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["open", "close", "dir", "entry", "sl", "tp", "exit", "r", "why", "hold_h"])
        for x in trades:
            w.writerow([datetime.fromtimestamp(int(x["open_t"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        datetime.fromtimestamp(int(x["close_t"]), timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        "buy" if x["d"] > 0 else "sell", f"{x['e']:.2f}", f"{x['sl']:.2f}", f"{x['tp']:.2f}", f"{x['exit']:.2f}",
                        f"{x['r']:.3f}", x["why"], f"{x['hold']*SECS[ltf]/3600:.2f}"])


def variants(sweep, fixed=False):
    if fixed:
        out = [("pullback 3/3 swing SL 2R (reference)", Cfg())]
        for sl, tp in ((5, 10), (5, 12.5), (5, 15), (7.5, 15), (10, 20), (10, 15), (3, 9)):
            out.append((f"pullback 3/3 SL${sl:g} TP${tp:g}", Cfg(sl="fixed", tp="fixed", sl_usd=sl, tp_usd=tp)))
        out.append(("pullback 3/3 SL$5 TP$15 hold0", Cfg(sl="fixed", tp="fixed", sl_usd=5, tp_usd=15, hold_h=0)))
        out.append(("pullback 3/3 SL$5 TP$15 buy-only", Cfg(sl="fixed", tp="fixed", sl_usd=5, tp_usd=15, side="buy")))
        for sl, tp in ((5, 10), (5, 15), (10, 20)):
            out.append((f"breakout 3/3 SL${sl:g} TP${tp:g}", Cfg(entry="breakout", sl="fixed", tp="fixed", sl_usd=sl, tp_usd=tp)))
        out.append(("pullback NO HTF SL$5 TP$15", Cfg(need=0, sl="fixed", tp="fixed", sl_usd=5, tp_usd=15)))
        return out
    if not sweep:
        return [("pullback need3 K24 swing rr2 hold24 (EA default)", Cfg()), ("breakout8 need3 swing rr2", Cfg(entry="breakout"))]
    return [
        ("pullback need3 swing rr2 hold24",     Cfg()),
        ("pullback need2 swing rr2 hold24",     Cfg(need=2)),
        ("pullback need0 (no HTF, baseline)",   Cfg(need=0)),
        ("pullback H4+D1 need2",                Cfg(tfs=("D1", "H4"), need=2)),
        ("pullback H1+H4 need2",                Cfg(tfs=("H4", "H1"), need=2)),
        ("pullback need3 rr1.5",                Cfg(rr=1.5)),
        ("pullback need3 rr3",                  Cfg(rr=3.0)),
        ("pullback need3 atrSL rr2",            Cfg(sl="atr")),
        ("pullback need3 tp=1xATR_H4",          Cfg(tp="htf")),
        ("pullback need3 hold8",                Cfg(hold_h=8)),
        ("pullback need3 hold0 (no time exit)", Cfg(hold_h=0)),
        ("pullback need3 exit on H1 flip",      Cfg(exit_flip=True, hold_h=0)),
        ("pullback need3 buy-only",             Cfg(side="buy")),
        ("pullback need3 K6",                   Cfg(K=6)),
        ("pullback need3 K12",                  Cfg(K=12)),
        ("pullback need3 max_pos2",             Cfg(max_pos=2)),
        ("breakout8 need3 swing rr2",           Cfg(entry="breakout")),
        ("breakout8 need3 atrSL rr1.5",         Cfg(entry="breakout", sl="atr", rr=1.5)),
        ("breakout8 need0 (baseline)",          Cfg(entry="breakout", need=0)),
        ("ema_cross need3 swing rr2",           Cfg(entry="ema_cross")),
        ("ema_cross need3 atrSL rr2 hold8",     Cfg(entry="ema_cross", sl="atr", hold_h=8)),
    ]


def load_ltf(ltf, start, end):
    m15 = load_m15(start_year=start, end_year=end)
    if ltf == "M15":
        return m15, m15
    m1 = []
    for y in range(start, end + 1):
        m1 += load_m1_year(y)
    m5 = resample_clock(m1, 300)
    return m5, m15


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ltf", default="M15", choices=["M15", "M5"])
    ap.add_argument("--start", type=int, default=2015); ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--oos", type=int, default=2021)
    ap.add_argument("--cost", type=float, default=0.25); ap.add_argument("--risk", type=float, default=0.005)
    ap.add_argument("--cost-bps", type=float, default=0.0, help="cost as basis points of price instead of $ (0.625 = $0.25 at $4000)")
    ap.add_argument("--fixed", action="store_true", help="fixed-point SL/TP variants (500 pts SL, 1000-1500 pts TP)")
    ap.add_argument("--sweep", action="store_true"); ap.add_argument("--detail", default="pullback need3 K24 swing rr2 hold24 (EA default)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    t_start = time.time()
    ltf_bars, m15 = load_ltf(args.ltf, args.start, args.end)
    L = arrays(ltf_bars)
    L["e20"], L["e9"], L["e21"] = ema(L["c"], 20), ema(L["c"], 9), ema(L["c"], 21)
    L["atr"] = atr(L["h"], L["l"], L["c"], 14)
    L_end = L["t"] + SECS[args.ltf]
    HT = {}
    for tf in ("H1", "H4", "D1"):
        arr = arrays(resample_clock(m15, SECS[tf]))
        HT[tf] = (arr, htf_bias(arr), atr(arr["h"], arr["l"], arr["c"], 14), asof(arr["t"], SECS[tf], L_end))
    years = (L["t"][-1] - L["t"][0]) / 86400 / 365.25
    oos_ts = int(datetime(args.oos, 1, 1, tzinfo=timezone.utc).timestamp())
    out = open(os.path.join(REPORTS, args.out or f"mtf_{args.ltf}.txt"), "w", encoding="utf-8")

    class Tee:
        def write(self, s): sys.stdout.write(s); out.write(s)
        def flush(self): sys.stdout.flush(); out.flush()
    tee = Tee(); P = lambda *a: print(*a, file=tee)
    P(f"MTF bot — bias D1/H4/H1 (EMA20/50), entries on {args.ltf} — XAUUSD {ltf_bars[0].time:%Y-%m-%d} -> {ltf_bars[-1].time:%Y-%m-%d} "
      f"({len(ltf_bars)} {args.ltf} bars, {years:.1f} yr), cost {('%.3f bps of price' % args.cost_bps) if args.cost_bps else ('$%s/oz' % args.cost)}, risk {args.risk*100:.2f}%/trade, IS < {args.oos} <= OOS\n")
    res = {}
    for label, cfg in variants(args.sweep, args.fixed):
        trades, eq = simulate(cfg, L, HT, args.cost, args.risk, args.cost_bps)
        res[label] = (trades, eq)
        P(line(label, trades, eq, L["t"], years, oos_ts)); tee.flush()
    if args.detail in res:
        detail(args.detail, *res[args.detail], L["t"], tee, args.ltf)
    P(f"\ndone in {time.time()-t_start:.0f}s")
    out.close()


if __name__ == "__main__":
    main()
