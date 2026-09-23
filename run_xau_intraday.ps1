# =====================================================================
#  INTRADAY day-trading bot — XAUUSD-ECN on VT Markets (one click)
# =====================================================================
#  One M15 loop running `intraday_momentum` (strategies.py): a closed M15
#  bar that breaks the high/low of the previous 8 bars in the direction of
#  the EMA20/EMA60 trend -> enter, SL 1 ATR, TP 1.5 x SL, max hold 4 h.
#  ~4 trades per trading day (roughly one every 1-2 hours in London/NY).
#
#  DAY-TRADING GOAL (new in main_loop): --daily-target-pct / --daily-stop-pct.
#  Once equity is +Target above the CET-midnight balance the loop flattens
#  and opens nothing more today; at -Stop it does the same. No overnight:
#  the compliance engine's session flatten stays as configured in .env.
#
#  !! READ BEFORE USING REAL MONEY (research/intraday_backtest.py):
#     2015-2026, $0.25/oz cost, 0.5%/trade, target 1%/stop 1%:
#       11,078 trades, WR 38%, W/L 1.16, PF 0.73, avgR -0.155  -> LOSES.
#       Every variant tested (18 rules x 7 goal settings) lost; a random
#       M15 entry with these exits gives WR ~40% and the spread eats the rest.
#     2025 alone (gold $3-4k, high volatility): PF 1.03 = break-even,
#       target hit 37% of days, stopped 33% of days, 48% positive days.
#     The daily target/stop change WHEN you stop, not the edge: with a
#     negative expectancy they only shape the distribution of daily results.
#     Opening-range breakout (-Strategy session_breakout, London+NY, ~1.7
#     trades/day) is the least bad: PF 0.91 (2015-26) / 0.95 (2022-26);
#     NY-only PF 0.97 / 1.06 but <1 trade/day. Details: docs/intraday_bot.md
#  Use this on DEMO to see the pace you asked for; the H4 trend bot
#  (run_xau_h4.ps1) and the quantum bot (run_xau_quantum.ps1) are the ones
#  with a measurable edge.
#
#  PRE-FLIGHT: MT5 (VT Markets) open + Algo Trading ON, XAUUSD-ECN in Market
#  Watch, Postgres up.   Stop everything:  .\stop_all.ps1
# =====================================================================
param([switch]$SkipPreflight, [double]$Risk = 0.005,
      [double]$Target = 0.01, [double]$Stop = 0.01,
      [string]$Strategy = 'intraday_momentum')   # or intraday_pullback

$root = $PSScriptRoot
$py   = Join-Path $root 'env\Scripts\python.exe'
$sym  = 'XAUUSD-ECN'

if (-not $SkipPreflight) {
    Write-Host "Running pre-flight checks..." -ForegroundColor Cyan
    $env:PYTHONUTF8 = '1'
    & $py (Join-Path $root 'tools\preflight.py') $sym
    Write-Host ""
    Write-Host "NOTE: the 11-year backtest of this bot is NEGATIVE (PF 0.73). Demo only unless you know why you disagree." -ForegroundColor Yellow
    $ans = Read-Host "Pre-flight OK? Launch the intraday bot ($Strategy, target $($Target*100)% / stop $($Stop*100)%)? (y/N)"
    if ($ans -ne 'y') { Write-Host "Aborted." -ForegroundColor Yellow; return }
}

function Start-Bot([string]$Title, [string]$Invoke) {
    $inner = "`$Host.UI.RawUI.WindowTitle='$Title'; `$env:PYTHONUTF8='1'; " +
             "Set-Location '$root'; $Invoke"
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoExit', '-Command', $inner) | Out-Null
    Write-Host ("  launched: {0}" -f $Title) -ForegroundColor Green
}

# identical to research/intraday_backtest.py: M15 closed bars, solo, one trade
# at a time, plain full exit at 1.5R (tp1 = tp2), daily target/stop lock
$common = "--trade --symbols $sym --tf 'M15:1.0' " +
          "--min-agree 1 --min-families 1 --min-agreement 0 --min-conviction 0 " +
          "--signal-floor 0 --tp1-r 1.5 --tp2-r 1.5 --be-trigger 0 --risk-pct $Risk " +
          "--daily-target-pct $Target --daily-stop-pct $Stop " +
          "--exposure own --no-shadow"

Write-Host "Launching intraday bot ($Strategy) on $sym (risk $Risk/trade, day goal +$($Target*100)% / -$($Stop*100)%)..." -ForegroundColor Cyan
Start-Bot "Intraday $Strategy" "& '$py' -u '$root\main_loop.py' $common --strategies $Strategy --ensemble-tag intra"
Start-Sleep -Seconds 4
Start-Bot 'watchdog'             "& '$py' -u '$root\watchdog.py'"
Start-Bot 'dashboard'            "& '$py' '$root\dashboard.py'"
Start-Bot 'news_service'         "& '$py' -u '$root\news_service.py'"

Write-Host ""
Write-Host "Launched. Verify:" -ForegroundColor Cyan
Write-Host "  * the loop prints roster=['$Strategy'], 'daily-goal=+1.0%/-1.0%' and 'closed bars only'"
Write-Host "  * dashboard -> http://127.0.0.1:8000 shows the 'intra' heartbeat / LIVE"
Write-Host "  * M15 bars close every 15 min: expect a decision each quarter hour, ~4 trades/day"
Write-Host "  * on '** daily target reached' / '** daily stop reached' the loop flattens and idles until the next CET day"
Write-Host "Stop everything later with:  .\stop_all.ps1" -ForegroundColor Yellow
