# DISABLED 2026-09-22: the all-13 vote bot LOSES money in every market era
# (reports/strategy_lab.txt: avgR -0.064, t=-3.9) and this launcher ran it at
# 1% risk. Use run_xau_h4.ps1. Remove the two lines below to re-enable.
Write-Host 'run_xau_vote.ps1 is DISABLED - vote bot is a proven loser; use run_xau_h4.ps1' -ForegroundColor Red
return

# =====================================================================
#  XAUUSD live launcher — ALL-TECHNIQUE "vote" roster (higher risk, precise)
# =====================================================================
#  A SECOND trading loop that runs ALONGSIDE run_xau_robust.ps1 on the SAME
#  FTMO account. It analyses ALL 13 techniques, votes which side (buy/sell) has
#  the greater combined weight, and opens only when the agreement is high
#  (precise). Bigger size per trade (1%) than the robust core (0.3%).
#
#  WHY it is safe to run two loops on one account:
#    * --ensemble-tag vote  -> this loop tags its orders "vote:*" and MANAGES
#      (TP1/BE/trail) only its OWN positions. The robust loop ("ensemble:*")
#      never touches a vote position and vice-versa.
#    * exposure cap = one position per symbol (read from the broker) -> the two
#      loops never stack XAUUSD exposure; they take turns. Per-trade account risk
#      is at most this loop's 1% (when it opens) or robust's 0.3%.
#
#  Reasoning is in THAI on open (why this side won the vote) AND on close (why it
#  closed) — see the Discord trade channel / dashboard.
#
#  Backtest 2025 (1% risk, TP1@2R): ~375 trades, PF 1.18, +7.25%, maxDD ~5.9%
#  (within FTMO's 10% overall; uses real margin — watch it).
#
#  PRE-FLIGHT: MT5 open + Algo ON, XAUUSD in Market Watch, Postgres up, and the
#  robust loop + watchdog + dashboard already running (run_xau_all.ps1).
#
#  Run:  .\run_xau_vote.ps1
# =====================================================================
$env:PYTHONUTF8 = "1"
& "$PSScriptRoot\env\Scripts\python.exe" "$PSScriptRoot\main_loop.py" `
    --trade --symbols XAUUSD-ECN,BTCUSD --ensemble-tag vote `
    --min-agree 3 --min-families 2 --min-agreement 0.80 --min-conviction 2.0 `
    --tf "M30:1.0,H1:1.6,H4:2.4" --risk-pct 0.01 --tp1-r 2.0 --be-trigger 0 `
    --no-shadow
