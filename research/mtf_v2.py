"""
MTF v2: stack the entry conditions that were good in BOTH halves of
mtf_features.py on top of the default (3/3 bias, M15 pullback, swing SL, 2R),
test each gate alone, all together, ablation (all minus one), and trade
management (break-even / trailing / 3R). IS 2015-2020, OOS 2021-2026.

CAVEAT: the gates were chosen looking at both halves, so the OOS here is not
untouched. The per-gate rows show how much each contributes; the 2024-2026
column is the most recent regime.

    ./env/Scripts/python.exe research/mtf_v2.py [--cost-bps 0.625 | --cost 0.25] [--detail <label>]
Writes reports/mtf_v2.txt (+ reports/mtf_v2_trades.csv for the detail row).
"""
from __future__ import annotations
import sys, os, argparse, csv
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15
from backtester import resample_clock
from mtf_backtest import Cfg, simulate, arrays, ema, atr, htf_bias, asof, SECS, REPORTS, stats, max_dd, detail
from mtf_features import build_features


def gates(feat, n):
    """Each gate: name -> {1: buy_ok[], -1: sell_ok[]} (direction-aware)."""
    T = np.ones(n, dtype=bool)
    def both(cond): return {1: cond, -1: cond}
    def signed(arr_buy, arr_sell): return {1: arr_buy, -1: arr_sell}
    g = {}
    g["session 15-24h"] = both((feat["hour"] >= 15))
    g["no Asia 3-7h"] = both(~((feat["hour"] >= 3) & (feat["hour"] < 7)))
    g["vol: ATR ratio > 0.75"] = both(np.nan_to_num(feat["atr_ratio"]) > 0.75)
    g["D1 strength >= 0.7 ATR"] = signed(np.nan_to_num(feat["D1_strength"]) >= 0.7, np.nan_to_num(-feat["D1_strength"]) >= 0.7)
    g["D1 bias age >= 4 days"] = both(np.nan_to_num(feat["D1_bias_age"]) >= 4)
    g["H4 bias age >= 6 bars"] = both(np.nan_to_num(feat["H4_bias_age"]) >= 6)
    g["H1 bias age <= 55 bars"] = both(np.nan_to_num(feat["H1_bias_age"], nan=999) <= 55)
    g["trigger >= 0.3 ATR from EMA20"] = signed(np.nan_to_num(feat["dist_e20_atr"]) >= 0.3, np.nan_to_num(-feat["dist_e20_atr"]) >= 0.3)
    g["close >= 0.4 ATR from EMA50"] = signed(np.nan_to_num(feat["dist_e50_atr"]) >= 0.4, np.nan_to_num(-feat["dist_e50_atr"]) >= 0.4)
    g["H1 RSI(dir) > 51"] = signed(np.nan_to_num(feat["H1_rsi"]) > 51, (100 - np.nan_to_num(feat["H1_rsi"], nan=50)) > 51)
    g["D1 ADX > 16"] = both(np.nan_to_num(feat["D1_adx"]) > 16)
    g["not Thursday"] = both(feat["weekday"] != 3)
    return g


def AND(*gs):
    out = {1: np.ones_like(gs[0][1]), -1: np.ones_like(gs[0][-1])}
    for g in gs:
        out[1] = out[1] & g[1]; out[-1] = out[-1] & g[-1]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2015); ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--oos", type=int, default=2021); ap.add_argument("--cost", type=float, default=0.25)
    ap.add_argument("--cost-bps", type=float, default=0.625); ap.add_argument("--risk", type=float, default=0.005)
    ap.add_argument("--detail", default="CORE + BE 1R"); ap.add_argument("--out", default="mtf_v2.txt")
    ap.add_argument("--quick", action="store_true", help="reduced-gate (MIN/MID) study")
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
    G = gates(feat, n)
    core_names = ["session 15-24h", "vol: ATR ratio > 0.75", "D1 strength >= 0.7 ATR", "D1 bias age >= 4 days",
                  "H4 bias age >= 6 bars", "trigger >= 0.3 ATR from EMA20", "H1 RSI(dir) > 51", "D1 ADX > 16"]
    CORE = AND(*[G[k] for k in core_names])
    lite_names = ["session 15-24h", "vol: ATR ratio > 0.75", "D1 bias age >= 4 days", "trigger >= 0.3 ATR from EMA20"]
    LITE = AND(*[G[k] for k in lite_names])
    MIN = AND(G["session 15-24h"], G["D1 strength >= 0.7 ATR"], G["H4 bias age >= 6 bars"], G["H1 RSI(dir) > 51"])
    MID = AND(MIN, G["D1 bias age >= 4 days"], G["vol: ATR ratio > 0.75"])
    if args.quick:
        runs = [("baseline (3/3 pullback K24 swing 2R)", Cfg(), None),
                ("MIN (session, D1 strength, H4 age, H1 RSI)", Cfg(), MIN),
                ("MIN + TP 3R + BE 1R", Cfg(rr=3.0, be_r=1.0), MIN),
                ("MIN + TP 3R + BE 1R buy-only", Cfg(rr=3.0, be_r=1.0, side="buy"), MIN),
                ("MIN + TP 2R + BE 1R", Cfg(be_r=1.0), MIN),
                ("MIN + TP 4R + BE 1R", Cfg(rr=4.0, be_r=1.0), MIN),
                ("MIN + TP 3R + BE 0.7R", Cfg(rr=3.0, be_r=0.7), MIN),
                ("MIN + TP 3R + BE 1.5R", Cfg(rr=3.0, be_r=1.5), MIN),
                ("MIN + TP 3R + BE 1R + trail 2R", Cfg(rr=3.0, be_r=1.0, trail_r=2.0), MIN),
                ("MIN + TP 3R + BE 1R + max_pos 2", Cfg(rr=3.0, be_r=1.0, max_pos=2), MIN),
                ("MIN + TP 3R + BE 1R + hold 0", Cfg(rr=3.0, be_r=1.0, hold_h=0), MIN),
                ("MIN + TP 3R + BE 1R + K12", Cfg(rr=3.0, be_r=1.0, K=12), MIN),
                ("MIN + TP 3R + BE 1R, session 13-24", Cfg(rr=3.0, be_r=1.0), AND(G["D1 strength >= 0.7 ATR"], G["H4 bias age >= 6 bars"], G["H1 RSI(dir) > 51"], {1: feat["hour"] >= 13, -1: feat["hour"] >= 13})),
                ("MIN + TP 3R + BE 1R, session 7-24", Cfg(rr=3.0, be_r=1.0), AND(G["D1 strength >= 0.7 ATR"], G["H4 bias age >= 6 bars"], G["H1 RSI(dir) > 51"], {1: feat["hour"] >= 7, -1: feat["hour"] >= 7})),
                ("MID (MIN + D1 age + vol)", Cfg(), MID),
                ("MID + TP 3R + BE 1R", Cfg(rr=3.0, be_r=1.0), MID),
                ("CORE + TP 3R + BE 1R", Cfg(rr=3.0, be_r=1.0), CORE),
                ("MIN + breakout + TP 3R + BE 1R", Cfg(entry="breakout", rr=3.0, be_r=1.0), MIN),
                ("MIN + TP 3R + BE 1R, need2", Cfg(rr=3.0, be_r=1.0, need=2), MIN)]
    else:
        runs = [("baseline (3/3 pullback K24 swing 2R)", Cfg(), None)]
    if not args.quick:
      for k in G: runs.append((f"+ {k}", Cfg(), G[k]))
    if not args.quick:
      runs.append(("LITE (session, vol, D1 age, trigger dist)", Cfg(), LITE))
      runs.append(("CORE (8 gates)", Cfg(), CORE))
      for k in core_names:
        runs.append((f"CORE - {k}", Cfg(), AND(*[G[j] for j in core_names if j != k])))
      runs += [
        ("CORE + BE 1R", Cfg(be_r=1.0), CORE),
        ("CORE + trail 1R", Cfg(trail_r=1.0), CORE),
        ("CORE + BE 1R + trail 1.5R", Cfg(be_r=1.0, trail_r=1.5), CORE),
        ("CORE + TP 3R", Cfg(rr=3.0), CORE),
        ("CORE + TP 3R + BE 1R", Cfg(rr=3.0, be_r=1.0), CORE),
        ("CORE + TP 1.5R", Cfg(rr=1.5), CORE),
        ("CORE + hold 0", Cfg(hold_h=0), CORE),
        ("CORE + hold 48", Cfg(hold_h=48), CORE),
        ("CORE + min stop 0.8 ATR", Cfg(min_stop_atr=0.8), CORE),
        ("CORE + max_pos 2", Cfg(max_pos=2), CORE),
        ("CORE buy-only", Cfg(side="buy"), CORE),
        ("CORE + breakout entry", Cfg(entry="breakout"), CORE),
        ("LITE + BE 1R", Cfg(be_r=1.0), LITE),
        ("LITE + TP 3R + BE 1R", Cfg(rr=3.0, be_r=1.0), LITE),
    ]
    years = (L["t"][-1] - L["t"][0]) / 86400 / 365.25
    oos_ts = int(datetime(args.oos, 1, 1, tzinfo=timezone.utc).timestamp())
    t24 = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    out = open(os.path.join(REPORTS, args.out), "w", encoding="utf-8")
    class Tee:
        def write(self, s): sys.stdout.write(s); out.write(s)
        def flush(self): sys.stdout.flush(); out.flush()
    tee = Tee(); P = lambda *a: print(*a, file=tee)
    P(f"MTF v2 — XAUUSD M15 {m15[0].time:%Y-%m-%d} -> {m15[-1].time:%Y-%m-%d}, cost {('%.3f bps' % args.cost_bps) if args.cost_bps else ('$%s' % args.cost)}, risk {args.risk*100:.2f}%  |  IS < {args.oos} <= OOS  |  last column = 2024-2026\n")
    P(f"{'variant':<44s} {'n':>5s} {'/day':>5s} {'WR%':>5s} {'W/L':>5s} {'PF':>5s} {'avgR':>7s} {'CAGR':>7s} {'DD':>6s} | {'IS PF':>5s} {'avgR':>7s} | {'OOS PF':>6s} {'avgR':>7s} {'DD':>5s} | {'24-26 PF':>8s} {'n':>4s}")
    res = {}
    for label, cfg, gate in runs:
        trades, eq = simulate(cfg, L, HT, args.cost, args.risk, args.cost_bps, gate=gate)
        res[label] = (trades, eq)
        s = stats(trades); days = (L["t"][-1] - L["t"][0]) / 86400 * 5 / 7
        is_t = [x for x in trades if x["close_t"] < oos_ts]; oos_t = [x for x in trades if x["close_t"] >= oos_ts]
        r24 = [x for x in trades if x["close_t"] >= t24]
        si, so, s24 = stats(is_t), stats(oos_t), stats(r24)
        k = int(np.searchsorted(L["t"], oos_ts)); dd_oos = max_dd(eq[k:] / eq[k - 1]) if 1 < k < len(eq) else 0
        P(f"{label:<44s} {s['n']:>5d} {s['n']/days:>5.2f} {s['wr']*100:>5.1f} {s['wl']:>5.2f} {s['pf']:>5.2f} {s['avg_r']:>+7.3f} {(max(eq[-1],1e-9)**(1/years)-1)*100:>+6.1f}% {max_dd(eq)*100:>5.1f}% "
          f"| {si['pf']:>5.2f} {si['avg_r']:>+7.3f} | {so['pf']:>6.2f} {so['avg_r']:>+7.3f} {dd_oos*100:>4.1f}% | {s24['pf']:>8.2f} {s24['n']:>4d}")
        tee.flush()
    if args.detail in res:
        detail(args.detail, *res[args.detail], L["t"], tee, "M15")
        os.replace(os.path.join(REPORTS, "mtf_M15_trades.csv"), os.path.join(REPORTS, "mtf_v2_trades.csv"))
    out.close()


if __name__ == "__main__":
    main()
