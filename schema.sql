-- ============================================================================
-- Trading System — State, Idempotency, Daily Baselines & Audit (PostgreSQL)
-- Stored procedures are used ONLY for audit writes, the daily rollover, and
-- read-side aggregations. All trading DECISIONS live in application code.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Account profile (single active account; "one account at a time")
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_profile (
    id                SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    login             BIGINT      NOT NULL,           -- MT5 login
    variant           TEXT        NOT NULL CHECK (variant IN ('standard','swing')),
    path              TEXT        NOT NULL CHECK (path    IN ('1-step','2-step')),
    phase             TEXT        NOT NULL CHECK (phase   IN ('challenge','verification','funded')),
    initial_capital   NUMERIC(18,2) NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Per-CET-day baseline. midnight_balance = closed BALANCE at the 00:00 CET
-- that opened the day. This anchors the daily floor and the 1-step trailing
-- overall floor (highest midnight balance ever).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS day_baseline (
    cet_date          DATE PRIMARY KEY,
    midnight_balance  NUMERIC(18,2) NOT NULL,
    source            TEXT NOT NULL DEFAULT 'live'      -- 'live' | 'reconstructed'
                       CHECK (source IN ('live','reconstructed','manual')),
    captured_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Orders. client_order_id is the IDEMPOTENCY KEY: generated before submission,
-- written into the MT5 order comment/magic so a post-crash reconcile can match
-- a broker fill back to our intent and NEVER double-submit.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   UUID PRIMARY KEY,
    strategy_id       TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    side              TEXT        NOT NULL CHECK (side IN ('buy','sell')),
    volume            NUMERIC(12,2) NOT NULL,
    intended_sl       NUMERIC(18,5),
    intended_tp       NUMERIC(18,5),
    -- lifecycle: created -> submitted -> filled | rejected | unknown(reconcile)
    status            TEXT        NOT NULL DEFAULT 'created'
                       CHECK (status IN ('created','submitted','filled','rejected','unknown')),
    broker_ticket     BIGINT,                          -- set once filled
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at      TIMESTAMPTZ,
    resolved_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_ticket ON orders(broker_ticket);
-- Market regime detected at entry (Pillar 4): lets the League score a strategy
-- only in the regime it traded in. Added via ALTER so re-running is safe.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS regime TEXT;
-- Detailed confluence reasoning (why this order was taken) — kept as a study log.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS rationale TEXT;

-- ---------------------------------------------------------------------------
-- Local mirror of open positions. The BROKER is the source of truth; this is
-- a cache that reconciliation re-syncs on every restart.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    broker_ticket     BIGINT PRIMARY KEY,
    client_order_id   UUID REFERENCES orders(client_order_id),
    symbol            TEXT        NOT NULL,
    side              TEXT        NOT NULL CHECK (side IN ('buy','sell')),
    volume            NUMERIC(12,2) NOT NULL,
    open_price        NUMERIC(18,5) NOT NULL,
    sl                NUMERIC(18,5),
    tp                NUMERIC(18,5),
    status            TEXT        NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','closed')),
    opened_at         TIMESTAMPTZ,
    closed_at         TIMESTAMPTZ,
    close_pnl         NUMERIC(18,2)
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
-- why a position closed (sl | tp | flatten_all | time_flatten | killswitch |
-- broker_close | manual). Added via ALTER so re-running stays safe.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS close_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_positions_closed ON positions(closed_at);
-- Advanced trade management (TP1 partial + break-even + trailing). `mgmt`:
-- 'running' (pre-TP1) -> 'tp1_hit' (partial banked, SL at break-even, trailing).
ALTER TABLE positions ADD COLUMN IF NOT EXISTS tp1  NUMERIC(18,5);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS tp2  NUMERIC(18,5);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS mgmt TEXT NOT NULL DEFAULT 'running';
-- original stop, kept so the risk distance R survives the move to break-even.
ALTER TABLE positions ADD COLUMN IF NOT EXISTS init_sl NUMERIC(18,5);

-- ---------------------------------------------------------------------------
-- Append-only audit log: the "why" behind every execution, rejection, veto,
-- reconciliation action, and kill-switch event.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type  TEXT NOT NULL,          -- 'order','veto','reconcile','rollover','kill',...
    decision    TEXT,                   -- 'allow','veto','flatten_all',...
    reason_code TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_ts   ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);

-- ---------------------------------------------------------------------------
-- Kill switch: a single shared row that the SEPARATE watchdog process trips and
-- the main loop obeys. Survives restarts; never auto-clears (manual reset only).
--   running        -> normal trading
--   halt_new       -> stop opening new trades; keep managing open ones
--   close_all_halt -> flatten everything and stay halted
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kill_switch (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mode        TEXT NOT NULL DEFAULT 'running'
                 CHECK (mode IN ('running','halt_new','close_all_halt')),
    reason      TEXT,
    source      TEXT,                    -- 'watchdog' | 'manual' | ...
    tripped_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO kill_switch (id, mode) VALUES (1, 'running')
    ON CONFLICT (id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Strategy League (Pillar 4): per-strategy, per-regime performance + standing.
-- Conditioning on regime is the whole point — a strategy is judged only where
-- it is meant to work. Benched strategies get a probation window for re-entry.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_league (
    strategy_id      TEXT NOT NULL,
    regime           TEXT NOT NULL CHECK (regime IN ('trend','range','unknown')),
    trades           INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    gross_pnl        NUMERIC(18,2) NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','probation','benched')),
    benched_at       TIMESTAMPTZ,
    probation_until  TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, regime)
);

-- ---------------------------------------------------------------------------
-- Economic-calendar events (ForexFactory weekly JSON). `relevant` flags the
-- XAUUSD-driving ones (high-impact USD). `alerted` dedupes the Discord ping.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS news_events (
    id          BIGSERIAL PRIMARY KEY,
    event_time  TIMESTAMPTZ NOT NULL,
    currency    TEXT NOT NULL,
    title       TEXT NOT NULL,
    impact      TEXT,
    forecast    TEXT,
    previous    TEXT,
    actual      TEXT,
    relevant    BOOLEAN NOT NULL DEFAULT FALSE,
    alerted     BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_time, currency, title)
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_events(event_time);

-- ---------------------------------------------------------------------------
-- Recent OHLC bars for the dashboard's live candlestick chart. The bot upserts
-- the latest bars each tick; old rows are pruned. Read-only for the dashboard.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_bars (
    symbol    TEXT NOT NULL,
    bar_time  TIMESTAMPTZ NOT NULL,
    o NUMERIC(18,5), h NUMERIC(18,5), l NUMERIC(18,5), c NUMERIC(18,5),
    PRIMARY KEY (symbol, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_price_bars ON price_bars(symbol, bar_time);

-- ============================================================================
-- Stored procedures / functions
-- ============================================================================

-- Non-blocking audit write (call fire-and-forget from an async queue worker).
CREATE OR REPLACE PROCEDURE sp_write_audit(
    p_event_type TEXT, p_decision TEXT, p_reason TEXT, p_payload JSONB
) LANGUAGE sql AS $$
    INSERT INTO audit_log(event_type, decision, reason_code, payload)
    VALUES (p_event_type, p_decision, p_reason, COALESCE(p_payload,'{}'::jsonb));
$$;

-- Idempotent daily rollover: records the midnight balance for a CET day.
-- Safe to call repeatedly (e.g. after a restart) — won't overwrite a 'live'
-- capture with a 'reconstructed' one.
CREATE OR REPLACE FUNCTION sp_roll_daily_baseline(
    p_cet_date DATE, p_midnight_balance NUMERIC, p_source TEXT DEFAULT 'live'
) RETURNS NUMERIC LANGUAGE plpgsql AS $$
DECLARE v_balance NUMERIC;
BEGIN
    INSERT INTO day_baseline(cet_date, midnight_balance, source)
    VALUES (p_cet_date, p_midnight_balance, p_source)
    ON CONFLICT (cet_date) DO UPDATE
        SET midnight_balance = EXCLUDED.midnight_balance,
            source           = EXCLUDED.source,
            captured_at      = now()
        WHERE day_baseline.source <> 'live';   -- never clobber a live capture
    SELECT midnight_balance INTO v_balance FROM day_baseline WHERE cet_date = p_cet_date;
    RETURN v_balance;
END;
$$;

-- Read-side risk inputs the compliance engine needs after a restart:
-- the current day's anchor + the highest midnight balance ever (1-step trailing).
CREATE OR REPLACE FUNCTION sp_risk_anchors(p_cet_date DATE)
RETURNS TABLE(today_midnight_balance NUMERIC, highest_midnight_balance NUMERIC)
LANGUAGE sql AS $$
    SELECT
        (SELECT midnight_balance FROM day_baseline WHERE cet_date = p_cet_date),
        (SELECT COALESCE(MAX(midnight_balance), 0) FROM day_baseline
         WHERE cet_date <= p_cet_date);
$$;

-- ---------------------------------------------------------------------------
-- SHADOW MODE (Phase A): benched (non-roster) strategies paper-trade in the
-- background so a regime shift that revives one becomes VISIBLE. SIMULATION
-- ONLY — no real orders, no money. Promotion to the live roster stays manual.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_positions (
    id            SERIAL PRIMARY KEY,
    strategy_id   TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('buy','sell')),
    tf            TEXT,
    entry         DOUBLE PRECISION NOT NULL,
    sl            DOUBLE PRECISION NOT NULL,
    tp            DOUBLE PRECISION NOT NULL,
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, symbol)            -- one open paper-trade per technique/symbol
);
CREATE TABLE IF NOT EXISTS shadow_trades (
    id            BIGSERIAL PRIMARY KEY,
    strategy_id   TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    entry         DOUBLE PRECISION NOT NULL,
    exit          DOUBLE PRECISION NOT NULL,
    r_mult        DOUBLE PRECISION NOT NULL,
    reason        TEXT,
    opened_at     TIMESTAMPTZ,
    closed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_strat
    ON shadow_trades(strategy_id, closed_at DESC);

-- ---------------------------------------------------------------------------
-- ADVISORY SIGNAL BOT (separate Discord channel) — alerts only, NOT executed.
-- Posts session outlooks + condition signals (entry/SL/TP1-3) and tracks each
-- signal's outcome so the daily scoreboard shows the REAL hit rate.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advisory_signals (
    id          BIGSERIAL PRIMARY KEY,
    strategy    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    entry       DOUBLE PRECISION NOT NULL,
    sl          DOUBLE PRECISION NOT NULL,
    tp1         DOUBLE PRECISION NOT NULL,
    tp2         DOUBLE PRECISION NOT NULL,
    tp3         DOUBLE PRECISION NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open|tp1|tp2|tp3|sl|expired
    result_r    DOUBLE PRECISION,
    posted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_advisory_status ON advisory_signals(status);
-- de-dup one-per-day posts (session outlooks, scoreboards)
CREATE TABLE IF NOT EXISTS advisory_posts (
    post_key    TEXT PRIMARY KEY,
    posted_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
