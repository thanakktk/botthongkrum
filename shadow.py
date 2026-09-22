"""
Shadow Monitor (Phase A) — paper-trade the bench
======================================================================
The 9 strategies that failed out-of-sample are benched, NOT deleted. Markets are
cyclical: a long sideways stretch could revive mean-reversion just as a trend
stretch favours breakouts. So each benched technique keeps generating signals in
the background; we paper-trade them (no real orders, no money) and record the
hypothetical outcome. The dashboard then shows each benched strategy's rolling
win-rate / expectancy, so a regime shift that wakes one up is VISIBLE.

Promotion stays MANUAL on purpose — auto-promoting on a short hot streak is the
same curve-fit trap the Phase-2 study just escaped. A human (or a future, strict,
sample-gated rule) decides when to move a strategy back onto the live roster.

Resolution model (deliberately simple, comparable to the live R profile):
  * entry = the signal's entry, stop = the signal's SL (defines R)
  * target = entry + 2.5R  (the system's TP2)
  * each tick, mark the open paper-trade against the live mid price; close at
    whichever of SL / 2.5R-target the price reaches first.

This module is wrapped so it can NEVER disrupt the live trading loop.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from strategies import Strategy


class ShadowTracker:
    def __init__(self, store, strategies: Sequence[Strategy],
                 symbols: Sequence[str], tf: str = "H1", tp2_r: float = 2.5):
        self.store = store
        self.strategies = list(strategies)     # the benched (non-roster) techniques
        self.symbols = list(symbols)
        self.tf = tf
        self.tp2_r = tp2_r

    def tick(self, broker, now: datetime) -> None:
        """Manage open paper-trades, then look for new benched signals. Any error
        is swallowed — the shadow monitor must never affect real trading."""
        try:
            self._manage(broker)
        except Exception:
            pass
        try:
            self._scan(broker, now)
        except Exception:
            pass

    # ----- resolve open paper-trades against the live price ---------------- #
    def _manage(self, broker) -> None:
        for p in self.store.shadow_open():
            try:
                bid, ask = broker.quote(p["symbol"])
            except Exception:
                continue
            px = (bid + ask) / 2 if bid and ask else (bid or ask)
            if not px:
                continue
            entry, sl, tp, side = p["entry"], p["sl"], p["tp"], p["side"]
            rdist = abs(entry - sl) or 1e-9
            hit = reason = None
            if side == "buy":
                if px <= sl:
                    hit, reason = sl, "sl"
                elif px >= tp:
                    hit, reason = tp, "tp"
            else:
                if px >= sl:
                    hit, reason = sl, "sl"
                elif px <= tp:
                    hit, reason = tp, "tp"
            if hit is not None:
                d = 1 if side == "buy" else -1
                r_mult = d * (hit - entry) / rdist
                self.store.shadow_close(p["id"], p["strategy_id"], p["symbol"],
                                        side, entry, hit, r_mult, reason,
                                        p["opened_at"])

    # ----- open a new paper-trade when a benched technique fires ----------- #
    def _scan(self, broker, now: datetime) -> None:
        for sym in self.symbols:
            try:
                bars = broker.get_bars(sym, self.tf, 200)
            except Exception:
                bars = []
            if not bars:
                continue
            for st in self.strategies:
                if self.store.shadow_has(st.id, sym):     # one open per technique/sym
                    continue
                try:
                    sig = st.generate(sym, bars, now)
                except Exception:
                    continue
                if sig is None or sig.is_expired(now):
                    continue
                rdist = abs(sig.entry - sig.sl)
                if rdist <= 0:
                    continue
                d = 1 if sig.direction.value == "buy" else -1
                tp = sig.entry + d * self.tp2_r * rdist
                self.store.shadow_add(st.id, sym, sig.direction.value, self.tf,
                                      sig.entry, sig.sl, tp)


# --------------------------------------------------------------------------- #
# Self-test: drive a benched strategy's paper-trade to its target on a         #
# PaperBroker (no DB schema needed beyond shadow_* tables; no MT5, no money).   #
#   ./env/Scripts/python.exe shadow.py                                         #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone, timedelta
    from signals import Bar
    from paper_broker import PaperBroker
    from pg_state_store import PgStateStore
    from strategies import select_strategies

    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    broker = PaperBroker(initial_balance=100_000.0)
    # a clean uptrend so a trend technique fires a BUY on the served bars
    closes = [100 + i * 1.0 for i in range(60)]
    bars = [Bar(now + timedelta(hours=i), c, c + 0.5, c - 0.5, c)
            for i, c in enumerate(closes)]
    broker.set_bars("XAUUSD", bars, timeframe="H1")
    broker.set_price("XAUUSD", closes[-1])

    with PgStateStore() as store:
        # clean any leftovers from a prior run
        store.conn.execute("DELETE FROM shadow_positions WHERE symbol='XAUUSD'")
        store.conn.execute("DELETE FROM shadow_trades WHERE symbol='XAUUSD'")
        trk = ShadowTracker(store, select_strategies(["macd_trend", "roc_momentum"]),
                            ["XAUUSD"], tf="H1")
        trk.tick(broker, now)
        opened = store.shadow_open()
        print("opened paper-trades:", [(p["strategy_id"], p["side"],
                                        round(p["entry"], 1), round(p["tp"], 1))
                                       for p in opened])
        if opened:
            # jump price to the first trade's target -> should close as a 'tp' win
            p = opened[0]
            broker.set_price("XAUUSD", p["tp"] + (1 if p["side"] == "buy" else -1))
            trk.tick(broker, now + timedelta(hours=1))
            stats = store.shadow_stats()
            print("stats after target hit:", {k: v for k, v in stats.items()
                                              if k == p["strategy_id"]})
            assert store.shadow_stats().get(p["strategy_id"], {}).get("n", 0) >= 1
        # cleanup
        store.conn.execute("DELETE FROM shadow_positions WHERE symbol='XAUUSD'")
        store.conn.execute("DELETE FROM shadow_trades WHERE symbol='XAUUSD'")
    print("Shadow monitor OK.")
