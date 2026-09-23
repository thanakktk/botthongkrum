"""
Smart-money-concepts (SMC / ICT) market context from closed bars
======================================================================
Pure functions on a bar window (no lookahead: a swing is only known `n`
bars after it printed, and every state is what a trader could have seen at
the close of the last bar). Used by `quantum_qpl` as a confluence filter:

  * swings          fractal swing highs / lows (n bars each side)
  * structure       BOS  = close beyond the last swing IN the current trend
                    CHoCH = first close beyond the last swing AGAINST it
                    -> trend (+1 bull / -1 bear / 0 unknown), last event
  * liquidity (LQ)  a bar that wicks beyond a prior unbroken swing low/high
                    and closes back inside = a stop-hunt / liquidity sweep;
                    equal lows/highs (within tol*ATR) count as a bigger pool
  * supply/demand   order blocks: the last opposite-colour candle before a
                    displacement (>= `disp_atr` ATR) that breaks a swing;
                    a zone dies when a close goes through it (mitigated);
                    "tap" = the current bar traded into a fresh zone

    ctx = smc_context(bars)      # SmcContext, see below
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from signals import Bar


@dataclass
class Zone:
    kind: str            # "demand" | "supply"
    low: float
    high: float
    born: int            # bar index of the OB candle
    swing_break: bool    # the displacement broke a swing (BOS-grade zone)
    touched: int = 0     # times price traded into it after birth


@dataclass
class SmcContext:
    trend: int = 0                        # +1 bull, -1 bear, 0 unknown
    last_event: str = ""                  # "bos_up" | "bos_dn" | "choch_up" | "choch_dn"
    event_bars_ago: int = 10 ** 6
    last_swing_high: Optional[float] = None
    last_swing_low: Optional[float] = None
    sweep_low_bars_ago: int = 10 ** 6     # liquidity grabbed BELOW a swing low (bullish)
    sweep_high_bars_ago: int = 10 ** 6    # liquidity grabbed ABOVE a swing high (bearish)
    sweep_low_level: Optional[float] = None
    sweep_high_level: Optional[float] = None
    demand: list = field(default_factory=list)   # active zones below/around price
    supply: list = field(default_factory=list)
    atr: float = 0.0

    # --- helpers for strategies --------------------------------------------
    def bullish_structure(self) -> bool:
        return self.trend > 0

    def bearish_structure(self) -> bool:
        return self.trend < 0

    def nearest_demand(self, price: float) -> Optional[Zone]:
        zs = [z for z in self.demand if z.low <= price]
        return max(zs, key=lambda z: z.high) if zs else None

    def nearest_supply(self, price: float) -> Optional[Zone]:
        zs = [z for z in self.supply if z.high >= price]
        return min(zs, key=lambda z: z.low) if zs else None


def _atr(bars: Sequence[Bar], n: int) -> float:
    if len(bars) < n + 1:
        return 0.0
    s = 0.0
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        s += max(h - l, abs(h - pc), abs(l - pc))
    return s / n


def swings(bars: Sequence[Bar], n: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(swing_highs, swing_lows) as (index, level), confirmed only where n
    bars exist on both sides (so nothing within the last n bars)."""
    hi: list[tuple[int, float]] = []
    lo: list[tuple[int, float]] = []
    for i in range(n, len(bars) - n):
        h, l = bars[i].high, bars[i].low
        if all(h > bars[j].high for j in range(i - n, i)) and \
                all(h >= bars[j].high for j in range(i + 1, i + n + 1)):
            hi.append((i, h))
        if all(l < bars[j].low for j in range(i - n, i)) and \
                all(l <= bars[j].low for j in range(i + 1, i + n + 1)):
            lo.append((i, l))
    return hi, lo


def smc_context(bars: Sequence[Bar], n: int = 3, atr_n: int = 14,
                disp_atr: float = 1.5, eq_tol: float = 0.15,
                max_zone_age: int = 120) -> SmcContext:
    """Walk the window once, bar by bar, updating structure / liquidity /
    zones exactly as they would have been known at each close."""
    ctx = SmcContext()
    N = len(bars)
    if N < 2 * n + 5:
        return ctx
    a = _atr(bars, atr_n)
    ctx.atr = a
    sh, sl = swings(bars, n)
    # a swing at index i becomes KNOWN at bar i + n
    known_h = {i + n: (i, lvl) for i, lvl in sh}
    known_l = {i + n: (i, lvl) for i, lvl in sl}

    trend = 0
    last_event, event_at = "", -10 ** 6
    # the swing levels that currently define structure (unbroken)
    cur_sh: Optional[tuple[int, float]] = None
    cur_sl: Optional[tuple[int, float]] = None
    pending_h: list[tuple[int, float]] = []      # all known, unbroken swing highs
    pending_l: list[tuple[int, float]] = []
    sweep_low_at, sweep_low_lvl = -10 ** 6, None
    sweep_high_at, sweep_high_lvl = -10 ** 6, None
    demand: list[Zone] = []
    supply: list[Zone] = []

    for t in range(N):
        b = bars[t]
        if t in known_h:
            cur_sh = known_h[t]
            pending_h.append(cur_sh)
        if t in known_l:
            cur_sl = known_l[t]
            pending_l.append(cur_sl)

        # ---- liquidity sweeps: wick through an unbroken swing, close back --
        if pending_l:
            lvl = min(x[1] for x in pending_l[-3:])       # nearest pools
            pool = [x for x in pending_l if abs(x[1] - lvl) <= eq_tol * a]  # equal lows
            lvl = max(x[1] for x in pool)
            if b.low < lvl and b.close > lvl:
                sweep_low_at, sweep_low_lvl = t, lvl
        if pending_h:
            lvl = max(x[1] for x in pending_h[-3:])
            pool = [x for x in pending_h if abs(x[1] - lvl) <= eq_tol * a]
            lvl = min(x[1] for x in pool)
            if b.high > lvl and b.close < lvl:
                sweep_high_at, sweep_high_lvl = t, lvl

        # ---- structure: close through the defining swing -------------------
        broke_up = cur_sh is not None and b.close > cur_sh[1]
        broke_dn = cur_sl is not None and b.close < cur_sl[1]
        if broke_up:
            last_event = "bos_up" if trend >= 0 else "choch_up"
            trend, event_at = 1, t
            pending_h = [x for x in pending_h if x[1] > b.close]
            cur_sh = None
            _spawn_zone(bars, t, "demand", a, disp_atr, demand, True)
        if broke_dn:
            last_event = "bos_dn" if trend <= 0 else "choch_dn"
            trend, event_at = -1, t
            pending_l = [x for x in pending_l if x[1] < b.close]
            cur_sl = None
            _spawn_zone(bars, t, "supply", a, disp_atr, supply, True)
        if not broke_up and not broke_dn:
            # displacement without a structure break still leaves a zone
            if t >= 1 and (b.close - b.open) >= disp_atr * a and b.close > bars[t - 1].high:
                _spawn_zone(bars, t, "demand", a, disp_atr, demand, False)
            if t >= 1 and (b.open - b.close) >= disp_atr * a and b.close < bars[t - 1].low:
                _spawn_zone(bars, t, "supply", a, disp_atr, supply, False)

        # ---- zone upkeep: touches and mitigation ----------------------------
        for z in demand:
            if z.born < t:
                if b.low <= z.high:
                    z.touched += 1
                if b.close < z.low:
                    z.touched = 10 ** 6            # mitigated -> drop below
        for z in supply:
            if z.born < t:
                if b.high >= z.low:
                    z.touched += 1
                if b.close > z.high:
                    z.touched = 10 ** 6
        demand = [z for z in demand if z.touched < 10 ** 6 and t - z.born <= max_zone_age]
        supply = [z for z in supply if z.touched < 10 ** 6 and t - z.born <= max_zone_age]

    ctx.trend, ctx.last_event = trend, last_event
    ctx.event_bars_ago = (N - 1) - event_at
    ctx.last_swing_high = cur_sh[1] if cur_sh else (pending_h[-1][1] if pending_h else None)
    ctx.last_swing_low = cur_sl[1] if cur_sl else (pending_l[-1][1] if pending_l else None)
    ctx.sweep_low_bars_ago = (N - 1) - sweep_low_at
    ctx.sweep_high_bars_ago = (N - 1) - sweep_high_at
    ctx.sweep_low_level, ctx.sweep_high_level = sweep_low_lvl, sweep_high_lvl
    ctx.demand, ctx.supply = demand, supply
    return ctx


def _spawn_zone(bars, t, kind, a, disp_atr, out, swing_break):
    """Order block = last opposite-colour candle within the 3 bars before the
    displacement bar t (needs the move from that candle to be >= disp_atr)."""
    for j in range(t - 1, max(t - 4, -1), -1):
        c = bars[j]
        if kind == "demand" and c.close < c.open and bars[t].close - c.low >= disp_atr * a:
            if not any(z.born == j for z in out):
                out.append(Zone("demand", c.low, max(c.open, c.close), j, swing_break))
            return
        if kind == "supply" and c.close > c.open and c.high - bars[t].close >= disp_atr * a:
            if not any(z.born == j for z in out):
                out.append(Zone("supply", min(c.open, c.close), c.high, j, swing_break))
            return


if __name__ == "__main__":       # synthetic self-test, no DB
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def mk(rows):
        return [Bar(now + timedelta(hours=4 * i), o, h, l, c) for i, (o, h, l, c) in enumerate(rows)]

    # zigzag with real swings: up to 104, down to 98, up to 101.5, down to 98.5,
    # then a sweep below the 98 low with reclaim, a bearish order-block candle
    # and a displacement through the 101.5 swing high -> bos_up + demand zone
    def leg(cs):
        return [(c - 0.3, c + 0.5, c - 0.8, c) for c in cs]
    rows = leg([100, 101, 102, 103, 104]) + leg([103, 102, 100, 99, 98]) +            leg([99, 100, 101, 101.5]) + leg([101, 100, 99, 98.5])
    rows += [(98.8, 99.0, 97.0, 98.7)]                # sweep the 97.2 swing low, close back above
    rows += [(98.7, 99.0, 98.2, 98.4)]                # bearish candle = order block
    rows += [(98.4, 103.0, 98.3, 102.5)]              # displacement through 101.5
    rows += leg([103, 103.5, 104, 104.5])
    ctx = smc_context(mk(rows))
    print(ctx.trend, ctx.last_event, "event", ctx.event_bars_ago, "bars ago; sweep_low",
          ctx.sweep_low_bars_ago, "bars ago @", ctx.sweep_low_level, "; demand zones",
          [(round(z.low, 1), round(z.high, 1), z.swing_break) for z in ctx.demand])
    assert ctx.trend == 1 and ctx.last_event in ("choch_up", "bos_up")
    assert ctx.sweep_low_bars_ago == 6 and abs(ctx.sweep_low_level - 97.2) < 1e-9
    assert ctx.demand and ctx.demand[0].swing_break
    print("smc.py self-test OK")
