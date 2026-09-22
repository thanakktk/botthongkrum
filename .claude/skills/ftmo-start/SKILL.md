---
name: ftmo-start
description: >-
  Start / launch / restart the FTMO trading bot stack. Use when the user wants to
  start, launch, run, bring up, or restart the bots (main_loop robust + watchdog +
  dashboard + signal_bot + news_service), or add the dual "vote" loop. Handles the
  pre-flight (Algo on, Postgres up, no stale processes) first.
---

# Start the FTMO bot stack

## Paths
- Root: `D:\ClaudeTrading_FTMO`  ·  venv: `D:\ClaudeTrading_FTMO\env\Scripts\python.exe`
- Launchers: `run_xau_all.ps1` (robust stack), `run_xau_vote.ps1` (adds the vote loop)
- Single launcher: `run_xau_robust.ps1`  ·  Pre-flight: `tools\preflight.py`  ·  Stop: `stop_all.ps1`

## Pre-flight (do before launching)
1. **No stale bots** — confirm nothing is already running, else you double-launch:
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select ProcessId, CommandLine`.
   If bots are up and the user wants a restart, run `.\stop_all.ps1` first.
2. **Algo Trading ON** + **Postgres up** + XAUUSD in Market Watch. Quick check:
   `$env:PYTHONUTF8="1"; & "D:\ClaudeTrading_FTMO\env\Scripts\python.exe" "D:\ClaudeTrading_FTMO\tools\preflight.py"`
   (or `tools\mt5_check.py` for `trade_allowed`). If Algo is OFF, the user must
   click the green Algo Trading button in MT5 first.

## Launch
The launchers open each bot in its OWN PowerShell window via `Start-Process` —
**best run by the user in their own terminal** (cd `D:\ClaudeTrading_FTMO`):

```powershell
.\run_xau_all.ps1            # robust stack; runs preflight + asks to confirm
.\run_xau_vote.ps1           # OPTIONAL: adds the 1% "vote" loop on top
```

`run_xau_all.ps1` prompts (`Read-Host`) — if YOU launch it from a non-interactive
tool call, use `.\run_xau_all.ps1 -SkipPreflight` (after doing the pre-flight
above yourself) so it doesn't hang.

## Verify after launch
- main_loop window shows `Startup reconcile: ok=True` then ticking heartbeats.
- watchdog shows `mode=running`, small `hb_age`.
- dashboard at http://127.0.0.1:8000
- `tools\db_state.py` → kill_switch=running, baseline anchored to current balance.
- Each script appears as 2 python PIDs (venv launcher + interpreter — expected).
- Stop everything later with `.\stop_all.ps1` (see the `ftmo-stop` skill).
