# =====================================================================
#  24/7 runner for the H4 trend bot — start / keep-alive / register
# =====================================================================
#  Everything the bot needs, brought up in order and re-checked forever:
#    1. Docker Desktop + the Postgres container (local_pgdb)
#    2. the VT Markets MT5 terminal (logged in, Algo Trading ON — the
#       terminal remembers both across restarts once you set them)
#    3. the 3 H4 loops + watchdog + dashboard + news (run_xau_h4.ps1)
#  Then loops every 5 min: if a main_loop.py / watchdog.py process is
#  missing, it relaunches the stack. Logs to logs\autostart.log.
#
#  One-time registration (Task Scheduler, runs at logon, restarts on failure):
#      .\autostart_h4.ps1 -Register
#  Remove:  .\autostart_h4.ps1 -Unregister
#  Manual:  .\autostart_h4.ps1          (foreground, Ctrl+C to stop the guard;
#                                        bots keep running -> .\stop_all.ps1)
#
#  ALSO DO ON THE MACHINE (once): Settings -> Power -> never sleep;
#  Windows Update -> pause / active hours; MT5 Tools -> Options -> Expert
#  Advisors -> "Allow algorithmic trading" (persists); Docker Desktop ->
#  "Start Docker Desktop when you sign in"; auto-login after reboot
#  (netplwiz) so the logon task fires.
# =====================================================================
param([switch]$Register, [switch]$Unregister)

$root = $PSScriptRoot
$py   = Join-Path $root 'env\Scripts\python.exe'
$log  = Join-Path $root 'logs\autostart.log'
$mt5  = 'C:\Program Files\VT Markets (Pty) MT5 Terminal\terminal64.exe'
$task = 'FTMO-H4-bot'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

function Log([string]$m) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $m
    Write-Host $line; Add-Content -Path $log -Value $line
}

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Log "task '$task' removed"; return
}
if ($Register) {
    $action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Minimized -File `"$root\autostart_h4.ps1`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 2) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Log "task '$task' registered (at logon, restart on failure). Start now with: Start-ScheduledTask $task"
    return
}

function Bots-Running {
    $p = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'main_loop\.py|watchdog\.py' }
    $loops = @($p | Where-Object { $_.CommandLine -match 'main_loop\.py' }).Count
    $wd    = @($p | Where-Object { $_.CommandLine -match 'watchdog\.py' }).Count
    return ($loops -ge 3 -and $wd -ge 1)
}

function Ensure-Postgres {
    # Either a native Windows service (VPS install via vps_setup.ps1) or the
    # docker container used on the dev PC.
    $svc = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue | Select-Object -First 1
    for ($i = 0; $i -lt 30; $i++) {
        if ($svc) {
            if ((Get-Service $svc.Name).Status -eq 'Running') { return $true }
            Log "postgres service stopped -> starting $($svc.Name)"
            try { Start-Service $svc.Name } catch {}
        } else {
            $ok = $false
            try { $ok = (docker inspect -f '{{.State.Running}}' local_pgdb 2>$null) -eq 'true' } catch {}
            if ($ok) { return $true }
            if ($i -eq 0) {
                Log "postgres not running -> starting Docker Desktop / container"
                $dd = "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
                if (Test-Path $dd) { Start-Process $dd | Out-Null }
            }
            try { docker start local_pgdb 2>$null | Out-Null } catch {}
        }
        Start-Sleep -Seconds 10
    }
    return $false
}

function Ensure-MT5 {
    if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
        Log "MT5 not running -> starting terminal"
        Start-Process $mt5 | Out-Null
        Start-Sleep -Seconds 40           # let it log in + sync history
    }
    $env:PYTHONUTF8 = '1'
    $out = & $py (Join-Path $root 'tools\mt5_check.py') 2>&1 | Out-String
    if ($out -match 'trade_allowed \(algo\): True') { return $true }
    Log "MT5 check failed or Algo Trading OFF:`n$out"
    return $false
}

Log "=== autostart guard up (pid $PID) ==="
while ($true) {
    if (-not (Bots-Running)) {
        Log "bots not (fully) running -> bringing the stack up"
        if ((Ensure-Postgres) -and (Ensure-MT5)) {
            & (Join-Path $root 'stop_all.ps1') | Out-Null     # clear half-dead leftovers
            Start-Sleep -Seconds 3
            & (Join-Path $root 'run_xau_h4.ps1') -SkipPreflight
            Log "run_xau_h4.ps1 launched"
            Start-Sleep -Seconds 60
        } else {
            Log "prerequisites missing; retry in 5 min"
        }
    }
    Start-Sleep -Seconds 300
}
