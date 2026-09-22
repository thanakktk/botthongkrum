# =====================================================================
#  Stop ALL FTMO bot processes (keeps MT5 terminal + Postgres running)
# =====================================================================
#  Kills the python processes for: main_loop, watchdog, dashboard,
#  signal_bot, news_service. Does NOT touch terminal64.exe or docker.
#
#  Run:  .\stop_all.ps1
# =====================================================================
$scripts = 'main_loop.py', 'watchdog.py', 'dashboard.py', 'signal_bot.py', 'news_service.py'
$pattern = ($scripts | ForEach-Object { [regex]::Escape($_) }) -join '|'

$procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) }

if (-not $procs) {
    Write-Host "No bot processes running." -ForegroundColor Yellow
    return
}

foreach ($p in $procs) {
    $short = ($p.CommandLine -replace '.*\\python\.exe\s+', '')
    Write-Host ("Stopping PID {0,-6} {1}" -f $p.ProcessId, $short) -ForegroundColor Cyan
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500
$left = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match $pattern) }
if ($left) {
    Write-Host "Still alive (retry):" -ForegroundColor Red
    $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
} else {
    Write-Host "All bot processes stopped. (MT5 terminal + Postgres untouched.)" -ForegroundColor Green
}
