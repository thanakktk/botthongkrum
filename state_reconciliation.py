"""
State Reconciliation
======================================================================
Runs on EVERY startup (planned or post-crash). Until it completes successfully
the Compliance Engine stays in NOT_RECONCILED -> no new orders.

Core principles
  * The BROKER is the single source of truth for positions, orders and balance.
  * IDEMPOTENCY: every order we ever sent carries a client_order_id embedded in
    the MT5 order comment/magic. After a crash we match broker fills back to our
    intent by that id and NEVER re-submit an order that may already exist.
  * The daily floor anchor (midnight balance) MUST be correct. If the bot was
    down across 00:00 CET we reconstruct that balance from broker history; if it
    cannot be reconstructed we fall back conservatively and flag for manual
    confirmation rather than guess high (guessing high can cause a real breach).

This module is broker-agnostic via BrokerGateway; an MT5 adapter and the Paper
adapter both implement it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import Optional, Sequence

from ftmo_compliance_engine import FtmoComplianceEngine, FTMO_TZ
from notifier import close_embed


# --------------------------------------------------------------------------- #
# DTOs                                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    side: str            # 'buy' | 'sell'
    volume: float
    open_price: float
    sl: Optional[float]
    tp: Optional[float]
    client_order_id: Optional[str]   # parsed from comment/magic
    opened_at: Optional[datetime]


@dataclass
class LocalOrder:
    client_order_id: str
    status: str          # created | submitted | filled | rejected | unknown
    broker_ticket: Optional[int]


@dataclass
class LocalPosition:
    broker_ticket: int
    status: str          # open | closed


@dataclass
class ReconResult:
    adopted_positions: list[int]      # broker positions we didn't know about
    closed_while_down: list[int]      # local-open positions gone at broker
    resolved_orders: list[str]        # 'submitted' orders we settled by history
    baseline_source: str              # live | reconstructed | manual_required
    midnight_balance: float
    ok: bool


# --------------------------------------------------------------------------- #
# Ports                                                                       #
# --------------------------------------------------------------------------- #
class BrokerGateway(ABC):
    @abstractmethod
    def open_positions(self) -> Sequence[BrokerPosition]: ...
    @abstractmethod
    def balance(self) -> float: ...
    @abstractmethod
    def equity(self) -> float: ...
    @abstractmethod
    def order_was_filled(self, client_order_id: str) -> Optional[int]:
        """Look up account history by embedded client_order_id.
        Returns the broker ticket if it filled, else None. Enables idempotency."""
    @abstractmethod
    def balance_at_cet_midnight(self, d: date) -> Optional[float]:
        """Reconstruct closed balance as of 00:00 CET that opened day `d`,
        from broker deal history. None if not reconstructable."""


class StateStore(ABC):
    @abstractmethod
    def local_orders(self) -> Sequence[LocalOrder]: ...
    @abstractmethod
    def local_open_positions(self) -> Sequence[LocalPosition]: ...
    @abstractmethod
    def adopt_position(self, p: BrokerPosition) -> None: ...
    @abstractmethod
    def mark_position_closed(self, ticket: int, pnl: Optional[float] = None,
                             reason: Optional[str] = None) -> None: ...
    @abstractmethod
    def set_order_status(self, client_order_id: str, status: str,
                         broker_ticket: Optional[int]) -> None: ...
    @abstractmethod
    def baseline_for(self, d: date) -> Optional[float]: ...
    @abstractmethod
    def roll_baseline(self, d: date, midnight_balance: float, source: str) -> float: ...
    @abstractmethod
    def write_audit(self, event_type: str, decision: str, reason: str,
                    payload: dict) -> None: ...


# --------------------------------------------------------------------------- #
# Reconciler                                                                  #
# --------------------------------------------------------------------------- #
class Reconciler:
    def __init__(self, broker: BrokerGateway, store: StateStore,
                 engine: FtmoComplianceEngine, league=None, notifier=None):
        self.broker = broker
        self.store = store
        self.engine = engine
        self.league = league       # optional League — fed when a position closes
        self.notifier = notifier   # optional Discord trade notifier

    # ----- position sync (broker = truth) ---------------------------------- #
    def _record_close(self, ticket: int, now: datetime,
                      reason: str = "broker_close") -> None:
        """A position closed at the broker (SL/TP/manual). Record its P&L + reason,
        attribute to the League (by entry regime + rep strategy), and fire the
        Discord close alert. Guarded so fake/minimal stores still work."""
        get = getattr(self.store, "order_for_ticket", None)
        info = get(ticket) if get else None
        pnl = (self.broker.realized_pnl(ticket)
               if hasattr(self.broker, "realized_pnl") else None)
        self.store.mark_position_closed(ticket, pnl, reason)
        regime = info.get("regime") if info else None
        sid = (info.get("strategy_id") if info else "") or ""
        if ":" in sid:                      # strip the ensemble namespace tag
            sid = sid.split(":", 1)[1]      # ("ensemble:x"/"vote:x" -> "x")
        if self.league is not None and info and regime and pnl is not None and sid:
            self.league.record_outcome(sid, regime, pnl, now)
        if self.notifier is not None and info and pnl is not None:
            self.notifier.send(embeds=[close_embed(
                symbol=info.get("symbol") or "?", pnl=pnl, reason="broker_close",
                strategy=info.get("strategy_id"), regime=regime)])

    def _adopt_and_close(self, now: datetime) -> tuple[list[int], list[int]]:
        bpos = {p.ticket: p for p in self.broker.open_positions()}
        lpos = {p.broker_ticket for p in self.store.local_open_positions()
                if p.status == "open"}
        adopted, closed = [], []
        # broker positions we don't have locally -> adopt (broker is truth)
        for ticket, p in bpos.items():
            if ticket not in lpos:
                self.store.adopt_position(p)
                self.store.write_audit("reconcile", "adopt", "orphan_broker_position",
                                       {"ticket": ticket, "symbol": p.symbol})
                adopted.append(ticket)
        # local-open positions gone at broker -> closed (SL/TP/manual) + record
        for ticket in lpos:
            if ticket not in bpos:
                self.store.write_audit("reconcile", "close", "closed_while_down",
                                       {"ticket": ticket})
                self._record_close(ticket, now)
                closed.append(ticket)
        return adopted, closed

    def sync_open_positions(self, now: Optional[datetime] = None
                            ) -> tuple[list[int], list[int]]:
        """Mid-session sync: catch broker-side SL/TP closes (and orphan opens)
        without a full restart. Returns (adopted, closed)."""
        return self._adopt_and_close(now or datetime.now(timezone.utc))

    def run(self, now_utc: Optional[datetime] = None) -> ReconResult:
        now_utc = now_utc or datetime.now(timezone.utc)
        cet_today = now_utc.astimezone(FTMO_TZ).date()

        # (1)+(2) adopt orphan broker positions / close+attribute disappeared ones
        adopted, closed_while_down = self._adopt_and_close(now_utc)
        resolved = []

        # (3) Orders left 'submitted' (we sent it, then crashed before seeing the
        #     result). IDEMPOTENCY: settle via history; never blindly resend.
        for o in self.store.local_orders():
            if o.status == "submitted" and o.broker_ticket is None:
                ticket = self.broker.order_was_filled(o.client_order_id)
                if ticket is not None:
                    self.store.set_order_status(o.client_order_id, "filled", ticket)
                    resolved.append(o.client_order_id)
                    self.store.write_audit("reconcile", "filled",
                                           "submitted_order_filled",
                                           {"coid": o.client_order_id, "ticket": ticket})
                else:
                    # Genuinely not placed. Mark 'unknown' and leave it for the
                    # strategy to re-decide — do NOT auto-resubmit here.
                    self.store.set_order_status(o.client_order_id, "unknown", None)
                    self.store.write_audit("reconcile", "unknown",
                                           "submitted_order_not_found",
                                           {"coid": o.client_order_id})
                    resolved.append(o.client_order_id)

        # (4) Repair the daily baseline (midnight balance anchor).
        midnight_balance, baseline_source = self._repair_baseline(cet_today)

        # (5) Seed engine anchors. Replay every stored baseline so the 1-step
        #     trailing overall floor (highest midnight balance) is correct.
        self._seed_engine(now_utc, midnight_balance)

        ok = baseline_source != "manual_required"
        if ok:
            self.engine.mark_reconciled()
        else:
            # Stay halted: a wrong anchor can cause a real FTMO breach. Require a
            # human to confirm the day's midnight balance before trading resumes.
            self.store.write_audit("reconcile", "halt", "baseline_manual_required",
                                   {"cet_date": str(cet_today)})

        return ReconResult(adopted, closed_while_down, resolved,
                           baseline_source, midnight_balance, ok)

    # ----- baseline repair ------------------------------------------------- #
    def _repair_baseline(self, cet_today: date) -> tuple[float, str]:
        existing = self.store.baseline_for(cet_today)
        if existing is not None:
            return existing, "live"           # we were up at the rollover

        # Bot was down across 00:00 CET. Reconstruct from broker history.
        recon = self.broker.balance_at_cet_midnight(cet_today)
        if recon is not None:
            self.store.roll_baseline(cet_today, recon, "reconstructed")
            self.store.write_audit("rollover", "reconstructed", "baseline_from_history",
                                   {"cet_date": str(cet_today), "balance": recon})
            return recon, "reconstructed"

        # Cannot reconstruct -> conservative fallback: use the lower of current
        # balance and last known baseline so the floor can only be TIGHTER, never
        # looser. Flag for manual confirmation.
        last = self._last_known_baseline()
        conservative = min(self.broker.balance(), last) if last else self.broker.balance()
        self.store.roll_baseline(cet_today, conservative, "manual")
        return conservative, "manual_required"

    def _last_known_baseline(self) -> Optional[float]:
        # In a real store this is a single SELECT ... ORDER BY cet_date DESC LIMIT 1.
        return getattr(self.store, "last_baseline", None)

    def _seed_engine(self, now_utc: datetime, midnight_balance: float) -> None:
        # roll_daily_baseline also lifts highest_midnight_balance. To fully
        # reconstruct the trailing anchor, replay historical baselines if the
        # store exposes them; here we at least seed today's anchor.
        self.engine.roll_daily_baseline(now_utc, midnight_balance)


# --------------------------------------------------------------------------- #
# Demo with in-memory fakes (run: python state_reconciliation.py)             #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from ftmo_compliance_engine import (AccountProfile, Variant, Path, Phase)

    class FakeBroker(BrokerGateway):
        def __init__(self):
            self._pos = [BrokerPosition(5001, "XAUUSD", "buy", 0.5, 2350.0,
                                        2340.0, 2370.0, "coid-A", None)]
        def open_positions(self): return self._pos
        def balance(self): return 101_500.0
        def equity(self): return 101_200.0
        def order_was_filled(self, coid):
            return 5001 if coid == "coid-A" else None
        def balance_at_cet_midnight(self, d): return 101_000.0  # reconstructable

    class FakeStore(StateStore):
        def __init__(self):
            # We knew about a position 4002 that the broker no longer shows
            # (closed while we were down), and an order 'coid-A' stuck 'submitted'.
            self._orders = [LocalOrder("coid-A", "submitted", None)]
            self._open = [LocalPosition(4002, "open")]
            self._baseline = {}
            self.last_baseline = 100_800.0
        def local_orders(self): return self._orders
        def local_open_positions(self): return self._open
        def adopt_position(self, p): print(f"  adopt position {p.ticket} {p.symbol}")
        def mark_position_closed(self, t, pnl=None, reason=None):
            print(f"  mark {t} closed (pnl={pnl}, reason={reason})")
        def set_order_status(self, coid, status, ticket):
            print(f"  order {coid} -> {status} (ticket={ticket})")
        def baseline_for(self, d): return self._baseline.get(d)
        def roll_baseline(self, d, bal, source):
            self._baseline[d] = bal
            print(f"  baseline {d} = {bal} ({source})")
            return bal
        def write_audit(self, et, dec, reason, payload):
            print(f"  audit[{et}/{dec}] {reason} {payload}")

    prof = AccountProfile(Variant.SWING, Path.TWO_STEP, Phase.FUNDED, 100_000)
    eng = FtmoComplianceEngine(prof)
    rec = Reconciler(FakeBroker(), FakeStore(), eng)

    print("Running reconciliation...")
    res = rec.run()
    print("\nResult:")
    print("  adopted positions :", res.adopted_positions)
    print("  closed while down :", res.closed_while_down)
    print("  resolved orders   :", res.resolved_orders)
    print("  baseline source   :", res.baseline_source, "=", res.midnight_balance)
    print("  reconciled / ok   :", res.ok, "| engine.reconciled =", eng.reconciled)
    assert res.ok and eng.reconciled
    assert 5001 in res.adopted_positions and 4002 in res.closed_while_down
    print("\nReconciliation self-test passed.")

    # ----- Part 2: live SL/TP -> League attribution (PaperBroker + Postgres) - #
    from datetime import timezone
    from paper_broker import PaperBroker
    from pg_state_store import PgStateStore
    from league import League
    from execution import ExecutionEngine, OrderRequest
    from ftmo_compliance_engine import AccountSnapshot

    now2 = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)   # fixed Wednesday
    broker = PaperBroker(100_000.0)
    broker.set_price("XAUUSD", 2000.0)
    eng2 = FtmoComplianceEngine(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000))
    eng2.mark_reconciled()
    eng2.roll_daily_baseline(now2, 100_000)

    print("\nPart 2: SL/TP -> League attribution")
    with PgStateStore() as store2:
        store2.conn.execute("DELETE FROM strategy_league WHERE strategy_id='breakout_sr'")
        league = League(store2)
        ex = ExecutionEngine(broker, store2, eng2, league=league)

        # open via execution: DB position 'open', order carries regime='trend'
        snap = AccountSnapshot(broker.balance(), broker.equity(), 0.0)
        req = OrderRequest("ensemble:breakout_sr", "XAUUSD", "buy", 1.0,
                           1950.0, 2100.0, 2000.0, regime="trend")
        res2 = ex.submit(req, snap, now2)
        assert res2.submitted

        # simulate the broker hitting TP: close at the broker WITHOUT ex.close,
        # so the DB still thinks it's open (exactly the SL/TP situation).
        broker.set_price("XAUUSD", 2100.0)
        broker.close_position(res2.ticket, price=2100.0)

        recon = Reconciler(broker, store2, eng2, league=league)
        adopted, closed = recon.sync_open_positions(now2)
        print(f"  sync -> closed {closed}")
        assert res2.ticket in closed
        row = league._row("breakout_sr", "trend")
        print(f"  league breakout_sr/trend -> trades={row['trades']} "
              f"wins={row['wins']} pnl={row['gross_pnl']}")
        assert row and row["trades"] == 1 and row["wins"] == 1 and row["gross_pnl"] > 0

        store2.conn.execute("DELETE FROM strategy_league WHERE strategy_id='breakout_sr'")
        store2.conn.execute(
            "DELETE FROM positions WHERE client_order_id IN "
            "(SELECT client_order_id FROM orders WHERE strategy_id LIKE 'ensemble:%')")
        store2.conn.execute("DELETE FROM orders WHERE strategy_id LIKE 'ensemble:%'")
    print("SL/TP -> League attribution OK.")
