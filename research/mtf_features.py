"""
MTF edge mining: which conditions at entry raise win-rate / expectancy, in BOTH
in-sample (2015-2020) and out-of-sample (2021-2026)?
======================================================================
Runs the default MTF config (3-of-3 D1/H4/H1 bias, M15 pullback, swing SL,
2R) with per-trade features recorded at the entry bar, then buckets every
feature and prints n / WR / avgR / PF per bucket for IS and OOS side by side.
A filter is "robust" only if the good buckets are good in both halves.

    ./env/Scripts/python.exe research/mtf_features.py [--cost-bps 0.625] [--start 2015] [--end 2026]
Writes reports/mtf_features.txt and reports/mtf_features_trades.csv
"""
from __future__ import annotations
import sys, os, argparse, csv
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15
from backtester import resample_clock
from mtf_backtest import Cfg, simulate, arrays, ema, atr, htf_bias, asof, SECS, REPORTS, stats


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0]); up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au = np.empty_like(c); ad = np.empty_like(c); au[0] = up[:n].mean(); ad[0] = dn[:n].mean()
    for i in range(1, len(c)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n; ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    return 100 - 100 / (1 + au / np.where(ad == 0, 1e-9, ad))


def adx(h, l, c, n=14):
    up = np.diff(h, prepend=h[0]); dn = -np.diff(l, prepend=l[0])
    pdm = np.where((up > dn) & (up > 0), up, 0.0); ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1)))); tr[0] = h[0] - l[0]
    def rma(x):
        out = np.empty_like(x); out[0] = x[:n].mean()
        for i in range(1, len(x)): out[i] = (out[i - 1] * (n - 1) + x[i]) / n
        return out
    atr_ = rma(tr); pdi = 100 * rma(pdm) / np.where(atr_ == 0, 1e-9, atr_); ndi = 100 * rma(ndm) / np.where(atr_ == 0, 1e-9, atr_)
    dx = 100 * abs(pdi - ndi) / np.where(pdi + ndi == 0, 1e-9, pdi + ndi)
    return rma(dx)


def build_features(L, HT, K=24):
    """Per-bar features as of the LTF bar close (buy-oriented; see mtf_v2 for direction-aware use)."""
    n = len(L["t"])
    feat = {}
    ts = np.array([datetime.fromtimestamp(int(x), timezone.utc) for x in L["t"]])
    feat["hour"] = np.array([t.hour for t in ts], dtype=float)
    feat["weekday"] = np.array([t.weekday() for t in ts], dtype=float)
    a15 = L["atr"]
    atr_ma = np.convolve(np.nan_to_num(a15), np.ones(96) / 96, mode="full")[:n]
    feat["atr_ratio"] = a15 / np.where(atr_ma == 0, np.nan, atr_ma)            # current vol vs 1-day mean
    feat["rsi15"] = rsi(L["c"])
    feat["bar_strength"] = (L["c"] - L["o"]) / np.where(L["h"] - L["l"] == 0, np.nan, L["h"] - L["l"])   # close location (signed)
    feat["body_atr"] = abs(L["c"] - L["o"]) / a15
    feat["dist_e20_atr"] = (L["c"] - L["e20"]) / a15
    e50_15 = ema(L["c"], 50)
    feat["dist_e50_atr"] = (L["c"] - e50_15) / a15
    K = 24
    swing_lo = np.full(n, np.nan); swing_hi = np.full(n, np.nan)
    for i in range(K, n):
        swing_lo[i] = L["l"][i - K:i + 1].min(); swing_hi[i] = L["h"][i - K:i + 1].max()
    feat["pull_depth_atr"] = (L["e20"] - swing_lo) / a15         # how far the dip went below EMA20 (buys)
    feat["pull_depth_atr_s"] = (swing_hi - L["e20"]) / a15       # sells
    feat["swing_dist_atr"] = (L["c"] - swing_lo) / a15           # = stop size proxy for buys
    for tf, (arr, bias, atr_tf, idx) in HT.items():
        ef, es = ema(arr["c"], 20), ema(arr["c"], 50)
        strength = (ef - es) / atr_tf
        ad = adx(arr["h"], arr["l"], arr["c"])
        r_tf = rsi(arr["c"])
        ii = np.clip(idx, 0, None)
        feat[f"{tf}_strength"] = np.where(idx >= 0, strength[ii], np.nan)      # EMA gap in ATR (signed)
        feat[f"{tf}_adx"] = np.where(idx >= 0, ad[ii], np.nan)
        feat[f"{tf}_rsi"] = np.where(idx >= 0, r_tf[ii], np.nan)
        feat[f"{tf}_dist_e20_atr"] = np.where(idx >= 0, (arr["c"][ii] - ef[ii]) / atr_tf[ii], np.nan)
        feat[f"{tf}_atr_bps"] = np.where(idx >= 0, atr_tf[ii] / arr["c"][ii] * 1e4, np.nan)
        # bars since the bias flipped to its current value (trend age)
        age = np.zeros(len(bias));
        for k in range(1, len(bias)): age[k] = age[k - 1] + 1 if bias[k] == bias[k - 1] else 0
        feat[f"{tf}_bias_age"] = np.where(idx >= 0, age[ii], np.nan)
    return feat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2015); ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--oos", type=int, default=2021); ap.add_argument("--cost", type=float, default=0.25)
    ap.add_argument("--cost-bps", type=float, default=0.625); ap.add_argument("--risk", type=float, default=0.005)
    args = ap.parse_args()
    m15 = load_m15(start_year=args.start, end_year=args.end)
    L = arrays(m15); n = len(L["t"])
    L["e20"], L["e9"], L["e21"] = ema(L["c"], 20), ema(L["c"], 9), ema(L["c"], 21)
    L["atr"] = atr(L["h"], L["l"], L["c"], 14)
    L_end = L["t"] + SECS["M15"]
    HT = {}
    for tf in ("H1", "H4", "D1"):
        arr = arrays(resample_clock(m15, SECS[tf]))
        HT[tf] = (arr, htf_bias(arr), atr(arr["h"], arr["l"], arr["c"], 14), asof(arr["t"], SECS[tf], L_end))
    feat = build_features(L, HT, K=24)
    # signed versions (so buys and sells share buckets): multiply by trade dir later
    cfg = Cfg()
    trades, eq = simulate(cfg, L, HT, args.cost, args.risk, args.cost_bps, feat=feat)
    signed = ("bar_strength", "dist_e20_atr", "dist_e50_atr", "H1_strength", "H4_strength", "D1_strength",
              "H1_dist_e20_atr", "H4_dist_e20_atr", "D1_dist_e20_atr")
    for t in trades:
        for k in signed: t[k] = t[k] * t["d"]
        t["rsi15_dir"] = t["rsi15"] if t["d"] > 0 else 100 - t["rsi15"]
        for tf in ("H1", "H4", "D1"): t[f"{tf}_rsi_dir"] = t[f"{tf}_rsi"] if t["d"] > 0 else 100 - t[f"{tf}_rsi"]
        t["pull_depth"] = t["pull_depth_atr"] if t["d"] > 0 else t["pull_depth_atr_s"]
    oos_ts = int(datetime(args.oos, 1, 1, tzinfo=timezone.utc).timestamp())
    out = open(os.path.join(REPORTS, "mtf_features.txt"), "w", encoding="utf-8")
    class Tee:
        def write(self, s): sys.stdout.write(s); out.write(s)
        def flush(self): sys.stdout.flush(); out.flush()
    P = lambda *a: print(*a, file=Tee())
    s_all = stats(trades)
    P(f"MTF default (3/3, pullback K24, swing SL, 2R), cost {args.cost_bps} bps: {len(trades)} trades, WR {s_all['wr']*100:.1f}%, PF {s_all['pf']:.2f}, avgR {s_all['avg_r']:+.3f}")
    is_t = [t for t in trades if t["close_t"] < oos_ts]; oos_t = [t for t in trades if t["close_t"] >= oos_ts]
    P(f"IS {args.start}-{args.oos-1}: n={len(is_t)} WR={stats(is_t)['wr']*100:.1f}% PF={stats(is_t)['pf']:.2f} avgR={stats(is_t)['avg_r']:+.3f}   "
      f"OOS {args.oos}-{args.end}: n={len(oos_t)} WR={stats(oos_t)['wr']*100:.1f}% PF={stats(oos_t)['pf']:.2f} avgR={stats(oos_t)['avg_r']:+.3f}\n")

    def table(name, key, edges=None, cats=None):
        vals = np.array([t[key] for t in trades], dtype=float)
        if cats is None:
            ok = ~np.isnan(vals)
            if edges is None:
                edges = np.nanquantile(vals[ok], [0.2, 0.4, 0.6, 0.8])
            bucket = np.digitize(vals, edges)
            labels = [f"<= {edges[0]:.2f}"] + [f"{edges[j-1]:.2f}..{edges[j]:.2f}" for j in range(1, len(edges))] + [f"> {edges[-1]:.2f}"]
        else:
            bucket = vals.astype(int); labels = cats
        P(f"--- {name} ---")
        P(f"{'bucket':<18s} | {'IS n':>5s} {'WR%':>5s} {'avgR':>7s} {'PF':>5s} | {'OOS n':>5s} {'WR%':>5s} {'avgR':>7s} {'PF':>5s} | verdict")
        for b in range(len(labels)):
            sel_is = [t for t, bb in zip(trades, bucket) if bb == b and t["close_t"] < oos_ts]
            sel_oos = [t for t, bb in zip(trades, bucket) if bb == b and t["close_t"] >= oos_ts]
            si, so = stats(sel_is), stats(sel_oos)
            if si["n"] + so["n"] == 0: continue
            v = ("+" if si["avg_r"] > stats(is_t)["avg_r"] + 0.02 else "-" if si["avg_r"] < stats(is_t)["avg_r"] - 0.02 else "=") + \
                ("+" if so["avg_r"] > stats(oos_t)["avg_r"] + 0.02 else "-" if so["avg_r"] < stats(oos_t)["avg_r"] - 0.02 else "=")
            tag = {"++": "ROBUST GOOD", "--": "ROBUST BAD"}.get(v, "")
            P(f"{labels[b]:<18s} | {si['n']:>5d} {si['wr']*100:>5.1f} {si['avg_r']:>+7.3f} {si['pf']:>5.2f} | {so['n']:>5d} {so['wr']*100:>5.1f} {so['avg_r']:>+7.3f} {so['pf']:>5.2f} | {v} {tag}")
        P("")

    table("direction (1 buy / -1 sell)", "d", cats=None, edges=np.array([0.0]))
    table("entry hour (server)", "hour", edges=np.array([3, 7, 11, 15, 19]))
    table("weekday (0=Mon)", "weekday", edges=np.array([0.5, 1.5, 2.5, 3.5]))
    table("M15 ATR / 1-day mean ATR (vol regime)", "atr_ratio")
    table("RSI(14) M15 in trade direction", "rsi15_dir")
    table("trigger bar close location (signed, 1 = closed at extreme)", "bar_strength")
    table("trigger bar body / ATR", "body_atr")
    table("close - EMA20 (M15) in ATR (how extended at entry)", "dist_e20_atr")
    table("close - EMA50 (M15) in ATR", "dist_e50_atr")
    table("pullback depth beyond EMA20 (ATR)", "pull_depth")
    table("stop distance (ATR-ish, $)", "rd")
    table("H1 EMA20-50 gap / ATR (trend strength)", "H1_strength")
    table("H4 EMA20-50 gap / ATR", "H4_strength")
    table("D1 EMA20-50 gap / ATR", "D1_strength")
    table("H1 ADX(14)", "H1_adx")
    table("H4 ADX(14)", "H4_adx")
    table("D1 ADX(14)", "D1_adx")
    table("H1 RSI in trade direction", "H1_rsi_dir")
    table("H4 RSI in trade direction", "H4_rsi_dir")
    table("D1 RSI in trade direction", "D1_rsi_dir")
    table("H1 close - EMA20 / ATR_H1 (extension)", "H1_dist_e20_atr")
    table("H4 close - EMA20 / ATR_H4", "H4_dist_e20_atr")
    table("D1 close - EMA20 / ATR_D1", "D1_dist_e20_atr")
    table("H4 ATR in bps of price (vol level)", "H4_atr_bps")
    table("H1 bias age (bars since H1 flipped)", "H1_bias_age")
    table("H4 bias age (bars)", "H4_bias_age")
    table("D1 bias age (days)", "D1_bias_age")
    out.close()
    keys = [k for k in trades[0].keys()]
    with open(os.path.join(REPORTS, "mtf_features_trades.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(keys)
        for t in trades: w.writerow([t[k] for k in keys])


if __name__ == "__main__":
    main()
