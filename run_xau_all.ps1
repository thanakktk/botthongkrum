# =====================================================================
#  XAUUSD live launcher — ALL bots, one click (Monday go-live)
# =====================================================================
#  Starts every process the live system needs, each in its OWN window so
#  you can watch its output:
#
#    1. main_loop.py  (XAU robust roster)  <- via run_xau_robust.ps1
#    2. watchdog.py   (kill-switch / safety net)
#    3. dashboard.py  (http://127.0.0.1:8000)
#    4. signal_bot.py (advisory Discord alerts, XAUUSD)
#    5. news_service.py (economic calendar + news alerts)
#
#  PRE-FLIGHT (must be true BEFORE running this):
#    * MT5 terminal open + logged in + ALGO TRADING ON (green Algo button)
#    * XAUUSD visible in MT5 Market Watch
#    * Postgres (docker 'local_pgdb') up
#
#  Run:  .\run_xau_all.ps1            (runs preflight first, asks to confirm)
#        .\run_xau_all.ps1 -SkipPreflight   (skip the check)
# =====================================================================
param([switch]$SkipPreflight)

$root = $PSScriptRoot
$py   = Join-Path $root 'env\Scripts\python.exe'

# ----- pre-flight ----------------------------------------------------- #
if (-not $SkipPreflight) {
    Write-Host "Running pre-flight checks..." -ForegroundColor Cyan
    $env:PYTHONUTF8 = '1'
    & $py (Join-Path $root 'tools\preflight.py')
    Write-Host ""
    $ans = Read-Host "Pre-flight OK? Launch all bots? (y/N)"
    if ($ans -ne 'y') { Write-Host "Aborted." -ForegroundColor Yellow; return }
}

# ----- launcher helper ------------------------------------------------ #
function Start-Bot([string]$Title, [string]$Invoke) {
    $inner = "`$Host.UI.RawUI.WindowTitle='$Title'; `$env:PYTHONUTF8='1'; " +
             "Set-Location '$root'; $Invoke"
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoExit', '-Command', $inner) | Out-Null
    Write-Host ("  launched: {0}" -f $Title) -ForegroundColor Green
}

Write-Host "Launching XAUUSD live stack..." -ForegroundColor Cyan

# 1) main_loop first so a heartbeat exists before the watchdog's grace ends.
Start-Bot 'FTMO main_loop (XAU)' "& '$root\run_xau_robust.ps1'"
Start-Sleep -Seconds 5

# 2) watchdog (safety net — always alongside the loop)
Start-Bot 'FTMO watchdog' "& '$py' '$root\watchdog.py'"

# 3) dashboard
Start-Bot 'FTMO dashboard' "& '$py' '$root\dashboard.py'"

# 4) advisory signal bot — XAUUSD (was BTCUSD before)
Start-Bot 'FTMO signal_bot (XAU)' "& '$py' '$root\signal_bot.py' --symbol XAUUSD-ECN --interval 60"

# 5) news service
Start-Bot 'FTMO news_service' "& '$py' '$root\news_service.py'"

Write-Host ""
Write-Host "All 5 windows launched. Verify:" -ForegroundColor Cyan
Write-Host "  * dashboard  -> http://127.0.0.1:8000"
Write-Host "  * main_loop  -> shows 'Startup reconcile: ok=True' then ticking heartbeats"
Write-Host "  * watchdog   -> mode=running, hb_age small"
Write-Host "  * no red errors in any window"
Write-Host ""
Write-Host "Stop everything later with:  .\stop_all.ps1" -ForegroundColor Yellow
