"""
Paper / Simulation Broker adapter (Pillar 6)
======================================================================
An in-memory BrokerGateway with the SAME interface as Mt5Broker, plus the
order-placement methods the Execution Engine needs. It lets the whole spine
(reconcile -> compliance -> execution) run with NO real money and without the
terminal's Algo-Trading flag.

P/L model (honest but simple): each symbol has a "value per 1.0 price unit per
1.0 lot" in account currency. Floating/realized P/L for a position is

    direction * (price - open_price) * volume * value_per_price_per_lot
    (direction: +1 buy, -1 sell)

Defaults cover a few common FTMO symbols; override via `specs=`. This is a
sim, not a tick-accurate backtester — spreads/slippage/commission are opt-in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, time, timezone
from typing import Optional, Sequence

from ftmo_compliance_engine import FTMO_TZ
from state_reconciliation import BrokerGateway, BrokerPosition
from signals import Bar

# value of a 1.0 price move, per 1.0 lot, in account currency (USD here).
# == MT5 trade_contract_size for these (verified live 2026-06-20).
DEFAULT_SPECS: dict[str, float] = {
    "XAUUSD": 100.0,        # 1 lot = 100 oz  -> $1 move = $100/lot (verified)
    "XAGUSD": 5000.0,       # 1 lot = 5000 oz
    "EURUSD": 100_000.0,    # 1 lot = 100k    -> 1 pip(0.0001) = $10/lot
    "GBPUSD": 100_000.0,
    "USDJPY": 100_000.0,    # quote-ccy nuance ignored for the sim
    "BTCUSD": 1.0,          # 1 lot = 1 BTC   -> $1 move = $1/lot (verified)
    "ETHUSD": 1.0,          # 1 lot = 1 ETH
}


def _dir(side: str) -> int:
    return 1 if side == "buy" else -1


@dataclass
class _Pos:
    ticket: int
    coid: Optional[str]
    symbol: str
    side: str
    volume: float
    open_price: float
    sl: Optional[float]
    tp: Optional[float]
    opened_at: datetime


@dataclass
class _Deal:
    when: datetime
    position_id: int
    profit: float
    comment: Optional[str]
    entry_in: bool          # True = position-opening deal


class PaperBroker(BrokerGateway):
    def __init__(self, initial_balance: float = 100_000.0,
                 specs: Optional[dict[str, float]] = None,
                 commission_per_lot: float = 0.0):
        self._balance = float(initial_balance)
        self.specs = dict(DEFAULT_SPECS, **(specs or {}))
        self.commission_per_lot = commission_per_lot
        self._prices: dict[str, float] = {}
        self._bars: dict[tuple, list[Bar]] = {}   # (symbol, timeframe) -> bars
        self._pos: dict[int, _Pos] = {}
        self._deals: list[_Deal] = []
        self._next_ticket = 1000

    # ----- lifecycle (match Mt5Broker so it's a drop-in) ------------------- #
    def connect(self) -> "PaperBroker":
        return self  # nothing to connect — purely in-memory sim

    def shutdown(self) -> None:
        pass  # nothing to release — purely in-memory sim

    def __enter__(self) -> "PaperBroker":
        return self

    def __exit__(self, *exc) -> None:
        pass

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _vpp(self, symbol: str) -> float:
        if symbol not in self.specs:
            raise KeyError(f"no sim spec for {symbol}; pass specs={{'{symbol}': ...}}")
        return self.specs[symbol]

    def _price(self, symbol: str, fallback: float) -> float:
        return self._prices.get(symbol, fallback)

    def _floating(self, p: _Pos) -> float:
        px = self._price(p.symbol, p.open_price)
        return _dir(p.side) * (px - p.open_price) * p.volume * self._vpp(p.symbol)

    # ----- price + bar feed (sim only) ------------------------------------ #
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = float(price)

    def quote(self, symbol: str) -> tuple[float, float]:
        px = self._prices.get(symbol, 0.0)
        return (px, px)

    def set_bars(self, symbol: str, bars: Sequence[Bar],
                 timeframe: str = "M5") -> None:
        self._bars[(symbol, timeframe)] = list(bars)
        if bars:
            self.set_price(symbol, bars[-1].close)

    def get_bars(self, symbol: str, timeframe: str = "M5",
                 count: int = 200) -> list[Bar]:
        return self._bars.get((symbol, timeframe), [])[-count:]

    def estimate_risk(self, symbol: str, side: str, volume: float,
                      entry: float, sl: float) -> float:
        """$ lost if a hypothetical order filled at `entry` is stopped at `sl`."""
        loss = _dir(side) * (sl - entry) * volume * self._vpp(symbol)
        return -loss if loss < 0 else 0.0

    def notional_per_lot(self, symbol: str, price: float) -> Optional[float]:
        """$ notional of 1.0 lot at `price`. For these specs value_per_price_per_lot
        equals the contract size, so notional = vpp × price."""
        if symbol not in self.specs:
            return None
        return self._vpp(symbol) * float(price)

    def realized_pnl(self, position_id: int) -> Optional[float]:
        """Net realized P&L of a closed position (sum of its deal effects)."""
        deals = [d for d in self._deals if d.position_id == position_id]
        if not deals:
            return None
        return sum(d.profit for d in deals)

    # ----- order placement (used by the Execution Engine) ----------------- #
    def place_market(self, *, symbol: str, side: str, volume: float,
                     sl: Optional[float] = None, tp: Optional[float] = None,
                     price: Optional[float] = None,
                     client_order_id: Optional[str] = None) -> int:
        assert side in ("buy", "sell")
        fill = float(price if price is not None else self._price(symbol, 0.0))
        if fill <= 0:
            raise ValueError(f"no price for {symbol}; call set_price() or pass price=")
        ticket = self._next_ticket
        self._next_ticket += 1
        now = self._now()
        self._pos[ticket] = _Pos(ticket, client_order_id, symbol, side, float(volume),
                                 fill, sl, tp, now)
        commission = -self.commission_per_lot * volume
        if commission:
            self._balance += commission
        self._deals.append(_Deal(now, ticket, commission, client_order_id, entry_in=True))
        return ticket

    def close_position(self, ticket: int, price: Optional[float] = None) -> float:
        p = self._pos.pop(ticket)
        px = float(price if price is not None else self._price(p.symbol, p.open_price))
        pnl = _dir(p.side) * (px - p.open_price) * p.volume * self._vpp(p.symbol)
        self._balance += pnl
        self._deals.append(_Deal(self._now(), ticket, pnl, p.coid, entry_in=False))
        return pnl

    def flatten_all(self) -> list[int]:
        closed = []
        for ticket in list(self._pos):
            self.close_position(ticket)
            closed.append(ticket)
        return closed

    def partial_close_position(self, ticket: int, volume: float,
                               price: Optional[float] = None) -> float:
        """Close `volume` lots of a position, realizing that slice's P&L; the rest
        keeps the same ticket. Used to bank profit at TP1."""
        p = self._pos.get(ticket)
        if p is None:
            return 0.0
        vol = min(float(volume), p.volume)
        px = float(price if price is not None else self._price(p.symbol, p.open_price))
        pnl = _dir(p.side) * (px - p.open_price) * vol * self._vpp(p.symbol)
        self._balance += pnl
        self._deals.append(_Deal(self._now(), ticket, pnl, p.coid, entry_in=False))
        if vol >= p.volume - 1e-9:
            del self._pos[ticket]
        else:
            p.volume = round(p.volume - vol, 2)
        return pnl

    def modify_sl_tp(self, ticket: int, sl: Optional[float] = None,
                     tp: Optional[float] = None) -> bool:
        """Move the stop (break-even / trailing) and/or take-profit of a position."""
        p = self._pos.get(ticket)
        if p is None:
            return False
        if sl is not None:
            p.sl = float(sl)
        if tp is not None:
            p.tp = float(tp)
        return True

    # ----- BrokerGateway -------------------------------------------------- #
    def balance(self) -> float:
        return self._balance

    def equity(self) -> float:
        return self._balance + sum(self._floating(p) for p in self._pos.values())

    def open_risk_to_sl(self) -> float:
        total = 0.0
        for p in self._pos.values():
            if p.sl is None:
                continue
            loss = _dir(p.side) * (p.sl - p.open_price) * p.volume * self._vpp(p.symbol)
            if loss < 0:
                total += -loss
        return total

    def open_positions(self) -> Sequence[BrokerPosition]:
        return [BrokerPosition(
            ticket=p.ticket, symbol=p.symbol, side=p.side, volume=p.volume,
            open_price=p.open_price, sl=p.sl, tp=p.tp,
            client_order_id=p.coid, opened_at=p.opened_at,
        ) for p in self._pos.values()]

    def order_was_filled(self, client_order_id: str) -> Optional[int]:
        for p in self._pos.values():
            if p.coid == client_order_id:
                return p.ticket
        for d in self._deals:
            if d.entry_in and d.comment == client_order_id:
                return d.position_id
        return None

    def balance_at_cet_midnight(self, d: date) -> Optional[float]:
        midnight_utc = datetime.combine(d, time(0, 0), tzinfo=FTMO_TZ) \
            .astimezone(timezone.utc)
        now = self._now()
        if midnight_utc >= now:
            return self._balance
        undo = sum(x.profit for x in self._deals if x.when >= midnight_utc)
        return self._balance - undo


# --------------------------------------------------------------------------- #
# Self-test: drive a position from profit into the FLATTEN zone, prove the     #
# compliance chain and the flatten actuator. (no DB, no MT5)                   #
#   ./env/Scripts/python.exe paper_broker.py                                   #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone as _tz
    from ftmo_compliance_engine import (
        FtmoComplianceEngine, AccountProfile, AccountSnapshot,
        Variant, Path, Phase, Action, Reason,
    )

    b = PaperBroker(initial_balance=100_000.0)
    prof = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000)
    eng = FtmoComplianceEngine(prof)
    eng.mark_reconciled()
    now = datetime.now(_tz.utc)
    eng.roll_daily_baseline(now, midnight_balance=100_000)
    print("floors: daily", eng.real_daily_floor(), "soft", eng.daily_soft_floor(),
          "hard", eng.daily_hard_floor(), "overall", eng.real_overall_floor())

    b.set_price("XAUUSD", 2000.0)
    t = b.place_market(symbol="XAUUSD", side="buy", volume=1.0,
                       sl=1950.0, tp=2100.0, client_order_id="sim-1")
    print(f"opened #{t}; order_was_filled('sim-1') -> {b.order_was_filled('sim-1')}")
    assert b.order_was_filled("sim-1") == t

    def snap() -> AccountSnapshot:
        return AccountSnapshot(b.balance(), b.equity(), b.open_risk_to_sl())

    for px in (2010.0, 1960.0, 1950.0):
        b.set_price("XAUUSD", px)
        s = snap()
        v = eng.evaluate(s, now)
        print(f"px={px}  equity={s.equity:,.2f}  risk@SL={s.open_risk_to_sl:,.2f}  "
              f"-> {v.action.value}/{v.reason.value}")
        if v.action == Action.FLATTEN_ALL:
            closed = b.flatten_all()
            print(f"  flatten_all -> closed {closed}; "
                  f"balance now {b.balance():,.2f}, positions {len(b.open_positions())}")

    assert eng.evaluate(snap(), now).reason in (Reason.DAILY_HARD, Reason.OVERALL_HARD)
    assert b.balance() == 95_000.0 and not b.open_positions()
    print("\nPaper-broker compliance + flatten chain OK.")
