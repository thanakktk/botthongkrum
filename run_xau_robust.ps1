# =====================================================================
#  XAUUSD live launcher — OOS-validated 'robust' roster (Phase 2 edge study)
# =====================================================================
#  Runs ONLY the 4 strategies that showed positive expectancy in BOTH the
#  train and the unseen test window, on XAUUSD AND BTCUSD:
#      macd_trend, roc_momentum, donchian_breakout, breakout_sr
#  (trend / breakout / momentum — the theme gold actually rewards)
#
#  Pre-flight (Monday, when the XAU market is open):
#    1. MT5 terminal running + Algo Trading ENABLED (the "Algo" button green)
#    2. Postgres up (docker)            3. Watchdog + dashboard running
#
#  Run:  .\run_xau_robust.ps1
#  Tune: lower --risk-pct for less drawdown; ~0.3% gave ~4.5% maxDD in test.
# =====================================================================
#  NOTE: the --require-regime "trend" gate was REMOVED — it looked great on the
#  recent ~7 months (+53% expectancy) but a full 11-year backtest
#  (backtest_history.py) showed it HURTS over the cycle (avgR +0.034 baseline vs
#  +0.008 filtered). Lesson: validate over many years, not one regime. Re-add a
#  filter ONLY if it proves out on the long history.
#  Trade management: bank the partial + break-even LATE (tp1@2.0R), early-BE OFF.
#  11-year sweep (test_history_variants.py) winner: avgR +0.068, 8/11 yrs positive
#  vs the old early-BE@0.8 (+0.034, 6/11). "Give the trend room."
$env:PYTHONUTF8 = "1"
& "$PSScriptRoot\env\Scripts\python.exe" "$PSScriptRoot\main_loop.py" `
    --trade --symbols XAUUSD-ECN,BTCUSD --strategies robust `
    --min-agree 2 --min-families 2 --min-agreement 0.70 --min-conviction 1.0 `
    --tf "M30:1.0,H1:1.6,H4:2.4" --risk-pct 0.003 --tp1-r 2.0 --be-trigger 0
