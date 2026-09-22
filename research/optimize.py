"""
Walk-forward parameter optimization (IS / OOS)
======================================================================
Guards against curve-fitting: parameters are chosen on IN-SAMPLE data and then
judged ONLY on the following OUT-OF-SAMPLE window the optimizer never saw. A
strategy set that looks great in-sample but collapses out-of-sample is overfit.

`build_fn(params)` constructs (strategies, arbitrator, backtest_cfg) from a
parameter dict, so you control exactly what is being optimized. The objective
heavily penalizes any FTMO floor breach — a high return that blows the account
is worthless.
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import itertools
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from signals import Bar
from ftmo_compliance_engine import AccountProfile
from backtester import Backtester, BacktestResult


def grid(param_grid: dict[str, list]) -> list[dict]:
    keys = list(param_grid)
    return [dict(zip(keys, combo))
            for combo in itertools.product(*(param_grid[k] for k in keys))]


def default_objective(r: BacktestResult) -> float:
    if r.floor_breached:
        return -1e9                       # never reward an account-killing run
    if not r.trades:
        return -1e8                       # took no trades -> useless
    return r.return_pct - 0.5 * r.max_drawdown_pct


@dataclass
class SplitOutcome:
    split: int
    best_params: dict
    is_score: float
    is_result: BacktestResult
    oos_result: BacktestResult


class WalkForwardOptimizer:
    def __init__(self, profile: AccountProfile,
                 build_fn: Callable[[dict], tuple],
                 objective: Callable[[BacktestResult], float] = default_objective,
                 specs: Optional[dict[str, float]] = None):
        self.profile = profile
        self.build_fn = build_fn
        self.objective = objective
        self.specs = specs

    def _backtest(self, symbol: str, bars: Sequence[Bar], params: dict) -> BacktestResult:
        strategies, arb, cfg = self.build_fn(params)
        return Backtester(self.profile, strategies, arb, cfg, self.specs).run(symbol, bars)

    def optimize(self, symbol: str, is_bars: Sequence[Bar],
                 param_grid: dict[str, list]) -> tuple[dict, float, BacktestResult]:
        best_p, best_score, best_res = None, float("-inf"), None
        for params in grid(param_grid):
            r = self._backtest(symbol, is_bars, params)
            s = self.objective(r)
            if s > best_score:
                best_p, best_score, best_res = params, s, r
        return best_p, best_score, best_res

    def walk_forward(self, symbol: str, bars: Sequence[Bar],
                     param_grid: dict[str, list], splits: int = 3) -> list[SplitOutcome]:
        """Anchored walk-forward: IS grows, OOS is always the next unseen chunk."""
        chunk = len(bars) // (splits + 1)
        out: list[SplitOutcome] = []
        for k in range(splits):
            is_bars = bars[: (k + 1) * chunk]
            oos_bars = bars[(k + 1) * chunk: (k + 2) * chunk]
            if len(oos_bars) < 40:
                break
            p, score, is_res = self.optimize(symbol, is_bars, param_grid)
            oos_res = self._backtest(symbol, oos_bars, p)
            out.append(SplitOutcome(k + 1, p, score, is_res, oos_res))
        return out


# --------------------------------------------------------------------------- #
# Self-test: optimize a small grid in-sample, validate out-of-sample.          #
#   ./env/Scripts/python.exe optimize.py                                       #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import math
    from datetime import datetime, timezone, timedelta
    from ftmo_compliance_engine import Variant, Path, Phase
    from arbitration import Arbitrator, ArbitrationConfig
    from backtester import BacktestConfig
    from strategies import (EmaCrossMomentum, BreakoutSR, DonchianBreakout,
                            RocMomentum)

    t0 = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)   # Monday

    def synth(n: int) -> list[Bar]:
        bars, price = [], 2000.0
        for i in range(n):
            price += 0.7 + 6.0 * math.sin(i / 11.0)
            bars.append(Bar(t0 + timedelta(minutes=5 * i), price,
                            price + 1.5, price - 1.5, price))
        return bars

    bars = synth(800)
    profile = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)

    # what we optimize: a trend strategy subset's lookback + sizing + selectivity
    def build_fn(p: dict):
        strategies = [EmaCrossMomentum(), BreakoutSR(lookback=p["lookback"]),
                      DonchianBreakout(n=p["lookback"]), RocMomentum()]
        arb = Arbitrator(ArbitrationConfig(
            timeframes=("M5",), risk_pct=p["risk_pct"],
            min_agreement=p["min_agreement"], min_agree=2, min_families=1,
            min_conviction=0.3))
        cfg = BacktestConfig(spread=0.3, slippage=0.1, commission_per_lot=2.0)
        return strategies, arb, cfg

    param_grid = {
        "lookback": [15, 25],
        "risk_pct": [0.005, 0.01],
        "min_agreement": [0.55, 0.70],   # how strong the combined agreement must be
    }

    opt = WalkForwardOptimizer(profile, build_fn, specs={"XAUUSD": 100.0})
    print(f"grid = {len(grid(param_grid))} combos, 3 walk-forward splits\n")
    outcomes = opt.walk_forward("XAUUSD", bars, param_grid, splits=3)

    oos_returns = []
    for o in outcomes:
        oos_returns.append(o.oos_result.return_pct)
        print(f"Split {o.split}: best={o.best_params}")
        print(f"   IS : {o.is_result.summary()}")
        print(f"   OOS: {o.oos_result.summary()}")

    assert outcomes, "no walk-forward splits produced"
    assert not any(o.oos_result.floor_breached for o in outcomes)
    mean_oos = sum(oos_returns) / len(oos_returns)
    print(f"\nMean OOS return across splits: {mean_oos*100:+.2f}%")
    print("Walk-forward IS/OOS optimization OK.")
