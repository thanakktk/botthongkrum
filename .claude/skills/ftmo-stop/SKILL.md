---
name: ftmo-stop
description: >-
  Stop / kill / shut down the running FTMO bot processes (main_loop, watchdog,
  dashboard, signal_bot, news_service) while leaving the MT5 terminal and
  Postgres running. Use when the user wants to stop, halt, kill, or bring down
  the bots, or before an account switch / state reset.
---

# Stop the FTMO bots

## Paths
- Root: `D:\ClaudeTrading_FTMO`  ·  Stop script: `stop_all.ps1`

## Stop all
```powershell
.\stop_all.ps1
```
Kills the python processes for `main_loop.py`, `watchdog.py`, `dashboard.py`,
`signal_bot.py`, `news_service.py`. Does **NOT** touch `terminal64.exe` (MT5) or
the Postgres docker container. `stop_all.ps1` stops BOTH trading loops if the dual
robust+vote setup is running (its regex matches every `main_loop.py`).

## Verify stopped
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Select-Object ProcessId, CommandLine | Format-List
```
Should return nothing for the bot scripts.

## Stop only the "vote" loop (keep robust running)
`stop_all.ps1` stops everything. To kill JUST the vote loop, close its window
(title "FTMO vote") or kill only the `main_loop.py ... --ensemble-tag vote` PIDs:
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'main_loop\.py' -and $_.CommandLine -match 'ensemble-tag vote' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## Note
Stopping the bots does NOT close open positions at the broker — the MT5 server
still holds them and their SL/TP. To flatten, use the kill-switch / watchdog flow,
not a process kill.
