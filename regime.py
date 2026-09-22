"""
Market Regime Detection (Pillar 4 input)
======================================================================
Classifies the current market as TREND or RANGE so the League can judge a
strategy only in the regime it is built for — the spec's key point: don't bench
EMA-cross for a low win-rate when it is simply trading inside a range.

Primary signal: Kaufman's Efficiency Ratio (ER) = |net move| / |path length|
over a window. ER near 1 => clean directional move (TREND); ER near 0 => price
oscillating with little net progress (RANGE). Cheap, robust, no look-ahead.
"""

from __future__ import annotations

from typing import Optional, Sequence

from signals import Bar, Regime


def efficiency_ratio(closes: Sequence[float], n: int) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    net = abs(closes[-1] - closes[-1 - n])
    noise = sum(abs(closes[i] - closes[i - 1])
                for i in range(len(closes) - n, len(closes)))
    if noise == 0:
        return 0.0                      # perfectly flat -> ranging
    return net / noise


def detect_regime(bars: Sequence[Bar], n: int = 20,
                  trend_threshold: float = 0.35) -> Regime:
    if len(bars) < n + 1:
        return Regime.UNKNOWN
    er = efficiency_ratio([b.close for b in bars], n)
    if er is None:
        return Regime.UNKNOWN
    return Regime.TREND if er >= trend_threshold else Regime.RANGE


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import datetime, timezone, timedelta
    t0 = datetime.now(timezone.utc)

    def bars(cs):
        return [Bar(t0 + timedelta(minutes=i), c, c + 0.5, c - 0.5, c)
                for i, c in enumerate(cs)]

    trend = bars([100 + i * 1.0 for i in range(40)])          # straight up
    chop = bars([100 + (2.0 if i % 2 else 0.0) for i in range(40)])  # oscillating

    er_t = efficiency_ratio([b.close for b in trend], 20)
    er_c = efficiency_ratio([b.close for b in chop], 20)
    print(f"trend ER={er_t:.2f} -> {detect_regime(trend).value}")
    print(f"chop  ER={er_c:.2f} -> {detect_regime(chop).value}")
    assert detect_regime(trend) == Regime.TREND
    assert detect_regime(chop) == Regime.RANGE
    print("\nRegime detection OK.")
