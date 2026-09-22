"""
MT5 Broker adapter
======================================================================
Concrete BrokerGateway backed by the live MetaTrader 5 terminal. This is
the read side the Reconciler needs to treat the broker as source-of-truth.
It NEVER sends an order — order placement belongs to the Execution Engine.

Idempotency note: we tag every order we send with a client_order_id placed
in the MT5 order *comment*. MT5 comments are short (~31 chars), so a full
36-char UUID does NOT fit — the Execution Engine must write a compact token
(e.g. the UUID's first segment, or a base62 sequence) and keep the full
UUID<->token map in our DB. `order_was_filled` matches on that token.

Timezone caveat: MT5 deal/position `.time` is a unix timestamp expressed in
the *broker server* clock (FTMO = CET/CEST), not true UTC. For the
`balance_at_cet_midnight` reconstruction this is actually convenient (the
boundary we want IS server-time midnight), but VERIFY the exact offset on a
real trade before trusting reconstructed anchors. The current Free-Trial
account has no trade history, so reconstruction returns the initial deposit.
"""

from __future__ import annotations

import os
from datetime import datetime, date, time, timezone
from typing import Optional, Sequence

from dotenv import load_dotenv
import MetaTrader5 as mt5

from ftmo_compliance_engine import FTMO_TZ
from state_reconciliation import BrokerGateway, BrokerPosition
from signals import Bar

load_dotenv()

# Look back this far when scanning full account history (account inception).
_HISTORY_EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Identifies orders sent by this bot (written to every order's `magic`).
MT5_MAGIC = 525601


_TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600,
               "H4": 14400, "D1": 86400}


def _timeframe_const(name: str):
    return {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }.get(name, mt5.TIMEFRAME_M5)


class Mt5Error(RuntimeError):
    pass


def _coid_from_comment(comment: Optional[str]) -> Optional[str]:
    c = (comment or "").strip()
    return c or None


def coid_tag(client_order_id: str) -> str:
    """Compact, broker-safe token written into the MT5 order comment. A full UUID
    (36 chars, with hyphens) is rejected by the terminal as an invalid comment, so
    we store the first 16 hex chars (no hyphens). The full UUID stays the DB key;
    `order_was_filled` matches a broker fill back via this same tag."""
    return (client_order_id or "").replace("-", "")[:16]


class Mt5Broker(BrokerGateway):
    def __init__(self, login: int | None = None, password: str | None = None,
                 server: str | None = None, path: str | None = None):
        self.login = int(login or os.getenv("MT5_LOGIN", "0"))
        self.password = password or os.getenv("MT5_PASSWORD", "")
        self.server = server or os.getenv("MT5_SERVER", "")
        self.path = path or os.getenv("MT5_TERMINAL_PATH") or None
        self._connected = False

    # ----- lifecycle ------------------------------------------------------- #
    def connect(self) -> "Mt5Broker":
        kwargs = {"login": self.login, "password": self.password,
                  "server": self.server}
        if self.path:
            kwargs["path"] = self.path
        if not mt5.initialize(**kwargs):
            code, msg = mt5.last_error()
            raise Mt5Error(f"initialize() failed: ({code}) {msg}")
        if mt5.account_info() is None:
            code, msg = mt5.last_error()
            mt5.shutdown()
            raise Mt5Error(f"login failed / account_info None: ({code}) {msg}")
        self._connected = True
        return self

    def shutdown(self) -> None:
        if self._connected:
            mt5.shutdown()
            self._connected = False

    def __enter__(self) -> "Mt5Broker":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.shutdown()

    def _acct(self):
        info = mt5.account_info()
        if info is None:
            code, msg = mt5.last_error()
            raise Mt5Error(f"account_info() None: ({code}) {msg}")
        return info

    # ----- BrokerGateway --------------------------------------------------- #
    def balance(self) -> float:
        return float(self._acct().balance)

    def equity(self) -> float:
        return float(self._acct().equity)

    def quote(self, symbol: str) -> tuple[float, float]:
        """(bid, ask) for live TP1/trailing checks."""
        t = mt5.symbol_info_tick(symbol)
        return (float(t.bid), float(t.ask)) if t else (0.0, 0.0)

    def open_risk_to_sl(self) -> float:
        """Total additional loss (positive $) if EVERY open position hit its stop
        loss from here. Positions with no SL contribute nothing here (unbounded —
        the sizing/execution layer must refuse to leave a position SL-less)."""
        total = 0.0
        for p in mt5.positions_get() or ():
            if not p.sl:
                continue
            action = (mt5.ORDER_TYPE_BUY if p.type == mt5.POSITION_TYPE_BUY
                      else mt5.ORDER_TYPE_SELL)
            profit = mt5.order_calc_profit(action, p.symbol, p.volume,
                                           p.price_open, p.sl)
            if profit is not None and profit < 0:
                total += -float(profit)
        return total

    def realized_pnl(self, position_id: int) -> Optional[float]:
        """Net realized P&L of a CLOSED position (profit + swap + commission + fee
        across all its deals). Used to attribute broker-side SL/TP closes to the
        League. None if the position has no deal history.

        IMPORTANT: use the position-ONLY overload `history_deals_get(position=...)`.
        Passing date_from/date_to alongside `position=` makes MT5 IGNORE the
        position filter and return the wrong rows (it returned the +100,000
        initial-balance deal once), so this MUST NOT take date arguments."""
        deals = mt5.history_deals_get(position=position_id)
        if not deals:
            return None
        return sum(float(d.profit) + float(d.swap) + float(d.commission)
                   + float(getattr(d, "fee", 0.0) or 0.0) for d in deals)

    def estimate_risk(self, symbol: str, side: str, volume: float,
                      entry: float, sl: float) -> float:
        """$ lost if a hypothetical order filled at `entry` is stopped at `sl`."""
        order_type = (mt5.ORDER_TYPE_BUY if side == "buy"
                      else mt5.ORDER_TYPE_SELL)
        profit = mt5.order_calc_profit(order_type, symbol, volume, entry, sl)
        if profit is None or profit >= 0:
            return 0.0
        return -float(profit)

    def notional_per_lot(self, symbol: str, price: float) -> Optional[float]:
        """$ notional of 1.0 lot at `price` (contract size × price). Lets the
        sizer cap position notional so a tight SL can't balloon the volume."""
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        return float(getattr(info, "trade_contract_size", 1.0)) * float(price)

    # When True, get_bars() drops the still-forming last candle so strategies
    # only ever see CLOSED bars — exactly what the backtester feeds them. The
    # trading loop turns this on; the dashboard's live chart keeps the default.
    closed_bars_only: bool = False

    def get_bars(self, symbol: str, timeframe: str = "M5",
                 count: int = 200) -> list[Bar]:
        if not mt5.symbol_select(symbol, True):
            return []
        rates = mt5.copy_rates_from_pos(symbol, _timeframe_const(timeframe), 0,
                                        count + 1 if self.closed_bars_only else count)
        if rates is None:
            return []
        if self.closed_bars_only and len(rates):
            tick = mt5.symbol_info_tick(symbol)
            secs = _TF_SECONDS.get(timeframe.upper(), 0)
            # position 0 is the live candle unless it has already closed
            # (server clock: bar time and tick time share it)
            if tick is not None and int(rates[-1]["time"]) + secs > int(tick.time):
                rates = rates[:-1]
            rates = rates[-count:]
        return [Bar(time=datetime.fromtimestamp(r["time"], tz=timezone.utc),
                    open=float(r["open"]), high=float(r["high"]),
                    low=float(r["low"]), close=float(r["close"]),
                    volume=float(r["tick_volume"])) for r in rates]

    def open_positions(self) -> Sequence[BrokerPosition]:
        raw = mt5.positions_get() or ()
        out: list[BrokerPosition] = []
        for p in raw:
            side = "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell"
            out.append(BrokerPosition(
                ticket=int(p.ticket),
                symbol=p.symbol,
                side=side,
                volume=float(p.volume),
                open_price=float(p.price_open),
                sl=float(p.sl) if p.sl else None,
                tp=float(p.tp) if p.tp else None,
                client_order_id=_coid_from_comment(p.comment),
                # server-time unix ts -> see module docstring caveat
                opened_at=datetime.fromtimestamp(p.time, tz=timezone.utc),
            ))
        return out

    @staticmethod
    def _comment_matches(comment: Optional[str], client_order_id: str) -> bool:
        """Match a stored comment back to a coid: it equals our compact tag, or
        (defensively) is a prefix of the full coid."""
        c = _coid_from_comment(comment)
        if not c:
            return False
        return c == coid_tag(client_order_id) or client_order_id.startswith(c)

    def order_was_filled(self, client_order_id: str) -> Optional[int]:
        """Return the broker ticket if an order tagged with this coid resulted in
        a position (open now, or filled in history); else None. Idempotency hinge."""
        # 1) still open?
        for p in mt5.positions_get() or ():
            if self._comment_matches(p.comment, client_order_id):
                return int(p.ticket)
        # 2) filled then maybe closed — scan history entry deals.
        deals = mt5.history_deals_get(_HISTORY_EPOCH, datetime.now(timezone.utc)) or ()
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_IN and \
                    self._comment_matches(d.comment, client_order_id):
                return int(d.position_id)
        return None

    def balance_at_cet_midnight(self, d: date) -> Optional[float]:
        """Closed balance as of 00:00 CET that opened day `d`.

        Reconstructed by walking BACK from the current closed balance and undoing
        every balance-affecting deal (realized P/L, swaps, commissions, fees, and
        deposit/withdrawal operations) that happened AFTER that midnight:

            balance(midnight) = current_balance - Σ(deal effects after midnight)

        This is robust where forward-summing fails: FTMO Free-Trial accounts carry
        NO initial-deposit deal in history, so summing deals from inception returns
        0. Working backward from the live balance needs neither a deposit deal nor
        the initial_capital constant. None if history can't be read.
        """
        midnight_utc = datetime.combine(d, time(0, 0), tzinfo=FTMO_TZ) \
            .astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        current_balance = float(self._acct().balance)
        if midnight_utc >= now:
            return current_balance  # day hasn't started yet; nothing to undo

        deals_after = mt5.history_deals_get(midnight_utc, now)
        if deals_after is None:
            code, _ = mt5.last_error()
            if code != 1:  # 1 == RES_S_OK; anything else is a real read error
                return None
            deals_after = ()
        undo = 0.0
        for x in deals_after:
            undo += float(x.profit) + float(x.swap) + float(x.commission) \
                + float(getattr(x, "fee", 0.0) or 0.0)
        return current_balance - undo

    # ----- order routing (Execution Engine side) -------------------------- #
    # NOTE: untested LIVE — requires the terminal's "Algo Trading" flag ON
    # (terminal_info().trade_allowed). Fully exercised via PaperBroker for now.
    def _filling_mode(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        allowed = getattr(info, "filling_mode", 0) if info else 0
        if allowed & getattr(mt5, "SYMBOL_FILLING_FOK", 1):
            return mt5.ORDER_FILLING_FOK
        if allowed & getattr(mt5, "SYMBOL_FILLING_IOC", 2):
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def place_market(self, *, symbol: str, side: str, volume: float,
                     sl: Optional[float] = None, tp: Optional[float] = None,
                     price: Optional[float] = None,
                     client_order_id: Optional[str] = None) -> int:
        if not mt5.symbol_select(symbol, True):
            raise Mt5Error(f"symbol_select({symbol}) failed: {mt5.last_error()}")
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise Mt5Error(f"no tick for {symbol}: {mt5.last_error()}")
        is_buy = side == "buy"
        px = float(price) if price is not None else (tick.ask if is_buy else tick.bid)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": px,
            "sl": float(sl) if sl else 0.0,
            "tp": float(tp) if tp else 0.0,
            "deviation": 20,
            "magic": MT5_MAGIC,
            "comment": coid_tag(client_order_id),   # compact, broker-safe token
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(symbol),
        }
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise Mt5Error(f"order_send rejected: "
                           f"{getattr(r, 'retcode', '?')} {getattr(r, 'comment', '')} "
                           f"{mt5.last_error()}")
        return int(r.order)

    def close_position(self, ticket: int, price: Optional[float] = None) -> float:
        rows = mt5.positions_get(ticket=ticket) or ()
        if not rows:
            return 0.0
        p = rows[0]
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        tick = mt5.symbol_info_tick(p.symbol)
        px = float(price) if price is not None else (tick.bid if is_buy else tick.ask)
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(ticket),
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": px,
            "deviation": 20,
            "magic": MT5_MAGIC,
            "comment": "flatten",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(p.symbol),
        }
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise Mt5Error(f"close rejected: {getattr(r, 'retcode', '?')} "
                           f"{mt5.last_error()}")
        return float(p.profit)

    def flatten_all(self) -> list[int]:
        closed: list[int] = []
        for p in mt5.positions_get() or ():
            self.close_position(int(p.ticket))
            closed.append(int(p.ticket))
        return closed

    def partial_close_position(self, ticket: int, volume: float) -> float:
        """Close `volume` lots of a position (banks TP1 profit); the runner keeps
        the same ticket with reduced volume."""
        rows = mt5.positions_get(ticket=ticket) or ()
        if not rows:
            return 0.0
        p = rows[0]
        vol = round(min(float(volume), float(p.volume)), 2)
        if vol <= 0:
            return 0.0
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        tick = mt5.symbol_info_tick(p.symbol)
        px = tick.bid if is_buy else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "position": int(ticket),
            "symbol": p.symbol, "volume": vol,
            "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
            "price": px, "deviation": 20, "magic": MT5_MAGIC,
            "comment": "partial_tp1", "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(p.symbol),
        }
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            raise Mt5Error(f"partial close rejected: {getattr(r, 'retcode', '?')} "
                           f"{mt5.last_error()}")
        return float(p.profit) * (vol / float(p.volume))

    def modify_sl_tp(self, ticket: int, sl: Optional[float] = None,
                     tp: Optional[float] = None) -> bool:
        """Move a position's SL (break-even / trailing) and/or TP."""
        rows = mt5.positions_get(ticket=ticket) or ()
        if not rows:
            return False
        p = rows[0]
        req = {
            "action": mt5.TRADE_ACTION_SLTP, "position": int(ticket),
            "symbol": p.symbol,
            "sl": float(sl) if sl is not None else float(p.sl),
            "tp": float(tp) if tp is not None else float(p.tp),
        }
        r = mt5.order_send(req)
        return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE


# --------------------------------------------------------------------------- #
# Smoke test: drive the real Reconciler against the live account (read-only).  #
#   ./env/Scripts/python.exe mt5_broker.py                                     #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import date as _date
    from ftmo_compliance_engine import (
        FtmoComplianceEngine, AccountProfile, Variant, Path, Phase,
        profile_from_env as _profile_from_env, config_from_env,
    )
    from state_reconciliation import (
        Reconciler, StateStore, LocalOrder, LocalPosition,
    )

    # Minimal in-memory store: empty local state, so the live broker position(s)
    # (if any) get adopted and today's baseline gets reconstructed.
    class MemStore(StateStore):
        def __init__(self):
            self._baseline: dict = {}
            self.last_baseline = None
        def local_orders(self): return []
        def local_open_positions(self): return []
        def adopt_position(self, p): print(f"    adopt {p.ticket} {p.symbol} {p.side} {p.volume}")
        def mark_position_closed(self, t): print(f"    close {t}")
        def set_order_status(self, coid, status, ticket): print(f"    order {coid} -> {status}")
        def baseline_for(self, d): return self._baseline.get(d)
        def roll_baseline(self, d, bal, source):
            self._baseline[d] = bal
            print(f"    baseline {d} = {bal} ({source})")
            return bal
        def write_audit(self, et, dec, reason, payload):
            print(f"    audit[{et}/{dec}] {reason} {payload}")

    with Mt5Broker() as broker:
        print(f"Connected. balance={broker.balance():,.2f} equity={broker.equity():,.2f}")
        pos = broker.open_positions()
        print(f"Open positions: {len(pos)}")
        for p in pos:
            print(f"  #{p.ticket} {p.symbol} {p.side} {p.volume} @ {p.open_price} "
                  f"coid={p.client_order_id}")

        today = datetime.now(FTMO_TZ).date()
        mb = broker.balance_at_cet_midnight(today)
        print(f"Reconstructed midnight balance for {today}: {mb}")

        print("\nRunning live reconciliation (read-only)...")
        engine = FtmoComplianceEngine(_profile_from_env(), config_from_env())
        result = Reconciler(broker, MemStore(), engine).run()
        print("\nResult:")
        print("  baseline source :", result.baseline_source, "=", result.midnight_balance)
        print("  reconciled/ok   :", result.ok, "| engine.reconciled =", engine.reconciled)
        if engine.reconciled:
            print("  daily floor     :", engine.real_daily_floor())
            print("  daily soft floor:", engine.daily_soft_floor())
            print("  overall floor   :", engine.real_overall_floor())
