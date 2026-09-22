# Systematic Trading System — Architecture & Implementation Prompt (v2)

> **Note for the builder:** All FTMO rule values below (loss %, reset time, consistency cap, news window) are design parameters captured as of mid-2026. **Verify each against FTMO's current official ruleset before coding** — these change periodically and a stale value can blow a funded account.

---

## Role
Act as a **Senior Systematic Trading System Architect** (mid-frequency systematic trading — **not HFT**; the loop runs on a ~30s cadence, so design for reliability and correctness, not microsecond latency).

## Objective
Design a robust, fault-tolerant **systematic trading system** that runs an ensemble of **13 strategies** (SMC/ICT, Momentum, Volatility, S/R) and executes on **MetaTrader 5** against **FTMO simulated prop accounts**.

**Hard priority order:** FTMO rule compliance → capital preservation → operational reliability → signal generation. A profitable strategy is worthless if the system breaches an FTMO rule and the account is terminated.

## Scope & Constraints
- **Single broker:** MT5 only (FTMO). No crypto, no second venue.
- **One account at a time** (no simultaneous multi-account / copy-trade — avoids FTMO's identical-trade and combined-allocation rules).
- **Two account variants supported via config:** FTMO Standard and FTMO Swing.
- **Three phases supported via config:** Challenge → Verification → Funded (rules differ by phase).

## Tech Stack Constraints
- **Architecture:** Dockerized microservices.
  - ⚠️ **MT5 hosting reality:** the MT5 Python API talks to a Windows-native terminal. It does not run natively in a Linux container — plan to host the MT5 terminal on a Windows VM/host (or Wine) and have the Dockerized services connect to it over the network. Decide this topology before writing `docker-compose.yml`.
- **Database & Logging:** PostgreSQL.
  - Use **stored procedures only for audit logging, aggregations, and reporting**. Keep all trading/decision logic in application code (testable, version-controlled, backtestable).
  - Prevent main-loop blocking via **async/queued DB writes**, not by pushing logic into the DB.
- **API:** MetaTrader 5 Python API, behind a **Broker Interface abstraction** so a **Paper/Simulation adapter** can implement the same interface for backtesting and dry-runs.

---

## Engineering Pillars

### Pillar 0 — FTMO Compliance Engine (HIGHEST priority, holds VETO over every order)
A dedicated microservice that sits between the Arbitration Layer and the Execution Engine. No order reaches the broker without passing it. Every veto is written to the audit log with a reason code.

- **Account Profile model:** `{ variant: Standard | Swing, path: 1-Step | 2-Step, phase: Challenge | Verification | Funded }`. All behavior below is derived from this profile.
- **Daily Loss Guard with safety buffer:**
  - Limits: ~5% daily for 2-Step, ~3% daily for 1-Step.
  - The bot must stop **before** the limit (configurable buffer, e.g. stop at 4% / 2.5%).
  - Computed on **start-of-day equity including open floating P&L** (do not forget floating positions).
- **Day boundary = FTMO server time (midnight CET, DST-aware → CEST).** Not local time. The "trading day" rollover/reset must match FTMO exactly.
- **Max Loss Guard — selectable by path:**
  - 2-Step: **static** ~10% from initial balance.
  - 1-Step: **trailing end-of-day** ~10%. (Compute the trailing high-water mark correctly — common cause of blown accounts.)
- **Consistency Rule (Standard variant):** no single day's profit may exceed ~50% of total profit. Throttle/limit on outsized days. (Swing variant is exempt.)
- **Phase-aware news restriction:** the news rule applies **only at the Funded stage**, not during Challenge/Verification.
- **Variant behavior:**
  - **Standard:** auto-flatten before daily session close and before the Friday weekend close; at Funded stage, be flat through high-impact news windows (~2 min before to ~2 min after NFP, FOMC, CPI, ECB, BOE, etc.).
  - **Swing:** may hold overnight/weekend and trade news without restriction.
- **Profit-target & minimum-trading-day tracking** for evaluation phases.

### Pillar 1 — Signal Arbitration Layer (conflict & exposure management)
- **Strict Signal Contract** every strategy must emit: `{ strategy_id, symbol, direction, confidence(0..1, normalized), entry, sl, tp, timeframe, regime_tag, timestamp, expiry }`. Arbitration is only as good as this contract — enforce it.
- **Weighted ensemble** to resolve conflicting signals (e.g. SMC=Buy vs Momentum=Sell). Weights may incorporate League standing and detected regime.
- **Exposure Cap & Correlation Filter:** prevent multiple strategies from stacking N separate same-direction positions on one asset. **v1 scope = same-symbol netting**; cross-asset correlation matrix is a later phase (avoid over-engineering v1).

### Pillar 2 — Decoupled Execution Loops
- Split the 13 strategies into **Time-based Polling** (~30s) and **Event-triggered** (London Breakout, News Fade, Opening Range) running on separate loops.
- **Debounce/Cooldown:** one signal per distinct setup; no duplicate entries on the same setup.
- **Idempotency:** every order carries a unique **client order ID** so retries after a network blip never double-submit.

### Pillar 3 — Advanced Risk Management & Sizing
- **Dynamic position sizing** (fixed fractional, e.g. 1% risk/trade) scaled to current equity.
- **Sizing must guarantee** that worst-case simultaneous SLs across all correlated open positions still cannot breach the daily-loss buffer in Pillar 0.
- **Margin/Equity monitor.**
- **Economic-calendar integration** (e.g. Forex Factory / calendar API) feeding the Pillar 0 news blackout; auto-enable based on variant + phase.

### Pillar 4 — Context-Aware League System
- Benchmark strategies **conditioned on Market Regime Detection (Trend vs Ranging)** — do not bench a strategy for low win-rate when it is simply in the wrong regime (e.g. EMA Cross in a range).
- Define a **safe re-entry path** for benched strategies (periodic probation/re-test rather than permanent removal).

### Pillar 5 — Operational Reliability (fail-safe)
- **Crash Recovery & State Reconciliation:** on restart, treat the **broker as the single source of truth**; sync local state to actual MT5 open positions/orders before resuming.
- **Heartbeat monitor** (alert on silence/disconnect, not just on trades).
- **Kill Switch as a SEPARATE watchdog process** that works even if the main loop hangs. Define two modes: `halt-new-only` vs `close-all-and-halt`.
- **Comprehensive Audit Logging** ("why" behind every execution and every rejection/veto), persisted via PostgreSQL stored procedures.

### Pillar 6 — Testing & Validation (required before any live capital)
- **Paper/Sim adapter** implementing the Broker Interface → run the full system without risking the evaluation fee.
- **Backtest with realistic spread, slippage, and commission** on the FTMO MT5 feed (not mid-price).
- **Walk-forward / out-of-sample** validation; guard against lookahead bias in the discretionary SMC/ICT strategies.

### Security
- FTMO/MT5 credentials and any API keys in a secrets manager, never in code/repo. IP-restrict where supported.

### Pre-build validation
- **Audit the 13 strategies against FTMO's Prohibited Trading Practices** (e.g. latency/tick-scalping abuse, gambling-style, grid/martingale concerns). A strategy that trips these gets the account flagged even when profitable.

---

## Deliverables (think step-by-step, prioritize capital preservation)
1. **Microservices in `docker-compose.yml`:** Data Feeder, Strategy Workers, Aggregator/Arbitration, **FTMO Compliance Engine**, Execution Engine, PostgreSQL, Watchdog/Kill-switch — plus the MT5-terminal connectivity note.
2. **Exact algorithmic flow of the Signal Arbitration Layer**, and how the Compliance Engine vetoes downstream.
3. **State Reconciliation schema + logic** on unexpected restart (broker-as-truth, idempotent re-sync).
4. **FTMO Compliance Engine logic in detail:** daily-loss buffer calculation, CET/CEST day-boundary handling, static-vs-trailing max-loss computation, consistency-rule enforcement.
5. **Phased delivery plan:** define a minimal v1 spine (one variant, Paper mode, 2–3 strategies, full Pillar 0 + Pillar 5 + audit) proven stable first, then scale strategies and add the second variant.
