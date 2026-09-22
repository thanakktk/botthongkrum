"""Python backtester on the SAME VT Markets H4 bars the Strategy Tester uses,
3 solo strategies at RiskPct 1%, MaxNotionalFrac 2.0, merged by close time,
compounded -> the reference the EA's tester report must resemble."""
import sys, os, json, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import datetime, timezone
from dotenv import load_dotenv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
from parity_dump import fetch_h4
from backtester import Backtester, BacktestConfig
from arbitration import Arbitrator, ArbitrationConfig
from strategies import select_strategies
from ftmo_compliance_engine import AccountProfile, Variant, Path, Phase, EngineConfig

start = datetime(2024, 6, 1, tzinfo=timezone.utc)
bars = fetch_h4("XAUUSD-ECN", start)
bars = [b for b in bars if b.time >= start]
no_rules = EngineConfig(enforce_daily_loss=False, enforce_overall_loss=False, enforce_consistency=False,
                        enforce_weekend_flatten=False, enforce_news_blackout=False)
RISK = 0.01
streams = {}
for sid in ("breakout_sr", "donchian_breakout", "roc_momentum"):
    arb = Arbitrator(ArbitrationConfig(timeframes=("M5",), tf_weights={"M5": 1.0}, risk_pct=RISK, tp1_r=2.0, tp2_r=2.5,
                                       min_agreement=0, min_agree=1, min_families=1, min_conviction=0, signal_floor=0,
                                       max_notional_frac=2.0))
    bt = Backtester(AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 10_000), select_strategies((sid,)), arb,
                    BacktestConfig(initial_balance=10_000, risk_pct=RISK, tf_factor={"M5": 1}, spread=0.11, slippage=0.05,
                                   manage=True, tp1_r=2.0, partial_pct=0.5, trail_r=1.0, be_trigger_r=0.0),
                    specs={"XAUUSD-ECN": 100.0}, engine_cfg=no_rules)
    res = bt.run("XAUUSD-ECN", bars)
    streams[sid] = res
    print(f"{sid:<18s} trades={len(res.trades):4d} WR={res.win_rate*100:3.0f}% PF={res.profit_factor:.2f} "
          f"ret={res.return_pct*100:+6.1f}% maxDD={res.max_drawdown_pct*100:4.1f}%")
# merged portfolio: apply each trade's R at 1% of running equity in close order
trades = sorted(((t.closed_at, t.r_mult, sid) for sid, r in streams.items() for t in r.trades), key=lambda x: x[0])
eq = peak = 10_000.0; dd = 0.0
for _, r, _ in trades:
    eq *= 1 + RISK * r; peak = max(peak, eq); dd = max(dd, 1 - eq / peak)
print(f"MERGED (3 strategies, 1%/trade compounding): {len(trades)} trades, final {eq:,.0f} ({eq/10_000-1:+.1%}), maxDD {dd:.1%}, "
      f"{bars[0].time:%Y-%m-%d} -> {bars[-1].time:%Y-%m-%d}")
