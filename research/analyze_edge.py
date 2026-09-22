"""
Edge Analyzer (Phase 2 — The Quest for Edge)
======================================================================
Data-driven pruning. For each of the 13 strategies, run it STANDALONE (no
confluence — relaxed arbitration gates so the single technique trades on its
own) on real XAUUSD history, on each analysis timeframe (M30/H1/H4), and
measure the things that actually decide whether it has an edge:

  * WR   — hit rate at the system's real 2.5R target / 1R stop
  * PF   — gross win / gross loss
  * avgR — expectancy per trade in R units (the number that compounds)
  * MAE  — Max Adverse Excursion (how deep it goes against us; SL too tight?)
  * MFE  — Max Favorable Excursion (how far it runs before reversing; TP wrong?)

…broken down by REGIME (trend/range) and SESSION (asian/london/ny), so we can
cut the dead combinations and keep only what pays. Nothing is tuned here — the
report just tells us what the data says.

Resampling note: each TF run is performed ON that TF's bars directly (M5
aggregated up), so 13×3 = 39 standalone backtests run in seconds rather than
replaying every M5 tick.

    ./env/Scripts/python.exe analyze_edge.py [SYMBOL] [M5_COUNT]
    ./env/Scripts/python.exe analyze_edge.py XAUUSD 40000
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import sys
from collections import defaultdict
from statistics import mean, median

from mt5_broker import Mt5Broker
from backtester import Backtester, BacktestConfig, Trade, resample
from arbitration import Arbitrator, ArbitrationConfig, FAMILY
from strategies import DEFAULT_STRATEGIES
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase

# analysis timeframes (M30+ only — lower TFs are noisy/less reliable), in M5 units
TFS: dict[str, int] = {"M30": 6, "H1": 12, "H4": 48}
MIN_N = 20                       # a config needs this many trades to be actionable

# Realistic XAUUSD costs (verified live 2026-06-20): spread ~$0.44. Commission on
# metals is negligible vs an R of several dollars — set 0 and confirm vs FTMO.
COST = dict(spread=0.44, slippage=0.05, commission_per_lot=0.0)


def _standalone_arb() -> ArbitrationConfig:
    """Arbitration with every confluence gate OPEN, so one technique trades alone
    and we can read its raw, isolated edge."""
    return ArbitrationConfig(
        timeframes=("M5",), tf_weights={"M5": 1.0},   # the served bars ARE this TF
        min_agreement=0.0, min_agree=1, min_families=1, min_conviction=0.0,
        signal_floor=0.0, risk_pct=0.0002,            # tiny risk -> never trip floors
        regime_n=20, tp1_r=1.0, tp2_r=2.5, allowed_sessions=(),
    )


def run_config(symbol, strategy, tf_bars) -> list[Trade]:
    profile = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    arb = Arbitrator(_standalone_arb())
    bt = Backtester(profile, [strategy], arb,
                    BacktestConfig(risk_pct=0.0002, **COST), specs={symbol: 100.0})
    res = bt.run(symbol, tf_bars)
    if res.floor_breached:                # tiny risk should prevent this; warn if not
        print(f"   ! {strategy.id}/{len(tf_bars)}bars breached a floor "
              f"(risk too high for scan)", file=sys.stderr)
    return res.trades


def agg(trades: list[Trade]) -> dict | None:
    n = len(trades)
    if n == 0:
        return None
    wins = [t for t in trades if t.pnl > 0]
    gw = sum(t.pnl for t in wins)
    gl = -sum(t.pnl for t in trades if t.pnl <= 0)
    pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
    return dict(n=n, wr=len(wins) / n, pf=pf,
                avgR=mean(t.r_mult for t in trades),
                maeR=mean(t.mae_r for t in trades),
                mfeR=mean(t.mfe_r for t in trades))


def verdict(a: dict) -> str:
    if a["n"] < MIN_N:
        return "THIN"
    if a["avgR"] > 0.05 and a["pf"] > 1.1:
        return "KEEP"
    if a["avgR"] > 0:
        return "WATCH"
    return "CUT"


def _pf(x: float) -> str:
    return "inf" if x == float("inf") else f"{x:.2f}"


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "XAUUSD"
    m5_count = int(sys.argv[2]) if len(sys.argv) > 2 else 40000

    with Mt5Broker() as b:
        m5 = b.get_bars(symbol, "M5", m5_count)
    if not m5:
        print(f"No M5 data for {symbol} (terminal history?).", file=sys.stderr)
        sys.exit(1)

    span = f"{m5[0].time:%Y-%m-%d} -> {m5[-1].time:%Y-%m-%d}"
    print(f"=== EDGE ANALYSIS: {symbol} ===")
    print(f"M5 bars: {len(m5):,}  ({span})  | TFs: {', '.join(TFS)} | "
          f"target 2.5R / stop 1R | costs spread={COST['spread']} | min_n={MIN_N}\n")

    # run every (strategy, TF) standalone; keep trades tagged by strategy/tf
    rows: list[tuple] = []                 # (strat_id, family, tf, agg, trades)
    all_trades: list[Trade] = []
    for st in DEFAULT_STRATEGIES:
        for tf, factor in TFS.items():
            bars = resample(m5, factor)
            if len(bars) < 60:
                continue
            trades = run_config(symbol, st, bars)
            for t in trades:               # retag tf for pooled breakdowns
                t.session = t.session or "off"
            a = agg(trades)
            if a:
                rows.append((st.id, FAMILY.get(st.id, "?"), tf, a, trades))
                all_trades.extend(trades)

    # ---- 1) per-config table, ranked by expectancy ---- #
    print("--- PER STRATEGY x TIMEFRAME (ranked by expectancy avgR) ---")
    print(f"{'strategy':22s}{'family':10s}{'TF':5s}{'n':>5s}{'WR%':>6s}"
          f"{'PF':>6s}{'avgR':>7s}{'maeR':>6s}{'mfeR':>6s}  verdict")
    for sid, fam, tf, a, _ in sorted(rows, key=lambda r: -r[3]["avgR"]):
        print(f"{sid[:21]:22s}{fam[:9]:10s}{tf:5s}{a['n']:5d}{a['wr']*100:6.0f}"
              f"{_pf(a['pf']):>6s}{a['avgR']:7.2f}{a['maeR']:6.2f}{a['mfeR']:6.2f}"
              f"  {verdict(a)}")

    # ---- 2) per-strategy pooled across TFs ---- #
    by_strat: dict[str, list[Trade]] = defaultdict(list)
    for sid, _, _, _, trades in rows:
        by_strat[sid].extend(trades)
    print("\n--- PER STRATEGY (pooled across M30/H1/H4) ---")
    print(f"{'strategy':22s}{'n':>6s}{'WR%':>6s}{'PF':>6s}{'avgR':>7s}  verdict")
    pooled = {sid: agg(ts) for sid, ts in by_strat.items()}
    for sid, a in sorted(pooled.items(), key=lambda kv: -kv[1]["avgR"]):
        print(f"{sid[:21]:22s}{a['n']:6d}{a['wr']*100:6.0f}{_pf(a['pf']):>6s}"
              f"{a['avgR']:7.2f}  {verdict(a)}")

    # ---- 3) session + regime breakdown (all techniques pooled) ---- #
    def breakdown(key_fn, title):
        groups: dict[str, list[Trade]] = defaultdict(list)
        for t in all_trades:
            groups[key_fn(t)].append(t)
        print(f"\n--- BY {title} (all techniques pooled) ---")
        print(f"{title.lower():12s}{'n':>7s}{'WR%':>6s}{'PF':>6s}{'avgR':>7s}")
        for k, ts in sorted(groups.items(), key=lambda kv: -agg(kv[1])["avgR"]):
            a = agg(ts)
            print(f"{k[:11]:12s}{a['n']:7d}{a['wr']*100:6.0f}{_pf(a['pf']):>6s}"
                  f"{a['avgR']:7.2f}")

    breakdown(lambda t: t.session or "off", "SESSION")
    breakdown(lambda t: t.regime or "?", "REGIME")

    # ---- 4) MFE/MAE insight: are TP/SL placed where the moves actually go? ---- #
    winners = [t for t in all_trades if t.pnl > 0]
    losers = [t for t in all_trades if t.pnl <= 0]
    print("\n--- TP/SL PLACEMENT (MFE/MAE) ---")
    if winners:
        print(f"winners: avg MFE {mean(t.mfe_r for t in winners):.2f}R "
              f"(median {median(t.mfe_r for t in winners):.2f}R) — "
              f"how far runners go (TP2 is 2.5R)")
    if losers:
        print(f"losers : avg MFE {mean(t.mfe_r for t in losers):.2f}R — "
              f"how green they got BEFORE reversing into the stop "
              f"(>1R ⇒ a TP1/break-even would have saved many)")
        print(f"losers : avg MAE {mean(t.mae_r for t in losers):.2f}R "
              f"(stop is at 1.0R)")

    # ---- 5) data-driven recommendations ---- #
    keep = [sid for sid, a in pooled.items() if verdict(a) in ("KEEP", "WATCH")]
    cut = [sid for sid, a in pooled.items() if verdict(a) == "CUT"]
    thin = [sid for sid, a in pooled.items() if verdict(a) == "THIN"]
    print("\n=== RECOMMENDATIONS (data-driven) ===")
    print(f"KEEP ({len(keep)}): {', '.join(keep) or '— none had positive expectancy'}")
    print(f"CUT  ({len(cut)}): {', '.join(cut) or '—'}")
    if thin:
        print(f"THIN ({len(thin)}, <{MIN_N} trades, inconclusive): {', '.join(thin)}")
    pos_sessions = [k for k in ("asian", "london", "ny")
                    if (g := [t for t in all_trades if k in (t.session or "")])
                    and agg(g)["avgR"] > 0]
    print(f"Best sessions (positive avgR): {', '.join(pos_sessions) or '— none'}")
    if winners:
        print(f"TP guidance: winners' median MFE ≈ "
              f"{median(t.mfe_r for t in winners):.1f}R "
              f"(current TP2=2.5R) — set TP2 near where runners actually stall.")


if __name__ == "__main__":
    main()
