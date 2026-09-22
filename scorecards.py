"""
Per-strategy condition scorecards (Strategy Intelligence)
======================================================================
For each of the 13 techniques this returns a transparent CHECKLIST of weighted
conditions (name + points + passed) and a readiness % = earned/total, plus the
direction it is leaning. This is INFORMATIONAL (for the dashboard panel) and
separate from the trading `generate()` logic — it never places a trade.
"""

from __future__ import annotations

from typing import Optional, Sequence

from signals import Bar
from strategies import (sma, ema_series, stdev, atr, highest, lowest,
                        rsi, macd_lines, roc)


def _c(name: str, points: float, passed: bool) -> dict:
    return {"name": name, "points": points, "passed": bool(passed)}


def _score(direction: str, checklist: list[dict]) -> dict:
    total = sum(c["points"] for c in checklist) or 1
    earned = sum(c["points"] for c in checklist if c["passed"])
    return {"direction": direction, "pct": round(earned / total * 100),
            "checklist": checklist}


def _closes(bars):
    return [b.close for b in bars]


# --------------------------------------------------------------------------- #
# One scorer per strategy id. Each returns _score(direction, checklist).       #
# --------------------------------------------------------------------------- #
def _ema_cross_momentum(bars):
    cs = _closes(bars)
    ef, es = ema_series(cs, 12), ema_series(cs, 26)
    e200 = ema_series(cs, 200) if len(cs) >= 200 else ema_series(cs, len(cs) - 1)
    a, r = atr(bars, 14), rsi(cs, 14)
    up = ef[-1] > es[-1]
    direction = "buy" if up else "sell"
    return _score(direction, [
        _c("Trend aligned EMA200", 25, (cs[-1] > e200[-1]) == up),
        _c("EMA21/50 direction", 25, up == (ema_series(cs, 21)[-1] > ema_series(cs, 50)[-1])),
        _c("RSI momentum", 20, (r is not None) and ((r > 50) == up)),
        _c("Fresh cross", 20, (ef[-2] <= es[-2]) != (ef[-1] <= es[-1])),
        _c("ATR active", 10, bool(a and a > 0)),
    ])


def _bollinger_reversion(bars):
    cs = _closes(bars)
    mid, sd = sma(cs, 20), stdev(cs, 20)
    if not mid or not sd:
        return _score("neutral", [])
    upper, lower, px = mid + 2 * sd, mid - 2 * sd, cs[-1]
    direction = "buy" if px < mid else "sell"
    return _score(direction, [
        _c("Beyond band", 35, px < lower or px > upper),
        _c("Stretch ≥ 1.5σ", 30, abs(px - mid) > 1.5 * sd),
        _c("Bands wide", 20, sd > 0),
        _c("RSI extreme", 15, _rsi_extreme(cs)),
    ])


def _rsi_extreme(cs):
    r = rsi(cs, 14)
    return r is not None and (r < 35 or r > 65)


def _breakout_sr(bars):
    prior = bars[:-1]
    hi, lo, a = highest(prior, 20), lowest(prior, 20), atr(bars, 14)
    px = bars[-1].close
    if hi is None or lo is None:
        return _score("neutral", [])
    direction = "buy" if px >= (hi + lo) / 2 else "sell"
    return _score(direction, [
        _c("Breaks 20-bar level", 35, px > hi or px < lo),
        _c("Close beyond range", 25, px > hi or px < lo),
        _c("ATR expanding", 20, bool(a and a > 0)),
        _c("Momentum aligns", 20, _roc_dir(_closes(bars), direction)),
    ])


def _roc_dir(cs, direction):
    r = roc(cs, 10)
    if r is None:
        return False
    return (r > 0) if direction == "buy" else (r < 0)


def _rsi_reversal(bars):
    cs = _closes(bars)
    r = rsi(cs, 14)
    if r is None:
        return _score("neutral", [])
    direction = "buy" if r < 50 else "sell"
    return _score(direction, [
        _c("RSI oversold/overbought", 35, r < 30 or r > 70),
        _c("RSI in zone", 25, r < 35 or r > 65),
        _c("Counter-trend stretch", 20, abs(r - 50) > 20),
        _c("ATR active", 20, bool(atr(bars, 14))),
    ])


def _macd_trend(bars):
    cs = _closes(bars)
    ml = macd_lines(cs)
    if ml is None:
        return _score("neutral", [])
    macd, sig = ml
    up = macd[-1] > sig[-1]
    direction = "buy" if up else "sell"
    return _score(direction, [
        _c("MACD above/below signal", 30, True),
        _c("Fresh signal cross", 30, (macd[-2] <= sig[-2]) != (macd[-1] <= sig[-1])),
        _c("Histogram expanding", 20, abs(macd[-1] - sig[-1]) > abs(macd[-2] - sig[-2])),
        _c("Above/below zero", 20, (macd[-1] > 0) == up),
    ])


def _donchian_breakout(bars):
    prior = bars[:-1]
    hi, lo = highest(prior, 30), lowest(prior, 30)
    px = bars[-1].close
    if hi is None or lo is None:
        return _score("neutral", [])
    direction = "buy" if px >= (hi + lo) / 2 else "sell"
    return _score(direction, [
        _c("Breaks 30-bar channel", 40, px > hi or px < lo),
        _c("ATR expanding", 25, bool(atr(bars, 14))),
        _c("Momentum aligns", 20, _roc_dir(_closes(bars), direction)),
        _c("Trend persists", 15, True),
    ])


def _keltner_reversion(bars):
    cs = _closes(bars)
    mid = ema_series(cs, 20)[-1]
    a = atr(bars, 14)
    if not a:
        return _score("neutral", [])
    upper, lower, px = mid + 2 * a, mid - 2 * a, cs[-1]
    direction = "buy" if px < mid else "sell"
    return _score(direction, [
        _c("Beyond ATR channel", 40, px < lower or px > upper),
        _c("Stretch from mean", 30, abs(px - mid) > 1.5 * a),
        _c("RSI extreme", 15, _rsi_extreme(cs)),
        _c("ATR active", 15, a > 0),
    ])


def _bollinger_squeeze_breakout(bars):
    cs = _closes(bars)
    mid, sd = sma(cs, 20), stdev(cs, 20)
    if not mid or not sd or len(cs) < 42:
        return _score("neutral", [])
    bw = 4 * sd / mid
    prev = [stdev(cs[: -k], 20) for k in range(1, 21)]
    prev = [p for p in prev if p]
    squeeze = bool(prev) and sd <= 1.2 * min(prev)
    px = cs[-1]
    direction = "buy" if px >= mid else "sell"
    return _score(direction, [
        _c("Squeeze (low bandwidth)", 40, squeeze),
        _c("Breaks the band", 30, px > mid + 2 * sd or px < mid - 2 * sd),
        _c("Volatility expanding", 20, bw > 0),
        _c("Momentum aligns", 10, _roc_dir(cs, direction)),
    ])


def _pivot_bounce(bars):
    prior = bars[:-1]
    sup, res = lowest(prior, 20), highest(prior, 20)
    if sup is None or res is None or res <= sup:
        return _score("neutral", [])
    b, mid = bars[-1], (sup + res) / 2
    near_sup = sup <= b.low <= sup + 0.15 * (res - sup)
    near_res = res - 0.15 * (res - sup) <= b.high <= res
    direction = "buy" if b.close < mid else "sell"
    return _score(direction, [
        _c("At support/resistance", 40, near_sup or near_res),
        _c("Rejection (wick)", 25, near_sup or near_res),
        _c("Inside range", 20, sup <= b.close <= res),
        _c("ATR active", 15, bool(atr(bars, 14))),
    ])


def _roc_momentum(bars):
    cs = _closes(bars)
    r = roc(cs, 10)
    if r is None:
        return _score("neutral", [])
    direction = "buy" if r > 0 else "sell"
    return _score(direction, [
        _c("Strong ROC (>1%)", 40, abs(r) > 1.0),
        _c("ROC direction clear", 25, abs(r) > 0.5),
        _c("Trend aligned", 20, _ema_dir(cs, direction)),
        _c("ATR active", 15, bool(atr(bars, 14))),
    ])


def _ema_dir(cs, direction):
    if len(cs) < 50:
        return False
    up = ema_series(cs, 21)[-1] > ema_series(cs, 50)[-1]
    return up == (direction == "buy")


def _fair_value_gap(bars):
    if len(bars) < 4:
        return _score("neutral", [])
    b3, b1 = bars[-3], bars[-1]
    bull = b3.high < b1.low
    bear = b3.low > b1.high
    direction = "buy" if bull else ("sell" if bear else "neutral")
    gap = (b1.low - b3.high) if bull else ((b3.low - b1.high) if bear else 0)
    a = atr(bars, 14) or 1
    return _score(direction, [
        _c("Imbalance gap present", 30, bull or bear),
        _c("Gap ≥ 0.3 ATR", 30, gap >= 0.3 * a),
        _c("Trend aligned", 20, _ema_dir(_closes(bars), direction)),
        _c("Strong impulse", 20, abs(b1.close - b1.open) > 0.4 * a),
    ])


def _order_block_retest(bars):
    cs = _closes(bars)
    recent = cs[-20:]
    if len(recent) < 20:
        return _score("neutral", [])
    lo, hi = min(recent), max(recent)
    span, a = hi - lo, atr(bars, 14) or 1
    px = cs[-1]
    in_demand = lo < px <= lo + 0.3 * span
    in_supply = hi - 0.3 * span <= px < hi
    direction = "buy" if in_demand else ("sell" if in_supply else "neutral")
    return _score(direction, [
        _c("Clear impulse (≥3 ATR)", 30, span >= 3 * a),
        _c("Price returned to OB", 30, in_demand or in_supply),
        _c("Zone fresh", 20, in_demand or in_supply),
        _c("Trend aligned", 20, _ema_dir(cs, direction)),
    ])


def _liquidity_sweep_reversal(bars):
    prior = bars[:-1]
    sup, res = lowest(prior, 20), highest(prior, 20)
    if sup is None or res is None:
        return _score("neutral", [])
    b = bars[-1]
    swept_low = b.low < sup and b.close > sup
    swept_high = b.high > res and b.close < res
    direction = "buy" if swept_low else ("sell" if swept_high else "neutral")
    a = atr(bars, 14) or 1
    return _score(direction, [
        _c("Liquidity swept", 35, swept_low or swept_high),
        _c("Level reclaimed", 30, swept_low or swept_high),
        _c("Sweep ≥ 0.4 ATR", 20, (sup - b.low if swept_low else b.high - res) >= 0.4 * a),
        _c("Rejection close", 15, swept_low or swept_high),
    ])


SCORERS = {
    "ema_cross_momentum": _ema_cross_momentum,
    "bollinger_reversion": _bollinger_reversion,
    "breakout_sr": _breakout_sr,
    "rsi_reversal": _rsi_reversal,
    "macd_trend": _macd_trend,
    "donchian_breakout": _donchian_breakout,
    "keltner_reversion": _keltner_reversion,
    "bollinger_squeeze_breakout": _bollinger_squeeze_breakout,
    "pivot_bounce": _pivot_bounce,
    "roc_momentum": _roc_momentum,
    "fair_value_gap": _fair_value_gap,
    "order_block_retest": _order_block_retest,
    "liquidity_sweep_reversal": _liquidity_sweep_reversal,
}


def score_all(bars: Sequence[Bar]) -> dict[str, dict]:
    """All 13 readiness scorecards for one bar series. Empty if too few bars."""
    out = {}
    if len(bars) < 30:
        return out
    for sid, fn in SCORERS.items():
        try:
            out[sid] = fn(bars)
        except Exception:
            out[sid] = {"direction": "neutral", "pct": 0, "checklist": []}
    return out


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta
    t0 = datetime.now(timezone.utc)
    closes = [100 + i * 0.5 for i in range(60)]
    bars = [Bar(t0 + timedelta(minutes=5 * i), c, c + 0.5, c - 0.5, c)
            for i, c in enumerate(closes)]
    scores = score_all(bars)
    print(f"scored {len(scores)} strategies:")
    for sid, s in scores.items():
        print(f"  {sid:28s} {s['direction']:7s} {s['pct']:3d}%  "
              f"({sum(1 for c in s['checklist'] if c['passed'])}/{len(s['checklist'])} conds)")
    assert len(scores) == 13
    print("\nScorecards OK.")
