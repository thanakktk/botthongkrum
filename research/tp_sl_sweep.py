"""What win-rate / expectancy do the H4 signals give with FIXED $ TP/SL (the
user's scalping-style exits: SL $5-10, TP $5-20) vs the ATR-based 1.5ATR/2.5R?
Entry at the H4 close (+cost), exits checked on M15 bars, stop-first."""
import sys, os, itertools
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from histdata import load_m15
from backtester import resample_clock
from strategies import select_strategies
COST = 0.25            # spread .11 + slip; charged once per round trip (in $/oz)
LOT_USD = 10.0         # 0.1 lot: $1 move = $10
MAX_HOLD_BARS = 4 * 24 * 10   # 10 days of M15
m15 = load_m15(start_year=2015)
h4 = resample_clock(m15, 14400)
t15 = np.array([int(b.time.timestamp()) for b in m15]); hi15 = np.array([b.high for b in m15]); lo15 = np.array([b.low for b in m15])
strats = select_strategies(("breakout_sr", "donchian_breakout", "roc_momentum"))
sigs = []   # (m15 start index, dir, close, atr_rdist)
for i in range(40, len(h4)):
    win = h4[i - 40:i + 1]
    for st in strats:
        s = st.generate("XAUUSD", win, win[-1].time)
        if s is None: continue
        d = 1 if s.direction.value == "buy" else -1
        j = np.searchsorted(t15, int(win[-1].time.timestamp()) + 14400)   # first M15 after the H4 close
        if j < len(t15): sigs.append((j, d, s.entry, abs(s.entry - s.sl)))
print(f"{len(sigs)} H4 signals 2015-2026 (3 strategies)")
def run(tp_fn, sl_fn, label):
    wins = 0; pnl = []; holds = []
    for j, d, entry, rdist in sigs:
        tp, sl = tp_fn(rdist), sl_fn(rdist)
        e = entry + d * COST
        res = None
        for k in range(j, min(j + MAX_HOLD_BARS, len(t15))):
            if d > 0:
                if lo15[k] <= e - sl: res = -sl; break
                if hi15[k] >= e + tp: res = tp; break
            else:
                if hi15[k] >= e + sl: res = -sl; break
                if lo15[k] <= e - tp: res = tp; break
        if res is None: res = 0.0            # time exit ~flat (approx)
        pnl.append(res); holds.append(k - j)
        wins += res > 0
    p = np.array(pnl) * LOT_USD
    gw, gl = p[p > 0].sum(), -p[p < 0].sum()
    print(f"{label:<28s} WR {wins/len(p)*100:5.1f}%  avg ${p.mean():+6.2f}/trade(0.1lot)  PF {gw/max(gl,1e-9):4.2f}  "
          f"total ${p.sum():+9,.0f}  avg hold {np.mean(holds)/4:4.1f}h")
print("\n--- fixed $ exits (your style) ---")
for tp, sl in [(5,5),(5,10),(10,5),(10,10),(20,10),(20,5),(20,20),(10,20),(5,20)]:
    run(lambda r, tp=tp: tp, lambda r, sl=sl: sl, f"TP ${tp:>2d}  SL ${sl:>2d}")
print("\n--- ATR-based (the EA) ---")
run(lambda r: 2.5 * r, lambda r: r, "TP 2.5R  SL 1.5ATR (EA)")
run(lambda r: 1.0 * r, lambda r: r, "TP 1R    SL 1.5ATR")
run(lambda r: 0.5 * r, lambda r: r, "TP 0.5R  SL 1.5ATR")
run(lambda r: 0.25 * r, lambda r: r, "TP 0.25R SL 1.5ATR")
