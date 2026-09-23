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


# --------------------------------------------------------------------------- #
# 14/15) Quantum Price Levels (Lee's quantum finance, see quantum.py)         #
# --------------------------------------------------------------------------- #
class QuantumPriceLevel(Strategy):
    """Trades the quantum price-level ladder QPL(+-n) built around the previous
    day's close from the anharmonic-oscillator energy levels.

    mode="breakout": a fresh H4 close through QPL(+k) = energy-level jump ->
        BUY (through QPL(-k) -> SELL); SL `sl_levels` rungs back, TP `tp_levels`
        rungs further along the ladder.
    mode="bounce":   the bar wicks into QPL(-k) and closes back above it ->
        BUY (mirror at QPL(+k)); SL `sl_levels` rungs beyond the touched level,
        TP `tp_levels` rungs back toward P0.
    Works on the 200 closed H4 bars the live loop hands over: sigma is the
    daily return std estimated from the last `vol_n` H4 log-returns
    (x sqrt(bars_per_day)); lambda is fitted from their kurtosis ("fit") or
    fixed. The ladder used for the last decision is kept in `last_ladder`.
    """
    id = "quantum_qpl"
    timeframe = "H4"
    expiry_mins = 240

    # Defaults = the config validated in research/quantum_backtest.py
    # (reports/quantum_qpl_buyonly_*.txt): BUY-only, enter through QPL(+3),
    # SL 3 rungs back, TP 6 rungs on (2R), EMA50 filter. 2005-2026: PF 1.34,
    # WR 43 %, maxDD 17 %, +4 %/yr at 1 % risk; both halves positive. The
    # two-sided version is break-even (sells lose) and the bounce twin ~PF 1.0.
    #
    # Optional SMC / ICT confluence (smc.py), each gate independent:
    #   smc_bos   structure must agree: last BOS/CHoCH in the trade direction
    #             (bos_max_age > 0 additionally limits how old that break is)
    #   smc_lq    a liquidity sweep of the opposite side within lq_lookback bars
    #             (buy: a wick below a swing low that closed back above it)
    #   smc_zone  a fresh demand (supply) order block within zone_atr ATR
    #             below (above) the entry; sl_mode="zone" then parks the stop
    #             just beyond that zone instead of on the ladder
    def __init__(self, mode: str = "breakout", k_in: int = 3, sl_levels: int = 3,
                 tp_levels: int = 6, vol_n: int = 120, bars_per_day: int = 6,
                 trend_n: int = 50, lam: str | float = "fit",
                 max_overshoot: int = 1, n_levels: int = 12, side: str = "buy",
                 smc_bos: bool = False, bos_max_age: int = 0, smc_lq: bool = False,
                 lq_lookback: int = 12, smc_zone: bool = False, zone_atr: float = 1.0,
                 sl_mode: str = "ladder"):
        assert mode in ("breakout", "bounce") and side in ("both", "buy", "sell")
        assert sl_mode in ("ladder", "zone")
        self.mode, self.k_in, self.side = mode, k_in, side
        self.sl_levels, self.tp_levels = sl_levels, tp_levels
        self.vol_n, self.bars_per_day, self.trend_n = vol_n, bars_per_day, trend_n
        self.lam, self.max_overshoot, self.n_levels = lam, max_overshoot, n_levels
        self.smc_bos, self.bos_max_age = smc_bos, bos_max_age
        self.smc_lq, self.lq_lookback = smc_lq, lq_lookback
        self.smc_zone, self.zone_atr, self.sl_mode = smc_zone, zone_atr, sl_mode
        self.last_ladder = None
        self.last_sigma = self.last_lambda = None
        self.last_smc = None          # SmcContext of the last decision (if used)
        self.last_smc_tags = ""       # "bos_up|lq3|zone*" summary for audits

    @property
    def uses_smc(self) -> bool:
        return self.smc_bos or self.smc_lq or self.smc_zone or self.sl_mode == "zone"

    def smc_for(self, bars: Sequence[Bar]):
        from smc import smc_context
        return smc_context(bars)

    def _smc_ok(self, bars: Sequence[Bar], d: int, entry: float):
        """Apply the enabled SMC gates for direction d (+1/-1). Returns
        (ok, sl_override or None, tags)."""
        if not self.uses_smc:
            return True, None, ""
        ctx = self.smc_for(bars)
        self.last_smc = ctx
        tags = []
        if self.smc_bos:
            if ctx.trend != d:
                return False, None, ""
            if self.bos_max_age and ctx.event_bars_ago > self.bos_max_age:
                return False, None, ""
            tags.append(ctx.last_event)
        if self.smc_lq:
            ago = ctx.sweep_low_bars_ago if d > 0 else ctx.sweep_high_bars_ago
            if ago > self.lq_lookback:
                return False, None, ""
            tags.append(f"lq{ago}")
        sl_override = None
        if self.smc_zone or self.sl_mode == "zone":
            a = ctx.atr or 0.0
            z = ctx.nearest_demand(entry) if d > 0 else ctx.nearest_supply(entry)
            near = z is not None and (
                (d > 0 and z.high >= entry - self.zone_atr * a) or
                (d < 0 and z.low <= entry + self.zone_atr * a))
            if self.smc_zone and not near:
                return False, None, ""
            if near:
                tags.append("zone" + ("*" if z.swing_break else ""))
                if self.sl_mode == "zone":
                    sl_override = z.low - 0.2 * a if d > 0 else z.high + 0.2 * a
        return True, sl_override, "|".join(tags)

    # ---- model ------------------------------------------------------------ #
    def ladder_for(self, bars: Sequence[Bar]):
        """(ladder, sigma_daily, lambda) for the decision bar bars[-1], or None."""
        import numpy as np
        from quantum import fit_lambda, qpl_ladder
        if len(bars) < self.vol_n + 2:
            return None
        closes = np.array([b.close for b in bars], dtype=float)
        lr = np.diff(np.log(closes))[-self.vol_n:]
        sigma = float(lr.std()) * (self.bars_per_day ** 0.5)
        if not sigma > 0:
            return None
        lam = fit_lambda(lr) if self.lam == "fit" else float(self.lam)
        today = bars[-1].time.date()
        p0 = None
        for b in reversed(bars[:-1]):            # last close of the previous day
            if b.time.date() < today:
                p0 = b.close
                break
        if p0 is None:
            return None
        return qpl_ladder(p0, sigma, lam, self.n_levels), sigma, lam

    def _L(self, ladder, k: int) -> float:
        return float(ladder[self.n_levels + k])

    def generate(self, symbol, bars, now):
        res = self.ladder_for(bars)
        if res is None:
            return None
        ladder, sigma, lam = res
        self.last_ladder, self.last_sigma, self.last_lambda = ladder, sigma, lam
        c, pc, b = bars[-1].close, bars[-2].close, bars[-1]
        trend = 0
        if self.trend_n:
            closes = [x.close for x in bars]
            if len(closes) < self.trend_n:
                return None
            e = ema_series(closes, self.trend_n)[-1]
            trend = 1 if c > e else -1
        if self.side == "buy":            # one-sided roster (gold's secular uptrend)
            trend = max(trend, 1) if trend >= 0 else 0
            if trend == 0:
                return None
        elif self.side == "sell":
            trend = min(trend, -1) if trend <= 0 else 0
            if trend == 0:
                return None
        k, s, t = self.k_in, self.sl_levels, self.tp_levels
        if k + t > self.n_levels or k + s > self.n_levels:
            return None
        if self.mode == "breakout":
            up, dn = self._L(ladder, k), self._L(ladder, -k)
            nxt_up, nxt_dn = self._L(ladder, k + 1), self._L(ladder, -k - 1)
            if c > up and pc <= up and trend >= 0 and \
                    (self.max_overshoot <= 0 or c < self._L(ladder, k + self.max_overshoot)):
                sl, tp = self._L(ladder, k - s), self._L(ladder, k + t)
                ok, sl_o, self.last_smc_tags = self._smc_ok(bars, 1, c)
                if ok and sl_o is not None:
                    sl = sl_o
                if ok and sl < c < tp:
                    return self._mk(symbol, Direction.BUY, (c - up) / (nxt_up - up),
                                    c, sl, tp, Regime.TREND, now)
            if c < dn and pc >= dn and trend <= 0 and \
                    (self.max_overshoot <= 0 or c > self._L(ladder, -k - self.max_overshoot)):
                sl, tp = self._L(ladder, -k + s), self._L(ladder, -k - t)
                ok, sl_o, self.last_smc_tags = self._smc_ok(bars, -1, c)
                if ok and sl_o is not None:
                    sl = sl_o
                if ok and tp < c < sl:
                    return self._mk(symbol, Direction.SELL, (dn - c) / (dn - nxt_dn),
                                    c, sl, tp, Regime.TREND, now)
            return None
        # bounce: wick into support QPL(-k) and close back above it
        sup, res_ = self._L(ladder, -k), self._L(ladder, k)
        if b.low <= sup < c and pc > sup and trend >= 0:
            sl, tp = self._L(ladder, -k - s), self._L(ladder, -k + t)
            ok, sl_o, self.last_smc_tags = self._smc_ok(bars, 1, c)
            if ok and sl_o is not None:
                sl = sl_o
            if ok and sl < c < tp:
                return self._mk(symbol, Direction.BUY, (sup - b.low) / (sup - self._L(ladder, -k - 1)),
                                c, sl, tp, Regime.RANGE, now)
        if b.high >= res_ > c and pc < res_ and trend <= 0:
            sl, tp = self._L(ladder, k + s), self._L(ladder, k - t)
            ok, sl_o, self.last_smc_tags = self._smc_ok(bars, -1, c)
            if ok and sl_o is not None:
                sl = sl_o
            if ok and tp < c < sl:
                return self._mk(symbol, Direction.SELL, (b.high - res_) / (self._L(ladder, k + 1) - res_),
                                c, sl, tp, Regime.RANGE, now)
        return None


class QuantumPriceLevelSmc(QuantumPriceLevel):
    """quantum_qpl + SMC supply/demand confluence: the BUY must sit within
    2 ATR above a fresh demand order block (smc.py). 2005-2026: 182 trades,
    PF 1.49 (IS 1.35 / OOS 1.65), WR 44 %, maxDD 9.4 %, +2.3 %/yr — fewer,
    better trades than the plain ladder (PF 1.34, +4.2 %/yr, DD 16.8 %).
    The BOS/CHoCH and liquidity-sweep gates were tested too and did NOT help
    (reports/quantum_smc_*.txt), so they stay off here."""
    id = "quantum_qpl_smc"

    def __init__(self, smc_zone: bool = True, zone_atr: float = 2.0, **kw):
        super().__init__(smc_zone=smc_zone, zone_atr=zone_atr, **kw)


class QuantumPriceLevelBounce(QuantumPriceLevel):
    """Mean-reversion twin of quantum_qpl (registered separately so each can be
    put on a roster on its own)."""
    id = "quantum_qpl_bounce"

    def __init__(self, k_in: int = 3, sl_levels: int = 2, tp_levels: int = 3,
                 side: str = "both", **kw):
        super().__init__(mode="bounce", k_in=k_in, sl_levels=sl_levels,
                         tp_levels=tp_levels, side=side, **kw)



# --------------------------------------------------------------------------- #
# 17/18) Intraday day-trading pair (M15) — "a trade about every hour"          #
# --------------------------------------------------------------------------- #
class IntradayMomentum(Strategy):
    """M15 momentum breakout with an EMA trend filter, ATR stops, short hold.

    BUY when the closed M15 bar closes above the highest high of the previous
    `n` bars and EMA(fast) > EMA(slow); SELL mirror. SL = sl_atr x ATR(14),
    TP = rr x SL. Meant to be run with a daily target / daily stop
    (main_loop --daily-target-pct / --daily-stop-pct, or the backtester's
    DailyGoal). `side` = both|buy|sell. `min_atr_frac` skips dead markets
    (ATR below that fraction of price, e.g. 0.0004 = 0.04 %)."""
    id = "intraday_momentum"
    timeframe = "M15"
    expiry_mins = 15

    def __init__(self, n: int = 8, fast: int = 20, slow: int = 60, atr_n: int = 14,
                 sl_atr: float = 1.0, rr: float = 1.5, side: str = "both",
                 min_atr_frac: float = 0.0, max_hold_bars: int = 16,
                 sessions: tuple = ()):
        self.n, self.fast, self.slow, self.atr_n = n, fast, slow, atr_n
        self.sl_atr, self.rr, self.side = sl_atr, rr, side
        self.min_atr_frac, self.max_hold_bars = min_atr_frac, max_hold_bars
        self.sessions = tuple(sessions)     # (start_hour, end_hour) pairs, bar time

    def _session_ok(self, t) -> bool:
        if not self.sessions:
            return True
        return any(a <= t.hour < b for a, b in self.sessions)

    def generate(self, symbol, bars, now):
        need = max(self.slow + 2, self.n + 2, self.atr_n + 2)
        if len(bars) < need or not self._session_ok(bars[-1].time):
            return None
        closes = [b.close for b in bars]
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        c = closes[-1]
        if self.min_atr_frac and a < self.min_atr_frac * c:
            return None
        ef, es = ema_series(closes, self.fast)[-1], ema_series(closes, self.slow)[-1]
        prior = bars[-1 - self.n:-1]
        hi, lo = max(b.high for b in prior), min(b.low for b in prior)
        sl_d = self.sl_atr * a
        if c > hi and ef > es and self.side in ("both", "buy"):
            return self._mk(symbol, Direction.BUY, _clamp01((c - hi) / a), c,
                            c - sl_d, c + sl_d * self.rr, Regime.TREND, now)
        if c < lo and ef < es and self.side in ("both", "sell"):
            return self._mk(symbol, Direction.SELL, _clamp01((lo - c) / a), c,
                            c + sl_d, c - sl_d * self.rr, Regime.TREND, now)
        return None


class IntradayPullback(Strategy):
    """M15 trend-pullback: in an EMA(fast)>EMA(slow) uptrend, a bar that dips
    to/below EMA(fast) and closes back above it (a held pullback) -> BUY;
    mirror for downtrends. Higher win-rate, smaller targets than the breakout."""
    id = "intraday_pullback"
    timeframe = "M15"
    expiry_mins = 15

    def __init__(self, fast: int = 20, slow: int = 60, atr_n: int = 14,
                 sl_atr: float = 1.0, rr: float = 1.2, side: str = "both",
                 min_atr_frac: float = 0.0, max_hold_bars: int = 16,
                 sessions: tuple = ()):
        self.fast, self.slow, self.atr_n = fast, slow, atr_n
        self.sl_atr, self.rr, self.side = sl_atr, rr, side
        self.min_atr_frac, self.max_hold_bars = min_atr_frac, max_hold_bars
        self.sessions = tuple(sessions)

    def _session_ok(self, t) -> bool:
        if not self.sessions:
            return True
        return any(a <= t.hour < b for a, b in self.sessions)

    def generate(self, symbol, bars, now):
        if len(bars) < self.slow + 2 or not self._session_ok(bars[-1].time):
            return None
        closes = [b.close for b in bars]
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        b, c = bars[-1], closes[-1]
        if self.min_atr_frac and a < self.min_atr_frac * c:
            return None
        ef_s, es_s = ema_series(closes, self.fast), ema_series(closes, self.slow)
        ef, es = ef_s[-1], es_s[-1]
        sl_d = self.sl_atr * a
        if ef > es and b.low <= ef < c and bars[-2].close > ef_s[-2] and self.side in ("both", "buy"):
            return self._mk(symbol, Direction.BUY, _clamp01((ef - b.low) / a), c,
                            c - sl_d, c + sl_d * self.rr, Regime.TREND, now)
        if ef < es and b.high >= ef > c and bars[-2].close < ef_s[-2] and self.side in ("both", "sell"):
            return self._mk(symbol, Direction.SELL, _clamp01((b.high - ef) / a), c,
                            c + sl_d, c - sl_d * self.rr, Regime.TREND, now)
        return None


class SessionRangeBreakout(Strategy):
    """Opening-range breakout (M15): the first `range_bars` bars from each
    session open (bar-time hours in `opens`) define a range; the first M15
    close beyond it within `window_bars` bars enters, SL at the range's other
    side (capped at max_sl_atr x ATR), TP = rr x SL. At most one trade per
    session. ~1-2 trades/day."""
    id = "session_breakout"
    timeframe = "M15"
    expiry_mins = 15

    def __init__(self, opens: tuple = (9, 16), range_bars: int = 4, window_bars: int = 12,
                 rr: float = 1.5, max_sl_atr: float = 2.0, atr_n: int = 14,
                 side: str = "both", max_hold_bars: int = 24):
        self.opens, self.range_bars, self.window_bars = tuple(opens), range_bars, window_bars
        self.rr, self.max_sl_atr, self.atr_n = rr, max_sl_atr, atr_n
        self.side, self.max_hold_bars = side, max_hold_bars

    def generate(self, symbol, bars, now):
        if len(bars) < self.atr_n + self.range_bars + self.window_bars + 2:
            return None
        b = bars[-1]
        # locate this bar's session open (same calendar day, hour in opens)
        idx = None
        for k in range(len(bars) - 1, max(len(bars) - self.range_bars - self.window_bars - 2, -1), -1):
            x = bars[k]
            if x.time.hour in self.opens and x.time.minute == 0 and x.time.date() == b.time.date():
                idx = k
                break
        if idx is None:
            return None
        end_range = idx + self.range_bars              # first bar after the range
        pos_in = len(bars) - 1 - end_range             # bars since the range closed
        if pos_in < 0 or pos_in >= self.window_bars:
            return None
        rng = bars[idx:end_range]
        hi, lo = max(x.high for x in rng), min(x.low for x in rng)
        # only the FIRST close outside the range counts (no re-entries)
        for x in bars[end_range:-1]:
            if x.close > hi or x.close < lo:
                return None
        a = atr(bars, self.atr_n)
        if a is None or a <= 0:
            return None
        c = b.close
        if c > hi and self.side in ("both", "buy"):
            sl = max(lo, c - self.max_sl_atr * a)
            return self._mk(symbol, Direction.BUY, _clamp01((c - hi) / a), c, sl,
                            c + (c - sl) * self.rr, Regime.TREND, now)
        if c < lo and self.side in ("both", "sell"):
            sl = min(hi, c + self.max_sl_atr * a)
            return self._mk(symbol, Direction.SELL, _clamp01((lo - c) / a), c, sl,
                            c - (sl - c) * self.rr, Regime.TREND, now)
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
    {"id": "quantum_qpl", "name": "Quantum Price Level Breakout",
     "family": "Quantum", "regime": "trend",
     "fits": "energy-level jump: H4 close through a quantum price level"},
    {"id": "quantum_qpl_smc", "name": "Quantum Price Level + SMC Demand Zone",
     "family": "Quantum", "regime": "trend",
     "fits": "QPL breakout that launches from a fresh demand order block"},
    {"id": "intraday_momentum", "name": "Intraday Momentum (M15)",
     "family": "Intraday", "regime": "trend",
     "fits": "M15 N-bar breakout with EMA trend filter; day-trading pace"},
    {"id": "intraday_pullback", "name": "Intraday Pullback (M15)",
     "family": "Intraday", "regime": "trend",
     "fits": "M15 held pullback to the fast EMA inside a trend"},
    {"id": "session_breakout", "name": "Session Opening-Range Breakout (M15)",
     "family": "Intraday", "regime": "trend",
     "fits": "first break of the London / NY opening range"},
    {"id": "quantum_qpl_bounce", "name": "Quantum Price Level Bounce",
     "family": "Quantum", "regime": "range",
     "fits": "rejection wick off a quantum support/resistance level"},
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
    QuantumPriceLevel(),         # 14 Quantum / trend  (quantum.py)
    QuantumPriceLevelBounce(),   # 15 Quantum / range
    QuantumPriceLevelSmc(),      # 16 Quantum + SMC demand zone (smc.py)
    IntradayMomentum(),          # 17 Intraday / trend (M15 day-trading)
    IntradayPullback(),          # 18 Intraday / trend (M15 day-trading)
    SessionRangeBreakout(),      # 19 Intraday / trend (opening range, 1-2/day)
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
# Self-test: run ALL strategies over a battery of scenarios. Any emitted Signal has     #
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

    assert len(DEFAULT_STRATEGIES) == 19
    assert len({s.id for s in DEFAULT_STRATEGIES}) == 19      # unique ids

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
    print(f"\n{n_fired}/{len(DEFAULT_STRATEGIES)} strategies fired across {len(scenarios)} scenarios; "
          f"all emitted contracts valid.")
    assert n_fired >= 12, f"only {n_fired} strategies fired"
