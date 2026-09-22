"""
Strategy Lab — many techniques, many trading styles, many bot types
======================================================================
One sweep over the full XAUUSD M15 history (2004-06 -> 2026-01) with
CLOCK-ALIGNED higher timeframes (like MT5), judged across four market eras so a
style that only worked in one regime is exposed:

    E1 2004-2010 gold bull | E2 2011-2015 top & bear | E3 2016-2020 | E4 2021-2026

Three families of jobs, run in parallel (multiprocessing):

  A. SOLO   — each of the 13 bot strategies ALONE on M30 / H1 / H4 (no
              confluence), through the real Backtester + live trade management.
  B. BOT    — ensemble bots through the real Backtester (MTF M30/H1/H4
              confluence): the live robust-4, the "vote" bot, all-13, and
              trend-only / reversion-only / SMC-only rosters.
  C. STYLE  — classic, textbook trading styles with their PUBLISHED default
              parameters (nothing tuned here): Turtle, MA 50/200, time-series
              momentum, Connors RSI(2), Bollinger fade, Asian-range breakout,
              Keltner / Donchian trend with chandelier trailing, M15 scalping.
              Plus buy & hold as the benchmark.

Costs everywhere: spread $0.44 + slippage $0.05 per fill. Risk 0.3% of equity
per trade, one position at a time, NO prop-firm rules (RULES_MODE=none).
Metric of record is R (profit / initial risk): comparable across styles.

    ./env/Scripts/python.exe research/strategy_lab.py [--only solo,bot,style] [--workers 11]

Writes reports/strategy_lab.json and reports/strategy_lab.txt.
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))

import argparse
import json
import math
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Pool

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = os.path.join(ROOT, "reports")

SPREAD, SLIP, RISK = 0.44, 0.05, 0.003
ERAS = [("E1 04-10", 2004, 2010), ("E2 11-15", 2011, 2015),
        ("E3 16-20", 2016, 2020), ("E4 21-26", 2021, 2026)]
TF_SECS = {"M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}

ALL13 = ("ema_cross_momentum", "bollinger_reversion", "breakout_sr", "rsi_reversal",
         "macd_trend", "donchian_breakout", "keltner_reversion",
         "bollinger_squeeze_breakout", "pivot_bounce", "roc_momentum",
         "fair_value_gap", "order_block_retest", "liquidity_sweep_reversal")
ROBUST4 = ("macd_trend", "roc_momentum", "donchian_breakout", "breakout_sr")
TREND6 = ("ema_cross_momentum", "macd_trend", "donchian_breakout", "breakout_sr",
          "roc_momentum", "bollinger_squeeze_breakout")
REVERT4 = ("bollinger_reversion", "keltner_reversion", "rsi_reversal", "pivot_bounce")
SMC3 = ("fair_value_gap", "order_block_retest", "liquidity_sweep_reversal")

LIVE_GATES = dict(min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0)
VOTE_GATES = dict(min_agree=3, min_families=2, min_agreement=0.80, min_conviction=2.0)
BOTS = {
    "bot:live_robust4": (ROBUST4, LIVE_GATES),
    "bot:vote_all13": (ALL13, VOTE_GATES),
    "bot:all13_loose": (ALL13, LIVE_GATES),
    "bot:trend6": (TREND6, LIVE_GATES),
    "bot:revert4": (REVERT4, LIVE_GATES),
    "bot:smc3": (SMC3, dict(LIVE_GATES, min_families=1)),
}


# --------------------------------------------------------------------------- #
# Data (per worker process, cached)                                            #
# --------------------------------------------------------------------------- #
_CACHE: dict = {}


def m15():
    if "m15" not in _CACHE:
        from histdata import load_m15
        _CACHE["m15"] = load_m15()
    return _CACHE["m15"]


def bars_tf(tf: str):
    if tf not in _CACHE:
        from backtester import resample_clock
        _CACHE[tf] = m15() if tf == "M15" else resample_clock(m15(), TF_SECS[tf])
    return _CACHE[tf]


def arrays(tf: str):
    key = "np_" + tf
    if key not in _CACHE:
        b = bars_tf(tf)
        _CACHE[key] = dict(
            t=np.array([int(x.time.timestamp()) for x in b]),
            o=np.array([x.open for x in b]), h=np.array([x.high for x in b]),
            l=np.array([x.low for x in b]), c=np.array([x.close for x in b]))
    return _CACHE[key]


# --------------------------------------------------------------------------- #
# Metrics                                                                      #
# --------------------------------------------------------------------------- #
def summarize(name: str, kind: str, trades: list[tuple[int, float]],
              extra: dict | None = None) -> dict:
    """trades = [(close_epoch, R), ...] in time order."""
    rs = [r for _, r in trades]
    yrs = defaultdict(list)
    for ts, r in trades:
        yrs[datetime.fromtimestamp(ts, timezone.utc).year].append(r)
    eras = {}
    for label, a, b in ERAS:
        er = [r for y, v in yrs.items() if a <= y <= b for r in v]
        eras[label] = {"n": len(er), "avgR": round(sum(er) / len(er), 3) if er else None,
                       "sumR": round(sum(er), 1)}
    years = {y: sum(v) for y, v in yrs.items() if len(v) >= 5}
    eq = peak = 1.0
    dd = 0.0
    for r in rs:
        eq = max(eq * (1 + RISK * r), 0.0)       # a -333R fill can't go below zero
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    span = (trades[-1][0] - trades[0][0]) / (365.25 * 86400) if len(trades) > 1 else 1
    wins = [r for r in rs if r > 0]
    gl = -sum(r for r in rs if r <= 0)
    se = (np.std(rs) / math.sqrt(len(rs))) if len(rs) > 1 else 0.0
    return {
        "name": name, "kind": kind, "n": len(rs),
        "trades_per_year": round(len(rs) / max(span, 1e-9), 1),
        "wr": round(len(wins) / len(rs) * 100, 1) if rs else 0,
        "avgR": round(sum(rs) / len(rs), 3) if rs else 0,
        "t_stat": round((sum(rs) / len(rs)) / se, 2) if rs and se > 0 else 0,
        "pf": round(sum(wins) / gl, 2) if gl > 0 else None,
        "sumR": round(sum(rs), 1),
        "eras": eras,
        "eras_positive": sum(1 for e in eras.values() if e["avgR"] and e["avgR"] > 0),
        "years_positive": f"{sum(1 for v in years.values() if v > 0)}/{len(years)}",
        "ret_pct_at_0.3": round((eq - 1) * 100, 1),
        "cagr_pct": round((eq ** (1 / max(span, 1)) - 1) * 100, 2) if eq > 0 else -100.0,
        "max_dd_pct": round(dd * 100, 1),
        **(extra or {}),
    }


# --------------------------------------------------------------------------- #
# A + B: the real Backtester                                                   #
# --------------------------------------------------------------------------- #
def _backtest(name, kind, ids, arb_kwargs, bars, tf_factor, timeframes, tf_weights):
    from backtester import Backtester, BacktestConfig
    from arbitration import Arbitrator, ArbitrationConfig
    from strategies import select_strategies
    from ftmo_compliance_engine import (AccountProfile, Variant, Path, Phase,
                                        EngineConfig)
    no_rules = EngineConfig(enforce_daily_loss=False, enforce_overall_loss=False,
                            enforce_consistency=False, enforce_weekend_flatten=False,
                            enforce_news_blackout=False)
    arb = Arbitrator(ArbitrationConfig(
        timeframes=timeframes, tf_weights=tf_weights, risk_pct=RISK,
        tp1_r=2.0, tp2_r=2.5, **arb_kwargs))
    bt = Backtester(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000),
        select_strategies(ids), arb,
        BacktestConfig(risk_pct=RISK, tf_factor=tf_factor, spread=SPREAD,
                       slippage=SLIP, manage=True, tp1_r=2.0, partial_pct=0.5,
                       trail_r=1.0, be_trigger_r=0.0),
        specs={"XAUUSD": 100.0}, engine_cfg=no_rules)
    res = bt.run("XAUUSD", bars)
    trades = [(int(t.closed_at.timestamp()), t.r_mult) for t in res.trades]
    return summarize(name, kind, trades)


def job_solo(sid: str, tf: str) -> dict:
    # The strategy reads the served bars directly: serve THIS TF as the base.
    return _backtest(f"solo:{sid}@{tf}", "solo", (sid,),
                     dict(min_agreement=0.0, min_agree=1, min_families=1,
                          min_conviction=0.0, signal_floor=0.0),
                     bars_tf(tf), {"M5": 1}, ("M5",), {"M5": 1.0})


def job_bot(name: str) -> dict:
    from histdata import M15_TF_FACTOR
    ids, gates = BOTS[name]
    return _backtest(name, "bot", ids, gates, bars_tf("M15"), M15_TF_FACTOR,
                     ("M30", "H1", "H4"), {"M30": 1.0, "H1": 1.6, "H4": 2.4})


# --------------------------------------------------------------------------- #
# C: classic styles — a small, explicit engine                                 #
# --------------------------------------------------------------------------- #
def sma(x, n):
    out = np.full(len(x), np.nan)
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n):
    out = np.empty(len(x))
    a = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(h, l, c, n=14):
    tr = np.maximum(h - l, np.maximum(abs(h - np.roll(c, 1)), abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    out = np.full(len(tr), np.nan)
    out[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):                      # Wilder smoothing
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def rsi(c, n):
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = np.full(len(c), np.nan), np.full(len(c), np.nan)
    au[n], ad[n] = up[1:n + 1].mean(), dn[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        au[i] = (au[i - 1] * (n - 1) + up[i]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i]) / n
    return 100 - 100 / (1 + au / np.where(ad == 0, 1e-12, ad))


def roll_max(x, n):          # max of the PREVIOUS n bars (excludes bar i)
    out = np.full(len(x), np.nan)
    for i in range(n, len(x)):
        out[i] = x[i - n:i].max()
    return out


def roll_min(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n, len(x)):
        out[i] = x[i - n:i].min()
    return out


class Style:
    """Signals are read on bar i's CLOSE and filled at bar i+1's OPEN (no
    look-ahead). Stops/targets are checked intrabar (stop first; a gap through
    the stop fills at the open). Trailing stops update on the close."""
    tf = "D1"
    max_hold = 0              # bars; 0 = no time exit

    def prepare(self, a): ...
    def entry(self, i):       # -> (side, stop_price, target_price|None) | None
        return None
    def exit(self, i, pos):   # -> bool, exit at next open
        return False
    def trail(self, i, pos):  # -> new stop | None
        return None


def run_style(st: Style) -> list[tuple[int, float]]:
    a = arrays(st.tf)
    st.prepare(a)
    t, o, h, l = a["t"], a["o"], a["h"], a["l"]
    cost = SPREAD / 2 + SLIP
    trades, pos, pend_entry, pend_exit = [], None, None, False

    def close(px, ts):
        nonlocal pos
        r = pos["d"] * (px - pos["entry"]) / pos["risk"]
        trades.append((int(ts), r))
        pos = None

    for i in range(1, len(t)):
        # fills at this bar's open
        if pend_exit and pos:
            close(o[i] - pos["d"] * cost, t[i])
        pend_exit = False
        if pend_entry and pos is None:
            side, stop, tgt = pend_entry
            d = 1 if side == "buy" else -1
            entry = o[i] + d * cost
            if d * (entry - stop) > 0:                 # not gapped through stop
                pos = {"d": d, "entry": entry, "stop": stop, "tgt": tgt,
                       "risk": d * (entry - stop), "i0": i, "hi": entry, "lo": entry}
        pend_entry = None
        # intrabar stop / target
        if pos:
            d = pos["d"]
            if (d == 1 and l[i] <= pos["stop"]) or (d == -1 and h[i] >= pos["stop"]):
                px = min(pos["stop"], o[i]) if d == 1 else max(pos["stop"], o[i])
                close(px - d * cost, t[i])
            elif pos["tgt"] is not None and (
                    (d == 1 and h[i] >= pos["tgt"]) or (d == -1 and l[i] <= pos["tgt"])):
                px = max(pos["tgt"], o[i]) if d == 1 else min(pos["tgt"], o[i])
                close(px - d * cost, t[i])
        # decisions on the close
        if pos:
            pos["hi"], pos["lo"] = max(pos["hi"], h[i]), min(pos["lo"], l[i])
            if st.exit(i, pos) or (st.max_hold and i - pos["i0"] >= st.max_hold):
                pend_exit = True
            else:
                ns = st.trail(i, pos)
                if ns is not None and not np.isnan(ns) and pos["d"] * (ns - pos["stop"]) > 0:
                    pos["stop"] = ns
        elif i < len(t) - 1:
            sig = st.entry(i)
            if sig is not None and not any(np.isnan(v) for v in sig[1:2]):
                pend_entry = sig
    if pos:
        close(a["c"][-1] - pos["d"] * cost, t[-1])
    return trades


# ---- the styles (textbook parameters) ------------------------------------- #
class Turtle(Style):
    """Donchian channel breakout (Turtle System 1/2): enter on an N-day high/low,
    2*ATR(20) stop, exit on the opposite X-day channel."""
    def __init__(self, n_in, n_out, long_only=False, tf="D1"):
        self.n_in, self.n_out, self.long_only, self.tf = n_in, n_out, long_only, tf
    def prepare(self, a):
        self.a = a
        self.hi, self.lo = roll_max(a["h"], self.n_in), roll_min(a["l"], self.n_in)
        self.xhi, self.xlo = roll_max(a["h"], self.n_out), roll_min(a["l"], self.n_out)
        self.atr = atr(a["h"], a["l"], a["c"], 20)
    def entry(self, i):
        c = self.a["c"][i]
        if c > self.hi[i]:
            return ("buy", c - 2 * self.atr[i], None)
        if c < self.lo[i] and not self.long_only:
            return ("sell", c + 2 * self.atr[i], None)
    def exit(self, i, pos):
        c = self.a["c"][i]
        return c < self.xlo[i] if pos["d"] == 1 else c > self.xhi[i]


class MaCross(Style):
    """Always-in-the-trend moving-average crossover (e.g. 50/200 'golden cross')."""
    def __init__(self, fast, slow, tf="D1", kind="sma", stop_atr=3.0, trail=False):
        self.f, self.s, self.tf, self.kind = fast, slow, tf, kind
        self.stop_atr, self.trail_on = stop_atr, trail
    def prepare(self, a):
        fn = sma if self.kind == "sma" else ema
        self.a, self.fa, self.sa = a, fn(a["c"], self.f), fn(a["c"], self.s)
        self.atr = atr(a["h"], a["l"], a["c"], 14)
    def entry(self, i):
        c = self.a["c"][i]
        up = self.fa[i] > self.sa[i]
        # enter on the cross, or re-enter in the trend after a stop-out
        if up and (self.fa[i - 1] <= self.sa[i - 1] or c > self.fa[i]):
            return ("buy", c - self.stop_atr * self.atr[i], None)
        if not up and (self.fa[i - 1] >= self.sa[i - 1] or c < self.fa[i]):
            return ("sell", c + self.stop_atr * self.atr[i], None)
    def exit(self, i, pos):
        return (self.fa[i] < self.sa[i]) if pos["d"] == 1 else (self.fa[i] > self.sa[i])
    def trail(self, i, pos):
        if not self.trail_on:
            return None
        k = self.stop_atr * self.atr[i]
        return pos["hi"] - k if pos["d"] == 1 else pos["lo"] + k


class Chandelier(Style):
    """Trend breakout (Keltner or Donchian) managed by a 3*ATR chandelier trail."""
    def __init__(self, mode, tf, n=20, mult=3.0):
        self.mode, self.tf, self.n, self.mult = mode, tf, n, mult
    def prepare(self, a):
        self.a, self.atr = a, atr(a["h"], a["l"], a["c"], 14)
        self.mid = ema(a["c"], self.n)
        self.hi, self.lo = roll_max(a["h"], self.n), roll_min(a["l"], self.n)
    def entry(self, i):
        c, k = self.a["c"][i], self.mult * self.atr[i]
        if self.mode == "keltner":
            up, dn = c > self.mid[i] + 2 * self.atr[i], c < self.mid[i] - 2 * self.atr[i]
        else:
            up, dn = c > self.hi[i], c < self.lo[i]
        if up:
            return ("buy", c - k, None)
        if dn:
            return ("sell", c + k, None)
    def trail(self, i, pos):
        k = self.mult * self.atr[i]
        return pos["hi"] - k if pos["d"] == 1 else pos["lo"] + k


class TSMom(Style):
    """Time-series momentum (managed-futures style): on the first D1 bar of each
    month go long/short by the sign of the trailing 12-month return; hold to the
    next month; 4*ATR catastrophe stop."""
    tf = "D1"
    def prepare(self, a):
        self.a, self.atr = a, atr(a["h"], a["l"], a["c"], 20)
        self.m = np.array([datetime.fromtimestamp(x, timezone.utc).month for x in a["t"]])
    def _new_month(self, i):
        return i + 1 < len(self.m) and self.m[i + 1] != self.m[i]
    def entry(self, i):
        if i < 252 or not self._new_month(i):
            return None
        c = self.a["c"][i]
        if c > self.a["c"][i - 252]:
            return ("buy", c - 4 * self.atr[i], None)
        return ("sell", c + 4 * self.atr[i], None)
    def exit(self, i, pos):
        return self._new_month(i) and i > pos["i0"]


class Rsi2(Style):
    """Connors RSI(2): buy extreme short-term dips in a 200-day uptrend (and the
    mirror short), exit on a close back through the 5-day SMA."""
    tf = "D1"
    max_hold = 10
    def prepare(self, a):
        c = a["c"]
        self.a, self.r = a, rsi(c, 2)
        self.s200, self.s5 = sma(c, 200), sma(c, 5)
        self.atr = atr(a["h"], a["l"], c, 14)
    def entry(self, i):
        c = self.a["c"][i]
        if c > self.s200[i] and self.r[i] < 10:
            return ("buy", c - 3 * self.atr[i], None)
        if c < self.s200[i] and self.r[i] > 90:
            return ("sell", c + 3 * self.atr[i], None)
    def exit(self, i, pos):
        c = self.a["c"][i]
        return c > self.s5[i] if pos["d"] == 1 else c < self.s5[i]


class BollFade(Style):
    """Mean reversion: fade a close outside the 20/2 Bollinger band back to the
    mid-line; 1.5*ATR stop; 24-bar time exit."""
    tf = "H1"
    max_hold = 24
    def prepare(self, a):
        c = a["c"]
        self.a, self.mid = a, sma(c, 20)
        sd = np.full(len(c), np.nan)
        for i in range(19, len(c)):
            sd[i] = c[i - 19:i + 1].std()
        self.up, self.dn = self.mid + 2 * sd, self.mid - 2 * sd
        self.atr = atr(a["h"], a["l"], c, 14)
    def entry(self, i):
        c = self.a["c"][i]
        if c < self.dn[i]:
            return ("buy", c - 1.5 * self.atr[i], None)
        if c > self.up[i]:
            return ("sell", c + 1.5 * self.atr[i], None)
    def exit(self, i, pos):
        c = self.a["c"][i]
        return c >= self.mid[i] if pos["d"] == 1 else c <= self.mid[i]


class AsianBreakout(Style):
    """Session breakout: the 00:00-07:00 (data server time) range; trade the first
    M15 close outside it between 07:00 and 12:00; stop at the opposite side,
    target 2R, flat by 20:00. One trade per day."""
    tf = "M15"
    def prepare(self, a):
        self.a = a
        dt = [datetime.fromtimestamp(x, timezone.utc) for x in a["t"]]
        self.hour = np.array([d.hour for d in dt])
        self.day = np.array([d.toordinal() for d in dt])
        n = len(dt)
        self.rhi, self.rlo = np.full(n, np.nan), np.full(n, np.nan)
        cur, hi, lo = None, -1e18, 1e18
        for i in range(n):
            if self.day[i] != cur:
                cur, hi, lo = self.day[i], -1e18, 1e18
            if self.hour[i] < 7:
                hi, lo = max(hi, a["h"][i]), min(lo, a["l"][i])
            elif hi > -1e18:
                self.rhi[i], self.rlo[i] = hi, lo
        self.traded = set()
    def entry(self, i):
        if not (7 <= self.hour[i] < 12) or self.day[i] in self.traded or np.isnan(self.rhi[i]):
            return None
        c, hi, lo = self.a["c"][i], self.rhi[i], self.rlo[i]
        if c > hi:
            self.traded.add(self.day[i])
            return ("buy", lo, c + 2 * (c - lo))
        if c < lo:
            self.traded.add(self.day[i])
            return ("sell", hi, c - 2 * (hi - c))
    def exit(self, i, pos):
        return self.hour[i] >= 20 or self.day[i] != self.day[pos["i0"]]


class Scalp(Style):
    """M15 trend-pullback scalp: with price above EMA50, a close back above EMA20
    after dipping below it; 1*ATR stop, 1.5R target, 4-hour time exit."""
    tf = "M15"
    max_hold = 16
    def prepare(self, a):
        c = a["c"]
        self.a, self.e20, self.e50 = a, ema(c, 20), ema(c, 50)
        self.atr = atr(a["h"], a["l"], c, 14)
    def entry(self, i):
        c, cp = self.a["c"][i], self.a["c"][i - 1]
        k = self.atr[i]
        if c > self.e50[i] and cp < self.e20[i - 1] and c > self.e20[i]:
            return ("buy", c - k, c + 1.5 * k)
        if c < self.e50[i] and cp > self.e20[i - 1] and c < self.e20[i]:
            return ("sell", c + k, c - 1.5 * k)


STYLES = {
    "style:turtle_20_10_D1": lambda: Turtle(20, 10),
    "style:turtle_55_20_D1": lambda: Turtle(55, 20),
    "style:turtle_20_10_D1_longonly": lambda: Turtle(20, 10, long_only=True),
    "style:ma_50_200_D1": lambda: MaCross(50, 200),
    "style:ema_20_50_H4_trail": lambda: MaCross(20, 50, tf="H4", kind="ema", trail=True),
    "style:keltner_trend_H4": lambda: Chandelier("keltner", "H4"),
    "style:donchian_trend_H4": lambda: Chandelier("donchian", "H4"),
    "style:donchian_trend_H1": lambda: Chandelier("donchian", "H1"),
    "style:tsmom_12m_monthly": lambda: TSMom(),
    "style:rsi2_connors_D1": lambda: Rsi2(),
    "style:bollinger_fade_H1": lambda: BollFade(),
    "style:asian_breakout_M15": lambda: AsianBreakout(),
    "style:scalp_pullback_M15": lambda: Scalp(),
}


def job_style(name: str) -> dict:
    st = STYLES[name]()
    return summarize(name, "style", run_style(st), {"tf": st.tf})


def buy_and_hold() -> dict:
    a = arrays("D1")
    t, c = a["t"], a["c"]
    years = np.array([datetime.fromtimestamp(x, timezone.utc).year for x in t])
    eras = {}
    for label, y0, y1 in ERAS:
        idx = np.where((years >= y0) & (years <= y1))[0]
        eras[label] = {"ret_pct": round((c[idx[-1]] / c[idx[0]] - 1) * 100, 1)}
    peak, dd = c[0], 0.0
    for x in c:
        peak = max(peak, x)
        dd = max(dd, (peak - x) / peak)
    span = (t[-1] - t[0]) / (365.25 * 86400)
    return {"name": "benchmark:buy_and_hold", "kind": "benchmark",
            "ret_pct": round((c[-1] / c[0] - 1) * 100, 1),
            "cagr_pct": round(((c[-1] / c[0]) ** (1 / span) - 1) * 100, 2),
            "max_dd_pct": round(dd * 100, 1), "eras": eras,
            "note": "100% notional, unleveraged; not risk-comparable to 0.3%/trade"}


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _run(job):
    kind, args = job
    t0 = time.time()
    try:
        fn = {"solo": job_solo, "bot": job_bot, "style": job_style,
              "bench": lambda: buy_and_hold()}[kind]
        out = fn(*args)
    except Exception as e:                       # one bad job must not sink the sweep
        out = {"name": f"{kind}:{args}", "kind": kind, "error": repr(e)}
    out["secs"] = round(time.time() - t0)
    print(f"  done {out['name']:<42s} {out['secs']:>5d}s  "
          f"n={out.get('n', '-')} avgR={out.get('avgR', '-')}", flush=True)
    return out


def fmt_table(rows: list[dict]) -> str:
    head = (f"{'name':<40s}{'n':>6s}{'/yr':>6s}{'WR%':>6s}{'avgR':>8s}{'t':>6s}{'PF':>6s}"
            + "".join(f"{e[0]:>10s}" for e in ERAS) + f"{'eras+':>7s}{'yrs+':>7s}"
            f"{'CAGR%':>7s}{'DD%':>6s}")
    lines = [head, "-" * len(head)]
    for r in rows:
        if "error" in r:
            lines.append(f"{r['name']:<40s} ERROR {r['error']}")
            continue
        eras = "".join(f"{(e['avgR'] if e['avgR'] is not None else float('nan')):>+10.3f}"
                       for e in r["eras"].values())
        lines.append(
            f"{r['name']:<40s}{r['n']:>6d}{r['trades_per_year']:>6.0f}{r['wr']:>6.0f}"
            f"{r['avgR']:>+8.3f}{r['t_stat']:>6.1f}{(r['pf'] or 0):>6.2f}{eras}"
            f"{r['eras_positive']:>5d}/4{r['years_positive']:>7s}"
            f"{r['cagr_pct']:>+7.2f}{r['max_dd_pct']:>6.1f}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="solo,bot,style")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args()
    kinds = set(args.only.split(","))

    jobs = [("bench", ())]
    if "bot" in kinds:                      # slowest first
        jobs += [("bot", (n,)) for n in BOTS]
    if "solo" in kinds:
        jobs += [("solo", (s, tf)) for tf in ("M30", "H1", "H4") for s in ALL13]
    if "style" in kinds:
        jobs += [("style", (n,)) for n in STYLES]

    print(f"{len(jobs)} jobs on {args.workers} workers ...", flush=True)
    t0 = time.time()
    with Pool(args.workers) as pool:
        results = pool.map(_run, jobs, chunksize=1)
    print(f"all done in {time.time() - t0:.0f}s", flush=True)

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "strategy_lab.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=1)

    ok = [r for r in results if r.get("kind") in ("solo", "bot", "style") and "error" not in r]
    rank = sorted(ok, key=lambda r: (r["eras_positive"], r["avgR"]), reverse=True)
    bench = next(r for r in results if r["kind"] == "benchmark")
    txt = [
        "STRATEGY LAB — XAUUSD 2004-06 -> 2026-01, clock-aligned TFs, spread 0.44 + slip 0.05, "
        "risk 0.3%/trade, no prop rules",
        "Ranked by: eras with positive avgR (of 4), then avgR. t = avgR / std-error "
        "(|t| < 2 = not distinguishable from luck).",
        "",
        fmt_table(rank),
        "",
        f"BENCHMARK buy & hold gold: total {bench['ret_pct']:+.1f}%  CAGR {bench['cagr_pct']:+.2f}%  "
        f"maxDD {bench['max_dd_pct']:.1f}%  eras: "
        + ", ".join(f"{k} {v['ret_pct']:+.1f}%" for k, v in bench["eras"].items()),
        "",
        "ERRORS: " + ", ".join(r["name"] for r in results if "error" in r) if any(
            "error" in r for r in results) else "",
    ]
    out = "\n".join(txt)
    with open(os.path.join(REPORTS, "strategy_lab.txt"), "w", encoding="utf-8") as f:
        f.write(out)
    print("\n" + out)


if __name__ == "__main__":
    main()
