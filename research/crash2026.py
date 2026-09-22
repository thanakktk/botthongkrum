"""Replay the calibrated buy-only grid (grandma_real) through the REAL 2026 gold
crash ($5,598 on Jan-29 -> $3,943 on Jun-30) using VT Markets' own H1 history,
which our 22-year CSV (ends Jan-2026) does not contain."""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
from dotenv import load_dotenv
import grid_lab as G
from grandma_real import GrandmaReal
G.LEVERAGE = 2000
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "backtest", "XAUUSD_ECN_H1_vt.csv")

def fetch():
    import MetaTrader5 as mt5
    load_dotenv(os.path.join(ROOT, ".env"))
    assert mt5.initialize(login=int(os.getenv("MT5_LOGIN")), password=os.getenv("MT5_PASSWORD"),
                          server=os.getenv("MT5_SERVER"), path=os.getenv("MT5_TERMINAL_PATH"))
    mt5.symbol_select("XAUUSD-ECN", True)
    r = mt5.copy_rates_range("XAUUSD-ECN", mt5.TIMEFRAME_H1, datetime(2020, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))
    mt5.shutdown()
    with open(CSV, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["time", "open", "high", "low", "close", "tick_volume"])
        for x in r: w.writerow([int(x["time"]), x["open"], x["high"], x["low"], x["close"], int(x["tick_volume"])])
    return len(r)

def load(start):
    rows = [r for r in csv.DictReader(open(CSV)) if int(r["time"]) >= start]
    return ([int(r["time"]) for r in rows], [float(r["open"]) for r in rows], [float(r["high"]) for r in rows],
            [float(r["low"]) for r in rows], [float(r["close"]) for r in rows])

def run(scale, spacing, start, label):
    t, o, h, l, c = load(start)
    lot = round(scale * G.CAPITAL / 1000, 5)
    acc = G.Account(G.CAPITAL); sys_ = GrandmaReal(lot, spacing)
    day = None; peak = G.CAPITAL; maxdd = 0; worst_eq = G.CAPITAL; worst_t = None; maxpos = 0
    for i in range(len(t)):
        d = datetime.fromtimestamp(t[i], timezone.utc)
        if day != d.date():
            if day is not None: acc.swap(c[i])
            day = d.date()
        if acc.check_stop_out(l[i], h[i], t[i]): break
        sys_.on_bar(acc, o[i], h[i], l[i], c[i], i)
        maxpos = max(maxpos, len(acc.pos))
        eq = acc.equity(l[i]); peak = max(peak, acc.equity(c[i]))
        if (peak - eq) / peak > maxdd: maxdd = (peak - eq) / peak; worst_eq = eq; worst_t = d
    fin = acc.equity(c[-1]) if acc.ruined_at is None else 0
    print(f"{label:<12s} scale={scale:<8} sp=${spacing:<3.0f} lot={lot}  end_equity={fin/G.CAPITAL-1:+7.1%}  "
          f"maxDD={maxdd:6.1%} at {worst_t:%Y-%m-%d}  max_open={maxpos:3d}  balance={acc.balance/G.CAPITAL-1:+.1%}  "
          f"ruin={datetime.fromtimestamp(acc.ruined_at,timezone.utc).date() if acc.ruined_at else '-'}")

if __name__ == "__main__":
    if not os.path.exists(CSV): print("fetched", fetch(), "H1 bars from VT")
    jul24 = int(datetime(2024, 7, 8, tzinfo=timezone.utc).timestamp())
    for sp in (7.0, 14.0):
        for k in (1, 2, 5):
            run(round(0.0001 / 1.3 * k, 6), sp, jul24, "Jul24->now")
