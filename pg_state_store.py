"""
PostgreSQL-backed StateStore
======================================================================
Concrete StateStore (port defined in state_reconciliation.py) persisting to
the schema in schema.sql. This is the local mirror that reconciliation
re-syncs to broker truth on every restart, plus the append-only audit log.

Design rules honored:
  * The BROKER is source of truth; this table set is a cache, not the authority.
  * Trading DECISIONS never live in SQL. We only persist state + audit and call
    the read-side/rollover stored procedures (sp_write_audit, sp_roll_daily_baseline).
  * client_order_id (UUID) is the idempotency key.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Optional, Sequence

import psycopg
from psycopg.types.json import Jsonb

import db
from state_reconciliation import (
    StateStore, BrokerPosition, LocalOrder, LocalPosition,
)


class PgStateStore(StateStore):
    def __init__(self, conn: psycopg.Connection | None = None):
        # One autocommit connection: every upsert/audit lands immediately, which
        # matches the incremental, idempotent reconciliation model.
        self.conn = conn or db.connect(autocommit=True)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PgStateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----- StateStore: reads ---------------------------------------------- #
    def local_orders(self) -> Sequence[LocalOrder]:
        rows = self.conn.execute(
            "SELECT client_order_id, status, broker_ticket FROM orders"
        ).fetchall()
        return [LocalOrder(str(coid), status,
                           int(tk) if tk is not None else None)
                for coid, status, tk in rows]

    def local_open_positions(self) -> Sequence[LocalPosition]:
        rows = self.conn.execute(
            "SELECT broker_ticket, status FROM positions WHERE status = 'open'"
        ).fetchall()
        return [LocalPosition(int(tk), status) for tk, status in rows]

    def baseline_for(self, d: date) -> Optional[float]:
        row = self.conn.execute(
            "SELECT midnight_balance FROM day_baseline WHERE cet_date = %s", (d,)
        ).fetchone()
        return float(row[0]) if row else None

    @property
    def last_baseline(self) -> Optional[float]:
        """Most recent stored baseline — used by the reconciler's conservative
        fallback when a midnight can't be reconstructed."""
        row = self.conn.execute(
            "SELECT midnight_balance FROM day_baseline "
            "ORDER BY cet_date DESC LIMIT 1"
        ).fetchone()
        return float(row[0]) if row else None

    # ----- StateStore: writes --------------------------------------------- #
    def adopt_position(self, p: BrokerPosition) -> None:
        # Broker truth. Link the FK to the originating order: prefer the coid the
        # broker preserved in the comment; if MT5 dropped/compacted it, fall back
        # to the order already linked by broker_ticket (set on fill). Only a true
        # orphan (no order with this ticket) stays NULL to satisfy the FK.
        coid = self._resolve_order_uuid(p.client_order_id) \
            or self._coid_for_ticket(p.ticket)
        self.conn.execute(
            """
            INSERT INTO positions (broker_ticket, client_order_id, symbol, side,
                                   volume, open_price, sl, tp, status, opened_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open', %s)
            ON CONFLICT (broker_ticket) DO UPDATE SET
                symbol     = EXCLUDED.symbol,
                side       = EXCLUDED.side,
                volume     = EXCLUDED.volume,
                open_price = EXCLUDED.open_price,
                sl         = EXCLUDED.sl,
                tp         = EXCLUDED.tp,
                status     = 'open'
            """,
            (p.ticket, coid, p.symbol, p.side, p.volume, p.open_price,
             p.sl, p.tp, p.opened_at),
        )

    def mark_position_closed(self, ticket: int, pnl: float | None = None,
                             reason: str | None = None) -> None:
        # COALESCE so re-marking (or a None) never wipes a recorded pnl/reason.
        self.conn.execute(
            "UPDATE positions SET status = 'closed', closed_at = now(), "
            "close_pnl = COALESCE(%s, close_pnl), "
            "close_reason = COALESCE(%s, close_reason) "
            "WHERE broker_ticket = %s",
            (pnl, reason, ticket),
        )

    def realized_equity_peak(self, initial_capital: float) -> float:
        """High-water mark of the account's REALISED balance path (initial
        capital + cumulative closed P&L, in close order). Seeds the drawdown
        throttle after a restart so it does not forget a drawdown in progress."""
        rows = self.conn.execute(
            "SELECT close_pnl FROM positions WHERE status='closed' "
            "AND close_pnl IS NOT NULL ORDER BY closed_at").fetchall()
        bal = peak = float(initial_capital)
        for (pnl,) in rows:
            bal += float(pnl)
            peak = max(peak, bal)
        return peak

    def set_order_status(self, client_order_id: str, status: str,
                         broker_ticket: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE orders SET status = %s, broker_ticket = %s, resolved_at = now() "
            "WHERE client_order_id = %s",
            (status, broker_ticket, client_order_id),
        )

    # ----- advanced trade management (TP1 / break-even / trailing) -------- #
    def set_position_mgmt(self, ticket: int, tp1, tp2, init_sl,
                          mgmt: str = "running") -> None:
        self.conn.execute(
            "UPDATE positions SET tp1=%s, tp2=%s, init_sl=%s, mgmt=%s "
            "WHERE broker_ticket=%s", (tp1, tp2, init_sl, mgmt, ticket))

    def open_managed_positions(self, owner: str | None = None) -> list[dict]:
        """Open positions to drive TP1/BE/trailing on. When `owner` is given (the
        ensemble tag, e.g. 'ensemble' or 'vote'), return ONLY positions this bot
        opened — so two loops on the same account each manage their OWN trades and
        never fight over a shared position's SL/TP1. owner=None -> all (default)."""
        sql = ("SELECT p.broker_ticket, p.symbol, p.side, p.volume, p.open_price, "
               "p.sl, p.tp1, p.tp2, p.init_sl, p.mgmt FROM positions p "
               "WHERE p.status='open'")
        params: tuple = ()
        if owner:
            sql += (" AND EXISTS (SELECT 1 FROM orders o "
                    "WHERE o.broker_ticket = p.broker_ticket "
                    "AND o.strategy_id LIKE %s)")
            params = (owner + ":%",)
        rows = self.conn.execute(sql, params).fetchall()
        return [{"ticket": int(r[0]), "symbol": r[1], "side": r[2],
                 "volume": float(r[3]), "entry": float(r[4]),
                 "sl": float(r[5]) if r[5] is not None else None,
                 "tp1": float(r[6]) if r[6] is not None else None,
                 "tp2": float(r[7]) if r[7] is not None else None,
                 "init_sl": float(r[8]) if r[8] is not None else None,
                 "mgmt": r[9]} for r in rows]

    def apply_tp1(self, ticket: int, new_volume: float, be_sl: float) -> None:
        self.conn.execute(
            "UPDATE positions SET volume=%s, sl=%s, mgmt='tp1_hit' "
            "WHERE broker_ticket=%s", (new_volume, be_sl, ticket))

    def update_position_sl(self, ticket: int, sl: float) -> None:
        self.conn.execute(
            "UPDATE positions SET sl=%s WHERE broker_ticket=%s", (sl, ticket))

    # ----- shadow mode (benched strategies paper-trade; sim only) --------- #
    def shadow_open(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, strategy_id, symbol, side, tf, entry, sl, tp, opened_at "
            "FROM shadow_positions").fetchall()
        return [{"id": r[0], "strategy_id": r[1], "symbol": r[2], "side": r[3],
                 "tf": r[4], "entry": float(r[5]), "sl": float(r[6]),
                 "tp": float(r[7]), "opened_at": r[8]} for r in rows]

    def shadow_has(self, strategy_id: str, symbol: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM shadow_positions WHERE strategy_id=%s AND symbol=%s",
            (strategy_id, symbol)).fetchone() is not None

    def shadow_add(self, strategy_id: str, symbol: str, side: str, tf: str,
                   entry: float, sl: float, tp: float) -> None:
        self.conn.execute(
            "INSERT INTO shadow_positions (strategy_id,symbol,side,tf,entry,sl,tp) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (strategy_id,symbol) DO NOTHING",
            (strategy_id, symbol, side, tf, entry, sl, tp))

    def shadow_close(self, pos_id: int, strategy_id: str, symbol: str, side: str,
                     entry: float, exit_px: float, r_mult: float, reason: str,
                     opened_at) -> None:
        self.conn.execute(
            "INSERT INTO shadow_trades "
            "(strategy_id,symbol,side,entry,exit,r_mult,reason,opened_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (strategy_id, symbol, side, entry, exit_px, r_mult, reason, opened_at))
        self.conn.execute("DELETE FROM shadow_positions WHERE id=%s", (pos_id,))

    def shadow_record(self, strategy_id: str, symbol: str, side: str,
                      entry: float, exit_px: float, r_mult: float, reason: str,
                      opened_at, closed_at) -> None:
        """Insert a resolved paper-trade with an explicit timestamp (used to
        backfill the shadow ledger from recent history)."""
        self.conn.execute(
            "INSERT INTO shadow_trades "
            "(strategy_id,symbol,side,entry,exit,r_mult,reason,opened_at,closed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (strategy_id, symbol, side, entry, exit_px, r_mult, reason,
             opened_at, closed_at))

    def shadow_clear(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM shadow_trades WHERE symbol=%s", (symbol,))
        self.conn.execute("DELETE FROM shadow_positions WHERE symbol=%s", (symbol,))

    def shadow_stats(self, last_n: int = 30) -> dict[str, dict]:
        """Per-strategy rolling stats over each technique's most recent `last_n`
        paper-trades (newest first). Powers the dashboard SHADOW cards."""
        rows = self.conn.execute(
            "SELECT strategy_id, r_mult FROM shadow_trades "
            "ORDER BY closed_at DESC LIMIT 2000").fetchall()
        by: dict[str, list] = {}
        for sid, r in rows:
            lst = by.setdefault(sid, [])
            if len(lst) < last_n:
                lst.append(float(r))
        out: dict[str, dict] = {}
        for sid, rs in by.items():
            n = len(rs)
            wins = sum(1 for r in rs if r > 0)
            out[sid] = {"n": n, "wr": wins / n if n else 0.0,
                        "avgR": sum(rs) / n if n else 0.0}
        return out

    def upsert_bars(self, symbol: str, bars) -> None:
        """Persist recent OHLC bars for the dashboard chart; keep the newest 500."""
        params = [(symbol, b.time, b.open, b.high, b.low, b.close) for b in bars]
        if not params:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO price_bars (symbol, bar_time, o, h, l, c) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (symbol, bar_time) DO UPDATE SET "
                "o=EXCLUDED.o, h=EXCLUDED.h, l=EXCLUDED.l, c=EXCLUDED.c", params)
        self.conn.execute(
            "DELETE FROM price_bars WHERE symbol=%s AND bar_time NOT IN "
            "(SELECT bar_time FROM price_bars WHERE symbol=%s "
            "ORDER BY bar_time DESC LIMIT 500)", (symbol, symbol))

    def mark_order_submitted(self, client_order_id: str) -> None:
        """Stamp submitted_at the instant before we hand the order to the broker.
        If we crash now, reconciliation finds a 'submitted' order with no ticket
        and settles it by history (idempotency) instead of re-sending."""
        self.conn.execute(
            "UPDATE orders SET status = 'submitted', submitted_at = now() "
            "WHERE client_order_id = %s",
            (client_order_id,),
        )

    def roll_baseline(self, d: date, midnight_balance: float, source: str) -> float:
        # Cast explicitly: a Python float binds as double precision and Postgres
        # won't implicitly resolve it to the function's NUMERIC parameter.
        row = self.conn.execute(
            "SELECT sp_roll_daily_baseline(%s::date, %s::numeric, %s::text)",
            (d, midnight_balance, source),
        ).fetchone()
        return float(row[0])

    def write_audit(self, event_type: str, decision: str, reason: str,
                    payload: dict) -> None:
        self.conn.execute(
            "CALL sp_write_audit(%s, %s, %s, %s)",
            (event_type, decision, reason, Jsonb(payload or {})),
        )

    def prune_heartbeats(self, keep_hours: int = 72) -> int:
        """Heartbeats are 30s liveness pings (~2,880/day) — only the latest matters
        to the watchdog and the equity curve only reads the last ~300. Drop old
        ones so audit_log doesn't grow unbounded. REAL events (order/kill/reconcile)
        are never touched. Returns rows removed."""
        cur = self.conn.execute(
            "DELETE FROM audit_log WHERE event_type='heartbeat' "
            "AND ts < now() - make_interval(hours => %s)", (keep_hours,))
        return cur.rowcount or 0

    # ----- kill switch + watchdog inputs ---------------------------------- #
    def kill_switch(self) -> tuple[str, Optional[str]]:
        row = self.conn.execute(
            "SELECT mode, reason FROM kill_switch WHERE id = 1"
        ).fetchone()
        return (row[0], row[1]) if row else ("running", None)

    def set_kill_switch(self, mode: str, reason: Optional[str] = None,
                        source: str = "watchdog") -> None:
        self.conn.execute(
            """
            UPDATE kill_switch SET
                mode       = %s,
                reason     = %s,
                source     = %s,
                tripped_at = CASE WHEN %s = 'running' THEN NULL ELSE now() END,
                updated_at = now()
            WHERE id = 1
            """,
            (mode, reason, source, mode),
        )

    def last_heartbeat_age_secs(self, now: datetime) -> Optional[float]:
        """Seconds since the most recent main-loop heartbeat, or None if there has
        never been one. The watchdog uses this to detect a hung/dead main loop."""
        row = self.conn.execute(
            "SELECT max(ts) FROM audit_log WHERE event_type = 'heartbeat'"
        ).fetchone()
        if not row or row[0] is None:
            return None
        last = row[0]
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds()

    def risk_anchors(self, cet_today: date) -> tuple[Optional[float], Optional[float]]:
        """(today_midnight_balance, highest_midnight_balance) via sp_risk_anchors —
        what an independent engine needs to recompute the floors after a restart."""
        row = self.conn.execute(
            "SELECT today_midnight_balance, highest_midnight_balance "
            "FROM sp_risk_anchors(%s::date)", (cet_today,)
        ).fetchone()
        today = float(row[0]) if row and row[0] is not None else None
        highest = float(row[1]) if row and row[1] is not None else None
        return today, highest

    def order_for_ticket(self, ticket: int) -> Optional[dict]:
        """The originating order's strategy + entry regime for a broker ticket —
        used to attribute a closed position's P&L to the League. Looks up by
        orders.broker_ticket (set on fill); the live MT5 comment holds only a
        compact coid tag, so positions.client_order_id can be NULL — don't join on
        it."""
        r = self.conn.execute(
            "SELECT strategy_id, regime, client_order_id, symbol FROM orders "
            "WHERE broker_ticket = %s ORDER BY resolved_at DESC NULLS LAST LIMIT 1",
            (ticket,)).fetchone()
        if not r:
            return None
        return {"strategy_id": r[0], "regime": r[1],
                "client_order_id": str(r[2]), "symbol": r[3]}

    def account_profile(self) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT login, variant, path, phase, initial_capital "
            "FROM account_profile WHERE id = 1"
        ).fetchone()
        if not row:
            return None
        return {"login": int(row[0]), "variant": row[1], "path": row[2],
                "phase": row[3], "initial_capital": float(row[4])}

    # ----- helpers (used by the execution engine later) ------------------- #
    def _resolve_order_uuid(self, coid: Optional[str]) -> Optional[str]:
        if not coid:
            return None
        try:
            uuid.UUID(coid)  # only a real UUID can satisfy the orders FK
        except (ValueError, AttributeError):
            return None
        row = self.conn.execute(
            "SELECT 1 FROM orders WHERE client_order_id = %s", (coid,)
        ).fetchone()
        return coid if row else None

    def _coid_for_ticket(self, ticket: int) -> Optional[str]:
        """Fallback FK link: the coid of the order filled as this broker ticket
        (orders.broker_ticket is set on fill). Lets a normally-opened position
        record its client_order_id even when MT5 didn't preserve the comment."""
        row = self.conn.execute(
            "SELECT client_order_id FROM orders WHERE broker_ticket = %s "
            "ORDER BY resolved_at DESC NULLS LAST LIMIT 1", (ticket,)).fetchone()
        return str(row[0]) if row else None

    def create_order(self, *, strategy_id: str, symbol: str, side: str,
                     volume: float, sl: float | None = None,
                     tp: float | None = None, regime: str | None = None,
                     rationale: str | None = None) -> str:
        """Pre-register an order before submission and return its client_order_id
        (the idempotency key the Execution Engine writes into the MT5 comment)."""
        coid = str(uuid.uuid4())
        self.conn.execute(
            """
            INSERT INTO orders (client_order_id, strategy_id, symbol, side,
                                volume, intended_sl, intended_tp, regime,
                                rationale, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'created')
            """,
            (coid, strategy_id, symbol, side, volume, sl, tp, regime, rationale),
        )
        return coid


# --------------------------------------------------------------------------- #
# Live end-to-end test: MT5 (broker truth) + Postgres (persisted) + engine.    #
#   ./env/Scripts/python.exe pg_state_store.py                                 #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from ftmo_compliance_engine import (
        FtmoComplianceEngine, AccountProfile, Variant, Path, Phase, FTMO_TZ,
        profile_from_env, config_from_env,
    )
    from state_reconciliation import Reconciler
    from mt5_broker import Mt5Broker

    load_dotenv()

    profile = profile_from_env()

    with Mt5Broker() as broker, PgStateStore() as store:
        engine = FtmoComplianceEngine(profile, config_from_env())
        print("Running live reconciliation -> Postgres ...")
        result = Reconciler(broker, store, engine).run()
        print("  baseline source :", result.baseline_source, "=", result.midnight_balance)
        print("  adopted         :", result.adopted_positions)
        print("  closed_while_down:", result.closed_while_down)
        print("  reconciled/ok   :", result.ok, "| engine.reconciled =", engine.reconciled)

        # Prove it persisted: read straight back from the DB.
        today = datetime.now(FTMO_TZ).date()
        bal = store.baseline_for(today)
        print(f"\nPersisted day_baseline[{today}] = {bal}")
        n_audit = store.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        print(f"audit_log rows now: {n_audit}")
        recent = store.conn.execute(
            "SELECT event_type, decision, reason_code FROM audit_log "
            "ORDER BY id DESC LIMIT 5"
        ).fetchall()
        for et, dec, rc in recent:
            print(f"  audit: {et}/{dec} {rc}")

        if engine.reconciled:
            print("\nCompliance floors (live):")
            print("  daily floor      :", engine.real_daily_floor())
            print("  daily soft floor :", engine.daily_soft_floor())
            print("  overall floor    :", engine.real_overall_floor())
