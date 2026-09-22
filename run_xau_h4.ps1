# =====================================================================
#  H4 TREND bot — XAUUSD-ECN on VT Markets (one click)
# =====================================================================
#  Three single-strategy loops, each trading ALONE on H4 (no confluence),
#  each with its own ensemble tag so it manages only its own positions and
#  the three can hold gold at the same time (--exposure own):
#
#    h4_brk  breakout_sr        (22-yr avgR +0.14, positive in all 4 eras)
#    h4_don  donchian_breakout  (+0.12)
#    h4_roc  roc_momentum       (+0.10)
#
#  Sizing (research/sizing_sim.py, 2004-2026): risk 1.0%/trade + drawdown
#  throttle (x0.5 below 20% from peak, x0.25 below 35%) -> avg ~+2.0%/month,
#  CAGR ~22%, historical max DD 38%, P(DD>50%) ~1%. Expect losing years
#  (2018/2019/2021/2022 were -9..-24%) and 6-9 month losing streaks.
#
#  Plus watchdog (kill switch; SELF_OVERALL_LOSS_PCT in .env is the hard
#  stop), dashboard (http://127.0.0.1:8000) and the news service.
#
#  PRE-FLIGHT: MT5 (VT Markets) open + Algo Trading ON, XAUUSD-ECN in Market
#  Watch, Postgres up.   Stop everything:  .\stop_all.ps1
# =====================================================================
param([switch]$SkipPreflight, [double]$Risk = 0.01)

$root = $PSScriptRoot
$py   = Join-Path $root 'env\Scripts\python.exe'
$sym  = 'XAUUSD-ECN'

if (-not $SkipPreflight) {
    Write-Host "Running pre-flight checks..." -ForegroundColor Cyan
    $env:PYTHONUTF8 = '1'
    & $py (Join-Path $root 'tools\preflight.py') $sym
    Write-Host ""
    $ans = Read-Host "Pre-flight OK? Launch the H4 trend bot? (y/N)"
    if ($ans -ne 'y') { Write-Host "Aborted." -ForegroundColor Yellow; return }
}

function Start-Bot([string]$Title, [string]$Invoke) {
    $inner = "`$Host.UI.RawUI.WindowTitle='$Title'; `$env:PYTHONUTF8='1'; " +
             "Set-Location '$root'; $Invoke"
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoExit', '-Command', $inner) | Out-Null
    Write-Host ("  launched: {0}" -f $Title) -ForegroundColor Green
}

# identical settings to the solo H4 backtests (strategy_lab / sizing_sim)
$common = "--trade --symbols $sym --tf 'H4:1.0' " +
          "--min-agree 1 --min-families 1 --min-agreement 0 --min-conviction 0 " +
          "--signal-floor 0 --tp1-r 2.0 --be-trigger 0 --risk-pct $Risk " +
          "--dd-levels '0.20:0.5,0.35:0.25' --exposure own --no-shadow"

Write-Host "Launching H4 trend bot on $sym (risk $Risk/trade)..." -ForegroundColor Cyan
Start-Bot 'H4 breakout_sr'       "& '$py' -u '$root\main_loop.py' $common --strategies breakout_sr --ensemble-tag h4_brk"
Start-Sleep -Seconds 4
Start-Bot 'H4 donchian_breakout' "& '$py' -u '$root\main_loop.py' $common --strategies donchian_breakout --ensemble-tag h4_don"
Start-Sleep -Seconds 4
Start-Bot 'H4 roc_momentum'      "& '$py' -u '$root\main_loop.py' $common --strategies roc_momentum --ensemble-tag h4_roc"
Start-Sleep -Seconds 4
Start-Bot 'watchdog'             "& '$py' -u '$root\watchdog.py'"
Start-Bot 'dashboard'            "& '$py' '$root\dashboard.py'"
Start-Bot 'news_service'         "& '$py' -u '$root\news_service.py'"

Write-Host ""
Write-Host "6 windows launched. Verify:" -ForegroundColor Cyan
Write-Host "  * each loop prints '[config] drawdown throttle ON' + 'closed bars only; exposure cap = own'"
Write-Host "  * dashboard -> http://127.0.0.1:8000 shows 3 heartbeats / LIVE"
Write-Host "  * H4 bars close at 00/04/08/12/16/20 server time (UTC+3): expect activity only then"
Write-Host "Stop everything later with:  .\stop_all.ps1" -ForegroundColor Yellow
