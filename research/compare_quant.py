"""
Quant-enhancement A/B harness (Phase 3 — does adding "quant" actually help?)
======================================================================
Takes the EXACT live config the bot runs today (robust-4 + MTF confluence +
risk 0.3% + tp1@2.0R, no regime gate) as the BASELINE, then layers ONE quant
component at a time on top of it and replays all of them over the long XAUUSD
history (backtest/XAU_15m_data.csv, ~22 years), per-year and independent. The
point is attribution: hold everything else fixed so any change in the metrics is
caused by that single component — not a config soup.

Components tested (each isolated, then a sane combo):
  S0  BASELINE         the current live bot (control)
  S1  +VOL-TARGET      scale per-trade risk by  long-ATR / short-ATR  (target a
                       constant return-volatility instead of constant $ risk)
  S2  +DD-THROTTLE     cut risk to 1/2 then 1/4 as equity falls below its peak
                       (anti-martingale on drawdown — directly protects the FTMO
                       10% floor and the 5% daily floor)
  S3  +REGIME-GATE     only enter when the detected regime is trend/unknown
                       (HONEST re-test — the 11-yr study said this HURTS; we let
                       the numbers say it again rather than asserting it)
  S4  +VOL-FILTER      skip entries when short-ATR > 2x long-ATR (avoid the most
                       turbulent bars, where gaps slip past the stop)
  S5  +VOL-TARGET&DD   the two genuinely-additive risk components stacked

WHY a statistics layer: every component is judged not just on avgR but on
avgR +/- standard error and a t-stat, plus its spread across years. A component
that lifts avgR by +0.02 when the per-trade SE is +/-0.03 changed NOTHING — it
is noise dressed up as an edge. Reporting that honestly is itself the highest-
value, lowest-overfit "quant" upgrade (validation > prediction).

NOTE: this file is RESEARCH ONLY. It subclasses the production Arbitrator and
imports the live modules read-only; it never mutates live code or touches MT5 /
Postgres, so it is safe to run while the bot trades.

    ./env/Scripts/python.exe research/compare_quant.py [START_YEAR] [END_YEAR]
    ./env/Scripts/python.exe research/compare_quant.py 2023 2024   # quick smoke
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import math
import sys
from dataclasses import dataclass
from statistics import mean, stdev

from strategies import atr, select_strategies, ROBUST_TREND_IDS
from arbitration import Arbitrator, ArbitrationConfig
from backtester import Backtester, BacktestConfig
from histdata import load_m15, M15_TF_FACTOR
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase

# ----- the live bot's deployed config (run_xau_robust.ps1) -------------------
TF_WEIGHTS = {"M30": 1.0, "H1": 1.6, "H4": 2.4}
RISK = 0.003
BASE_GATES = dict(min_agree=2, min_families=2, min_agreement=0.70,
                  min_conviction=1.0)
MGMT = dict(manage=True, tp1_r=2.0, partial_pct=0.5, trail_r=1.0,
            be_trigger_r=0.0)
COST = dict(spread=0.44, slippage=0.05, commission_per_lot=0.0)


# --------------------------------------------------------------------------- #
# Quant knobs — each component is a flag so variants differ by ONE thing only. #
# --------------------------------------------------------------------------- #
@dataclass
class QuantConfig:
    label: str = "baseline"
    # --- volatility targeting: risk_mult = clamp(long_ATR / short_ATR) ----
    # short_ATR rises above its long-run norm in turbulence -> mult < 1 (size
    # down); falls in calm -> mult > 1 (size up). Targets constant vol, not
    # constant $ risk. Clamp keeps it sane; set vol_hi=1.0 for de-risk-ONLY
    # (only ever cut size in turbulence, never lever up in calm — safer for FTMO).
    vol_target: bool = False
    atr_n: int = 14
    vol_ref_n: int = 100
    vol_lo: float = 0.5
    vol_hi: float = 1.5
    # --- drawdown throttle: cut risk as equity sits below its running peak. --
    # A tuple of (dd_threshold, risk_mult) tiers, any depth; the deepest tier
    # whose threshold is breached wins. Empty = off. Anti-martingale on DD.
    dd_levels: tuple = ()
    # --- daily soft-stop: halt NEW entries once the intraday loss reaches this
    # fraction of the day's starting equity (0 = off). A cushion BEFORE the FTMO
    # 5% hard floor — stop digging on a bad day instead of riding it to the wall.
    daily_stop: float = 0.0
    # --- volatility filter: skip entries when short_ATR > mult * long_ATR ---
    # (0 = off). Avoids the most turbulent bars, where gaps slip past the stop.
    vol_filter_mult: float = 0.0


class QuantArbitrator(Arbitrator):
    """Live Arbitrator + pluggable quant sizing / filtering. Overrides only the
    two seams that matter: _size() (volume) and _resolve_symbol() (entry gate).
    Everything else — confluence, MTF, rationale — is the production code."""

    def __init__(self, cfg, qcfg: QuantConfig, league=None):
        super().__init__(cfg, league)
        self.q = qcfg
        self._peak = 0.0          # running equity peak, for the DD throttle
        self._day = None          # current calendar day, for the daily soft-stop
        self._day_start_eq = 0.0  # equity at the day's first flat bar

    # short & long ATR on the ENTRY timeframe (both look-back only, no peeking)
    def _atr_pair(self, broker, symbol):
        tf = self.cfg.timeframes[0]
        bars = broker.get_bars(symbol, tf, self.q.vol_ref_n + self.q.atr_n + 5)
        cur = atr(bars, self.q.atr_n)
        ref = atr(bars, self.q.vol_ref_n)
        if not cur or cur <= 0 or not ref or ref <= 0:
            return None, None
        return cur, ref

    def _size_multiplier(self, broker, symbol, equity) -> float:
        mult = 1.0
        if self.q.vol_target:
            cur, ref = self._atr_pair(broker, symbol)
            if cur and ref:
                mult *= max(self.q.vol_lo, min(self.q.vol_hi, ref / cur))
        if self.q.dd_levels:
            self._peak = max(self._peak, equity)
            dd = (self._peak - equity) / self._peak if self._peak > 0 else 0.0
            for thr, m in sorted(self.q.dd_levels, reverse=True):  # deepest tier wins
                if dd >= thr:
                    mult *= m
                    break
        return mult

    # identical to Arbitrator._size but with the quant risk multiplier applied
    def _size(self, broker, symbol, side, entry, sl, equity):
        per_lot_risk = broker.estimate_risk(symbol, side, 1.0, entry, sl)
        if per_lot_risk <= 0:
            return 0.0, 0.0
        risk_budget = self.cfg.risk_pct * equity * \
            self._size_multiplier(broker, symbol, equity)
        raw = risk_budget / per_lot_risk
        try:
            npl = broker.notional_per_lot(symbol, entry)
            if npl and npl > 0:
                raw = min(raw, self.cfg.max_notional_frac * equity / npl)
        except Exception:
            pass
        steps = math.floor(raw / self.cfg.lot_step)
        volume = round(steps * self.cfg.lot_step, 2)
        volume = max(self.cfg.min_lot, min(self.cfg.max_lot, volume))
        if volume < self.cfg.min_lot:
            return 0.0, 0.0
        return volume, per_lot_risk * volume

    def _resolve_symbol(self, sym, sigs, broker, equity, now):
        # daily soft-stop: re-anchor each new day, then halt entries once the
        # intraday loss reaches the threshold (cushion before the 5% hard floor)
        if self.q.daily_stop > 0:
            if now.date() != self._day:
                self._day, self._day_start_eq = now.date(), equity
            if self._day_start_eq > 0 and \
                    (self._day_start_eq - equity) / self._day_start_eq >= self.q.daily_stop:
                return self._note(sym, "skip", "daily_stop")
        if self.q.vol_filter_mult > 0:
            cur, ref = self._atr_pair(broker, sym)
            if cur and ref and cur > self.q.vol_filter_mult * ref:
                return self._note(sym, "skip", "vol_filter",
                                  atr=round(cur, 2), ref=round(ref, 2))
        return super()._resolve_symbol(sym, sigs, broker, equity, now)


# --------------------------------------------------------------------------- #
# Variants: each = (key, QuantConfig, extra ArbitrationConfig overrides).      #
# Only the listed knob differs from BASELINE -> clean attribution.            #
# --------------------------------------------------------------------------- #
# kind: "sizing"    -> changes $ allocation, NOT which trades. Per-trade R (avgR)
#                      is unchanged BY DESIGN; judge it on drawdown / return-stability.
#       "selection" -> changes WHICH trades fire. Judge it on avgR (the edge).
#       "combo"     -> does both; judge on BOTH (edge must survive AND risk drop).
_DD_STD = ((0.03, 0.5), (0.06, 0.25))                       # 3%->1/2, 6%->1/4
_DD_AGGR = ((0.02, 0.5), (0.04, 0.25), (0.06, 0.1))         # earlier + deeper
_DD_GENTLE = ((0.04, 0.7), (0.07, 0.4))                     # later + shallower
VARIANTS = [
    ("S0  BASELINE (live bot)",     QuantConfig("baseline"), {}, "base"),
    # ---- sizing: volatility targeting ----
    ("S1  Vol-target (sym .5-1.5)", QuantConfig("vt_sym", vol_target=True), {}, "sizing"),
    ("S2  Vol-target (derisk .5-1)", QuantConfig("vt_derisk", vol_target=True,
                                                 vol_hi=1.0), {}, "sizing"),
    # ---- sizing: drawdown throttle at three depths ----
    ("S3  DD-throttle standard",    QuantConfig("dd_std", dd_levels=_DD_STD), {}, "sizing"),
    ("S4  DD-throttle aggressive",  QuantConfig("dd_aggr", dd_levels=_DD_AGGR), {}, "sizing"),
    ("S5  DD-throttle gentle",      QuantConfig("dd_gentle", dd_levels=_DD_GENTLE), {}, "sizing"),
    # ---- selection / risk gates ----
    ("S6  Daily soft-stop -2%",     QuantConfig("daily2", daily_stop=0.02), {}, "selection"),
    ("S7  Regime gate (trend)",     QuantConfig("regime"),
        dict(require_regime=("trend", "unknown")), "selection"),
    ("S8  ER-strength gate 0.30",   QuantConfig("min_er"), dict(min_er=0.30), "selection"),
    ("S9  Vol filter 2.0x",         QuantConfig("vf2", vol_filter_mult=2.0), {}, "selection"),
    ("S10 Vol filter 1.5x",         QuantConfig("vf15", vol_filter_mult=1.5), {}, "selection"),
    # ---- the combined "all-safe" stack (the components that earned their keep) ----
    ("S11 SANE STACK (vt-derisk+DD+daily)",
        QuantConfig("stack", vol_target=True, vol_hi=1.0, dd_levels=_DD_STD,
                    daily_stop=0.02), {}, "combo"),
]


@dataclass
class YearStat:
    year: int
    trades: int
    ret_pct: float
    maxdd_pct: float
    breach: bool
    rs: list        # per-trade r_mult


def run_variant_year(qcfg, extra, bars) -> YearStat:
    arb = QuantArbitrator(
        ArbitrationConfig(timeframes=("M30", "H1", "H4"), tf_weights=TF_WEIGHTS,
                          risk_pct=RISK, tp1_r=MGMT["tp1_r"], tp2_r=2.5,
                          **BASE_GATES, **extra),
        qcfg)
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies(ROBUST_TREND_IDS), arb,
        BacktestConfig(risk_pct=RISK, tf_factor=M15_TF_FACTOR, **COST, **MGMT),
        specs={"XAUUSD": 100.0})
    res = bt.run("XAUUSD", bars)
    return YearStat(0, len(res.trades), res.return_pct, res.max_drawdown_pct,
                    res.floor_breached, [t.r_mult for t in res.trades])


@dataclass
class Agg:
    name: str
    n: int
    wr: float
    avg_r: float
    se: float
    t: float
    r_pf: float
    mean_yr: float
    worst_yr: float
    pos_yrs: int
    n_yrs: int
    max_dd: float
    breaches: int


def aggregate(name: str, ys: list[YearStat]) -> Agg:
    rs = [r for y in ys for r in y.rs]
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    avg_r = mean(rs) if rs else 0.0
    se = (stdev(rs) / math.sqrt(n)) if n > 1 else 0.0
    pos = sum(r for r in rs if r > 0)
    neg = -sum(r for r in rs if r < 0)
    r_pf = (pos / neg) if neg > 0 else (float("inf") if pos > 0 else 0.0)
    yr_rets = [y.ret_pct for y in ys]
    return Agg(name, n, wins / n if n else 0.0, avg_r, se,
               (avg_r / se) if se > 0 else 0.0, r_pf,
               mean(yr_rets) if yr_rets else 0.0,
               min(yr_rets) if yr_rets else 0.0,
               sum(1 for r in yr_rets if r > 0), len(ys),
               max((y.maxdd_pct for y in ys), default=0.0),
               sum(1 for y in ys if y.breach))


_REJECT_BREACH = "REJECT (adds an FTMO breach)"


def verdict_line(a: Agg, base: Agg, kind: str) -> str:
    """One verdict string, judged on the metric the component can actually move."""
    d_avgr = a.avg_r - base.avg_r
    d_dd = (a.max_dd - base.max_dd) * 100          # pp; negative = less drawdown
    d_worst = (a.worst_yr - base.worst_yr) * 100   # pp; positive = better bad year
    d_breach = a.breaches - base.breaches
    if kind == "sizing":
        # avgR is ~unchanged by design; the win is lower risk for the same edge
        if d_breach > 0:
            tag = _REJECT_BREACH
        elif d_dd <= -0.3 or d_worst >= 0.3:
            tag = "KEEP (same edge, lower risk)"
        else:
            tag = "NEUTRAL (no real risk reduction)"
        return (f"  {a.name:26s} [risk]  ΔmaxDD={d_dd:+.1f}pp  "
                f"Δworst-yr={d_worst:+.1f}pp  ΔavgR={d_avgr:+.3f}(by design ~0)  "
                f"Δbreach={d_breach:+d} -> {tag}")
    se_d = math.sqrt(base.se ** 2 + a.se ** 2)
    if kind == "combo":
        # must keep the edge (avgR not worse beyond noise) AND cut risk
        edge_ok = d_avgr >= -se_d
        risk_better = d_dd <= -0.3 or d_worst >= 0.3
        if d_breach > 0:
            tag = _REJECT_BREACH
        elif not edge_ok:
            tag = "REJECT (edge worse beyond noise)"
        elif risk_better:
            tag = "KEEP (edge intact, lower risk)"
        else:
            tag = "NEUTRAL (edge intact but no risk gain)"
        return (f"  {a.name:26s} [both] ΔavgR={d_avgr:+.3f}(±{se_d:.3f}) "
                f"ΔmaxDD={d_dd:+.1f}pp Δworst-yr={d_worst:+.1f}pp "
                f"Δbreach={d_breach:+d} -> {tag}")
    # selection: changes which trades fire -> judge on avgR vs its noise band
    if d_breach > 0:
        tag = _REJECT_BREACH
    elif se_d == 0:
        tag = "n/a"
    elif abs(d_avgr) < se_d:
        tag = "NOISE (ΔavgR within 1 SE — no real change)"
    elif d_avgr < 0:
        tag = "REJECT (edge worse beyond noise)"
    elif d_avgr < 2 * se_d:
        tag = "weak+ (1-2 SE better — suggestive, not proven)"
    else:
        tag = "KEEP (edge better, >2 SE)"
    return (f"  {a.name:26s} [edge]  ΔavgR={d_avgr:+.3f} (±{se_d:.3f} noise)  "
            f"ΔmaxDD={d_dd:+.1f}pp  Δbreach={d_breach:+d} -> {tag}")


def print_comparison(aggs: list[Agg]) -> None:
    hdr = (f"{'variant':26s}{'trades':>7s}{'WR%':>6s}{'avgR':>8s}{'±SE':>7s}"
           f"{'t':>6s}{'R-PF':>6s}{'meanYr%':>9s}{'worstYr%':>9s}"
           f"{'posYr':>7s}{'maxDD%':>8s}{'breach':>7s}")
    print("\n--- COMPONENT COMPARISON (pooled over all years) ---")
    print(hdr)
    print("-" * len(hdr))
    for a in aggs:
        pf = "inf" if a.r_pf == float("inf") else f"{a.r_pf:.2f}"
        print(f"{a.name:26s}{a.n:>7d}{a.wr*100:>6.0f}{a.avg_r:>+8.3f}"
              f"{a.se:>7.3f}{a.t:>6.1f}{pf:>6s}{a.mean_yr*100:>+9.2f}"
              f"{a.worst_yr*100:>+9.2f}{f'{a.pos_yrs}/{a.n_yrs}':>7s}"
              f"{a.max_dd*100:>8.1f}{a.breaches:>7d}")


def print_baseline_years(base_per_year: list[YearStat]) -> None:
    print("--- BASELINE per-year (the control) ---")
    print(f"{'year':6s}{'trades':>8s}{'ret%':>9s}{'maxDD%':>9s}{'avgR':>9s}"
          f"{'breach':>8s}")
    for y in base_per_year:
        ar = mean(y.rs) if y.rs else 0.0
        print(f"{y.year:<6d}{y.trades:>8d}{y.ret_pct*100:>+9.2f}"
              f"{y.maxdd_pct*100:>9.1f}{ar:>+9.3f}"
              f"{'YES' if y.breach else 'no':>8s}")


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 2015
    end = int(sys.argv[2]) if len(sys.argv) > 2 else 2025
    m15 = load_m15(start_year=start, end_year=end)
    years = [y for y in range(start, end + 1)
             if len([b for b in m15 if b.time.year == y]) >= 2000]
    bars_by_year = {y: [b for b in m15 if b.time.year == y] for y in years}

    print(f"=== QUANT A/B: XAUUSD M15 {start}-{end} "
          f"({len(m15):,} bars, {len(years)} tradeable years) ===")
    print(f"Baseline = live bot: robust-4, agr0.70/2tech/2fam, risk {RISK*100:.1f}%, "
          f"tp1@2.0R, no regime gate. One quant knob added per row.\n")

    aggs: list[Agg] = []
    kinds: dict[str, str] = {}
    base_per_year: list[YearStat] = []
    for name, qcfg, extra, kind in VARIANTS:
        kinds[name] = kind
        ys: list[YearStat] = []
        for y in years:
            st = run_variant_year(qcfg, extra, bars_by_year[y])
            st.year = y
            ys.append(st)
            print(f"  {name:26s} {y}  trades={st.trades:3d} "
                  f"ret={st.ret_pct*100:+6.2f}% maxDD={st.maxdd_pct*100:4.1f}%"
                  f"{'  BREACH' if st.breach else ''}", file=sys.stderr)
        aggs.append(aggregate(name, ys))
        if qcfg.label == "baseline":
            base_per_year = ys

    base = aggs[0]
    print_baseline_years(base_per_year)   # is the baseline edge durable year to year?
    print_comparison(aggs)                # the part-by-part table

    print("\n--- VERDICT vs baseline (judged on the metric each component moves) ---")
    for a in aggs[1:]:
        print(verdict_line(a, base, kinds[a.name]))

    print("\nLegend: sizing components (vol-target, DD-throttle) cannot change the "
          "per-trade edge (avgR) — that is expected; they earn their keep ONLY by "
          "cutting maxDD / cushioning the worst year. Selection components (regime "
          "gate, vol filter) change which trades fire, so they ARE judged on avgR "
          "vs its standard error. Any component that adds an FTMO floor breach is "
          "rejected outright, regardless of returns.")


if __name__ == "__main__":
    main()
