---
name: ftmo-status
description: >-
  Check the health/status of the FTMO trading system. Use when the user asks
  whether the bots are running, what's the status, is it trading, why isn't it
  trading, or to sanity-check before/after a restart. Reports running bot PIDs,
  MT5 connection + Algo state, account profile, kill-switch mode, open positions,
  and the daily baseline.
---

# FTMO system status

## Paths
- Root: `D:\ClaudeTrading_FTMO`  ·  venv: `D:\ClaudeTrading_FTMO\env\Scripts\python.exe`
- DB health: `tools\db_state.py`  ·  MT5 connection: `tools\mt5_check.py`

## Checks

1. **Running processes** — which bots are up:
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Select-Object ProcessId, CreationDate, CommandLine | Format-List
   ```
   Expect (each script = 2 PIDs, venv launcher + interpreter — NOT a duplicate):
   `main_loop.py`, `watchdog.py`, `dashboard.py`, `signal_bot.py`, `news_service.py`.
   A second `main_loop.py` with `--ensemble-tag vote` = the dual "vote" loop is on.

2. **DB / account state** — `$env:PYTHONUTF8="1"; & "D:\ClaudeTrading_FTMO\env\Scripts\python.exe" "D:\ClaudeTrading_FTMO\tools\db_state.py"`
   Reports account_profile (login/variant/path/phase), kill_switch mode
   (anything other than `running` = trading is halted), open positions,
   non-terminal orders, recent day_baseline, and kept learning tables.

3. **MT5 + Algo** (optional, only if connection in doubt) — `tools\mt5_check.py`.
   `trade_allowed: False` = Algo Trading button is OFF → bot can't send orders.
   Avoid running this while bots are mid-trade unless needed; it's read-only but
   shares the terminal session.

## Common conclusions
- **Bots up + kill_switch=running + Algo on** → healthy, trading.
- **Signals but no orders** → usually the confluence gate in a ranging market,
  not a bug (logged as `arb` audit events), or the exposure cap (one position
  per symbol). See the `signals-no-orders-is-confluence-gate` memory.
- **kill_switch != running** → halted by the watchdog (e.g. daily-loss buffer);
  needs a manual reset before it trades again.
- **Algo False** → user must click the green Algo Trading button in MT5.
