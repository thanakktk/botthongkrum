"""
Execution Engine
======================================================================
The actuator that turns an approved trade intent into a broker order — and,
critically, the thing that FLATTENS when the Compliance Engine says so. It
sits downstream of the (future) Arbitration Layer; every order still passes
the Compliance Engine's pre-trade veto first. Nothing reaches the broker
without that check.

Idempotency: a client_order_id (UUID) is created and persisted BEFORE the
order is sent, and written into the broker order's comment. If we crash
between send and confirm, reconciliation matches the broker fill back to that
coid and never double-submits.

Broker-agnostic: works against any OrderRouter — the live Mt5Broker or the
PaperBroker — so the whole path is testable with no money and no Algo flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol, Sequence, runtime_checkable

from ftmo_compliance_engine import (
    FtmoComplianceEngine, AccountSnapshot, Action, Verdict, NewsEvent,
)
from state_reconciliation import BrokerPosition
from notifier import open_embed, close_embed, mgmt_embed


@runtime_checkable
class OrderRouter(Protocol):
    """The order side both brokers expose (read side is BrokerGateway)."""
    def place_market(self, *, symbol: str, side: str, volume: float,
                     sl: Optional[float], tp: Optional[float],
                     client_order_id: Optional[str]) -> int: ...
    def close_position(self, ticket: int) -> float: ...
    def flatten_all(self) -> list[int]: ...
    def open_positions(self) -> Sequence[BrokerPosition]: ...


@dataclass
class OrderRequest:
    strategy_id: str
    symbol: str
    side: str                 # 'buy' | 'sell'
    volume: float
    sl: Optional[float]
    tp: Optional[float]
    # $ this single order loses if its SL is hit — the pre-trade gate input.
    risk_to_sl: float
    # market regime detected at entry — stored so the League scores by regime.
    regime: Optional[str] = None
    entry: Optional[float] = None       # intended entry (for the alert)
    rationale: str = ""                 # WHY this trade — shown in the Discord alert
    tp1: Optional[float] = None         # partial-take-profit (bank + go break-even)
    tp2: Optional[float] = None         # runner target (= the broker TP)


@dataclass
class TradeMgmtConfig:
    """Advanced trade management: optionally lift the stop to break-even EARLY,
    bank a partial at TP1 + break-even, then trail the runner toward TP2."""
    enabled: bool = True
    partial_pct: float = 0.5        # fraction closed at TP1
    trail_r: float = 1.0            # trail distance, in units of the initial risk R
    be_buffer_r: float = 0.05       # break-even nudged this far into profit (× R)
    # EARLY break-even: once price tags this many R in our favour (before TP1),
    # move the stop to entry. The MFE study showed losers peak ~0.7R; an early BE
    # turns many would-be -1R losses into ~0R. 0 = off. Validated net-positive at
    # 0.8R on the robust roster (slightly higher return + lower drawdown).
    be_trigger_r: float = 0.8


@dataclass
class ExecResult:
    submitted: bool
    verdict: Verdict
    client_order_id: Optional[str] = None
    ticket: Optional[int] = None
    detail: str = ""


class ExecutionEngine:
    def __init__(self, broker: OrderRouter, store, engine: FtmoComplianceEngine,
                 league=None, notifier=None, mgmt: "TradeMgmtConfig | None" = None):
        self.broker = broker
        self.store = store
        self.engine = engine
        self.league = league       # optional League (Pillar 4) — fed on close
        self.notifier = notifier   # optional Discord trade notifier
        self.mgmt = mgmt or TradeMgmtConfig()   # TP1/break-even/trailing rules

    # ----- open ------------------------------------------------------------ #
    def submit(self, req: OrderRequest, snap: AccountSnapshot, now: datetime,
               upcoming_news: Sequence[NewsEvent] = ()) -> ExecResult:
        # 1) Compliance VETO (daily/overall floors, worst-case sizing, time gates).
        verdict = self.engine.check_new_order(
            snap=snap, new_order_risk=req.risk_to_sl, now_utc=now,
            upcoming_news=upcoming_news,
        )
        if not verdict.allowed:
            self.store.write_audit("order", "veto", verdict.reason.value,
                                   {"strategy": req.strategy_id, "symbol": req.symbol,
                                    "side": req.side, "volume": req.volume,
                                    "detail": verdict.detail})
            return ExecResult(False, verdict, detail="vetoed: " + verdict.detail)

        # 2) Persist intent FIRST (idempotency key), then mark submitted.
        coid = self.store.create_order(
            strategy_id=req.strategy_id, symbol=req.symbol, side=req.side,
            volume=req.volume, sl=req.sl, tp=req.tp, regime=req.regime,
            rationale=req.rationale,
        )
        self.store.mark_order_submitted(coid)

        # 3) Send to broker (coid travels in the comment).
        try:
            ticket = self.broker.place_market(
                symbol=req.symbol, side=req.side, volume=req.volume,
                sl=req.sl, tp=req.tp, client_order_id=coid,
            )
        except Exception as e:  # broker rejected — leave 'submitted' for reconcile
            self.store.write_audit("order", "error", "broker_rejected",
                                   {"coid": coid, "error": str(e)})
            return ExecResult(False, verdict, client_order_id=coid,
                              detail=f"broker error: {e}")

        # 4) Confirm fill: settle the order and mirror the position locally.
        self.store.set_order_status(coid, "filled", ticket)
        self._mirror_position(ticket)
        if req.tp1 is not None:      # arm advanced trade management
            self.store.set_position_mgmt(ticket, req.tp1, req.tp2, req.sl, "running")
        self.store.write_audit("order", "allow", "filled",
                               {"coid": coid, "ticket": ticket,
                                "symbol": req.symbol, "side": req.side,
                                "volume": req.volume})
        if self.notifier is not None:
            self.notifier.send(embeds=[open_embed(
                symbol=req.symbol, side=req.side, volume=req.volume,
                entry=req.entry, sl=req.sl, tp=req.tp, risk=req.risk_to_sl,
                regime=req.regime, strategy=req.strategy_id,
                rationale=req.rationale)])
        return ExecResult(True, verdict, client_order_id=coid, ticket=ticket)

    def _mirror_position(self, ticket: int) -> None:
        for p in self.broker.open_positions():
            if p.ticket == ticket:
                self.store.adopt_position(p)
                return

    # ----- flatten (kill-switch actuator) ---------------------------------- #
    def flatten_all(self, reason: str = "compliance_flatten",
                    now: Optional[datetime] = None) -> list[int]:
        now = now or datetime.now(timezone.utc)
        # close each position individually so every one records its P&L/reason,
        # feeds the League, and fires its own Discord close alert.
        closed = []
        for p in self.broker.open_positions():   # already a fresh snapshot list
            self.close(p.ticket, reason=reason, now=now)
            closed.append(p.ticket)
        self.store.write_audit("kill", "flatten_all", reason, {"closed": closed})
        return closed

    def close(self, ticket: int, reason: str = "manual",
              now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        info = self.store.order_for_ticket(ticket)   # read BEFORE closing
        gross = self.broker.close_position(ticket)
        # Record the NET realized P&L (incl. commission/swap) so this path matches
        # the reconciler's broker-side close exactly; fall back to gross.
        net = (self.broker.realized_pnl(ticket)
               if hasattr(self.broker, "realized_pnl") else None)
        pnl = net if net is not None else gross
        self.store.mark_position_closed(ticket, pnl, reason)
        self.store.write_audit("order", "close", reason,
                               {"ticket": ticket, "pnl": pnl, "gross": gross})
        self._after_close(info, pnl, reason, now)
        return pnl

    def _after_close(self, info: Optional[dict], pnl: float, reason: str,
                     now: datetime) -> None:
        """On any close: attribute P&L to the League (by entry regime + rep
        strategy) and fire the Discord close alert."""
        regime = info.get("regime") if info else None
        sid = (info.get("strategy_id") if info else "") or ""
        if ":" in sid:                      # strip the ensemble namespace tag
            sid = sid.split(":", 1)[1]      # ("ensemble:x"/"vote:x" -> "x")
        if self.league is not None and info and regime and pnl is not None and sid:
            self.league.record_outcome(sid, regime, pnl, now)
        if self.notifier is not None and info:
            self.notifier.send(embeds=[close_embed(
                symbol=info.get("symbol") or "?", pnl=pnl, reason=reason,
                strategy=info.get("strategy_id"), regime=regime)])

    # ----- advanced trade management (TP1 partial + break-even + trailing) - #
    def manage_position(self, rec: dict, bid: float, ask: float,
                        now: datetime) -> Optional[str]:
        """Drive one open position's TP1/BE/trailing. `rec` from
        store.open_managed_positions(). Returns the action taken, or None."""
        if not self.mgmt.enabled or rec.get("tp1") is None \
                or rec.get("init_sl") is None:
            return None
        side, entry, ticket = rec["side"], rec["entry"], rec["ticket"]
        rdist = abs(entry - rec["init_sl"])
        if rdist <= 0:
            return None
        d = 1 if side == "buy" else -1
        px = bid if side == "buy" else ask          # exit-side price

        if rec["mgmt"] == "running":
            hit = px >= rec["tp1"] if side == "buy" else px <= rec["tp1"]
            if not hit:
                # not at TP1 yet — EARLY break-even once price tags be_trigger_r.
                # Stays 'running' (SL only moves), so TP1 can still fire later.
                if self.mgmt.be_trigger_r > 0 and rec["sl"] is not None:
                    fav_r = d * (px - entry) / rdist
                    be = entry + d * self.mgmt.be_buffer_r * rdist
                    improves = (be - rec["sl"]) > 0 if side == "buy" \
                        else (rec["sl"] - be) > 0
                    if fav_r >= self.mgmt.be_trigger_r and improves:
                        self.broker.modify_sl_tp(ticket, sl=be)
                        self.store.update_position_sl(ticket, be)
                        self.store.write_audit("manage", "be_locked",
                                               "early break-even",
                                               {"ticket": ticket, "be": round(be, 5),
                                                "at_r": round(fav_r, 2)})
                        return "be_locked"
                return None
            # bank a partial if BOTH slices stay >= a tradeable lot; else just BE
            part = round(rec["volume"] * self.mgmt.partial_pct, 2)
            banked = 0.0
            if part >= 0.01 and round(rec["volume"] - part, 2) >= 0.01:
                banked = self.broker.partial_close_position(ticket, part)
                new_vol = round(rec["volume"] - part, 2)
            else:
                new_vol = rec["volume"]
            be = entry + d * self.mgmt.be_buffer_r * rdist
            self.broker.modify_sl_tp(ticket, sl=be)
            self.store.apply_tp1(ticket, new_vol, be)
            self.store.write_audit("manage", "tp1_hit", "partial+breakeven",
                                   {"ticket": ticket, "banked": round(banked, 2),
                                    "be": be, "runner": new_vol})
            if self.notifier is not None:
                self.notifier.send(embeds=[mgmt_embed(
                    symbol=rec["symbol"], event="TP1 hit — banked + SL→break-even",
                    detail=f"banked ${banked:,.2f} ({self.mgmt.partial_pct:.0%}), "
                           f"runner {new_vol} lots, SL→{be:.2f} toward TP2")])
            return "tp1_hit"

        if rec["mgmt"] == "tp1_hit":          # trail the runner's stop
            trail = px - d * self.mgmt.trail_r * rdist
            better = trail > rec["sl"] if side == "buy" else trail < rec["sl"]
            if better:
                self.broker.modify_sl_tp(ticket, sl=trail)
                self.store.update_position_sl(ticket, trail)
                return "trailed"
        return None


# --------------------------------------------------------------------------- #
# Live-DB end-to-end test on the PaperBroker (no MT5, no real money).          #
# Exercises: veto path, happy path (DB order 'filled' + position 'open'),      #
# and the FLATTEN_ALL actuator. Cleans up its own demo rows at the end.        #
#   ./env/Scripts/python.exe execution.py                                      #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone
    from ftmo_compliance_engine import (
        AccountProfile, Variant, Path, Phase,
    )
    from paper_broker import PaperBroker
    from pg_state_store import PgStateStore

    STRAT = "demo-exec"
    # Fixed Wednesday so the Standard weekend gate doesn't veto this mechanics test.
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)

    broker = PaperBroker(initial_balance=100_000.0)
    prof = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    engine = FtmoComplianceEngine(prof)
    engine.mark_reconciled()
    engine.roll_daily_baseline(now, midnight_balance=100_000)
    broker.set_price("XAUUSD", 2000.0)

    with PgStateStore() as store:
        ex = ExecutionEngine(broker, store, engine)

        def snap():
            return AccountSnapshot(broker.balance(), broker.equity(),
                                   broker.open_risk_to_sl())

        # (1) VETO: a huge order whose worst case pierces the daily soft floor.
        big = OrderRequest(STRAT, "XAUUSD", "buy", 5.0, 1950.0, 2100.0,
                           risk_to_sl=5_000.0)
        r1 = ex.submit(big, snap(), now)
        print(f"(1) big order -> submitted={r1.submitted} reason={r1.verdict.reason.value}")
        assert not r1.submitted

        # (2) HAPPY PATH: a sized order that passes the gate -> filled.
        ok = OrderRequest(STRAT, "XAUUSD", "buy", 1.0, 1950.0, 2100.0,
                          risk_to_sl=2_000.0)
        r2 = ex.submit(ok, snap(), now)
        print(f"(2) sized order -> submitted={r2.submitted} ticket={r2.ticket} "
              f"coid={r2.client_order_id}")
        assert r2.submitted and r2.ticket

        row = store.conn.execute(
            "SELECT status, broker_ticket FROM orders WHERE client_order_id = %s",
            (r2.client_order_id,)).fetchone()
        pos = store.conn.execute(
            "SELECT status FROM positions WHERE broker_ticket = %s",
            (r2.ticket,)).fetchone()
        print(f"    DB order={row}  position={pos}")
        assert row[0] == "filled" and pos[0] == "open"

        # (3) FLATTEN: drive equity into the hard floor, then flatten.
        broker.set_price("XAUUSD", 1950.0)
        v = engine.evaluate(snap(), now)
        print(f"(3) px=1950 -> {v.action.value}/{v.reason.value}")
        if v.action == Action.FLATTEN_ALL:
            closed = ex.flatten_all(reason=v.reason.value)
            print(f"    flatten_all -> {closed}; balance {broker.balance():,.2f}")
            posn = store.conn.execute(
                "SELECT status FROM positions WHERE broker_ticket = %s",
                (r2.ticket,)).fetchone()
            print(f"    DB position now: {posn}")
            assert posn[0] == "closed"

        # cleanup demo rows (keep the append-only audit log).
        store.conn.execute("DELETE FROM positions WHERE client_order_id IN "
                           "(SELECT client_order_id FROM orders WHERE strategy_id=%s)",
                           (STRAT,))
        store.conn.execute("DELETE FROM orders WHERE strategy_id = %s", (STRAT,))
        print("\nExecution Engine end-to-end OK (demo rows cleaned up).")
