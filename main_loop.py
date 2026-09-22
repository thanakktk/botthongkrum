"""
Main control loop (monitoring spine — does NOT trade yet)
======================================================================
Ties the v1 spine together:

  startup  : reconcile against broker truth (Pillar 5). Engine stays
             NOT_RECONCILED -> HALT_NEW until this succeeds.
  each tick: detect CET-day rollover and re-anchor the daily baseline;
             snapshot the account; ask the Compliance Engine (Pillar 0)
             for a verdict; act on it; write a heartbeat to the audit log.

No orders are placed or closed here. FLATTEN_ALL is logged as kill-switch
territory; wiring the Execution Engine / Watchdog to actually close
positions is the next milestone.

Run:
  ./env/Scripts/python.exe main_loop.py                 # loop forever @30s
  ./env/Scripts/python.exe main_loop.py --ticks 3 --interval 1   # bounded test
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from datetime import timedelta

from ftmo_compliance_engine import (
    FtmoComplianceEngine, AccountProfile, AccountSnapshot, Action, Reason,
    NewsEvent, Variant, Path, Phase, FTMO_TZ,
    profile_from_env, config_from_env, rules_summary,
)
from state_reconciliation import Reconciler
from mt5_broker import Mt5Broker
from pg_state_store import PgStateStore
from execution import ExecutionEngine
from strategies import atr

load_dotenv()


def _parse_tf(spec: str) -> tuple[tuple, dict]:
    """'M5:1.0,M15:1.6,H1:2.4' -> (('M5','M15','H1'), {'M5':1.0,...})."""
    tfs, weights = [], {}
    for part in spec.split(","):
        name, _, w = part.partition(":")
        name = name.strip().upper()
        if name:
            tfs.append(name)
            weights[name] = float(w) if w.strip() else 1.0
    return tuple(tfs), weights


class TradingLoop:
    def __init__(self, broker: Mt5Broker, store: PgStateStore,
                 engine: FtmoComplianceEngine,
                 executor: ExecutionEngine | None = None,
                 strategies=None, arbitrator=None, symbols=None,
                 shadow=None, heartbeat_every: int = 10,
                 manage_owner: str | None = None, own_exposure: bool = False):
        self.broker = broker
        self.own_exposure = own_exposure  # exposure cap scoped to manage_owner
        self.store = store
        self.engine = engine
        self.executor = executor or ExecutionEngine(broker, store, engine)
        # One reconciler (sharing the executor's League) handles startup recon and
        # the per-tick sync that catches broker-side SL/TP closes mid-session.
        self.reconciler = Reconciler(
            broker, store, engine,
            league=getattr(self.executor, "league", None),
            notifier=getattr(self.executor, "notifier", None))
        # Optional trading: when strategies are provided the loop generates,
        # arbitrates and submits orders; otherwise it only monitors.
        self.strategies = strategies
        self.arbitrator = arbitrator
        self.symbols = symbols or []
        self.shadow = shadow              # optional ShadowTracker (benched 9, sim only)
        # ensemble tag this loop OWNS — it manages (TP1/BE/trail) only positions it
        # opened, so a second loop on the same account never touches its trades.
        self.manage_owner = manage_owner
        self.heartbeat_every = heartbeat_every
        self._last_action: Action | None = None
        self._tick = 0
        self.spike_mult = 2.5                 # bar range > this × ATR = anomaly
        self._anomaly_alert_tick: dict = {}   # symbol -> tick (alert cooldown)
        self._last_arb_reason: dict = {}      # symbol -> last arb reason (audit dedupe)

    # ----- startup --------------------------------------------------------- #
    def startup(self, now: datetime):
        result = self.reconciler.run(now)
        if not result.ok:
            print("  [HALT] reconciliation needs manual baseline confirmation — "
                  "engine stays NOT_RECONCILED, no trading.")
        return result

    # ----- per-tick -------------------------------------------------------- #
    def _maybe_rollover(self, now: datetime) -> None:
        d = self.engine.cet_date(now)
        if self.engine.current_cet_date != d:
            midnight_balance = self.broker.balance()
            self.store.roll_baseline(d, midnight_balance, "live")
            self.engine.roll_daily_baseline(now, midnight_balance)
            self.store.write_audit("rollover", "live", "cet_day_rollover",
                                   {"cet_date": str(d),
                                    "midnight_balance": midnight_balance})
            print(f"  -- CET day rollover -> {d}; new midnight balance "
                  f"{midnight_balance:,.2f}")

    def _apply_kill_switch(self) -> None:
        """Obey the shared kill switch the watchdog writes. close_all_halt also
        flattens here (idempotent with the watchdog) so the halt is enforced from
        both sides. A manual reset to 'running' lets the loop resume."""
        mode, reason = self.store.kill_switch()
        if mode == "running":
            if self.engine.halted:
                self.engine.resume()
                print("  -- kill switch cleared -> resuming normal trading")
            return
        if not self.engine.halted:
            self.engine.halt()
            print(f"  !! kill switch = {mode} ({reason}) -> main loop halting")
        if mode == "close_all_halt" and list(self.broker.open_positions()):
            try:
                closed = self.executor.flatten_all(reason=f"killswitch:{reason}")
                print(f"     killswitch flatten: {closed}")
            except Exception as e:
                print(f"     [ERROR] killswitch flatten failed: {e}")

    def tick(self, now: datetime):
        self._tick += 1
        self._apply_kill_switch()
        # catch broker-side SL/TP closes since last tick -> mark closed + feed League
        self.reconciler.sync_open_positions(now)
        # advanced trade management: TP1 partial + break-even + trailing, every tick
        self._manage_open_positions(now)
        self._store_price_bars()          # feed the dashboard's live candle chart
        self._maybe_rollover(now)
        # bound audit_log growth: prune liveness heartbeats older than ~3 days
        # (~every 6h at 30s). Real events stay forever; only the pings are dropped.
        if self._tick % 720 == 0:
            try:
                n = self.store.prune_heartbeats(72)
                if n:
                    print(f"  -- pruned {n} old heartbeat rows (kept last 72h)")
            except Exception:
                pass
        snap = AccountSnapshot(
            balance=self.broker.balance(),
            equity=self.broker.equity(),
            open_risk_to_sl=self.broker.open_risk_to_sl(),
        )
        verdict = self.engine.evaluate(snap, now)
        self._react(now, verdict, snap)

        # Routine time-flatten (Standard weekend/session): close open positions and
        # skip new trades — NOT a kill-switch, so the watchdog never latches on it.
        flat_reason = self.engine.time_flatten_required(now)
        if flat_reason is not None:
            self._apply_time_flatten(flat_reason)
        elif (self.strategies and self.arbitrator
              and verdict.action == Action.NORMAL):
            self._trade_cycle(snap, now)

        # Shadow Monitor: paper-trade the benched 9 (sim only; never touches money).
        # Runs every tick regardless of the live verdict so a regime shift shows up.
        if self.shadow is not None:
            self.shadow.tick(self.broker, now)
        return verdict

    def _apply_time_flatten(self, reason: Reason) -> None:
        if list(self.broker.open_positions()):
            print(f"  -- time-flatten ({reason.value}): closing all (Standard rule)")
            try:
                closed = self.executor.flatten_all(reason=reason.value)
                print(f"     flattened: {closed}")
            except Exception as e:
                print(f"     [ERROR] time-flatten failed: {e}")

    def _upcoming_news(self, now: datetime) -> list[NewsEvent]:
        """High-impact relevant events near `now` for the Compliance news gate
        (only consulted at the Funded stage)."""
        try:
            rows = self.store.conn.execute(
                "SELECT event_time, title FROM news_events WHERE relevant = TRUE "
                "AND event_time BETWEEN %s AND %s",
                (now - timedelta(minutes=5), now + timedelta(minutes=5))).fetchall()
            return [NewsEvent(when_utc=r[0], label=r[1]) for r in rows]
        except Exception:
            return []

    def _store_price_bars(self) -> None:
        for sym in self.symbols:
            bars = self.broker.get_bars(sym, "M5", 150)
            if bars:
                try:
                    self.store.upsert_bars(sym, bars)
                except Exception:
                    pass

    def _manage_open_positions(self, now: datetime) -> None:
        """Drive TP1/break-even/trailing on every open managed position."""
        if self.executor is None:
            return
        try:
            recs = self.store.open_managed_positions(self.manage_owner)
        except Exception:
            return
        for rec in recs:
            bid, ask = self.broker.quote(rec["symbol"])
            if bid <= 0:
                continue
            try:
                action = self.executor.manage_position(rec, bid, ask, now)
            except Exception as e:
                print(f"     [manage error] #{rec['ticket']}: {e}")
                continue
            if action:
                print(f"  -- manage {rec['symbol']} #{rec['ticket']} -> {action}")

    def _notify_anomaly(self, symbol: str) -> None:
        # alert at most once per 20 ticks per symbol
        if self._tick - self._anomaly_alert_tick.get(symbol, -999) < 20:
            return
        self._anomaly_alert_tick[symbol] = self._tick
        self.store.write_audit("anomaly", "skip", "volatility_spike",
                               {"symbol": symbol})
        notifier = getattr(self.executor, "notifier", None)
        if notifier is not None:
            from notifier import mgmt_embed
            notifier.send(embeds=[mgmt_embed(
                symbol=symbol, event="⚠️ Volatility spike — entries paused",
                detail="A chaotic bar (range >> ATR) was detected; new entries are "
                       "skipped until the market settles.")])

    def _is_anomaly(self, symbol: str) -> bool:
        """Abnormal-volatility guard: a bar whose range >> ATR = chaotic price
        action — don't open into it (react to strange chart moves)."""
        bars = self.broker.get_bars(symbol, "M5", 20)
        a = atr(bars, 14) if len(bars) >= 16 else None
        if not a or a <= 0:
            return False
        return (bars[-1].high - bars[-1].low) > self.spike_mult * a

    def _log_arb_considerations(self) -> None:
        """Surface WHY arbitration did / did not produce an order per symbol — a
        gated signal otherwise vanishes silently. Console every cycle (so it's
        visible live in the loop window); audit only when the reason CHANGES per
        symbol (a persistent 'range/low conviction' must not flood the log)."""
        for sym, info in (getattr(self.arbitrator, "last_consider", {}) or {}).items():
            why = info.get("why", "?")
            bits = []
            if "regime" in info:
                bits.append(f"regime={info['regime']}")
            if "techniques" in info:
                need = info.get("need", {})
                bits.append(f"tech={info['techniques']}/{need.get('agree','?')}")
                bits.append(f"fam={info['families']}/{need.get('families','?')}")
                bits.append(f"conv={info['conviction']}/{need.get('conviction','?')}")
                bits.append(f"agree={info['agreement']:.0%}/{need.get('agreement',0):.0%}")
            detail = f" ({', '.join(bits)})" if bits else ""
            print(f"     arb {sym}: {info.get('decision')}/{why}{detail}")
            if self._last_arb_reason.get(sym) != why:
                self._last_arb_reason[sym] = why
                try:
                    self.store.write_audit("arb", info.get("decision", "skip"),
                                           why, {"symbol": sym, **info})
                except Exception:
                    pass

    def _exposure_symbols(self) -> set | None:
        """--exposure own: the one-position-per-symbol cap counts only THIS
        loop's positions (by ensemble tag), so several single-strategy loops can
        hold XAUUSD at once. Default (account): every position on the account."""
        if not self.own_exposure or not self.manage_owner:
            return None
        try:
            return {r["symbol"]
                    for r in self.store.open_managed_positions(self.manage_owner)}
        except Exception:
            return None                      # fall back to the account-wide cap

    def _trade_cycle(self, snap: AccountSnapshot, now: datetime) -> None:
        sigs = self.arbitrator.collect(self.strategies, self.symbols, self.broker, now)
        if not sigs:
            return
        news = (self._upcoming_news(now)
                if (self.engine.cfg.enforce_news_blackout
                    and self.engine.profile.phase == Phase.FUNDED) else ())
        reqs = self.arbitrator.arbitrate(sigs, self.broker, snap.equity, now,
                                         open_symbols=self._exposure_symbols())
        self._log_arb_considerations()        # visibility: why did/didn't we trade
        for req in reqs:
            if self._is_anomaly(req.symbol):     # don't trade into a spike
                print(f"     skip {req.symbol}: volatility anomaly (chaotic bar)")
                self._notify_anomaly(req.symbol)
                continue
            res = self.executor.submit(req, snap, now, upcoming_news=news)
            status = (f"filled #{res.ticket}" if res.submitted
                      else f"blocked/{res.verdict.reason.value}")
            print(f"     trade: {req.symbol} {req.side} {req.volume} "
                  f"(risk {req.risk_to_sl:,.0f}) -> {status}")

    def _react(self, now: datetime, verdict, snap: AccountSnapshot) -> None:
        cet = now.astimezone(FTMO_TZ)
        print(f"[{cet:%Y-%m-%d %H:%M:%S %Z}] "
              f"eq={snap.equity:,.2f} bal={snap.balance:,.2f} "
              f"risk@SL={snap.open_risk_to_sl:,.2f} "
              f"-> {verdict.action.value}/{verdict.reason.value}")

        if verdict.action == Action.FLATTEN_ALL:
            print(f"  !! FLATTEN_ALL ({verdict.reason.value}) — closing ALL positions.")
            try:
                closed = self.executor.flatten_all(reason=verdict.reason.value)
                print(f"     flattened: {closed or 'nothing open'}")
            except Exception as e:
                # Last-resort safety net failed — escalate loudly to the audit log.
                print(f"     [ERROR] flatten failed: {e}")
                self.store.write_audit("kill", "flatten_error",
                                       verdict.reason.value, {"error": str(e)})

        # Heartbeat EVERY tick — it is the watchdog's liveness signal, so its
        # cadence must stay well under the watchdog's max_silence (and the
        # dashboard's stale threshold). Append-only; ~2880 rows/day at 30s.
        self.store.write_audit(
            "heartbeat", verdict.action.value, verdict.reason.value,
            {"equity": snap.equity, "balance": snap.balance,
             "open_risk_to_sl": snap.open_risk_to_sl,
             # None when the floor is not enforced (RULES_MODE=none), so the
             # dashboard shows "-" instead of a limit nothing polices.
             "daily_floor": (self.engine.real_daily_floor()
                             if self.engine.cfg.enforce_daily_loss else None),
             "daily_soft_floor": (self.engine.daily_soft_floor()
                                  if self.engine.cfg.enforce_daily_loss else None),
             "overall_floor": (self.engine.real_overall_floor()
                               if self.engine.cfg.enforce_overall_loss else None)},
        )
        self._last_action = verdict.action

    # ----- driver ---------------------------------------------------------- #
    def run(self, ticks: int | None = None, interval: float = 30.0) -> None:
        now = datetime.now(timezone.utc)
        res = self.startup(now)
        print(f"Startup reconcile: ok={res.ok} baseline={res.baseline_source} "
              f"= {res.midnight_balance:,.2f}")
        print(rules_summary(self.engine.profile, self.engine.cfg))
        if self.engine.cfg.enforce_daily_loss or self.engine.cfg.enforce_overall_loss:
            daily = (f"{self.engine.real_daily_floor():,.2f} "
                     f"soft={self.engine.daily_soft_floor():,.2f}"
                     if self.engine.cfg.enforce_daily_loss else "off")
            overall = (f"{self.engine.real_overall_floor():,.2f}"
                       if self.engine.cfg.enforce_overall_loss else "off")
            print(f"Floors: daily={daily} overall={overall}")
        else:
            print("Floors: none — equity floors disabled; per-trade risk sizing "
                  "and the kill switch still apply.")
        print(f"--- monitoring loop (interval={interval}s, "
              f"{'inf' if ticks is None else ticks} ticks) ---")
        i = 0
        fails = 0
        notifier = getattr(self.executor, "notifier", None)
        try:
            while ticks is None or i < ticks:
                # RESILIENCE: a transient MT5/DB error (e.g. terminal restart,
                # "IPC send failed") must NOT kill the bot. Catch, log, try to
                # reconnect, alert once, and keep ticking. KeyboardInterrupt is a
                # BaseException so it still propagates to the clean-shutdown path.
                try:
                    self.tick(datetime.now(timezone.utc))
                    if fails:
                        print(f"  [OK] recovered after {fails} failed tick(s)")
                        if notifier is not None:
                            try:
                                notifier.send(content="✅ FTMO bot recovered — "
                                              "ticking normally again.")
                            except Exception:
                                pass
                        fails = 0
                except Exception as e:
                    fails += 1
                    print(f"  [WARN] tick failed ({fails}x): "
                          f"{type(e).__name__}: {e}")
                    try:
                        self.store.write_audit("error", "tick_failed",
                                               type(e).__name__,
                                               {"error": str(e), "consecutive": fails})
                    except Exception:
                        pass
                    try:                       # attempt a clean MT5 reconnect
                        self.broker.shutdown()
                        self.broker.connect()
                    except Exception:
                        pass
                    if fails == 3 and notifier is not None:
                        try:
                            notifier.send(content=f"⚠️ FTMO bot: {fails} consecutive "
                                          f"tick failures ({type(e).__name__}). MT5/DB "
                                          f"may be down — bot is retrying, not dead.")
                        except Exception:
                            pass
                i += 1
                if ticks is not None and i >= ticks:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n[shutdown] interrupted — writing shutdown audit.")
            self.store.write_audit("lifecycle", "shutdown", "keyboard_interrupt", {})
        finally:
            notifier = getattr(self.executor, "notifier", None)
            if notifier is not None:
                notifier.flush()        # let the last alert actually go out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=None,
                    help="number of ticks then exit (default: run forever)")
    ap.add_argument("--interval", type=float,
                    default=float(os.getenv("LOOP_INTERVAL_SECS", "30")),
                    help="seconds between ticks (default 30)")
    ap.add_argument("--trade", action="store_true",
                    help="enable strategy generation + order submission "
                         "(needs Algo Trading ON in the terminal)")
    ap.add_argument("--symbols", default=os.getenv("TRADE_SYMBOLS", "EURUSD,XAUUSD"),
                    help="comma-separated symbols to trade when --trade is set")
    ap.add_argument("--variant", choices=["standard", "swing"], default=None,
                    help="override account variant (use 'swing' for a weekend "
                         "crypto test so the Standard weekend gate doesn't veto)")
    ap.add_argument("--risk-pct", type=float, default=0.01,
                    help="fraction of equity risked per trade (default 0.01 = 1%%)")
    ap.add_argument("--min-agreement", type=float, default=0.70,
                    help="combined agreement %% required to trade (0..1, default 0.70). "
                         "0.85 = very strict; backtest to find the best value")
    ap.add_argument("--tf", default="M30:1.0,H1:1.6,H4:2.4",
                    help="timeframes:weights (analyse on M30+; small TFs are noisy)")
    ap.add_argument("--sessions", default="",
                    help="restrict entries to sessions, e.g. 'london,ny' "
                         "(empty = all sessions; good for XAU)")
    ap.add_argument("--require-regime", default="",
                    help="HARD regime gate, e.g. 'trend,unknown' for trend-"
                         "followers (empty = no gate). OOS-validated: +53%% "
                         "expectancy on the robust roster.")
    ap.add_argument("--strategies", default=os.getenv("TRADE_STRATEGIES", ""),
                    help="comma-separated strategy ids to run (empty = all 13). "
                         "Use 'robust' for the OOS-validated trend/breakout core.")
    ap.add_argument("--min-agree", type=int, default=3,
                    help="min distinct techniques that must agree (default 3; "
                         "lower to 2 for a small pruned roster)")
    ap.add_argument("--min-families", type=int, default=2,
                    help="min distinct strategy families that must agree (default 2)")
    ap.add_argument("--min-conviction", type=float, default=1.5,
                    help="min total weighted conviction on the winning side (default 1.5)")
    ap.add_argument("--be-trigger", type=float, default=0.0,
                    help="early break-even: lift SL to entry once price tags this R "
                         "before TP1 (0 = off). 11-yr study: early BE HURTS gold "
                         "(cuts the trend tail) — keep this OFF.")
    ap.add_argument("--tp1-r", type=float, default=2.0,
                    help="bank the partial + go break-even at this R. 11-yr study: "
                         "later is better on gold (tp1@2.0 > 1.5 > 1.0); 'give the "
                         "trend room'. Default 2.0.")
    ap.add_argument("--dd-throttle", action="store_true",
                    help="drawdown throttle (OOS-validated 'S3'): cut risk to 1/2 "
                         "at >=3%% below the run's equity peak, 1/4 at >=6%%. Same "
                         "edge, lower drawdown / softer worst day. Default OFF.")
    ap.add_argument("--shadow", dest="shadow", action="store_true", default=True,
                    help="paper-trade the benched (non-roster) strategies for "
                         "monitoring (default on; sim only, never trades money)")
    ap.add_argument("--no-shadow", dest="shadow", action="store_false",
                    help="disable the shadow monitor")
    ap.add_argument("--shadow-tf", default="H1",
                    help="timeframe the shadow monitor evaluates benched techniques on")
    ap.add_argument("--signal-floor", type=float, default=0.10,
                    help="ignore signals below this confidence (default 0.10; "
                         "0 = take every valid signal, as the solo backtests do)")
    ap.add_argument("--dd-levels", default="",
                    help="drawdown throttle tiers 'dd:mult,...' e.g. "
                         "'0.20:0.5,0.35:0.25' (>=20%% below the equity peak -> "
                         "half risk, >=35%% -> quarter). Overrides --dd-throttle. "
                         "Peak is seeded from the DB's realised P&L on start.")
    ap.add_argument("--exposure", choices=["account", "own"], default="account",
                    help="one-position-per-symbol cap counts positions of the whole "
                         "account (default) or only THIS loop's tag ('own': several "
                         "single-strategy loops may hold the same symbol at once)")
    ap.add_argument("--ensemble-tag", default="ensemble",
                    help="namespace for THIS loop's orders (strategy_id = tag:rep). "
                         "Run a 2nd loop on the same account with a DIFFERENT tag "
                         "(e.g. 'vote') so each manages only its own positions.")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
        if args.trade else []

    profile = profile_from_env()
    if args.variant:
        profile.variant = Variant(args.variant)   # weekend-test override
        print(f"[override] account variant -> {profile.variant.value}")

    with Mt5Broker() as broker, PgStateStore() as store:
        engine = FtmoComplianceEngine(profile, config_from_env())
        strategies = arbitrator = executor = shadow = None
        if args.trade:
            from strategies import (DEFAULT_STRATEGIES, ROBUST_TREND_IDS,
                                     select_strategies)
            from arbitration import Arbitrator, ArbitrationConfig
            from execution import TradeMgmtConfig
            from league import League
            from notifier import trade_notifier
            # One League shared by arbitration (weights) and execution (records
            # outcomes). Arbitrator config is regime-aware (Pillar 4).
            league = League(store)
            notifier = trade_notifier()
            # roster: all 13, the OOS-validated 'robust' core, or an explicit list
            if not args.strategies:
                strategies = DEFAULT_STRATEGIES
            elif args.strategies.strip().lower() == "robust":
                strategies = select_strategies(ROBUST_TREND_IDS)
            else:
                ids = [s.strip() for s in args.strategies.split(",") if s.strip()]
                strategies = select_strategies(ids)
            tfs, tf_weights = _parse_tf(args.tf)
            sess = tuple(s.strip().lower() for s in args.sessions.split(",")
                         if s.strip())
            req_regime = tuple(s.strip().lower()
                               for s in args.require_regime.split(",") if s.strip())
            # OOS-validated drawdown throttle (off unless --dd-throttle): >=3%
            # below the run's equity peak -> half risk, >=6% -> quarter risk.
            dd_levels = ((0.03, 0.5), (0.06, 0.25)) if args.dd_throttle else ()
            if args.dd_levels:
                dd_levels = tuple((float(a), float(b)) for a, b in
                                  (t.split(":") for t in args.dd_levels.split(",")))
            arbitrator = Arbitrator(ArbitrationConfig(
                risk_pct=args.risk_pct, min_agreement=args.min_agreement,
                timeframes=tfs, tf_weights=tf_weights, allowed_sessions=sess,
                require_regime=req_regime, tp1_r=args.tp1_r,
                min_agree=args.min_agree, min_families=args.min_families,
                min_conviction=args.min_conviction, dd_throttle_levels=dd_levels,
                signal_floor=args.signal_floor,
                ensemble_tag=args.ensemble_tag), league=league)
            if dd_levels:
                peak = store.realized_equity_peak(profile.initial_capital)
                arbitrator.seed_peak_equity(peak)
                print("[config] drawdown throttle ON: "
                      + ", ".join(f">={a:.0%} below peak -> x{b:g} risk"
                                  for a, b in sorted(dd_levels))
                      + f" (peak seeded {peak:,.2f})")
            # strategies must see only CLOSED candles, like the backtester
            broker.closed_bars_only = True
            print(f"[config] closed bars only; exposure cap = {args.exposure}")
            executor = ExecutionEngine(broker, store, engine, league=league,
                                       notifier=notifier,
                                       mgmt=TradeMgmtConfig(be_trigger_r=args.be_trigger))
            # Shadow Monitor: paper-trade every strategy NOT on the live roster, so
            # a regime shift that revives one is visible (promotion stays manual).
            shadow = None
            if args.shadow:
                from shadow import ShadowTracker
                active_ids = {s.id for s in strategies}
                benched = [s for s in DEFAULT_STRATEGIES if s.id not in active_ids]
                if benched:
                    shadow = ShadowTracker(store, benched, symbols,
                                           tf=args.shadow_tf)
                    print(f"SHADOW MONITOR on (sim only): "
                          f"{[s.id for s in benched]} @ {args.shadow_tf}")
            print(f"TRADING ENABLED on {symbols} risk={args.risk_pct:.3%} "
                  f"min_agreement={args.min_agreement:.0%} TFs={tfs} "
                  f"roster={[s.id for s in strategies]} "
                  f"gates(agree>={args.min_agree},fam>={args.min_families}) "
                  f"regime={req_regime or 'any'} tp1={args.tp1_r}R "
                  f"be_trigger={args.be_trigger}R "
                  f"(Algo {'ON' if broker else '?'}; "
                  f"Discord {'ON' if notifier.enabled else 'OFF'}).")

        TradingLoop(broker, store, engine, executor=executor,
                    strategies=strategies, arbitrator=arbitrator,
                    symbols=symbols, shadow=shadow,
                    manage_owner=(args.ensemble_tag if args.trade else None),
                    own_exposure=(args.exposure == "own")).run(
                        ticks=args.ticks, interval=args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
