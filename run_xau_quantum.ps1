# =====================================================================
#  QUANTUM PRICE LEVEL bot — XAUUSD-ECN on VT Markets (one click)
# =====================================================================
#  One solo H4 loop running the `quantum_qpl` strategy (strategies.py,
#  math in quantum.py): Raymond Lee's quantum-finance price ladder
#      QPL(+-n) = P0 * (1 +- 0.21 * sigma * E(n)/E(0))
#  built around the previous day's close from the energy levels E(n) of a
#  quantum anharmonic oscillator (lambda fitted from the return kurtosis).
#
#  Rule (validated config, BUY-only): a fresh H4 close through QPL(+3) = an
#  "energy-level jump" -> BUY, SL 3 rungs back, TP 6 rungs on (= 2R), EMA50
#  filter, one trade at a time. Sells were removed: two-sided is break-even
#  over 21 years (sells PF 0.85), buy-only is not.
#
#  Backtest (research/quantum_backtest.py, M15 fills, $0.25 cost, 1%/trade):
#    2005-2026: 472 trades, WR 43%, W/L 1.8, PF 1.34, +4.2%/yr, maxDD 17%,
#               6 losing years of 21 (worst -6%), max 9 losses in a row
#    2015-2026: 251 trades, WR 41%, W/L 1.8, PF 1.28, +3.6%/yr, maxDD 11%
#  -> a modest, consistent edge (like each H4 trend strategy alone), NOT a
#  high win-rate machine. Full tables: reports/quantum_qpl_buyonly_*.txt
#
#  Runs alongside the H4 trend bot (own --ensemble-tag, --exposure own) or
#  alone. Same risk/throttle settings as run_xau_h4.ps1.
#
#  PRE-FLIGHT: MT5 (VT Markets) open + Algo Trading ON, XAUUSD-ECN in Market
#  Watch, Postgres up.   Stop everything:  .\stop_all.ps1
# =====================================================================
#  -Smc : run `quantum_qpl_smc` instead — the same entry but only when it
#         launches from a fresh SMC demand zone (order block) within 2 ATR.
#         2005-2026: 182 trades, PF 1.49, WR 44%, maxDD 9.4%, +2.3%/yr
#         (fewer, cleaner trades; less total return). BOS/CHoCH and
#         liquidity-sweep gates were tested and did not help -> not offered.
param([switch]$SkipPreflight, [double]$Risk = 0.01, [switch]$Smc)

$root = $PSScriptRoot
$py   = Join-Path $root 'env\Scripts\python.exe'
$sym  = 'XAUUSD-ECN'

if (-not $SkipPreflight) {
    Write-Host "Running pre-flight checks..." -ForegroundColor Cyan
    $env:PYTHONUTF8 = '1'
    & $py (Join-Path $root 'tools\preflight.py') $sym
    Write-Host ""
    $ans = Read-Host "Pre-flight OK? Launch the quantum QPL bot? (y/N)"
    if ($ans -ne 'y') { Write-Host "Aborted." -ForegroundColor Yellow; return }
}

function Start-Bot([string]$Title, [string]$Invoke) {
    $inner = "`$Host.UI.RawUI.WindowTitle='$Title'; `$env:PYTHONUTF8='1'; " +
             "Set-Location '$root'; $Invoke"
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoExit', '-Command', $inner) | Out-Null
    Write-Host ("  launched: {0}" -f $Title) -ForegroundColor Green
}

# identical to research/quantum_backtest.py: solo, closed H4 bars, one trade
# at a time, plain full exit at 2R (tp1 = tp2 = 2.0 -> no separate partial)
$common = "--trade --symbols $sym --tf 'H4:1.0' " +
          "--min-agree 1 --min-families 1 --min-agreement 0 --min-conviction 0 " +
          "--signal-floor 0 --tp1-r 2.0 --tp2-r 2.0 --be-trigger 0 --risk-pct $Risk " +
          "--dd-levels '0.20:0.5,0.35:0.25' --exposure own --no-shadow"

$sid = 'quantum_qpl'; $tag = 'q_qpl'
if ($Smc) { $sid = 'quantum_qpl_smc'; $tag = 'q_qpls' }
Write-Host "Launching $sid bot on $sym (risk $Risk/trade)..." -ForegroundColor Cyan
Start-Bot "Quantum QPL ($sid)" "& '$py' -u '$root\main_loop.py' $common --strategies $sid --ensemble-tag $tag"
# (quantum_qpl_bounce exists for research only: PF ~1.0 in every era, not launched)
Start-Sleep -Seconds 4
Start-Bot 'watchdog'             "& '$py' -u '$root\watchdog.py'"
Start-Bot 'dashboard'            "& '$py' '$root\dashboard.py'"
Start-Bot 'news_service'         "& '$py' -u '$root\news_service.py'"

Write-Host ""
Write-Host "Launched. Verify:" -ForegroundColor Cyan
Write-Host "  * the loop prints roster=['$sid'] and 'closed bars only; exposure cap = own'"
Write-Host "  * dashboard -> http://127.0.0.1:8000 shows the $tag heartbeat / LIVE"
Write-Host "  * H4 bars close at 00/04/08/12/16/20 server time (UTC+3): expect activity only then"
Write-Host "Stop everything later with:  .\stop_all.ps1" -ForegroundColor Yellow
