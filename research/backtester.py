"""
Backtester (Pillar 6) — event-driven, single symbol
======================================================================
Replays historical bars through the SAME strategy -> arbitration pipeline used
live, fills on a PaperBroker with spread/slippage/commission, and enforces the
FTMO Compliance Engine (floors, sizing gate, weekend flatten) bar by bar. It
reports the metrics that matter for a prop account — including whether any FTMO
floor would have been breached.

It is intentionally DB-free and fast (no Postgres, no MT5 orders), so it can be
run repeatedly and walk-forward over out-of-sample windows.

Fill model (per bar):
  * entry: at bar close +/- half-spread +/- slippage (buy pays up, sell pays down)
  * exit : SL/TP checked against the bar's high/low; if both touch in one bar we
           assume the STOP filled first (conservative). Otherwise mark-to-close.
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

from dataclasses import dataclass, field
import bisect
from datetime import datetime, timedelta
from typing import Optional, Sequence

from signals import Bar
from ftmo_compliance_engine import (
    FtmoComplianceEngine, AccountProfile, AccountSnapshot, Action, EngineConfig,
)
from paper_broker import PaperBroker
from arbitration import Arbitrator, ArbitrationConfig
from strategies import Strategy, DEFAULT_STRATEGIES
from sessions import label as session_label


_TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
               "H4": 14400, "D1": 86400}


def resample_clock(base: Sequence[Bar], secs: int) -> list[Bar]:
    """Aggregate into CLOCK-ALIGNED buckets of `secs` (how MT5 builds H1/H4:
    00:00, 04:00, ...). Unlike `resample`, gaps (weekends, missing bars) never
    shift the bucket boundaries."""
    out: list[Bar] = []
    for b in base:
        ts = int(b.time.timestamp())
        start = b.time - timedelta(seconds=ts % secs)
        last = out[-1] if out else None
        if last is not None and last.time == start:
            out[-1] = Bar(time=start, open=last.open, high=max(last.high, b.high),
                          low=min(last.low, b.low), close=b.close,
                          volume=last.volume + b.volume)
        else:
            out.append(Bar(time=start, open=b.open, high=b.high, low=b.low,
                           close=b.close, volume=b.volume))
    return out


# bars handed to the broker per TF each step (>= Arbitrator.collect bar_count)
_TAIL = 400

# higher timeframes expressed in M5 units
_TF_FACTOR = {"M5": 1, "M15": 3, "M30": 6, "H1": 12, "H4": 48, "D1": 288}


def resample(m5: Sequence[Bar], factor: int) -> list[Bar]:
    """Aggregate consecutive M5 bars into a higher timeframe (OHLC)."""
    if factor <= 1:
        return list(m5)
    out = []
    for i in range(0, len(m5) - factor + 1, factor):
        g = m5[i:i + factor]
        out.append(Bar(time=g[0].time, open=g[0].open,
                       high=max(b.high for b in g), low=min(b.low for b in g),
                       close=g[-1].close, volume=sum(b.volume for b in g)))
    return out


@dataclass
class BacktestConfig:
    initial_balance: float = 100_000.0
    spread: float = 0.0              # price units (full spread; half each side)
    slippage: float = 0.0            # price units added against us per fill
    commission_per_lot: float = 0.0
    warmup: int = 30                 # bars before trading starts
    risk_pct: float = 0.01
    # ----- live trade management (off by default; matches ExecutionEngine) ----
    manage: bool = False            # simulate TP1 partial + break-even + trailing
    tp1_r: float = 1.0              # bank a partial + go break-even at this R
    partial_pct: float = 0.5        # fraction banked at TP1
    be_buffer_r: float = 0.05       # break-even nudged this far into profit (× R)
    trail_r: float = 1.0            # trail distance after TP1, in R
    be_trigger_r: float = 0.0       # EARLY break-even at this R (0 = off). The MFE
                                    # study showed losers peak ~0.7R; an early BE
                                    # turns some -1R losses into ~0R.
    # resample factors for the BASE series. Default None = the M5-based _TF_FACTOR
    # (live path). Pass histdata.M15_TF_FACTOR when the base series is M15.
    tf_factor: Optional[dict] = None
    # True (default): higher TFs are clock-aligned like MT5's. False: the legacy
    # count-based grouping, which drifts across gaps (~91% of H4 bars misaligned
    # on the M15 history) — kept only to reproduce old reports.
    clock_align: bool = True


@dataclass
class Trade:
    side: str
    entry: float
    exit: float
    volume: float
    pnl: float
    opened_at: datetime
    closed_at: datetime
    reason: str
    # ----- study fields (the "microscope") -----
    strategy: str = ""        # the rep/ensemble strategy that drove the entry
    regime: str = ""          # detected regime at entry (trend/range/unknown)
    session: str = ""         # asian / london / ny / off (at entry, UTC)
    mae_r: float = 0.0        # Max Adverse Excursion in R units (>=0)
    mfe_r: float = 0.0        # Max Favorable Excursion in R units (>=0)
    r_mult: float = 0.0       # realized return in R (pnl / planned $ risk)


@dataclass
class BacktestResult:
    symbol: str
    bars: int
    trades: list[Trade] = field(default_factory=list)
    final_balance: float = 0.0
    return_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    max_daily_loss: float = 0.0
    floor_breached: bool = False
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        n = len(self.trades)
        return (f"{self.symbol}: bars={self.bars} trades={n} "
                f"win%={self.win_rate*100:.0f} PF={self.profit_factor:.2f} "
                f"ret={self.return_pct*100:+.2f}% maxDD={self.max_drawdown_pct*100:.1f}% "
                f"maxDailyLoss=${self.max_daily_loss:,.0f} "
                f"{'BREACH!' if self.floor_breached else 'ok'}")


class Backtester:
    def __init__(self, profile: AccountProfile, strategies: Sequence[Strategy],
                 arbitrator: Arbitrator, cfg: Optional[BacktestConfig] = None,
                 specs: Optional[dict[str, float]] = None,
                 engine_cfg: Optional[EngineConfig] = None):
        self.profile = profile
        # None -> full FTMO ruleset (the historical default). Pass
        # `config_from_env()` to backtest under the same RULES_MODE as live.
        self.engine_cfg = engine_cfg
        self.strategies = strategies
        self.arb = arbitrator
        self.cfg = cfg or BacktestConfig()
        self.specs = specs

    def run(self, symbol: str, bars: Sequence[Bar]) -> BacktestResult:
        cfg = self.cfg
        broker = PaperBroker(cfg.initial_balance, specs=self.specs,
                             commission_per_lot=cfg.commission_per_lot)
        engine = FtmoComplianceEngine(self.profile, self.engine_cfg)
        engine.mark_reconciled()
        engine.roll_daily_baseline(bars[cfg.warmup].time, cfg.initial_balance)

        res = BacktestResult(symbol=symbol, bars=len(bars))
        peak_equity = cfg.initial_balance
        day = engine.cet_date(bars[cfg.warmup].time)
        day_start_balance = cfg.initial_balance
        half = cfg.spread / 2.0
        # per-open-ticket study metadata (MAE/MFE accumulate bar by bar)
        self._meta: dict[int, dict] = {}

        # MULTI-TIMEFRAME: precompute each higher TF the arbitrator reads, so the
        # backtest matches the live MTF logic instead of only M5. fmap maps each TF
        # to a multiple of the BASE bar (M5 live, or M15 for the long history CSV).
        fmap = cfg.tf_factor or _TF_FACTOR
        tfs = getattr(self.arb.cfg, "timeframes", ("M5",))
        base_tf = next((k for k, v in fmap.items() if v == 1), "M5")
        base_secs = _TF_SECONDS[base_tf]
        if cfg.clock_align:
            series = {tf: resample_clock(bars, _TF_SECONDS[tf]) for tf in tfs}
            # a bucket is visible once it has CLOSED (start + tf <= base bar close)
            closes = {tf: [int(b.time.timestamp()) + _TF_SECONDS[tf]
                           for b in series[tf]] for tf in tfs}
        else:
            series = {tf: resample(bars, fmap.get(tf, 1)) for tf in tfs}

        for i in range(cfg.warmup, len(bars)):
            bar = bars[i]
            # serve each timeframe's bars as-of this M5 bar (only completed candles)
            # Only a TAIL window: consumers read <= 200 bars (Arbitrator.collect's
            # bar_count), and slicing from 0 made long runs O(n^2).
            for tf in tfs:
                f = fmap.get(tf, 1)
                if f == 1:
                    hi, src = i + 1, bars
                elif cfg.clock_align:
                    now_close = int(bar.time.timestamp()) + base_secs
                    hi, src = bisect.bisect_right(closes[tf], now_close), series[tf]
                else:
                    hi, src = (i + 1) // f, series[tf]
                broker.set_bars(symbol, src[max(0, hi - _TAIL):hi], timeframe=tf)

            # CET day rollover -> re-anchor + reset the day's loss tracking
            d = engine.cet_date(bar.time)
            if d != day:
                day = d
                day_start_balance = broker.balance()
                engine.roll_daily_baseline(bar.time, day_start_balance)

            # update MAE/MFE for every still-open position using this bar's range
            self._update_excursions(broker, bar)

            # (1) exits: SL/TP against this bar's range (uses SL as of bar start)
            self._process_exits(broker, symbol, bar, res)

            # (1b) live-style management: TP1 partial / (early) break-even / trail.
            # Runs AFTER exits so SL changes apply from the NEXT bar (no look-ahead).
            if cfg.manage:
                self._manage(broker, bar)

            # (2) mark-to-close, snapshot, floors
            broker.set_price(symbol, bar.close)
            snap = AccountSnapshot(broker.balance(), broker.equity(),
                                   broker.open_risk_to_sl())
            res.equity_curve.append(snap.equity)
            peak_equity = max(peak_equity, snap.equity)
            res.max_drawdown_pct = max(res.max_drawdown_pct,
                                       (peak_equity - snap.equity) / peak_equity)
            res.max_daily_loss = max(res.max_daily_loss,
                                     day_start_balance - snap.equity)
            # "breach" = crossing a floor the engine actually enforces; with
            # RULES_MODE=none there is none unless SELF_*_LOSS_PCT is set.
            if (engine.cfg.enforce_overall_loss
                    and snap.equity <= engine.real_overall_floor()) or \
                    (engine.cfg.enforce_daily_loss
                     and snap.equity <= engine.real_daily_floor()):
                res.floor_breached = True

            verdict = engine.evaluate(snap, bar.time)
            if verdict.action == Action.FLATTEN_ALL:
                self._flatten(broker, symbol, bar, res, "flatten_all")
                continue

            # (3) Standard weekend/session flatten -> close + no new entries
            if engine.time_flatten_required(bar.time) is not None:
                self._flatten(broker, symbol, bar, res, "time_flatten")
                continue

            # (4) new entries (exposure cap = one position/symbol via arbitrator)
            if verdict.action != Action.NORMAL or broker.open_positions():
                continue
            sigs = self.arb.collect(self.strategies, [symbol], broker, bar.time)
            for req in self.arb.arbitrate(sigs, broker, broker.equity(), bar.time):
                gate = engine.check_new_order(snap=snap, new_order_risk=req.risk_to_sl,
                                              now_utc=bar.time)
                if gate.action != Action.NORMAL:
                    continue
                fill = (bar.close + half + cfg.slippage if req.side == "buy"
                        else bar.close - half - cfg.slippage)
                ticket = broker.place_market(symbol=symbol, side=req.side,
                                             volume=req.volume, sl=req.sl, tp=req.tp,
                                             price=fill, client_order_id=f"bt-{i}")
                self._meta[ticket] = {
                    "strategy": req.strategy_id, "regime": req.regime or "",
                    "session": session_label(bar.time), "entry": fill,
                    "rdist": abs(fill - req.sl) if req.sl is not None else 0.0,
                    "risk": req.risk_to_sl, "side": req.side, "mae": 0.0, "mfe": 0.0,
                    "mgmt": "running", "banked": 0.0, "orig_vol": req.volume,
                }

        # close anything still open at the last bar
        self._flatten(broker, symbol, bars[-1], res, "end_of_data")
        return self._finalize(broker, res)

    # ----- helpers --------------------------------------------------------- #
    def _update_excursions(self, broker: PaperBroker, bar: Bar) -> None:
        """Accumulate Max Adverse / Favorable Excursion (price) for open trades."""
        for p in broker.open_positions():
            m = self._meta.get(p.ticket)
            if m is None:
                continue
            if p.side == "buy":
                fav, adv = bar.high - m["entry"], m["entry"] - bar.low
            else:
                fav, adv = m["entry"] - bar.low, bar.high - m["entry"]
            if fav > m["mfe"]:
                m["mfe"] = fav
            if adv > m["mae"]:
                m["mae"] = adv

    def _manage(self, broker: PaperBroker, bar: Bar) -> None:
        """Simulate the live ExecutionEngine.manage_position: at TP1 bank a partial
        + go break-even, then trail; optionally an EARLY break-even at be_trigger_r.
        SL edits take effect next bar (set here, enforced by _process_exits later)."""
        cfg = self.cfg
        for p in list(broker.open_positions()):
            m = self._meta.get(p.ticket)
            if m is None or m["rdist"] <= 0:
                continue
            d = 1 if m["side"] == "buy" else -1
            entry, rdist = m["entry"], m["rdist"]
            fav_extreme = bar.high if m["side"] == "buy" else bar.low
            fav_r = d * (fav_extreme - entry) / rdist        # peak R in our favour
            be = entry + d * cfg.be_buffer_r * rdist

            if m["mgmt"] in ("running", "be_locked"):
                if fav_r >= cfg.tp1_r:                        # TP1: bank partial + BE
                    tp1_px = entry + d * cfg.tp1_r * rdist
                    part = round(m["orig_vol"] * cfg.partial_pct, 2)
                    if part >= 0.01 and round(p.volume - part, 2) >= 0.01:
                        m["banked"] += broker.partial_close_position(
                            p.ticket, part, price=tp1_px)
                    broker.modify_sl_tp(p.ticket, sl=be)
                    m["mgmt"] = "tp1_hit"
                elif m["mgmt"] == "running" and cfg.be_trigger_r > 0 \
                        and fav_r >= cfg.be_trigger_r:        # early break-even only
                    broker.modify_sl_tp(p.ticket, sl=be)
                    m["mgmt"] = "be_locked"

            if m["mgmt"] == "tp1_hit":                        # trail the runner
                trail = bar.close - d * cfg.trail_r * rdist
                better = trail > p.sl if m["side"] == "buy" else trail < p.sl
                if p.sl is None or better:
                    broker.modify_sl_tp(p.ticket, sl=trail)

    def _process_exits(self, broker: PaperBroker, symbol: str, bar: Bar,
                       res: BacktestResult) -> None:
        for p in list(broker.open_positions()):
            sl, tp = p.sl, p.tp
            exit_px = reason = None
            if p.side == "buy":
                if sl is not None and bar.low <= sl:        # stop first
                    exit_px, reason = sl - self.cfg.slippage, "sl"
                elif tp is not None and bar.high >= tp:
                    exit_px, reason = tp, "tp"
            else:
                if sl is not None and bar.high >= sl:
                    exit_px, reason = sl + self.cfg.slippage, "sl"
                elif tp is not None and bar.low <= tp:
                    exit_px, reason = tp, "tp"
            if exit_px is not None:
                self._close(broker, p.ticket, p.side, exit_px, bar, res, reason)

    def _flatten(self, broker: PaperBroker, symbol: str, bar: Bar,
                 res: BacktestResult, reason: str) -> None:
        for p in list(broker.open_positions()):
            self._close(broker, p.ticket, p.side, bar.close, bar, res, reason)

    def _close(self, broker, ticket, side, price, bar, res, reason) -> None:
        # capture entry before the broker drops the position
        entry = next(p.open_price for p in broker.open_positions()
                     if p.ticket == ticket)
        vol = next(p.volume for p in broker.open_positions() if p.ticket == ticket)
        pnl = broker.close_position(ticket, price=price)
        m = self._meta.pop(ticket, None)
        if m:
            rdist = m["rdist"] or 0.0
            pnl += m.get("banked", 0.0)        # include any TP1 partial already banked
            vol = m.get("orig_vol", vol)       # report the trade's original size
            mae_r = (m["mae"] / rdist) if rdist > 0 else 0.0
            mfe_r = (m["mfe"] / rdist) if rdist > 0 else 0.0
            r_mult = (pnl / m["risk"]) if m.get("risk") else 0.0
            strat, regime, sess = m["strategy"], m["regime"], m["session"]
        else:
            mae_r = mfe_r = r_mult = 0.0
            strat = regime = sess = ""
        res.trades.append(Trade(side, entry, price, vol, pnl, bar.time, bar.time,
                                reason, strat, regime, sess, mae_r, mfe_r, r_mult))

    def _finalize(self, broker: PaperBroker, res: BacktestResult) -> BacktestResult:
        res.final_balance = broker.balance()
        res.return_pct = (res.final_balance / self.cfg.initial_balance) - 1.0
        wins = [t for t in res.trades if t.pnl > 0]
        losses = [t for t in res.trades if t.pnl <= 0]
        res.win_rate = len(wins) / len(res.trades) if res.trades else 0.0
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        res.profit_factor = (gross_win / gross_loss if gross_loss > 0
                             else (float("inf") if gross_win > 0 else 0.0))
        return res

    # ----- walk-forward: sequential out-of-sample windows ------------------ #
    def walk_forward(self, symbol: str, bars: Sequence[Bar],
                     windows: int = 4) -> list[BacktestResult]:
        out: list[BacktestResult] = []
        size = len(bars) // windows
        for w in range(windows):
            seg = bars[w * size: (w + 1) * size] if w < windows - 1 \
                else bars[w * size:]
            if len(seg) > self.cfg.warmup + 5:
                out.append(self.run(symbol, seg))
        return out


# --------------------------------------------------------------------------- #
# Self-test: a noisy uptrend with pullbacks -> breakout takes trades.          #
#   ./env/Scripts/python.exe backtester.py                                     #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import math
    from datetime import timezone, timedelta
    from ftmo_compliance_engine import Variant, Path, Phase

    # Monday start so the synthetic intraday series never hits the weekend gate.
    t0 = datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)

    def synth(n: int) -> list[Bar]:
        bars = []
        price = 2000.0
        for i in range(n):
            price += 0.8 + 6.0 * math.sin(i / 9.0)   # uptrend + oscillation
            hi, lo = price + 1.5, price - 1.5
            bars.append(Bar(t0 + timedelta(minutes=5 * i), price, hi, lo, price))
        return bars

    bars = synth(400)
    profile = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    # Realistic XAUUSD costs (verified live 2026-06-20): spread ~$0.44; the
    # commission could not be measured (XAU market closed on the weekend) — set a
    # placeholder and confirm against FTMO's schedule / a Monday test trade.
    # Single timeframe for the backtest (PaperBroker has one bar series); relaxed
    # confluence so the synthetic run produces trades to exercise the mechanics.
    arb = Arbitrator(ArbitrationConfig(timeframes=("M5",), min_agreement=0.60,
                                       min_agree=2, min_families=1, min_conviction=0.3))
    bt = Backtester(profile, DEFAULT_STRATEGIES, arb,
                    BacktestConfig(spread=0.44, slippage=0.1, commission_per_lot=5.0),
                    specs={"XAUUSD": 100.0})

    res = bt.run("XAUUSD", bars)
    print("FULL  ", res.summary())
    assert res.bars == 400 and len(res.equity_curve) > 0
    assert not res.floor_breached            # tiny 1% sizing must not breach FTMO

    print("\nWalk-forward (4 OOS windows):")
    for k, r in enumerate(bt.walk_forward("XAUUSD", bars, windows=4)):
        print(f"  W{k+1}", r.summary())

    print("\nBacktester OK.")
