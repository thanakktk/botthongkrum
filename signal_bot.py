"""
Advisory Signal Bot (separate Discord channel)
======================================================================
An ALERTS-ONLY companion to the trading bot. It never touches the account — it
reads live XAUUSD data and posts to its own Discord webhook:

  Time-based (session pings)         Condition-based (live signals)
  1. Asia outlook (+ daily overview) 4. Smart Structure  (order-block, H1)
  2. London outlook                  5. Momentum Scalp   (ROC momentum, M15)
  3. New York outlook                6. Volatility Breakout (squeeze, H1)
                                     7. Structural Swing (MACD trend, H4)

Each signal posts entry / SL / TP1·TP2·TP3 and the "move SL to break-even after
TP1" reminder. Every signal's outcome is tracked, and a daily SCOREBOARD posts
the REAL hit rate — no invented "70%". 24/7.

Honesty: these are the same technical strategies the 11-year study showed have a
THIN, regime-dependent edge (~50-55% reach TP1, NOT 70%). The scoreboard tells
the truth over time; treat the alerts as study aids, not guarantees.

    ./env/Scripts/python.exe signal_bot.py [--interval 60] [--symbol XAUUSD]
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

import db
from mt5_broker import Mt5Broker
from notifier import DiscordNotifier, GREEN, RED, BLUE, GOLD, GREY
from regime import efficiency_ratio
from strategies import (ema_series, OrderBlockRetest, RocMomentum,
                        BollingerSqueezeBreakout, MacdTrend)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# the 4 condition strategies (key, display, emoji, instance, timeframe)
CONDITION = [
    ("smart_structure", "Smart Structure", "🏗️", OrderBlockRetest(), "H1"),
    ("momentum_scalp", "Momentum Scalp", "⚡", RocMomentum(), "M15"),
    ("volatility_breakout", "Volatility Breakout", "💥", BollingerSqueezeBreakout(), "H1"),
    ("structural_swing", "Structural Swing", "🌊", MacdTrend(), "H4"),
]
# session pings: name, emoji, open-hour (UTC)
SESSIONS = [("Asia", "🌏", 0), ("London", "🇬🇧", 7), ("New York", "🗽", 12)]
EXPIRE_H = 24          # an unresolved signal expires after this many hours
SCOREBOARD_HOUR = 23   # UTC hour to post the daily scoreboard


class SignalBot:
    def __init__(self, broker, notifier: DiscordNotifier, symbol="XAUUSD"):
        self.broker = broker
        self.note = notifier
        self.symbol = symbol

    # ----- tiny DB helpers (own connection; advisory_* tables) ------------- #
    def _c(self):
        return db.connect(autocommit=True)

    def _posted(self, key: str) -> bool:
        with self._c() as c:
            if c.execute("SELECT 1 FROM advisory_posts WHERE post_key=%s",
                         (key,)).fetchone():
                return True
            c.execute("INSERT INTO advisory_posts (post_key) VALUES (%s) "
                      "ON CONFLICT DO NOTHING", (key,))
            return False

    def _has_open(self, c, strategy: str) -> bool:
        return c.execute("SELECT 1 FROM advisory_signals WHERE strategy=%s "
                         "AND symbol=%s AND status='open'",
                         (strategy, self.symbol)).fetchone() is not None

    # ----- main tick ------------------------------------------------------- #
    def _market_fresh(self, max_age_min: int = 180) -> bool:
        """True only if the latest bar is recent — keeps the bot silent when the
        market is closed (weekend gold) instead of alerting on stale prices."""
        bars = self.broker.get_bars(self.symbol, "M15", 2)
        if not bars:
            return False
        last = bars[-1].time
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() < max_age_min * 60

    def tick(self, now: datetime) -> None:
        if not self._market_fresh():
            return                      # market closed / stale -> stay quiet
        for fn in (self._update_open, self._session_pings, self._scan,
                   self._scoreboard):
            try:
                fn(now)
            except Exception as e:                       # advisory must not crash
                print(f"[signal_bot] {fn.__name__} error: {e}")

    # ----- market context -------------------------------------------------- #
    def _bias(self):
        bars = self.broker.get_bars(self.symbol, "H4", 60)
        if len(bars) < 52:
            return None
        closes = [b.close for b in bars]
        e20, e50 = ema_series(closes, 20)[-1], ema_series(closes, 50)[-1]
        er = efficiency_ratio(closes, 20) or 0.0
        up = e20 > e50
        strength = ("strong" if er > 0.4 else "moderate" if er > 0.25
                    else "weak / choppy")
        return {"dir": "🟢 Bullish" if up else "🔴 Bearish", "up": up,
                "er": er, "strength": strength, "price": closes[-1]}

    def _session_pings(self, now: datetime) -> None:
        for name, emoji, hr in SESSIONS:
            if now.hour < hr:
                continue
            key = f"outlook:{now:%Y-%m-%d}:{name}:{self.symbol}"
            if self._posted(key):
                continue
            b = self._bias()
            if not b:
                return
            extra = ("📊 **Daily overview** — bias for the session ahead\n"
                     if name == "Asia" else "")
            self.note.send(embeds=[{
                "title": f"{emoji} {name} session — {self.symbol}",
                "description": (f"{extra}Price **{b['price']:.2f}**\n"
                               f"H4 trend: **{b['dir']}** ({b['strength']}, "
                               f"ER {b['er']:.2f})\n"
                               f"_{'Favor longs / pullback buys' if b['up'] else 'Favor shorts / rally sells'} "
                               f"while the H4 bias holds._"),
                "color": GREEN if b["up"] else RED,
                "footer": {"text": "session outlook • not a trade order"},
                "timestamp": now.isoformat()}])
            print(f"[signal_bot] posted {name} outlook")

    # ----- condition signals ----------------------------------------------- #
    def _scan(self, now: datetime) -> None:
        with self._c() as c:
            for key, label, emoji, strat, tf in CONDITION:
                if self._has_open(c, key):
                    continue
                bars = self.broker.get_bars(self.symbol, tf, 200)
                if len(bars) < 60:
                    continue
                try:
                    sig = strat.generate(self.symbol, bars, now)
                except Exception:
                    continue
                if sig is None or sig.is_expired(now):
                    continue
                self._post_signal(c, key, label, emoji, tf, sig)

    def _post_signal(self, c, key, label, emoji, tf, sig) -> None:
        entry, sl = sig.entry, sig.sl
        r = abs(entry - sl)
        if r <= 0:
            return
        d = 1 if sig.direction.value == "buy" else -1
        tp1, tp2, tp3 = (entry + d * r, entry + d * 2 * r, entry + d * 3 * r)
        c.execute(
            "INSERT INTO advisory_signals "
            "(strategy,symbol,side,entry,sl,tp1,tp2,tp3) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (key, self.symbol, sig.direction.value, entry, sl, tp1, tp2, tp3))
        buy = sig.direction.value == "buy"
        self.note.send(embeds=[{
            "title": f"{emoji} {('🟢 BUY NOW' if buy else '🔴 SELL NOW')} "
                     f"{self.symbol} — {label}",
            "description": (
                f"**Entry** `{entry:.2f}`  ·  TF {tf}  ·  conf {sig.confidence:.0%}\n"
                f"🛑 **SL** `{sl:.2f}`  (risk = {r:.2f})\n"
                f"🎯 **TP1** `{tp1:.2f}` (+1R)\n"
                f"🎯 **TP2** `{tp2:.2f}` (+2R)\n"
                f"🎯 **TP3** `{tp3:.2f}` (+3R)\n"
                f"⚙️ _หลังชน TP1 → เลื่อน SL มาหน้าทุน (break-even)_"),
            "color": GREEN if buy else RED,
            "footer": {"text": "advisory signal • manage your own risk"},
            "timestamp": datetime.now(timezone.utc).isoformat()}])
        print(f"[signal_bot] SIGNAL {label} {sig.direction.value} @ {entry:.2f}")

    # ----- outcome tracking ------------------------------------------------ #
    def _update_open(self, now: datetime) -> None:
        bid, ask = self.broker.quote(self.symbol)
        px = (bid + ask) / 2 if bid and ask else (bid or ask)
        if not px:
            return
        with self._c() as c:
            # resolved_at IS NULL is REQUIRED: _finish() marks a closed-at-BE
            # signal with status 'tp1'/'tp2' (the banked level) — both of which
            # are still in the IN(...) set — so without this guard every closed
            # signal is re-selected and re-"closed" each tick, spamming the
            # "closed at BE (banked +NR)" alert forever. resolved_at is set only
            # by _finish()/expire, so it cleanly excludes terminal rows while
            # still tracking running tp1/tp2 milestones (resolved_at = NULL).
            rows = c.execute(
                "SELECT id,strategy,side,entry,sl,tp1,tp2,tp3,status,posted_at "
                "FROM advisory_signals WHERE status IN ('open','tp1','tp2') "
                "AND resolved_at IS NULL AND symbol=%s", (self.symbol,)).fetchall()
            for (sid, strat, side, entry, sl, tp1, tp2, tp3, st, posted) in rows:
                self._resolve_one(c, now, px, sid, strat, side, entry, sl,
                                  tp1, tp2, tp3, st, posted)

    def _resolve_one(self, c, now, px, sid, strat, side, entry, sl,
                     tp1, tp2, tp3, st, posted) -> None:
        buy = side == "buy"
        hit = (lambda lvl: px >= lvl) if buy else (lambda lvl: px <= lvl)
        stop = (px <= sl) if buy else (px >= sl)
        reached = {"open": 0, "tp1": 1, "tp2": 2}.get(st, 0)
        if hit(tp3):
            self._finish(c, sid, "tp3", 3.0, strat, "🎯🎯🎯 TP3 +3R")
        elif hit(tp2) and reached < 2:
            c.execute("UPDATE advisory_signals SET status='tp2' WHERE id=%s", (sid,))
            self._milestone(strat, "🎯 TP2 hit (+2R) — trail / lock profit")
        elif hit(tp1) and reached < 1:
            c.execute("UPDATE advisory_signals SET status='tp1' WHERE id=%s", (sid,))
            self._milestone(strat, "🎯 TP1 hit (+1R) — เลื่อน SL มาหน้าทุน")
        elif stop:
            # after TP1 the stop is at break-even, so reaching it ~= the best TP hit
            res = float(reached) if reached else -1.0
            tag = "🛑 SL" if reached == 0 else f"↩️ closed at BE (banked +{reached}R)"
            self._finish(c, sid, "sl" if reached == 0 else f"tp{reached}", res,
                         strat, tag)
        elif now - (posted if posted.tzinfo else posted.replace(
                tzinfo=timezone.utc)) > timedelta(hours=EXPIRE_H):
            c.execute("UPDATE advisory_signals SET status='expired',"
                      "resolved_at=now() WHERE id=%s", (sid,))

    def _finish(self, c, sid, status, result_r, strat, tag) -> None:
        c.execute("UPDATE advisory_signals SET status=%s, result_r=%s,"
                  "resolved_at=now() WHERE id=%s", (status, result_r, sid))
        col = GREEN if result_r > 0 else RED
        self.note.send(embeds=[{"title": f"{tag} — {self.symbol}",
                                "description": f"Signal `{strat}` closed "
                                f"**{result_r:+.0f}R**", "color": col}])

    def _milestone(self, strat, msg) -> None:
        self.note.send(embeds=[{"description": f"`{strat}` · {msg}", "color": GOLD}])

    # ----- daily scoreboard ------------------------------------------------ #
    def _scoreboard(self, now: datetime) -> None:
        if now.hour < SCOREBOARD_HOUR:
            return
        if self._posted(f"scoreboard:{now:%Y-%m-%d}:{self.symbol}"):
            return
        with self._c() as c:
            rows = c.execute(
                "SELECT strategy,status,result_r FROM advisory_signals "
                "WHERE posted_at::date = %s AND result_r IS NOT NULL",
                (now.date(),)).fetchall()
        if not rows:
            return
        wins = [r for r in rows if r[2] > 0]
        losses = [r for r in rows if r[2] <= 0]
        total_r = sum(r[2] for r in rows)
        wr = len(wins) / len(rows) * 100 if rows else 0
        per: dict = {}
        for strat, _, rr in rows:
            d = per.setdefault(strat, [0, 0.0])
            d[0] += 1
            d[1] += rr
        lines = "\n".join(f"`{s}`: {n} signals, {tot:+.1f}R"
                          for s, (n, tot) in per.items())
        self.note.send(embeds=[{
            "title": f"📋 Daily Scoreboard — {now:%Y-%m-%d}",
            "description": (f"Signals resolved: **{len(rows)}**\n"
                           f"✅ Wins (≥TP1): **{len(wins)}**  ·  "
                           f"❌ Losses: **{len(losses)}**\n"
                           f"**Win rate: {wr:.0f}%**  ·  Net: **{total_r:+.1f}R**\n\n"
                           f"{lines}"),
            "color": GREEN if total_r > 0 else RED,
            "footer": {"text": "real results — no invented win rate"}}])
        print(f"[signal_bot] posted scoreboard ({len(rows)} signals, WR {wr:.0f}%)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--ticks", type=int, default=None)
    args = ap.parse_args()

    note = DiscordNotifier(os.getenv("ADVISORY_WEBHOOK"))
    if not note.enabled:
        print("ADVISORY_WEBHOOK missing in .env"); return 1
    print(f"Advisory signal bot: {args.symbol} every {args.interval}s "
          f"(Discord {'ON' if note.enabled else 'OFF'})")
    with Mt5Broker() as broker:
        bot = SignalBot(broker, note, args.symbol)
        i = 0
        while args.ticks is None or i < args.ticks:
            try:
                bot.tick(datetime.now(timezone.utc))
            except Exception as e:
                print(f"[signal_bot] tick failed: {e}")
            i += 1
            if args.ticks is not None and i >= args.ticks:
                break
            time.sleep(args.interval)
    note.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
