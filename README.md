# FTMO Systematic Trading System

> ## Quick start for testers (VT Markets demo, H4 Trend bot) — อ่านตรงนี้ก่อน
>
> สิ่งที่ต้องมี (Windows): **Python 3.11+**, **Docker Desktop** (สำหรับ Postgres),
> **MT5 ของ VT Markets** ติดตั้ง + ล็อกอินบัญชี demo + กดปุ่ม **Algo Trading** ให้เขียว,
> และเปิด `XAUUSD-ECN` ใน Market Watch.
>
> ```powershell
> git clone https://github.com/thanakktk/botthongkrum.git ; cd botthongkrum
> python -m venv env ; .\env\Scripts\python.exe -m pip install -r requirements.txt
> docker run -d --name local_pgdb -e POSTGRES_PASSWORD=postgres -p 5432:5432 postgres:17
> copy .env.example .env      # แล้วแก้ MT5_LOGIN / MT5_PASSWORD / MT5_TERMINAL_PATH / ACCOUNT_INITIAL_CAPITAL
> $env:PYTHONUTF8 = "1"
> .\env\Scripts\python.exe db_setup.py            # สร้าง DB + ตาราง (รันซ้ำได้)
> .\env\Scripts\python.exe tools\mt5_check.py     # ต้องขึ้น login ถูก + trade_allowed: True
> .\run_xau_h4.ps1                                 # เปิดบอท (3 loop H4 + watchdog + dashboard)
> .\stop_all.ps1                                   # หยุดทุกอย่าง
> ```
>
> Dashboard: http://127.0.0.1:8000 · ฝึกเทรดมือบนกราฟย้อนหลัง: `python replay.py` → http://127.0.0.1:8050
>
> **บอทที่ใช้จริงคือ `run_xau_h4.ps1`** — 3 กลยุทธ์ trend/breakout เทรดเดี่ยวบน H4 (`breakout_sr`,
> `donchian_breakout`, `roc_momentum`), risk 1%/ไม้ + drawdown throttle. จากข้อมูลทอง 22 ปี:
> เฉลี่ย ~+2%/เดือน, CAGR ~22%, **max drawdown ในอดีต 38%**, มีปีขาดทุน (−9…−36%) และช่วง
> ขาดทุนติดกัน 6–9 เดือน — ดู `reports/sizing_sim.txt`, `reports/strategy_lab.txt`.
> `run_xau_robust.ps1` เป็นบอทรุ่นเก่า (confluence, edge ≈ 0) และ `run_xau_vote.ps1` ถูกปิดไว้
> (ขาดทุนทุกช่วงตลาด). **นี่คือ demo เท่านั้น — ยังไม่ผ่านการรันจริงนานพอ อย่าใช้เงินจริง.**
>
> **รัน 24 ชม. บน Windows VPS (ไม่ต้องมีความรู้):** เช่า VPS Windows Server (2 vCPU / 8 GB, เช่น Contabo
> "Cloud VPS" + Windows, ~$10–20/เดือน) → Remote Desktop เข้าไป (Win+R → `mstsc` → IP, user
> Administrator) → เปิด PowerShell **แบบ Run as administrator** แล้ววาง:
>
> ```powershell
> Set-ExecutionPolicy Bypass -Scope Process -Force
> irm https://raw.githubusercontent.com/thanakktk/botthongkrum/main/vps_setup.ps1 | iex
> ```
>
> สคริปต์ติดตั้ง Python, PostgreSQL (ไม่ใช้ Docker), โปรเจกต์ และตั้งค่าเครื่องให้เอง เหลือ 2 อย่างที่ต้องทำเอง:
> ติดตั้ง MT5 ของ VT Markets + ล็อกอิน + กด Algo Trading, และใส่ login/password ใน `C:\bot\.env`
> แล้วรัน `C:\bot\vps_finish.ps1` → บอทจะเริ่มเองทุกครั้งที่เครื่องเปิด และ relaunch เองถ้าตาย
> (`autostart_h4.ps1`). ออกจาก Remote Desktop ด้วยการ**ปิดหน้าต่าง** (disconnect) ห้าม Sign out.
>
> Backtest ซ้ำ: `python research\backtest_history.py 2015 2026 --preset live` (รายปี),
> `python research\monthly_report.py` (รายเดือน), `python research\strategy_lab.py` (59 แบบ, ~5 นาที),
> `python research\sizing_sim.py` (ขนาด position / Monte Carlo). ทั้งหมดใช้ `backtest\XAU_15m_data.csv`
> ที่อยู่ใน repo แล้ว ไม่ต้องต่อ MT5.

A fault-tolerant systematic trading bot for **FTMO** prop accounts on **MetaTrader 5**
(now also running rule-free on a plain broker demo via `RULES_MODE=none`).
Hard priority order: **FTMO rule compliance → capital preservation → operational
reliability → signal generation.** A profitable strategy is worthless if a rule
breach terminates the account.

> ⚠️ The FTMO numeric rule values in the code (loss %, reset time, consistency cap,
> news/weekend windows) are design parameters. **Verify each against FTMO's current
> official ruleset before risking a funded account** — they change periodically.

## Architecture (data flow)

```
                 ┌──────────────── main_loop.py ────────────────┐
 MT5 terminal →  │ reconcile → strategies → arbitration → sizing │ → orders
 (broker truth)  │      → COMPLIANCE veto → execution / flatten   │
                 └───────────────┬───────────────────────────────┘
                                 │  Postgres (state + audit + kill_switch)
                 ┌───────────────┴───────────────┐      ┌────────────────┐
                 │ watchdog.py (separate process) │      │ dashboard.py   │
                 │ trips kill-switch if loop hangs │      │ read-only view │
                 │ or equity breaches a floor      │      └────────────────┘
                 └─────────────────────────────────┘
```

## Project structure

The **live runtime** stays flat in the root (cohesive system, simple imports). The
research, tooling, generated reports and docs are separated out. Research/tools
scripts add the project root to `sys.path` so they still run from their subfolder.

```
ClaudeTrading_FTMO/
├── (root)            LIVE RUNTIME — the bots + everything they import
│   main_loop.py  watchdog.py  dashboard.py            ← processes you run
│   signal_bot.py                                      ← advisory Discord bot
│   strategies.py  arbitration.py  league.py  scorecards.py  signals.py
│   execution.py  ftmo_compliance_engine.py  state_reconciliation.py
│   mt5_broker.py  paper_broker.py  regime.py  sessions.py  shadow.py
│   db.py  db_setup.py  pg_state_store.py  schema.sql  notifier.py
│   news_feed.py  news_service.py  run_xau_robust.ps1  requirements.txt
├── research/         backtests & the edge study (run standalone)
│   backtester.py  histdata.py  analyze_edge.py  validate_edge.py
│   compare_pruning.py  backtest_history.py  test_history_variants.py
│   test_filters.py  test_breakeven.py  shadow_backfill.py  tune.py  optimize.py
├── tools/            one-off utilities (mt5_check.py, live_order_test.py)
├── backtest/         historical price data (XAUUSD 2004-2026 M15 + per-year M1)
├── reports/          generated analysis outputs (*.txt)
└── docs/             design notes / prompts
```

## Components / Pillars

| File | Role |
|------|------|
| `ftmo_compliance_engine.py` | **Pillar 0** — VETO over every order: daily/overall floors with soft/hard buffers, CET day boundary, 1-step trailing vs 2-step static, consistency rule, weekend/news time gates. |
| `signals.py` `strategies.py` `arbitration.py` | **Pillar 1** — Strict Signal Contract, **13 strategies** (momentum / volatility / S-R / SMC-ICT), weighted ensemble + same-symbol netting + 1%-risk sizing. |
| `regime.py` `league.py` | **Pillar 4** — regime detection (TREND/RANGE) + per-strategy×regime League (bench → probation → promote). |
| `state_reconciliation.py` `pg_state_store.py` `schema.sql` | **Pillar 5** — broker-as-truth restart reconciliation, idempotency, Postgres persistence + audit. |
| `watchdog.py` | **Pillar 5** — separate kill-switch process (heartbeat-silence + independent floor check). |
| `mt5_broker.py` `paper_broker.py` | Broker adapters (live MT5 + in-memory sim) behind one interface. |
| `execution.py` | Execution Engine — idempotent submission + flatten + **trade management** (partial + break-even + trail; `--tp1-r`/`--be-trigger`). |
| `shadow.py` | **Shadow mode** — the benched (non-roster) strategies paper-trade in the background so a regime shift that revives one is visible (promotion stays manual). |
| `main_loop.py` | The control loop that ties it together. |
| `dashboard.py` | Web monitor (FastAPI): floors, positions, **trade history**, League, strategy playbook (4 ★active / 9 👻shadow), **upcoming news**, audit + kill-switch controls. |
| `signal_bot.py` | **Advisory bot** — separate Discord channel: session outlooks + signals (entry/SL/TP1·2·3) + a daily scoreboard with the REAL hit rate. Alerts only, never trades. |
| `notifier.py` | Discord alerts — order opened (with the WHY), order closed (with P&L). |
| `news_feed.py` `news_service.py` | ForexFactory calendar → Postgres; Discord alerts for high-impact USD/XAUUSD events. |
| `research/` | **Pillar 6 + edge study** — `backtester.py` (event-driven, MTF, trade-mgmt sim, MAE/MFE), `analyze_edge`/`validate_edge` (per-strategy IS/OOS), `backtest_history`+`histdata` (22-yr gold), `test_history_variants` (config sweep), `optimize`/`tune`. **Finding: only 4 trend/breakout strategies survive OOS; edge is thin & risk-managed — see `memory`.** |

## Setup (Windows)

1. **MT5 terminal** installed and logged in to the FTMO account. Enable
   **Algo Trading** (toolbar button / `Ctrl+E`) before live order execution.
2. **PostgreSQL** running (this project uses a Docker `postgres` container).
3. Create the venv and install deps:
   ```bash
   python -m venv env
   ./env/Scripts/python.exe -m pip install -r requirements.txt
   ```
4. Copy `.env.example` → `.env` and fill in MT5 credentials + Postgres DSN.
   `.env` is gitignored — never commit it.
5. Bootstrap the database (idempotent — safe to re-run):
   ```bash
   ./env/Scripts/python.exe db_setup.py
   ```

## Run

```bash
# 1) Verify the MT5 connection (read-only)
./env/Scripts/python.exe tools/mt5_check.py

# 2) Monitoring only (no trading)
./env/Scripts/python.exe main_loop.py

# 3) Full trading loop  (Algo Trading must be ON)
#    The OOS-validated config (robust roster + management) — see research/ findings:
./env/Scripts/python.exe main_loop.py --trade --symbols XAUUSD --strategies robust \
    --min-agree 2 --min-families 2 --tp1-r 2.0          # or just: ./run_xau_robust.ps1

# 4) Watchdog — ALWAYS run alongside the loop, in its own window
./env/Scripts/python.exe watchdog.py
./env/Scripts/python.exe watchdog.py --reset      # clear a tripped kill-switch

# 5) Dashboard — http://127.0.0.1:8000
./env/Scripts/python.exe dashboard.py

# 6) Advisory signal bot (separate Discord channel — alerts only, no trades)
./env/Scripts/python.exe signal_bot.py --symbol XAUUSD
#   needs ADVISORY_WEBHOOK in .env

# 7) News service (economic calendar + Discord news alerts)
./env/Scripts/python.exe news_service.py
#   needs DISCORD_TRADE_WEBHOOK / DISCORD_NEWS_WEBHOOK in .env

# 8) Edge research / backtests (run from the project root)
./env/Scripts/python.exe research/analyze_edge.py XAUUSD       # per-strategy edge
./env/Scripts/python.exe research/validate_edge.py XAUUSD      # IS/OOS robustness
./env/Scripts/python.exe research/backtest_history.py 2015 2026  # 22-yr per-year
./env/Scripts/python.exe research/test_history_variants.py     # config sweep
```

Most modules have a built-in self-test: `./env/Scripts/python.exe <module>.py`
(for research/tools, prefix the path, e.g. `research/backtester.py`).

> On a Thai/legacy Windows console, run with `PYTHONUTF8=1` to avoid encoding errors.

## Safety notes

- **Kill switch** (`kill_switch` table) is the shared signal: the watchdog trips it,
  the main loop obeys it. Modes: `running → halt_new → close_all_halt`. It never
  auto-clears — reset manually once you've checked the account.
- **Standard accounts** are flattened over the weekend and won't open new trades
  from Fri 20:00 CET through the Sunday reopen. **Swing** accounts are exempt.
- The MT5 order path is verified live (a 0.01 BTCUSD open/close round-trip). Sizing
  is fixed-fractional (1% of equity) and gated so worst-case simultaneous stops
  cannot pierce the daily-loss buffer.
