"""
Starter strategies + indicators (Pillar 1)
======================================================================
Three deterministic, backtestable strategies that each emit the Strict Signal
Contract. They are intentionally simple — the v1 goal is a proven pipeline
(strategy -> arbitration -> compliance -> execution), not alpha. Add the
remaining strategies of the 13-ensemble later behind the same contract.

Every strategy is conditioned on a regime tag so the League system (Pillar 4)
can later bench a strategy only when it is in the WRONG regime, not merely
unlucky.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from signals import Signal, Direction, Regime, Bar


# --------------------------------------------------------------------------- #
# Indicators (pure functions on plain float lists)                            #
# --------------------------------------------------------------------------- #
def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values: Sequence[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (n + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def stdev(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    window = values[-n:]
    mean = sum(window) / n
    var = sum((v - mean) ** 2 for v in window) / (n - 1)
    return var ** 0.5


def atr(bars: Sequence[Bar], n: int) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n


def highest(bars: Sequence[Bar], n: int) -> Optional[float]:
    if len(bars) < n:
        return None
    return max(b.high for b in bars[-n:])


def lowest(bars: Sequence[Bar], n: int) -> Optional[float]:
    if len(bars) < n:
        return None
    return min(b.low for b in bars[-n:])


def rsi(closes: Sequence[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(len(closes) - n, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain += ch if ch > 0 else 0.0
        loss += -ch if ch < 0 else 0.0
    avg_loss = loss / n
    if avg_loss == 0:
        return 100.0
    rs = (gain / n) / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd_lines(closes: Sequence[float], fast: int = 12, slow: int = 26,
               signal: int = 9) -> Optional[tuple[list[float], list[float]]]:
    if len(closes) < slow + signal:
        return None
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    return macd, ema_series(macd, signal)


def roc(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    past = closes[-1 - n]
    return 0.0 if past == 0 else (closes[-1] - past) / past * 100.0


def _bollinger_bandwidth(closes: Sequence[float], n: int, k: float,
                         end: int) -> Optional[float]:
    window = closes[:end]
    m, sd = sma(window, n), stdev(window, n)
    if not m or not sd or m == 0:
        return None
    return (2.0 * k * sd) / m


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------- #
# Strategy base                                                               #
# --------------------------------------------------------------------------- #
class Strategy:
    id: str = "base"
    timeframe: str = "M5"
    expiry_mins: int = 15

    def generate(self, symbol: str, bars: Sequence[Bar],
                 now: datetime) -> Optional[Signal]:
        raise NotImplementedError

    def _mk(self, symbol: str, direction: Direction, confidence: float,
            entry: float, sl: float, tp: float, regime: Regime,
            now: datetime) -> Signal:
        return Signal(
            strategy_id=self.id, symbol=symbol, direction=direction,
            confidence=_clamp01(confidence), entry=entry, sl=sl, tp=tp,
            timeframe=self.timeframe, regime_tag=regime, timestamp=now,
            expiry=now + timedelta(minutes=self.expiry_mins),
        ).validate()


# --------------------------------------------------------------------------- #
# 1) EMA-cross momentum (TREND)                                               #
# --------------------------------------------------------------------------- #
class EmaCrossMomentum(Strategy):
    id = "ema_cross_momentum"

    def __init__(self, fast: int = 12, slow: int = 26, atr_n: int = 14,
                 sl_mult: float = 1.5, rr: float = 2.0):
        self.fast, self.slow, self.atr_n = fast, slow, atr_n
        self.sl_mult, self.rr = sl_mult, rr

    def generate(self, symbol, bars, now):
        if len(bars) < self.slow + 2:
            return None
        closes = [b.close for b in bars]
        ef, es = ema_series(closes, self.fast), ema_series(closes, self.slow)
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        crossed_up = ef[-2] <= es[-2] and ef[-1] > es[-1]
        crossed_dn = ef[-2] >= es[-2] and ef[-1] < es[-1]
        entry = closes[-1]
        conf = _clamp01(abs(ef[-1] - es[-1]) / a)
        if crossed_up:
            sl = entry - self.sl_mult * a
            tp = entry + self.sl_mult * a * self.rr
            return self._mk(symbol, Direction.BUY, conf, entry, sl, tp,
                            Regime.TREND, now)
        if crossed_dn:
            sl = entry + self.sl_mult * a
            tp = entry - self.sl_mult * a * self.rr
            return self._mk(symbol, Direction.SELL, conf, entry, sl, tp,
                            Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 2) Bollinger mean-reversion (RANGE)                                         #
# --------------------------------------------------------------------------- #
class BollingerReversion(Strategy):
    id = "bollinger_reversion"

    def __init__(self, n: int = 20, k: float = 2.0):
        self.n, self.k = n, k

    def generate(self, symbol, bars, now):
        if len(bars) < self.n + 1:
            return None
        closes = [b.close for b in bars]
        mid = sma(closes, self.n)
        sd = stdev(closes, self.n)
        if not mid or not sd or sd <= 0:
            return None
        upper, lower = mid + self.k * sd, mid - self.k * sd
        entry = closes[-1]
        if entry < lower:
            conf = _clamp01((lower - entry) / sd)
            return self._mk(symbol, Direction.BUY, conf, entry,
                            entry - sd, mid, Regime.RANGE, now)
        if entry > upper:
            conf = _clamp01((entry - upper) / sd)
            return self._mk(symbol, Direction.SELL, conf, entry,
                            entry + sd, mid, Regime.RANGE, now)
        return None


# --------------------------------------------------------------------------- #
# 3) Support/Resistance breakout (TREND)                                      #
# --------------------------------------------------------------------------- #
class BreakoutSR(Strategy):
    id = "breakout_sr"

    def __init__(self, lookback: int = 20, atr_n: int = 14,
                 sl_mult: float = 1.5, rr: float = 2.0):
        self.lookback, self.atr_n = lookback, atr_n
        self.sl_mult, self.rr = sl_mult, rr

    def generate(self, symbol, bars, now):
        if len(bars) < self.lookback + 2:
            return None
        prior = bars[:-1]                      # exclude the breaking bar
        hi, lo = highest(prior, self.lookback), lowest(prior, self.lookback)
        a = atr(bars, self.atr_n)
        if hi is None or lo is None or a is None or a <= 0:
            return None
        entry = bars[-1].close
        if entry > hi:
            conf = _clamp01((entry - hi) / a)
            return self._mk(symbol, Direction.BUY, conf, entry,
                            entry - self.sl_mult * a,
                            entry + self.sl_mult * a * self.rr,
                            Regime.TREND, now)
        if entry < lo:
            conf = _clamp01((lo - entry) / a)
            return self._mk(symbol, Direction.SELL, conf, entry,
                            entry + self.sl_mult * a,
                            entry - self.sl_mult * a * self.rr,
                            Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 4) RSI reversal (oscillator, RANGE)                                         #
# --------------------------------------------------------------------------- #
class RsiReversal(Strategy):
    id = "rsi_reversal"

    def __init__(self, n: int = 14, low: float = 30.0, high: float = 70.0,
                 atr_n: int = 14, sl_mult: float = 1.5, rr: float = 1.5):
        self.n, self.low, self.high = n, low, high
        self.atr_n, self.sl_mult, self.rr = atr_n, sl_mult, rr

    def generate(self, symbol, bars, now):
        if len(bars) < self.n + 2:
            return None
        closes = [b.close for b in bars]
        r, a = rsi(closes, self.n), atr(bars, self.atr_n)
        if r is None or a is None or a <= 0:
            return None
        entry = closes[-1]
        if r < self.low:
            return self._mk(symbol, Direction.BUY, _clamp01((self.low - r) / self.low),
                            entry, entry - self.sl_mult * a,
                            entry + self.sl_mult * a * self.rr, Regime.RANGE, now)
        if r > self.high:
            return self._mk(symbol, Direction.SELL,
                            _clamp01((r - self.high) / (100 - self.high)),
                            entry, entry + self.sl_mult * a,
                            entry - self.sl_mult * a * self.rr, Regime.RANGE, now)
        return None


# --------------------------------------------------------------------------- #
# 5) MACD signal-line cross (momentum, TREND)                                 #
# --------------------------------------------------------------------------- #
class MacdTrend(Strategy):
    id = "macd_trend"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9,
                 atr_n: int = 14, sl_mult: float = 1.5, rr: float = 2.0):
        self.fast, self.slow, self.signal = fast, slow, signal
        self.atr_n, self.sl_mult, self.rr = atr_n, sl_mult, rr

    def generate(self, symbol, bars, now):
        closes = [b.close for b in bars]
        ml = macd_lines(closes, self.fast, self.slow, self.signal)
        a = atr(bars, self.atr_n)
        if ml is None or a is None or a <= 0:
            return None
        macd, sig = ml
        entry = closes[-1]
        conf = _clamp01(abs(macd[-1] - sig[-1]) / a)
        if macd[-2] <= sig[-2] and macd[-1] > sig[-1]:
            return self._mk(symbol, Direction.BUY, conf, entry,
                            entry - self.sl_mult * a,
                            entry + self.sl_mult * a * self.rr, Regime.TREND, now)
        if macd[-2] >= sig[-2] and macd[-1] < sig[-1]:
            return self._mk(symbol, Direction.SELL, conf, entry,
                            entry + self.sl_mult * a,
                            entry - self.sl_mult * a * self.rr, Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 6) Donchian channel breakout (S/R, TREND)                                   #
# --------------------------------------------------------------------------- #
class DonchianBreakout(Strategy):
    id = "donchian_breakout"

    def __init__(self, n: int = 30, atr_n: int = 14, sl_mult: float = 1.5,
                 rr: float = 2.0):
        self.n, self.atr_n, self.sl_mult, self.rr = n, atr_n, sl_mult, rr

    def generate(self, symbol, bars, now):
        if len(bars) < self.n + 2:
            return None
        prior = bars[:-1]
        up, lo, a = highest(prior, self.n), lowest(prior, self.n), atr(bars, self.atr_n)
        if up is None or lo is None or a is None or a <= 0:
            return None
        entry = bars[-1].close
        if entry > up:
            return self._mk(symbol, Direction.BUY, _clamp01((entry - up) / a), entry,
                            entry - self.sl_mult * a,
                            entry + self.sl_mult * a * self.rr, Regime.TREND, now)
        if entry < lo:
            return self._mk(symbol, Direction.SELL, _clamp01((lo - entry) / a), entry,
                            entry + self.sl_mult * a,
                            entry - self.sl_mult * a * self.rr, Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 7) Keltner channel reversion (volatility, RANGE)                            #
# --------------------------------------------------------------------------- #
class KeltnerReversion(Strategy):
    id = "keltner_reversion"

    def __init__(self, n: int = 20, mult: float = 2.0, atr_n: int = 14):
        self.n, self.mult, self.atr_n = n, mult, atr_n

    def generate(self, symbol, bars, now):
        if len(bars) < max(self.n, self.atr_n) + 2:
            return None
        closes = [b.close for b in bars]
        mid = ema_series(closes, self.n)[-1]
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        upper, lower, entry = mid + self.mult * a, mid - self.mult * a, closes[-1]
        if entry < lower:
            return self._mk(symbol, Direction.BUY, _clamp01((lower - entry) / a),
                            entry, entry - a, mid, Regime.RANGE, now)
        if entry > upper:
            return self._mk(symbol, Direction.SELL, _clamp01((entry - upper) / a),
                            entry, entry + a, mid, Regime.RANGE, now)
        return None


# --------------------------------------------------------------------------- #
# 8) Bollinger squeeze breakout (volatility, TREND)                           #
# --------------------------------------------------------------------------- #
class BollingerSqueezeBreakout(Strategy):
    id = "bollinger_squeeze_breakout"

    def __init__(self, n: int = 20, k: float = 2.0, squeeze_lookback: int = 20,
                 atr_n: int = 14, sl_mult: float = 1.5, rr: float = 2.0):
        self.n, self.k, self.squeeze_lookback = n, k, squeeze_lookback
        self.atr_n, self.sl_mult, self.rr = atr_n, sl_mult, rr

    def generate(self, symbol, bars, now):
        closes = [b.close for b in bars]
        if len(closes) < self.n + self.squeeze_lookback + 2:
            return None
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        # Measure the squeeze EXCLUDING the current (would-be breakout) bar, else
        # the breakout bar's own volatility inflates the bandwidth and hides it.
        bws = [b for j in range(len(closes) - 1 - self.squeeze_lookback, len(closes))
               if (b := _bollinger_bandwidth(closes, self.n, self.k, j)) is not None]
        prev_bw = _bollinger_bandwidth(closes, self.n, self.k, len(closes) - 1)
        if not bws or prev_bw is None or prev_bw > 1.2 * min(bws):   # no squeeze
            return None
        # breakout judged against the prior (squeezed) band
        mp, sdp = sma(closes[:-1], self.n), stdev(closes[:-1], self.n)
        if not mp or not sdp:
            return None
        upper, lower, entry = mp + self.k * sdp, mp - self.k * sdp, closes[-1]
        if entry > upper:
            return self._mk(symbol, Direction.BUY, _clamp01((entry - upper) / a),
                            entry, entry - self.sl_mult * a,
                            entry + self.sl_mult * a * self.rr, Regime.TREND, now)
        if entry < lower:
            return self._mk(symbol, Direction.SELL, _clamp01((lower - entry) / a),
                            entry, entry + self.sl_mult * a,
                            entry - self.sl_mult * a * self.rr, Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 9) Support/Resistance pivot bounce (S/R, RANGE)                             #
# --------------------------------------------------------------------------- #
class PivotBounce(Strategy):
    id = "pivot_bounce"

    def __init__(self, n: int = 20, atr_n: int = 14, tol: float = 0.1):
        self.n, self.atr_n, self.tol = n, atr_n, tol

    def generate(self, symbol, bars, now):
        if len(bars) < self.n + 2:
            return None
        prior = bars[:-1]
        sup, res, a = lowest(prior, self.n), highest(prior, self.n), atr(bars, self.atr_n)
        if sup is None or res is None or a is None or a <= 0 or res <= sup:
            return None
        rng, mid = res - sup, (sup + res) / 2.0
        b, entry = bars[-1], bars[-1].close
        # respected support (touched but not broken) -> bounce up
        if sup <= b.low <= sup + self.tol * rng and sup <= entry < mid:
            return self._mk(symbol, Direction.BUY,
                            _clamp01(1 - (b.low - sup) / (self.tol * rng + 1e-9)),
                            entry, sup - 0.5 * a, mid, Regime.RANGE, now)
        if res - self.tol * rng <= b.high <= res and mid < entry <= res:
            return self._mk(symbol, Direction.SELL,
                            _clamp01(1 - (res - b.high) / (self.tol * rng + 1e-9)),
                            entry, res + 0.5 * a, mid, Regime.RANGE, now)
        return None


# --------------------------------------------------------------------------- #
# 10) Rate-of-change momentum (momentum, TREND)                               #
# --------------------------------------------------------------------------- #
class RocMomentum(Strategy):
    id = "roc_momentum"

    def __init__(self, n: int = 10, threshold: float = 1.0, atr_n: int = 14,
                 sl_mult: float = 1.5, rr: float = 2.0):
        self.n, self.threshold = n, threshold
        self.atr_n, self.sl_mult, self.rr = atr_n, sl_mult, rr

    def generate(self, symbol, bars, now):
        closes = [b.close for b in bars]
        r, a = roc(closes, self.n), atr(bars, self.atr_n)
        if r is None or a is None or a <= 0:
            return None
        entry = closes[-1]
        conf = _clamp01((abs(r) - self.threshold) / max(self.threshold, 1e-9))
        if r > self.threshold:
            return self._mk(symbol, Direction.BUY, conf, entry,
                            entry - self.sl_mult * a,
                            entry + self.sl_mult * a * self.rr, Regime.TREND, now)
        if r < -self.threshold:
            return self._mk(symbol, Direction.SELL, conf, entry,
                            entry + self.sl_mult * a,
                            entry - self.sl_mult * a * self.rr, Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 11) ICT Fair Value Gap (imbalance) continuation (SMC/ICT, TREND)            #
# --------------------------------------------------------------------------- #
class FairValueGap(Strategy):
    id = "fair_value_gap"

    def __init__(self, atr_n: int = 14, rr: float = 2.0):
        self.atr_n, self.rr = atr_n, rr

    def generate(self, symbol, bars, now):
        if len(bars) < max(self.atr_n + 1, 4):
            return None
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        b3, b1, entry = bars[-3], bars[-1], bars[-1].close
        if b3.high < b1.low:                       # bullish imbalance (gap up)
            return self._mk(symbol, Direction.BUY,
                            _clamp01((b1.low - b3.high) / a), entry,
                            b3.high - 0.2 * a, entry + (b1.low - b3.high) * self.rr
                            + 0.1 * a, Regime.TREND, now)
        if b3.low > b1.high:                       # bearish imbalance (gap down)
            return self._mk(symbol, Direction.SELL,
                            _clamp01((b3.low - b1.high) / a), entry,
                            b3.low + 0.2 * a, entry - (b3.low - b1.high) * self.rr
                            - 0.1 * a, Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 12) SMC supply/demand order-block retest (SMC/ICT, TREND)                    #
# --------------------------------------------------------------------------- #
class OrderBlockRetest(Strategy):
    id = "order_block_retest"

    def __init__(self, lookback: int = 20, atr_n: int = 14, min_impulse: float = 3.0,
                 rr: float = 2.0):
        self.lookback, self.atr_n = lookback, atr_n
        self.min_impulse, self.rr = min_impulse, rr

    def generate(self, symbol, bars, now):
        if len(bars) < self.lookback + 2:
            return None
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        recent = [b.close for b in bars[-self.lookback:]]
        lo, hi, entry = min(recent), max(recent), bars[-1].close
        span = hi - lo
        if span < self.min_impulse * a:            # no clear impulse to retest
            return None
        # retrace into demand (near impulse low) -> expect continuation up
        if lo < entry <= lo + 0.3 * span:
            return self._mk(symbol, Direction.BUY, _clamp01(1 - (entry - lo) / span),
                            entry, lo - 0.5 * a, entry + (span * 0.5) * self.rr,
                            Regime.TREND, now)
        if hi - 0.3 * span <= entry < hi:          # retrace into supply
            return self._mk(symbol, Direction.SELL, _clamp01(1 - (hi - entry) / span),
                            entry, hi + 0.5 * a, entry - (span * 0.5) * self.rr,
                            Regime.TREND, now)
        return None


# --------------------------------------------------------------------------- #
# 13) ICT liquidity sweep / stop-hunt reversal (SMC/ICT, RANGE)               #
# --------------------------------------------------------------------------- #
class LiquiditySweepReversal(Strategy):
    id = "liquidity_sweep_reversal"

    def __init__(self, n: int = 20, atr_n: int = 14, rr: float = 2.0):
        self.n, self.atr_n, self.rr = n, atr_n, rr

    def generate(self, symbol, bars, now):
        if len(bars) < self.n + 2:
            return None
        prior = bars[:-1]
        sup, res, a = lowest(prior, self.n), highest(prior, self.n), atr(bars, self.atr_n)
        if sup is None or res is None or a is None or a <= 0:
            return None
        b, entry = bars[-1], bars[-1].close
        # swept support then reclaimed (stop-hunt below -> reversal up)
        if b.low < sup and entry > sup:
            return self._mk(symbol, Direction.BUY, _clamp01((sup - b.low) / a),
                            entry, b.low - 0.2 * a,
                            entry + (entry - b.low) * self.rr, Regime.RANGE, now)
        # swept resistance then reclaimed
        if b.high > res and entry < res:
            return self._mk(symbol, Direction.SELL, _clamp01((b.high - res) / a),
                            entry, b.high + 0.2 * a,
                            entry - (b.high - entry) * self.rr, Regime.RANGE, now)
        return None


# Catalog for the dashboard: which family each technique belongs to and the
# market regime it is BUILT for (the League judges it only in that regime).
CATALOG: list[dict] = [
    {"id": "ema_cross_momentum", "name": "EMA Cross Momentum",
     "family": "Momentum", "regime": "trend",
     "fits": "trending markets; rides a fresh fast/slow EMA cross"},
    {"id": "bollinger_reversion", "name": "Bollinger Reversion",
     "family": "Volatility", "regime": "range",
     "fits": "ranging markets; fades a stretch beyond the bands"},
    {"id": "breakout_sr", "name": "Support/Resistance Breakout",
     "family": "S/R", "regime": "trend",
     "fits": "trending markets; enters on a break of recent S/R"},
    {"id": "rsi_reversal", "name": "RSI Reversal",
     "family": "Oscillator", "regime": "range",
     "fits": "ranging markets; buys oversold / sells overbought"},
    {"id": "macd_trend", "name": "MACD Trend",
     "family": "Momentum", "regime": "trend",
     "fits": "trending markets; MACD signal-line cross"},
    {"id": "donchian_breakout", "name": "Donchian Breakout",
     "family": "S/R", "regime": "trend",
     "fits": "trending markets; breaks the N-bar channel"},
    {"id": "keltner_reversion", "name": "Keltner Reversion",
     "family": "Volatility", "regime": "range",
     "fits": "ranging markets; fades the ATR channel back to the mean"},
    {"id": "bollinger_squeeze_breakout", "name": "Bollinger Squeeze Breakout",
     "family": "Volatility", "regime": "trend",
     "fits": "post-squeeze expansion; breaks out of low volatility"},
    {"id": "pivot_bounce", "name": "Pivot Bounce",
     "family": "S/R", "regime": "range",
     "fits": "ranging markets; bounces off respected support/resistance"},
    {"id": "roc_momentum", "name": "Rate-of-Change Momentum",
     "family": "Momentum", "regime": "trend",
     "fits": "trending markets; strong rate-of-change thrust"},
    {"id": "fair_value_gap", "name": "ICT Fair Value Gap",
     "family": "SMC/ICT", "regime": "trend",
     "fits": "imbalance continuation after a 3-bar gap"},
    {"id": "order_block_retest", "name": "SMC Order Block Retest",
     "family": "SMC/ICT", "regime": "trend",
     "fits": "retest of a supply/demand zone after an impulse"},
    {"id": "liquidity_sweep_reversal", "name": "ICT Liquidity Sweep",
     "family": "SMC/ICT", "regime": "range",
     "fits": "stop-hunt reversal after a swept high/low is reclaimed"},
]


DEFAULT_STRATEGIES: list[Strategy] = [
    EmaCrossMomentum(),          # 1  momentum / trend
    BollingerReversion(),        # 2  volatility / range
    BreakoutSR(),                # 3  S/R / trend
    RsiReversal(),               # 4  oscillator / range
    MacdTrend(),                 # 5  momentum / trend
    DonchianBreakout(),          # 6  S/R / trend
    KeltnerReversion(),          # 7  volatility / range
    BollingerSqueezeBreakout(),  # 8  volatility / trend
    PivotBounce(),               # 9  S/R / range
    RocMomentum(),               # 10 momentum / trend
    FairValueGap(),              # 11 SMC-ICT / trend
    OrderBlockRetest(),          # 12 SMC-ICT / trend
    LiquiditySweepReversal(),    # 13 SMC-ICT / range
]

# --------------------------------------------------------------------------- #
# Out-of-sample validated core (Phase 2 edge study, 2026-06-21).               #
# Each of these showed POSITIVE expectancy in BOTH the train and the unseen    #
# test window, on XAUUSD AND BTCUSD — the trend/breakout/momentum theme that   #
# gold & crypto actually reward. The other 9 techniques (mean-reversion + most  #
# SMC fades) looked fine in-sample but collapsed out-of-sample (curve-fit), so  #
# they are excluded from the live roster. Re-run validate_edge.py to refresh.   #
# Families spanned: Momentum (macd_trend, roc_momentum) + S/R (the breakouts).  #
ROBUST_TREND_IDS: tuple = (
    "macd_trend", "roc_momentum", "donchian_breakout", "breakout_sr",
)


def select_strategies(ids: Sequence[str]) -> list[Strategy]:
    """Pick strategies by id (preserves DEFAULT_STRATEGIES order). Unknown ids
    raise — a typo must not silently shrink the roster."""
    wanted = list(ids)
    have = {s.id for s in DEFAULT_STRATEGIES}
    missing = [i for i in wanted if i not in have]
    if missing:
        raise ValueError(f"unknown strategy id(s): {missing}")
    return [s for s in DEFAULT_STRATEGIES if s.id in set(wanted)]


# --------------------------------------------------------------------------- #
# Self-test: run ALL 13 over a battery of scenarios. Any emitted Signal has     #
# already passed validate() (in _mk), so no exception => every contract valid.  #
#   ./env/Scripts/python.exe strategies.py                                      #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone

    now = datetime.now(timezone.utc)

    def from_closes(cs, spread=1.0):
        return [Bar(now + timedelta(minutes=5 * i), c, c + spread, c - spread, c)
                for i, c in enumerate(cs)]

    def from_ohlc(rows):   # rows = (o,h,l,c)
        return [Bar(now + timedelta(minutes=5 * i), o, h, l, c)
                for i, (o, h, l, c) in enumerate(rows)]

    assert len(DEFAULT_STRATEGIES) == 13
    assert len({s.id for s in DEFAULT_STRATEGIES}) == 13      # unique ids

    # --- scenarios designed to trigger different strategy families --- #
    scenarios: dict[str, list[Bar]] = {}
    scenarios["uptrend"] = from_closes([100 + i * 1.2 for i in range(60)])
    scenarios["downtrend"] = from_closes([200 - i * 1.2 for i in range(60)])
    scenarios["dip"] = from_closes([150.0] * 35 + [150 - 4 * i for i in range(6)])
    scenarios["spike"] = from_closes([150.0] * 35 + [150 + 4 * i for i in range(6)])

    # momentum cross: downtrend then a sharp up-leg (fires EMA/MACD on the cross)
    cross = from_closes([100 - i for i in range(30)])
    for j in range(20):
        p = 70 + 4.0 * (j + 1)
        cross.append(Bar(now + timedelta(minutes=5 * (30 + j)), p, p + 1, p - 1, p))
    scenarios["cross_up"] = cross

    # bullish Fair Value Gap: a 3-bar gap up (bar[-3].high < bar[-1].low)
    fvg = [(100, 101, 99, 100)] * 20 + [(101, 103, 100, 102), (108, 110, 107, 109),
                                        (112, 114, 111, 113)]
    scenarios["fvg_up"] = from_ohlc(fvg)

    # liquidity sweep: range ~100, last bar wicks below support then reclaims
    sweep = [(100, 101.5, 98.5, 100)] * 40 + [(99, 99.5, 94.0, 99.2)]
    scenarios["sweep_down"] = from_ohlc(sweep)

    # squeeze then breakout: a long tight range, then a thrust above the band
    squeeze = [100 + 0.3 * (1 if i % 2 else -1) for i in range(48)] + [103.0]
    scenarios["squeeze_up"] = from_closes(squeeze)

    # respected support: oscillate 98..102, last bar taps support and holds
    rb = [(100, 102, 98, 100)] * 24 + [(99, 99.5, 98.1, 99.0)]
    scenarios["range_bounce"] = from_ohlc(rb)

    # impulse then retrace into the demand zone (order-block retest)
    impulse = [100.0] * 30 + [100 + i * 3 for i in range(11)] + [124, 118, 112, 107]
    scenarios["impulse_retrace"] = from_closes(impulse)

    # Evaluate bar-by-bar over growing prefixes (as the live loop would), so
    # cross-on-the-last-bar strategies (EMA/MACD) get their trigger bar.
    fired: dict[str, set] = {}
    for name, bars in scenarios.items():
        for st in DEFAULT_STRATEGIES:
            for end in range(30, len(bars) + 1):
                sig = st.generate("TEST", bars[:end], now)
                if sig is not None:
                    fired.setdefault(st.id, set()).add(f"{name}:{sig.direction.value}")
                    break

    for st in DEFAULT_STRATEGIES:
        marks = sorted(fired.get(st.id, []))
        print(f"  {st.id:28s} {'fired ' + str(marks) if marks else '— (no trigger)'}")

    n_fired = len(fired)
    print(f"\n{n_fired}/13 strategies fired across {len(scenarios)} scenarios; "
          f"all emitted contracts valid.")
    assert n_fired >= 12, f"only {n_fired} strategies fired"
