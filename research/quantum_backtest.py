"""
Quantum Price Level (QPL) bot backtest — win-rate by year / month / day,
max drawdown, W/L, on 11+ years of XAUUSD.
======================================================================
Uses the SAME `QuantumPriceLevel` strategy class the live loop runs
(strategies.py, math in quantum.py), fed 200 closed H4 bars per decision like
the live arbitrator does, so live == backtest by construction. Fills are then
simulated on the M15 bars (stop checked before target inside each bar), with
the round-trip cost charged at entry, 1 % of equity risked per trade,
compounding, one open trade at a time (the H4 solo-loop setup).

    ./env/Scripts/python.exe research/quantum_backtest.py [--start 2015] [--end 2026]
          [--sweep | --buyonly | --smc] [--detail quantum_qpl] [--cost 0.25]
Default (no flag) runs the three registered ids: quantum_qpl (BUY-only
k3_s3_t6), quantum_qpl_smc (+ demand zone) and quantum_qpl_bounce.

Writes reports/quantum_qpl.txt (+ _trades.csv, _monthly.csv, _daily.csv).
"""
from __future__ import annotations

import sys, os, argparse, csv, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15
from backtester import resample_clock
from strategies import QuantumPriceLevel, QuantumPriceLevelBounce, QuantumPriceLevelSmc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")
WINDOW = 200                  # closed H4 bars handed to the strategy (= live)
RISK = 0.01                   # fraction of equity risked per trade
MAX_HOLD_DAYS = 10
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
WD = "Mon Tue Wed Thu Fri Sun Sat".split()


# --------------------------------------------------------------------------- #
# simulation                                                                   #
# --------------------------------------------------------------------------- #
class Trade:
    __slots__ = ("dir", "open_t", "close_t", "entry", "sl0", "tp", "exit", "r",
                 "reason", "bars", "level", "sigma", "lam", "smc")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def simulate(st, h4, m15_t, m15_h, m15_l, m15_c, cost, trail=False,
             ladder_cache=None):
    """Run strategy `st` over the H4 series; return (trades, equity_per_m15)."""
    n15 = len(m15_t)
    eq_curve = np.empty(n15)
    eq = 1.0
    ptr = 0                      # m15 index up to which eq_curve is filled
    trades: list[Trade] = []
    h4_end = np.array([int(b.time.timestamp()) for b in h4]) + 14400
    max_hold = MAX_HOLD_DAYS * 24 * 4
    key = (st.vol_n, st.lam, st.n_levels)
    orig_ladder_for, orig_smc_for = st.ladder_for, st.smc_for
    i = WINDOW
    while i < len(h4):
        win = h4[i - WINDOW + 1:i + 1]
        if ladder_cache is not None:
            ck = key + (i,)
            if ck not in ladder_cache:
                ladder_cache[ck] = orig_ladder_for(win)
            st.ladder_for = lambda bars, _r=ladder_cache[ck]: _r
            if st.uses_smc:
                sk = ("smc", i)
                if sk not in ladder_cache:
                    ladder_cache[sk] = orig_smc_for(win)
                st.smc_for = lambda bars, _r=ladder_cache[sk]: _r
        st.last_smc_tags = ""
        sig = st.generate("XAUUSD", win, win[-1].time)
        st.ladder_for, st.smc_for = orig_ladder_for, orig_smc_for
        if sig is None:
            i += 1
            continue
        j = int(np.searchsorted(m15_t, h4_end[i]))   # first M15 after the H4 close
        if j >= n15:
            break
        d = 1 if sig.direction.value == "buy" else -1
        e = sig.entry + d * cost
        sl, tp = sig.sl, sig.tp
        risk = abs(e - sl)
        if risk <= 0:
            i += 1
            continue
        ladder, k0, s = st.last_ladder, st.k_in, st.sl_levels
        nl = st.n_levels
        eq_curve[ptr:j] = eq
        ptr = j
        res = None
        reason = "time"
        k_end = min(j + max_hold, n15)
        for k in range(j, k_end):
            if d > 0:
                if m15_l[k] <= sl:
                    res, reason = sl, "sl"
                elif m15_h[k] >= tp:
                    res, reason = tp, "tp"
            else:
                if m15_h[k] >= sl:
                    res, reason = sl, "sl"
                elif m15_l[k] <= tp:
                    res, reason = tp, "tp"
            if res is not None:
                eq_curve[k] = eq * (1 + RISK * d * (res - e) / risk)
                break
            # mark-to-market
            eq_curve[k] = eq * (1 + RISK * d * (m15_c[k] - e) / risk)
            if trail:            # ratchet the stop up the ladder rung by rung
                c = m15_c[k]
                if d > 0:
                    r = k0
                    while r + 1 <= nl and c >= ladder[nl + r + 1]:
                        r += 1
                    if r > k0:
                        sl = max(sl, float(ladder[nl + r - s]))
                else:
                    r = k0
                    while r + 1 <= nl and c <= ladder[nl - r - 1]:
                        r += 1
                    if r > k0:
                        sl = min(sl, float(ladder[nl - r + s]))
        if res is None:
            k = k_end - 1
            res = m15_c[k]
            eq_curve[k] = eq * (1 + RISK * d * (res - e) / risk)
        r_mult = d * (res - e) / risk
        eq *= 1 + RISK * r_mult
        ptr = k + 1
        trades.append(Trade(dir=d, open_t=datetime.fromtimestamp(m15_t[j], timezone.utc),
                            close_t=datetime.fromtimestamp(m15_t[k], timezone.utc),
                            entry=e, sl0=sig.sl, tp=tp, exit=res, r=r_mult,
                            reason=reason, bars=k - j + 1, level=k0,
                            sigma=st.last_sigma, lam=st.last_lambda, smc=st.last_smc_tags))
        # resume at the first H4 bar that closes after the exit
        i = int(np.searchsorted(h4_end, m15_t[k] + 1))
        i = max(i, WINDOW)
    eq_curve[ptr:] = eq
    return trades, eq_curve


# --------------------------------------------------------------------------- #
# metrics                                                                      #
# --------------------------------------------------------------------------- #
def trade_stats(trs):
    n = len(trs)
    if n == 0:
        return dict(n=0, wr=0, pf=0, avg_r=0, wl=0, ret=0, exp=0, maxw=0, maxl=0,
                    avg_win=0, avg_loss=0, hold_h=0)
    rs = np.array([t.r for t in trs])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    eq = np.cumprod(1 + RISK * rs)
    maxw = maxl = cw = cl = 0
    for r in rs:
        if r > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        maxw, maxl = max(maxw, cw), max(maxl, cl)
    aw = wins.mean() if len(wins) else 0.0
    al = -losses.mean() if len(losses) else 0.0
    return dict(n=n, wr=len(wins) / n, pf=wins.sum() / max(-losses.sum(), 1e-9),
                avg_r=rs.mean(), wl=aw / al if al else float("inf"), ret=eq[-1] - 1,
                exp=rs.mean(), maxw=maxw, maxl=maxl, avg_win=aw, avg_loss=al,
                hold_h=np.mean([t.bars for t in trs]) / 4)


def max_dd(curve):
    peak = np.maximum.accumulate(curve)
    return float((1 - curve / peak).max())


def cagr(curve, years):
    return curve[-1] ** (1 / years) - 1 if years > 0 else 0.0


def fmt_row(label, s, dd=None, years=None, curve=None):
    dd_s = f"{dd*100:5.1f}%" if dd is not None else "  n/a "
    cg = f"{cagr(curve, years)*100:+6.1f}%" if curve is not None and years else "   n/a "
    return (f"{label:<26s} n={s['n']:4d} WR={s['wr']*100:5.1f}% W/L={s['wl']:4.2f} "
            f"PF={s['pf']:4.2f} avgR={s['avg_r']:+.3f} ret={s['ret']*100:+8.1f}% "
            f"CAGR={cg} maxDD={dd_s} maxW/L={s['maxw']}/{s['maxl']}")


# --------------------------------------------------------------------------- #
# detailed report for one variant                                              #
# --------------------------------------------------------------------------- #
def detail_report(label, trades, eq_curve, m15_t, out):
    P = lambda *a: print(*a, file=out)
    t0 = datetime.fromtimestamp(m15_t[0], timezone.utc)
    t1 = datetime.fromtimestamp(m15_t[-1], timezone.utc)
    years = (t1 - t0).days / 365.25
    s = trade_stats(trades)
    P(f"\n{'='*100}\nDETAIL: {label}   {t0:%Y-%m-%d} -> {t1:%Y-%m-%d} ({years:.1f} yr), "
      f"risk {RISK*100:.0f}%/trade compounding, 1 trade at a time\n{'='*100}")
    P(f"trades {s['n']}   win-rate {s['wr']*100:.1f}%   W/L (avg win / avg loss) {s['wl']:.2f}   "
      f"avg win {s['avg_win']:+.2f}R  avg loss {-s['avg_loss']:+.2f}R   expectancy {s['exp']:+.3f}R/trade")
    P(f"profit factor {s['pf']:.2f}   total return {s['ret']*100:+.1f}%   CAGR {cagr(eq_curve, years)*100:+.1f}%   "
      f"max DD (M15 mark-to-market) {max_dd(eq_curve)*100:.1f}%   max consecutive wins {s['maxw']} / losses {s['maxl']}   "
      f"avg hold {s['hold_h']:.1f} h")
    by = defaultdict(int)
    for t in trades:
        by[t.reason] += 1
    P("exits: " + ", ".join(f"{k} {v} ({v/max(len(trades),1)*100:.0f}%)" for k, v in sorted(by.items())))
    buys = [t for t in trades if t.dir > 0]; sells = [t for t in trades if t.dir < 0]
    for nm, tt in (("BUY", buys), ("SELL", sells)):
        ss = trade_stats(tt)
        P(f"  {nm:<4s} n={ss['n']:4d} WR={ss['wr']*100:5.1f}% PF={ss['pf']:4.2f} avgR={ss['avg_r']:+.3f}")
    lam_pos = sum(1 for t in trades if (t.lam or 0) > 0)
    P(f"lambda > 0 (anharmonic ladder) on {lam_pos}/{len(trades)} entries; "
      f"median sigma_daily {np.median([t.sigma for t in trades])*100:.2f}%")

    # ---- yearly ------------------------------------------------------------
    P(f"\n--- YEARLY ---\n{'year':<6s}{'trades':>7s}{'WR%':>7s}{'W/L':>6s}{'PF':>6s}{'avgR':>8s}"
      f"{'return%':>9s}{'maxDD%':>8s}{'maxL':>6s}")
    dates = np.array([datetime.fromtimestamp(x, timezone.utc).year for x in m15_t])
    yrs = sorted(set(dates))
    for y in yrs:
        m = dates == y
        seg = eq_curve[m]
        # year return = last / previous year's last
        prev_idx = np.where(m)[0][0] - 1
        base = eq_curve[prev_idx] if prev_idx >= 0 else 1.0
        tt = [t for t in trades if t.close_t.year == y]
        ss = trade_stats(tt)
        P(f"{y:<6d}{ss['n']:>7d}{ss['wr']*100:>7.1f}{ss['wl']:>6.2f}{ss['pf']:>6.2f}{ss['avg_r']:>+8.3f}"
          f"{(seg[-1]/base-1)*100:>+9.1f}{max_dd(np.concatenate([[base], seg]))*100:>8.1f}{ss['maxl']:>6d}")

    # ---- monthly -----------------------------------------------------------
    ym = np.array([datetime.fromtimestamp(x, timezone.utc).year * 100 +
                   datetime.fromtimestamp(x, timezone.utc).month for x in m15_t])
    month_ret, month_wr, month_n = {}, {}, {}
    prev = 1.0
    for key in sorted(set(ym)):
        m = ym == key
        last = eq_curve[m][-1]
        month_ret[key] = last / prev - 1
        prev = last
        tt = [t for t in trades if t.close_t.year * 100 + t.close_t.month == key]
        month_n[key] = len(tt)
        month_wr[key] = (sum(1 for t in tt if t.r > 0) / len(tt)) if tt else None
    pos = sum(1 for v in month_ret.values() if v > 0)
    P(f"\n--- MONTHLY --- {pos}/{len(month_ret)} months positive ({pos/len(month_ret)*100:.0f}%), "
      f"avg {np.mean(list(month_ret.values()))*100:+.2f}%/month, best {max(month_ret.values())*100:+.1f}%, "
      f"worst {min(month_ret.values())*100:+.1f}%")
    P("return % by month:")
    P(f"{'year':<6s}" + "".join(f"{m:>7s}" for m in MONTHS) + f"{'YEAR':>8s}")
    for y in yrs:
        cells = []
        for mo in range(1, 13):
            v = month_ret.get(y * 100 + mo)
            cells.append(f"{v*100:>+7.1f}" if v is not None else f"{'':>7s}")
        yr = np.prod([1 + month_ret[k] for k in month_ret if k // 100 == y]) - 1
        P(f"{y:<6d}" + "".join(cells) + f"{yr*100:>+8.1f}")
    P("win-rate % by month (trades closed in the month; '-' = no trade):")
    P(f"{'year':<6s}" + "".join(f"{m:>7s}" for m in MONTHS))
    for y in yrs:
        cells = []
        for mo in range(1, 13):
            v = month_wr.get(y * 100 + mo)
            n = month_n.get(y * 100 + mo, 0)
            cells.append(f"{v*100:>4.0f}/{n:<2d}" if v is not None else f"{'-':>7s}")
        P(f"{y:<6d}" + "".join(cells))
    with open(os.path.join(REPORTS, "quantum_qpl_monthly.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["year", "month", "return_pct", "trades", "win_rate_pct"])
        for key in sorted(month_ret):
            w.writerow([key // 100, key % 100, f"{month_ret[key]*100:.3f}", month_n[key],
                        f"{month_wr[key]*100:.1f}" if month_wr[key] is not None else ""])

    # ---- daily -------------------------------------------------------------
    day = np.array([x // 86400 for x in m15_t])
    d_keys, d_idx = np.unique(day, return_index=True)
    d_last = np.append(d_idx[1:] - 1, len(m15_t) - 1)
    d_eq = eq_curve[d_last]
    d_ret = d_eq / np.concatenate([[1.0], d_eq[:-1]]) - 1
    active = d_ret != 0
    P(f"\n--- DAILY --- {active.sum()} days with P/L movement out of {len(d_ret)} trading days")
    P(f"positive days {(d_ret > 0).sum()/max(active.sum(),1)*100:.1f}% of active days   "
      f"avg active day {d_ret[active].mean()*100:+.3f}%   best day {d_ret.max()*100:+.2f}%   worst day {d_ret.min()*100:+.2f}%")
    cl = maxcl = 0
    for v in d_ret:
        if v < 0:
            cl += 1; maxcl = max(maxcl, cl)
        elif v > 0:
            cl = 0
    P(f"max consecutive losing days {maxcl}")
    P("trade win-rate by ENTRY weekday:")
    for wd in range(5):
        tt = [t for t in trades if t.open_t.weekday() == wd]
        if tt:
            ss = trade_stats(tt)
            P(f"  {WD[wd]}  n={ss['n']:4d} WR={ss['wr']*100:5.1f}% avgR={ss['avg_r']:+.3f} PF={ss['pf']:4.2f}")
    P("trade win-rate by ENTRY hour (server time):")
    for hr in sorted({t.open_t.hour for t in trades}):
        tt = [t for t in trades if t.open_t.hour == hr]
        ss = trade_stats(tt)
        P(f"  {hr:02d}:00  n={ss['n']:4d} WR={ss['wr']*100:5.1f}% avgR={ss['avg_r']:+.3f} PF={ss['pf']:4.2f}")
    with open(os.path.join(REPORTS, "quantum_qpl_daily.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["date", "equity", "return_pct"])
        for dk, e, r in zip(d_keys, d_eq, d_ret):
            w.writerow([datetime.fromtimestamp(int(dk) * 86400, timezone.utc).date(), f"{e:.6f}", f"{r*100:.4f}"])
    with open(os.path.join(REPORTS, "quantum_qpl_trades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["open", "close", "dir", "entry", "sl", "tp", "exit", "r", "reason", "hold_h", "level", "sigma", "lambda", "smc"])
        for t in trades:
            w.writerow([t.open_t.strftime("%Y-%m-%d %H:%M"), t.close_t.strftime("%Y-%m-%d %H:%M"),
                        "buy" if t.dir > 0 else "sell", f"{t.entry:.2f}", f"{t.sl0:.2f}", f"{t.tp:.2f}",
                        f"{t.exit:.2f}", f"{t.r:.3f}", t.reason, f"{t.bars/4:.1f}", t.level,
                        f"{t.sigma:.5f}", f"{t.lam:.3f}", t.smc])


# --------------------------------------------------------------------------- #
def variants(sweep: bool, buyonly: bool = False, smc: bool = False):
    out = []
    if smc:
        Q = QuantumPriceLevel
        B = QuantumPriceLevelBounce
        out += [
            ("k3_s3_t6_buy (baseline)",     Q(), False),
            ("k3_s3_t6_buy+bos",            Q(smc_bos=True), False),
            ("k3_s3_t6_buy+bos_age24",      Q(smc_bos=True, bos_max_age=24), False),
            ("k3_s3_t6_buy+lq12",           Q(smc_lq=True, lq_lookback=12), False),
            ("k3_s3_t6_buy+lq60",           Q(smc_lq=True, lq_lookback=60), False),
            ("k3_s3_t6_buy+lq30",           Q(smc_lq=True, lq_lookback=30), False),
            ("k3_s3_t6_buy+zone",           Q(smc_zone=True), False),
            ("k3_s3_t6_buy+zone_atr2",      Q(smc_zone=True, zone_atr=2.0), False),
            ("k3_s3_t6_buy+zone_atr3",      Q(smc_zone=True, zone_atr=3.0), False),
            ("k3_s3_t6_buy+zone3_slzone",   Q(smc_zone=True, zone_atr=3.0, sl_mode="zone"), False),
            ("k3_s3_t6_buy+bos+zone3",      Q(smc_bos=True, smc_zone=True, zone_atr=3.0), False),
            ("k3_s3_t6_buy+bos+lq60",       Q(smc_bos=True, smc_lq=True, lq_lookback=60), False),
            ("k3_s3_t6_buy+bos+lq60+zone3", Q(smc_bos=True, smc_lq=True, lq_lookback=60, smc_zone=True, zone_atr=3.0), False),
            ("k3_s3_t6_buy+zone_slzone",    Q(smc_zone=True, sl_mode="zone"), False),
            ("k3_s3_t6_buy+bos+lq30",       Q(smc_bos=True, smc_lq=True, lq_lookback=30), False),
            ("k3_s3_t6_buy+bos+zone",       Q(smc_bos=True, smc_zone=True), False),
            ("k3_s3_t6_buy+bos+lq30+zone",  Q(smc_bos=True, smc_lq=True, lq_lookback=30, smc_zone=True), False),
            ("k2_s2_t4_buy (loose)",        Q(k_in=2, sl_levels=2, tp_levels=4), False),
            ("k2_s2_t4_buy+bos",            Q(k_in=2, sl_levels=2, tp_levels=4, smc_bos=True), False),
            ("k2_s2_t4_buy+bos+lq30",       Q(k_in=2, sl_levels=2, tp_levels=4, smc_bos=True, smc_lq=True, lq_lookback=30), False),
            ("k2_s2_t4_buy+bos+zone3",      Q(k_in=2, sl_levels=2, tp_levels=4, smc_bos=True, smc_zone=True, zone_atr=3.0), False),
            ("k2_s2_t4_buy+bos+lq60",       Q(k_in=2, sl_levels=2, tp_levels=4, smc_bos=True, smc_lq=True, lq_lookback=60), False),
            ("k2_s2_t4_buy+all",            Q(k_in=2, sl_levels=2, tp_levels=4, smc_bos=True, smc_lq=True, lq_lookback=30, smc_zone=True), False),
            ("k3_s3_t6_both (baseline)",    Q(side="both"), False),
            ("k3_s3_t6_both+bos",           Q(side="both", smc_bos=True), False),
            ("k3_s3_t6_both+bos+zone3",     Q(side="both", smc_bos=True, smc_zone=True, zone_atr=3.0), False),
            ("k3_s3_t6_both+bos+lq60",      Q(side="both", smc_bos=True, smc_lq=True, lq_lookback=60), False),
            ("k3_s3_t6_both+all",           Q(side="both", smc_bos=True, smc_lq=True, lq_lookback=30, smc_zone=True), False),
            ("bounce_k3 (baseline)",        B(), False),
            ("bounce_k3+lq12",              B(smc_lq=True, lq_lookback=12), False),
            ("bounce_k3+lq30",              B(smc_lq=True, lq_lookback=30), False),
            ("bounce_k3+zone3",             B(smc_zone=True, zone_atr=3.0), False),
            ("bounce_k3+choch+zone3",       B(smc_bos=True, smc_zone=True, zone_atr=3.0), False),
            ("bounce_k3+zone",              B(smc_zone=True), False),
            ("bounce_k3+zone_slzone",       B(smc_zone=True, sl_mode="zone"), False),
            ("bounce_k3+choch(bos)+lq12",   B(smc_bos=True, smc_lq=True, lq_lookback=12), False),
            ("bounce_k3+lq12+zone",         B(smc_lq=True, lq_lookback=12, smc_zone=True), False),
            ("bounce_k3_buy+lq12+zone",     B(side="buy", smc_lq=True, lq_lookback=12, smc_zone=True), False),
        ]
        return out
    if buyonly:
        for k, s, t in ((2, 2, 4), (2, 2, 6), (3, 3, 6), (1, 2, 6), (2, 3, 6)):
            out.append((f"k{k}_s{s}_t{t}_tr50_buy", QuantumPriceLevel(k_in=k, sl_levels=s, tp_levels=t, side="buy"), False))
            out.append((f"k{k}_s{s}_t{t}_tr200_buy", QuantumPriceLevel(k_in=k, sl_levels=s, tp_levels=t, side="buy", trend_n=200), False))
        out.append(("k2_s2_t4_tr0_buy", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, side="buy", trend_n=0), False))
        out.append(("k2_s2_t4_tr50_buy_trail", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, side="buy"), True))
        out.append(("bounce_k3_s2_t3_tr50_buy", QuantumPriceLevelBounce(side="buy"), False))
        out.append(("bounce_k2_s2_t2_tr50_buy", QuantumPriceLevelBounce(k_in=2, tp_levels=2, side="buy"), False))
        return out
    if sweep:
        for k in (1, 2, 3):
            for s in (2, 3):
                for t in (3, 4, 6):
                    out.append((f"k{k}_s{s}_t{t}_tr50", QuantumPriceLevel(k_in=k, sl_levels=s, tp_levels=t, side="both"), False))
        for k, s, t in ((2, 2, 4), (1, 2, 4), (3, 3, 6)):
            out.append((f"k{k}_s{s}_t{t}_tr0", QuantumPriceLevel(k_in=k, sl_levels=s, tp_levels=t, trend_n=0, side="both"), False))
            out.append((f"k{k}_s{s}_t{t}_tr50_trail", QuantumPriceLevel(k_in=k, sl_levels=s, tp_levels=t, side="both"), True))
            out.append((f"k{k}_s{s}_t{t}_tr50_os0", QuantumPriceLevel(k_in=k, sl_levels=s, tp_levels=t, max_overshoot=0, side="both"), False))
        out.append(("k2_s2_t4_tr50_lam0", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, lam=0.0, side="both"), False))
        out.append(("k2_s2_t4_tr50_lam0.2", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, lam=0.2, side="both"), False))
        out.append(("k2_s2_t4_tr50_vol60", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, vol_n=60, side="both"), False))
        out.append(("k2_s2_t4_tr50_vol190", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, vol_n=190, side="both"), False))
        out.append(("k2_s2_t4_tr200", QuantumPriceLevel(k_in=2, sl_levels=2, tp_levels=4, trend_n=200, side="both"), False))
        for k in (2, 3, 4):
            for t in (2, 3):
                for tr in (0, 50):
                    out.append((f"bounce_k{k}_s2_t{t}_tr{tr}",
                                QuantumPriceLevelBounce(k_in=k, sl_levels=2, tp_levels=t, trend_n=tr), False))
    else:
        out.append(("quantum_qpl", QuantumPriceLevel(), False))            # live default (k3_s3_t6 buy)
        out.append(("quantum_qpl_smc", QuantumPriceLevelSmc(), False))     # + demand zone 2 ATR
        out.append(("quantum_qpl_bounce", QuantumPriceLevelBounce(), False))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2015)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--oos", type=int, default=2021, help="first out-of-sample year")
    ap.add_argument("--cost", type=float, default=0.25, help="$/oz round trip (VT spread .11 + slippage)")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--buyonly", action="store_true", help="buy-side-only variants")
    ap.add_argument("--smc", action="store_true", help="SMC confluence variants (BOS/CHoCH, LQ sweep, supply/demand)")
    ap.add_argument("--detail", default="quantum_qpl")
    ap.add_argument("--out", default="quantum_qpl.txt")
    args = ap.parse_args()
    t_start = time.time()
    m15 = load_m15(start_year=args.start, end_year=args.end)
    h4 = resample_clock(m15, 14400)
    m15_t = np.array([int(b.time.timestamp()) for b in m15])
    m15_h = np.array([b.high for b in m15]); m15_l = np.array([b.low for b in m15]); m15_c = np.array([b.close for b in m15])
    years = (m15_t[-1] - m15_t[0]) / 86400 / 365.25
    os.makedirs(REPORTS, exist_ok=True)
    out = open(os.path.join(REPORTS, args.out), "w", encoding="utf-8")

    class Tee:
        def write(self, s): sys.stdout.write(s); out.write(s)
        def flush(self): sys.stdout.flush(); out.flush()
    tee = Tee()
    P = lambda *a: print(*a, file=tee)
    P(f"Quantum Price Level bot — XAUUSD M15 {m15[0].time:%Y-%m-%d} -> {m15[-1].time:%Y-%m-%d} "
      f"({len(m15)} M15 bars, {len(h4)} H4 bars, {years:.1f} yr), cost ${args.cost}/oz round trip, "
      f"risk {RISK*100:.0f}%/trade, {WINDOW} H4 bars per decision (= live loop)")
    P("label = k<entry level>_s<SL rungs>_t<TP rungs>_tr<trend EMA n>[_trail|_os0|_lam*|_vol*]; "
      f"IS = {args.start}-{args.oos-1}, OOS = {args.oos}-{args.end}\n")
    cache = {}
    results = {}
    oos_start = datetime(args.oos, 1, 1, tzinfo=timezone.utc)
    hdr = f"{'variant':<30s} {'FULL':^60s} | {'IS':^40s} | {'OOS':^40s}"
    P(hdr)
    for label, st, trail in variants(args.sweep, args.buyonly, args.smc):
        trades, eq = simulate(st, h4, m15_t, m15_h, m15_l, m15_c, args.cost, trail, cache)
        results[label] = (trades, eq)
        s_full = trade_stats(trades)
        is_t = [t for t in trades if t.close_t < oos_start]; oos_t = [t for t in trades if t.close_t >= oos_start]
        si, so = trade_stats(is_t), trade_stats(oos_t)
        split = int(np.searchsorted(m15_t, int(oos_start.timestamp())))
        dd_is = max_dd(eq[:split]) if split > 1 else 0.0
        dd_oos = max_dd(eq[split:] / eq[split - 1]) if split < len(eq) - 1 else 0.0
        P(f"{label:<30s} n={s_full['n']:4d} WR={s_full['wr']*100:5.1f}% W/L={s_full['wl']:4.2f} PF={s_full['pf']:4.2f} "
          f"avgR={s_full['avg_r']:+.3f} CAGR={cagr(eq, years)*100:+5.1f}% DD={max_dd(eq)*100:4.1f}% "
          f"| n={si['n']:4d} WR={si['wr']*100:4.1f}% PF={si['pf']:4.2f} avgR={si['avg_r']:+.3f} DD={dd_is*100:4.1f}% "
          f"| n={so['n']:4d} WR={so['wr']*100:4.1f}% PF={so['pf']:4.2f} avgR={so['avg_r']:+.3f} DD={dd_oos*100:4.1f}%")
        tee.flush()
    if args.detail in results:
        detail_report(args.detail, *results[args.detail], m15_t, tee)
    else:
        P(f"\n(no variant named {args.detail}; detail skipped)")
    P(f"\ndone in {time.time()-t_start:.0f}s")
    out.close()


if __name__ == "__main__":
    main()
