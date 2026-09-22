---
name: ftmo-switch-account
description: >-
  Switch the FTMO trading bots to a NEW MT5 account after the user edits .env.
  Use whenever the user says they changed/swapped the MT5 account, updated the
  login/password in .env, blew or reset an account, or got a new FTMO
  challenge/trial. Runs the full safe cutover: stop bots, verify the new
  connection, re-seed the DB profile, wipe old-account state, reset the
  kill-switch, confirm Algo Trading, relaunch.
---

# Switch FTMO account (cutover runbook)

The user has already edited `D:\ClaudeTrading_FTMO\.env` (MT5_LOGIN / MT5_PASSWORD,
sometimes MT5_SERVER). Drive the rest. **Order matters** — do not skip the state
reset or the new account inherits the old account's daily floor and a possibly
tripped kill-switch.

## Paths (this project)
- Root: `D:\ClaudeTrading_FTMO`
- venv python: `D:\ClaudeTrading_FTMO\env\Scripts\python.exe`
- Secrets: `D:\ClaudeTrading_FTMO\.env`
- Smoke test: `tools\mt5_check.py`
- DB health: `tools\db_state.py`
- State reset: `tools\reset_account_state.py`
- DB re-seed: `db_setup.py`
- Stop / launch: `stop_all.ps1` / `run_xau_all.ps1` (+ `run_xau_vote.ps1`)

Run python with `$env:PYTHONUTF8="1"` and the venv interpreter, e.g.
`$env:PYTHONUTF8="1"; & "D:\ClaudeTrading_FTMO\env\Scripts\python.exe" "D:\ClaudeTrading_FTMO\tools\db_state.py"`

## Steps

1. **Confirm .env was saved** — read `.env`, note the new `MT5_LOGIN`. Profile
   fields (`ACCOUNT_VARIANT`/`ACCOUNT_PATH`/`ACCOUNT_PHASE`/`ACCOUNT_INITIAL_CAPITAL`)
   only change if the new account is a different product/size.

2. **Stop the bots** — `.\stop_all.ps1`. Confirm no `main_loop.py` python
   processes remain (`Get-CimInstance Win32_Process -Filter "Name='python.exe'"`).
   The reset in step 5 MUST run with bots stopped, or you delete a `positions`
   row the broker still holds → desync.

3. **Verify the new account** — `tools\mt5_check.py`. Check: `login` matches .env,
   `name` matches the expected product, `balance`/`equity` fresh, **`trade_allowed`**
   (Algo). If the printed `name` (e.g. "$100k FTMO Free Trial 2-Step") implies a
   variant/path/phase different from .env, fix `.env` before continuing.

4. **Re-seed the DB profile** — `db_setup.py` (idempotent). Confirms
   `account_profile seeded: login=<new>`.

5. **Reset account-tied state** — preview then execute:
   - `tools\reset_account_state.py` (dry-run — shows what it will delete)
   - `tools\reset_account_state.py --yes` (default KEEPS strategy_league + shadow)
   - add `--wipe-learning` only if the user wants a full clean slate.
   This deletes `positions`/`orders`/`day_baseline` and resets `kill_switch` to
   `running`. Wiping `day_baseline` is mandatory: today's row holds the OLD
   account's midnight balance (`source='live'` is never overwritten by the loop).

6. **Confirm Algo Trading is ON** — if step 3 showed `trade_allowed: False`, tell
   the user to click the green **Algo Trading** button in the MT5 terminal
   (Tools → Options → Expert Advisors → Allow algorithmic trading). The bot can
   read data but cannot send orders until this is on.

7. **Relaunch** — invoke the `ftmo-start` skill (or tell the user to run
   `.\run_xau_all.ps1`; add `.\run_xau_vote.ps1` for the dual loop). Verify with
   `tools\db_state.py`: profile=new login, kill_switch=running, baseline
   re-anchored to the new balance.

## Verify success
`tools\db_state.py` shows the new `login`, `kill_switch mode=running`, and a fresh
`day_baseline` at the new account's balance. Update the
`account-and-infra-config` memory with the new login if it changed.
