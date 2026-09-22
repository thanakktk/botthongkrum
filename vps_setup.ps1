# =====================================================================
#  One-shot Windows VPS setup for the H4 gold bot (run as Administrator)
# =====================================================================
#  On a fresh Windows Server 2019/2022/2025 (or Windows 10/11) VPS, open
#  PowerShell AS ADMINISTRATOR and paste:
#
#    Set-ExecutionPolicy Bypass -Scope Process -Force
#    irm https://raw.githubusercontent.com/thanakktk/botthongkrum/main/vps_setup.ps1 | iex
#
#  It installs: Python 3.12, PostgreSQL 17 (Windows service, no Docker),
#  the project (from GitHub, no git needed), the venv + requirements,
#  creates the database, writes .env with VT Markets defaults, disables
#  sleep, and opens the two things only YOU can do:
#     (1) install + log in the VT Markets MT5 terminal, Algo Trading ON
#     (2) put your MT5 login/password in .env
#  Then run  C:\bot\vps_finish.ps1  to verify and register the 24/7 task.
#
#  Re-runnable: every step skips what is already done.
# =====================================================================
$ErrorActionPreference = 'Stop'
$Dest    = 'C:\bot'
$RepoZip = 'https://github.com/thanakktk/botthongkrum/archive/refs/heads/main.zip'
$PyUrl   = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
$PgUrl   = 'https://get.enterprisedb.com/postgresql/postgresql-17.5-1-windows-x64.exe'
$PgPass  = 'postgres'

function Step([string]$m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Have([string]$exe) { return [bool](Get-Command $exe -ErrorAction SilentlyContinue) }
function Download([string]$url, [string]$to) {
    if (-not (Test-Path $to)) {
        Write-Host "   downloading $url"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $to -UseBasicParsing
    }
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in a PowerShell window opened AS ADMINISTRATOR."
}
New-Item -ItemType Directory -Force $Dest, "$Dest\_installers" | Out-Null

# ---------------------------------------------------------------- Python
Step "Python 3.12"
$py = Get-ChildItem "C:\Program Files\Python312\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $py) {
    $inst = "$Dest\_installers\python.exe"
    Download $PyUrl $inst
    Start-Process $inst -ArgumentList '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' -Wait
    $py = Get-Item "C:\Program Files\Python312\python.exe"
}
Write-Host "   $($py.FullName)"

# ---------------------------------------------------------------- PostgreSQL
Step "PostgreSQL 17 (Windows service)"
$pgSvc = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pgSvc) {
    $inst = "$Dest\_installers\postgresql.exe"
    Download $PgUrl $inst
    Start-Process $inst -ArgumentList "--mode unattended --unattendedmodeui none --superpassword $PgPass --serverport 5432 --disable-components stackbuilder" -Wait
    $pgSvc = Get-Service -Name 'postgresql*' | Select-Object -First 1
}
if ($pgSvc.Status -ne 'Running') { Start-Service $pgSvc.Name }
Set-Service $pgSvc.Name -StartupType Automatic
Write-Host "   service $($pgSvc.Name): $((Get-Service $pgSvc.Name).Status)"

# ---------------------------------------------------------------- project
Step "Project files -> $Dest"
if (-not (Test-Path "$Dest\main_loop.py")) {
    $zip = "$Dest\_installers\repo.zip"
    Remove-Item $zip -ErrorAction SilentlyContinue
    Download $RepoZip $zip
    Expand-Archive $zip -DestinationPath "$Dest\_installers\repo" -Force
    $src = Get-ChildItem "$Dest\_installers\repo" -Directory | Select-Object -First 1
    Copy-Item "$($src.FullName)\*" $Dest -Recurse -Force
}
Write-Host "   ok"

# ---------------------------------------------------------------- venv
Step "Python venv + requirements"
if (-not (Test-Path "$Dest\env\Scripts\python.exe")) { & $py.FullName -m venv "$Dest\env" }
& "$Dest\env\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Dest\env\Scripts\python.exe" -m pip install --quiet -r "$Dest\requirements.txt"
Write-Host "   ok"

# ---------------------------------------------------------------- .env
Step ".env"
if (-not (Test-Path "$Dest\.env")) {
    $envText = Get-Content "$Dest\.env.example" -Raw
    $envText = $envText -replace '(?m)^MT5_TERMINAL_PATH=.*$', 'MT5_TERMINAL_PATH=C:\Program Files\VT Markets (Pty) MT5 Terminal\terminal64.exe'
    $envText = $envText -replace '(?m)^PGPASSWORD=.*$', "PGPASSWORD=$PgPass"
    $envText = $envText -replace '(?m)^DATABASE_URL=.*$', "DATABASE_URL=postgresql://postgres:$PgPass@localhost:5432/FTMO"
    Set-Content "$Dest\.env" $envText -Encoding utf8
}
Write-Host "   $Dest\.env (fill in MT5_LOGIN / MT5_PASSWORD / MT5_SERVER)"

# ---------------------------------------------------------------- machine settings
Step "Never sleep / keep RDP sessions alive"
powercfg -change standby-timeout-ac 0 | Out-Null
powercfg -change monitor-timeout-ac 0 | Out-Null
powercfg -change hibernate-timeout-ac 0 | Out-Null
Write-Host "   ok"

# ---------------------------------------------------------------- finish script
$finish = @'
# Run AFTER MT5 is installed+logged in (Algo Trading ON) and .env is filled.
$env:PYTHONUTF8 = '1'
Set-Location C:\bot
& .\env\Scripts\python.exe db_setup.py
& .\env\Scripts\python.exe tools\mt5_check.py
Write-Host ""
$ans = Read-Host "Did mt5_check show your login and 'trade_allowed (algo): True'? Register the 24/7 task now? (y/N)"
if ($ans -eq 'y') {
    & .\autostart_h4.ps1 -Register
    Start-ScheduledTask -TaskName 'FTMO-H4-bot'
    Write-Host "Started. Dashboard: http://127.0.0.1:8000  Log: C:\bot\logs\autostart.log" -ForegroundColor Green
    Write-Host "When you leave: just CLOSE the Remote Desktop window (disconnect). Do NOT 'Sign out'." -ForegroundColor Yellow
}
'@
Set-Content "$Dest\vps_finish.ps1" $finish -Encoding utf8

Step "DONE with the automatic part. Two manual steps remain:"
Write-Host @"
   1) Install the VT Markets MT5 terminal (download from your VT client portal,
      or https://www.vtmarkets.com -> Platforms -> MetaTrader 5 -> Windows).
      Log in to your account, then Tools -> Options -> Expert Advisors ->
      tick 'Allow algorithmic trading' and press the green 'Algo Trading' button.
      In Market Watch, right-click -> Show All (so XAUUSD-ECN is visible).
   2) Fill MT5_LOGIN / MT5_PASSWORD / MT5_SERVER in C:\bot\.env  (Notepad opens now).
   Then run:   powershell -ExecutionPolicy Bypass -File C:\bot\vps_finish.ps1
"@ -ForegroundColor Yellow
Start-Process notepad.exe "$Dest\.env"
