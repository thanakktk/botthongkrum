"""
Signal Arbitration Layer (Pillar 1)
======================================================================
Turns many strategies' signals into at most ONE order per symbol:

  1. Collect + drop expired/invalid signals (the contract is already enforced
     at Signal construction).
  2. Weighted ensemble per symbol: score = Σ weight * confidence * direction.
     Net direction = sign(score); weak nets are ignored.
  3. Exposure cap / same-symbol netting (v1): never stack a second position on
     a symbol that already has one. (Cross-asset correlation is a later phase.)
  4. Position sizing (Pillar 3): fixed-fractional risk (default 1% of equity),
     so the order's worst-case loss equals the risk budget. The resulting
     risk_to_sl is what the Compliance pre-trade gate checks.

Output is a list of OrderRequest — the ExecutionEngine still holds the final
compliance veto before anything reaches the broker.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Sequence

from signals import Signal, Direction, Regime, Bar
from strategies import Strategy, CATALOG
from regime import detect_regime, efficiency_ratio
from sessions import in_allowed
from execution import OrderRequest

# strategy_id -> family (Momentum / Volatility / S/R / Oscillator / SMC-ICT)
FAMILY: dict[str, str] = {c["id"]: c["family"] for c in CATALOG}


@dataclass
class ArbitrationConfig:
    weights: dict[str, float] = field(default_factory=dict)  # strategy_id -> weight
    default_weight: float = 1.0
    min_score: float = 0.10        # ignore a net weaker than this
    risk_pct: float = 0.01         # 1% of equity at risk per trade
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 50.0
    # Hard cap on position NOTIONAL as a multiple of equity. Stops a tight-SL
    # signal from ballooning volume (e.g. 4.88 BTC ≈ 3× equity) — a slippage/gap
    # past such an oversized SL can lose far more than the risk budget. 0.5 is
    # conservative for volatile crypto; raise for calmer FX/metals if desired.
    max_notional_frac: float = 0.5
    # DRAWDOWN THROTTLE (Phase-3 quant, OOS-validated in research/compare_quant.py
    # as "S3"): scale per-trade risk DOWN as equity sits below its running peak —
    # an anti-martingale that protects the FTMO floors and cushions the worst year
    # without changing the per-trade edge. A tuple of (drawdown_fraction,
    # risk_multiplier) tiers; the DEEPEST breached tier wins. Empty = OFF (no
    # behaviour change). 11-yr validated "standard" = ((0.03, 0.5), (0.06, 0.25)):
    # >=3% below peak -> half size, >=6% -> quarter size. Peak is tracked in-memory
    # per run (resets on restart), so protection is intra-session — which suits the
    # manual-run style and aligns with the FTMO daily horizon.
    dd_throttle_levels: tuple = ()
    # Pillar 4: damp a strategy whose regime_tag != the detected market regime.
    mismatch_weight: float = 0.25
    regime_n: int = 20             # bars window for regime detection
    # ----- MULTI-TIMEFRAME: read every TF; higher TFs carry more weight -----
    # Analyse on HIGHER timeframes (M30+) — lower TFs are noisy/less reliable.
    timeframes: tuple = ("M30", "H1", "H4")
    tf_weights: dict = field(default_factory=lambda: {
        "M5": 0.5, "M15": 0.8, "M30": 1.0, "H1": 1.6, "H4": 2.4, "D1": 3.2})
    # ----- CONFLUENCE / combined-probability gate -----
    # Trade only when the WEIGHTED agreement across all techniques & timeframes
    # points one way by >= min_agreement (e.g. 0.85 = 85%), with substance
    # (>= min_conviction total) and breadth (>= min_agree techniques, >= families).
    min_agreement: float = 0.85
    min_conviction: float = 1.5
    min_agree: int = 3
    min_families: int = 2
    signal_floor: float = 0.10
    # take-profit ladder in units of the risk R (= |entry - sl|): TP1 banks a
    # partial + moves to break-even; TP2 is the runner target (= broker TP).
    tp1_r: float = 1.0
    tp2_r: float = 2.5
    # only open during these sessions (edge filter); () = no restriction.
    allowed_sessions: tuple = ()
    # HARD regime gate: only open when the detected regime is in this set, e.g.
    # ("trend",) for trend-followers. () = no restriction (regime still DAMPS via
    # mismatch_weight). 'unknown' should usually be allowed so a noisy/undecided
    # detector doesn't block everything: ("trend","unknown").
    require_regime: tuple = ()
    # STRICT trend-strength gate: only open when the Kaufman Efficiency Ratio on
    # the entry TF is >= this (0 = off). Higher = only strong, clean trends —
    # avoids the choppy, wide-range markets that bleed (e.g. 2015/2018 gold).
    min_er: float = 0.0
    # NAMESPACE for this arbitrator's orders: every OrderRequest is tagged
    # strategy_id = f"{ensemble_tag}:{rep}". Two loops on the SAME account use
    # DIFFERENT tags ("ensemble" vs "vote") so each manages only its OWN positions
    # (open_managed_positions(owner=tag)) and they don't fight over SL/TP1.
    ensemble_tag: str = "ensemble"


class Arbitrator:
    def __init__(self, cfg: Optional[ArbitrationConfig] = None, league=None):
        self.cfg = cfg or ArbitrationConfig()
        self.league = league       # optional League (Pillar 4 standings)
        # VISIBILITY: after each arbitrate(), holds per-symbol why a trade was or
        # was NOT produced (which confluence gate vetoed). Otherwise a gated
        # signal vanishes silently and you can't tell "why isn't it trading?".
        self.last_consider: dict[str, dict] = {}
        # running equity high-water mark for the drawdown throttle (dd_throttle_levels)
        self._peak_equity: float = 0.0

    def _note(self, sym: str, decision: str, why: str, **metrics) -> None:
        """Record (and return None for) the reason a symbol produced no order."""
        self.last_consider[sym] = {"decision": decision, "why": why, **metrics}
        return None

    def weight(self, strategy_id: str) -> float:
        return self.cfg.weights.get(strategy_id, self.cfg.default_weight)

    def _regime_fit(self, strat_regime: Regime, detected: Regime) -> float:
        if detected == Regime.UNKNOWN or strat_regime == Regime.UNKNOWN:
            return 1.0
        return 1.0 if strat_regime == detected else self.cfg.mismatch_weight

    def effective_weight(self, sig: Signal, detected: Regime,
                         now: datetime) -> float:
        """Base weight × regime-fit × timeframe weight × League standing."""
        w = self.weight(sig.strategy_id) * self._regime_fit(sig.regime_tag, detected)
        w *= self.cfg.tf_weights.get(sig.timeframe, 1.0)
        if self.league is not None:
            w *= self.league.weight_multiplier(sig.strategy_id, detected, now)
        return w

    # ----- gather signals across all strategies × symbols × TIMEFRAMES ----- #
    def collect(self, strategies: Sequence[Strategy], symbols: Sequence[str],
                broker, now: datetime, bar_count: int = 200) -> list[Signal]:
        signals: list[Signal] = []
        for sym in symbols:
            for tf in self.cfg.timeframes:
                bars: list[Bar] = broker.get_bars(sym, tf, bar_count)
                if not bars:
                    continue
                for st in strategies:
                    try:
                        sig = st.generate(sym, bars, now)
                    except Exception:
                        continue
                    if sig is not None and not sig.is_expired(now):
                        sig.timeframe = tf      # tag with the TF it was read on
                        signals.append(sig)
        return signals

    # ----- resolve to at most one OrderRequest per symbol ------------------ #
    def seed_peak_equity(self, equity: float) -> None:
        """Restore the drawdown-throttle high-water mark across restarts (the
        loop seeds it from the DB's realised P&L history)."""
        if equity > self._peak_equity:
            self._peak_equity = equity

    def arbitrate(self, signals: Sequence[Signal], broker, equity: float,
                  now: datetime, open_symbols: Optional[set] = None) -> list[OrderRequest]:
        """`open_symbols` overrides the exposure cap's view of what is already
        open: pass only THIS loop's positions so several one-strategy loops can
        each hold the same symbol. None -> every position on the account."""
        self.last_consider = {}                          # reset per-cycle reasons
        # track the realised-equity high-water mark (sampled when flat, which is
        # when arbitrate runs) for the drawdown throttle applied in _size().
        if equity > self._peak_equity:
            self._peak_equity = equity
        if not in_allowed(now, self.cfg.allowed_sessions):   # session edge filter
            self.last_consider["*"] = {"decision": "skip", "why": "session_closed"}
            return []
        if open_symbols is None:
            open_symbols = {p.symbol for p in broker.open_positions()}
        by_sym: dict[str, list[Signal]] = defaultdict(list)
        for s in signals:
            if not s.is_expired(now):
                by_sym[s.symbol].append(s)

        requests: list[OrderRequest] = []
        for sym, sigs in by_sym.items():
            if sym in open_symbols:          # exposure cap: one position / symbol
                self._note(sym, "skip", "position_open")
                continue
            req = self._resolve_symbol(sym, sigs, broker, equity, now)
            if req is not None:
                requests.append(req)
        return requests

    def _resolve_symbol(self, sym, sigs, broker, equity, now):
        entry_tf = self.cfg.timeframes[0]
        bars = broker.get_bars(sym, entry_tf, self.cfg.regime_n + 5)
        regime = detect_regime(bars, self.cfg.regime_n)
        er = efficiency_ratio([b.close for b in bars], self.cfg.regime_n) or 0.0

        # HARD regime gate (edge filter): skip entirely if this regime isn't allowed
        if self.cfg.require_regime and regime.value not in self.cfg.require_regime:
            return self._note(sym, "skip", "regime_gate",
                              regime=regime.value, er=round(er, 3))
        # STRICT trend-strength gate: skip weak/choppy markets (low efficiency)
        if self.cfg.min_er > 0 and er < self.cfg.min_er:
            return self._note(sym, "skip", "min_er",
                              er=round(er, 3), need_er=self.cfg.min_er)

        # conviction per signal = base × regime-fit × TF-weight × League × confidence
        contribs = []
        for s in sigs:
            w = self.effective_weight(s, regime, now)
            if w > 0 and s.confidence >= self.cfg.signal_floor:
                contribs.append((s, w * s.confidence))
        if not contribs:
            return self._note(sym, "skip", "no_contributions",
                              regime=regime.value, n_signals=len(sigs))

        bull = sum(c for s, c in contribs if s.direction == Direction.BUY)
        bear = sum(c for s, c in contribs if s.direction == Direction.SELL)
        total = bull + bear
        if total <= 0:
            return self._note(sym, "skip", "no_direction", regime=regime.value)
        net = Direction.BUY if bull >= bear else Direction.SELL
        agreement = max(bull, bear) / total          # the COMBINED probability %
        aligned = [(s, c) for s, c in contribs if s.direction == net]
        opposing = [(s, c) for s, c in contribs if s.direction != net]

        # aggregate aligned conviction per technique (across timeframes)
        by_strat: dict[str, float] = defaultdict(float)
        tfs_of: dict[str, set] = defaultdict(set)
        for s, c in aligned:
            by_strat[s.strategy_id] += c
            tfs_of[s.strategy_id].add(s.timeframe)
        families = {FAMILY.get(sid, "?") for sid in by_strat}

        # snapshot the confluence metrics so every gate outcome is explainable
        conviction = max(bull, bear)
        metrics = dict(regime=regime.value, er=round(er, 3), net=net.value,
                       agreement=round(agreement, 4),
                       techniques=len(by_strat), families=len(families),
                       conviction=round(conviction, 3),
                       need=dict(agree=self.cfg.min_agree,
                                 families=self.cfg.min_families,
                                 conviction=self.cfg.min_conviction,
                                 agreement=self.cfg.min_agreement))

        # ---- gates: breadth + substance + >= min_agreement combined % ----
        if len(by_strat) < self.cfg.min_agree:
            return self._note(sym, "skip", "min_agree", **metrics)
        if len(families) < self.cfg.min_families:
            return self._note(sym, "skip", "min_families", **metrics)
        if conviction < self.cfg.min_conviction:
            return self._note(sym, "skip", "min_conviction", **metrics)
        if agreement < self.cfg.min_agreement:
            return self._note(sym, "skip", "min_agreement", **metrics)

        # entry levels: strongest aligned signal on the entry TF, else strongest
        entry_aligned = [(s, c) for s, c in aligned if s.timeframe == entry_tf]
        rep = max(entry_aligned or aligned, key=lambda sc: sc[1])[0]
        volume, risk = self._size(broker, sym, net.value, rep.entry, rep.sl, equity)
        if volume <= 0:
            return self._note(sym, "skip", "zero_volume", **metrics)
        self.last_consider[sym] = {"decision": "trade", "why": "ok", **metrics}

        # TP ladder from the risk distance R (TP2 = the broker TP)
        rdist = abs(rep.entry - rep.sl)
        d = 1 if net == Direction.BUY else -1
        tp1 = rep.entry + d * self.cfg.tp1_r * rdist
        tp2 = rep.entry + d * self.cfg.tp2_r * rdist

        rationale = self._build_rationale(regime, er, net, agreement, total,
                                          by_strat, tfs_of, families, opposing,
                                          rep, tp1, tp2, volume, risk)
        return OrderRequest(
            strategy_id=f"{self.cfg.ensemble_tag}:{rep.strategy_id}",
            symbol=sym, side=net.value,
            volume=volume, sl=rep.sl, tp=tp2, risk_to_sl=risk,
            regime=regime.value, entry=rep.entry, rationale=rationale,
            tp1=tp1, tp2=tp2,
        )

    # regime / direction -> Thai (the rationale below is written for a Thai reader)
    _TH_REGIME = {"trend": "เทรนด์", "range": "ออกข้าง", "unknown": "ไม่ชัด"}
    _TH_DIR = {"buy": "ซื้อ (BUY)", "sell": "ขาย (SELL)"}

    @classmethod
    def _build_rationale(cls, regime, er, net, agreement, total, by_strat, tfs_of,
                         families, opposing, rep, tp1, tp2, volume, risk) -> str:
        """เหตุผลการ "เปิด" ไม้ (ภาษาไทย) — โหวตทุกเทคนิคว่าฝั่งไหนน้ำหนักมากกว่า:
        ความเห็นพ้องรวมกี่ %, แต่ละเทคนิคโหวตอะไรบนไทม์เฟรมไหน, ฝั่งสวนกี่ %, ระดับราคา."""
        rr = (abs(tp2 - rep.entry) / abs(rep.entry - rep.sl)
              if rep.entry != rep.sl else 0.0)
        per_tech = ", ".join(
            f"{sid}[{FAMILY.get(sid,'?')}·{'/'.join(sorted(tfs_of[sid]))}] "
            f"{c / total * 100:.0f}%"
            for sid, c in sorted(by_strat.items(), key=lambda kv: -kv[1]))
        opp_by: dict[str, float] = defaultdict(float)
        for s, c in opposing:
            opp_by[s.strategy_id] += c
        opp = (", ".join(f"{sid} {c / total * 100:.0f}%"
                         for sid, c in sorted(opp_by.items(), key=lambda kv: -kv[1]))
               or "ไม่มี")
        return (
            f"[ตลาด{cls._TH_REGIME.get(regime.value, regime.value)} · ER={er:.2f}] "
            f"เปิด{cls._TH_DIR.get(net.value, net.value)} — เพราะเทคนิคส่วนใหญ่โหวตฝั่งนี้: "
            f"เห็นพ้องรวม {agreement * 100:.0f}% จาก {len(by_strat)} เทคนิค / "
            f"{len(families)} กลุ่ม ({', '.join(sorted(families))}) ข้ามหลายไทม์เฟรม. "
            f"รายเทคนิค (น้ำหนักโหวต): {per_tech}. ฝั่งสวน: {opp}. "
            f"ระดับราคา: เข้า={rep.entry:.2f} SL={rep.sl:.2f} TP1={tp1:.2f} "
            f"TP2={tp2:.2f} R:R={rr:.2f}; ขนาด={volume} ล็อต เสี่ยง=${risk:,.0f}."
        )

    # ----- drawdown throttle ---------------------------------------------- #
    def _dd_risk_multiplier(self, equity: float) -> float:
        """<1.0 when equity sits below its running peak — see dd_throttle_levels.
        Returns 1.0 (no change) when the throttle is off or we're at a new high."""
        if not self.cfg.dd_throttle_levels or self._peak_equity <= 0:
            return 1.0
        dd = (self._peak_equity - equity) / self._peak_equity
        for thr, mult in sorted(self.cfg.dd_throttle_levels, reverse=True):
            if dd >= thr:                        # deepest breached tier wins
                return mult
        return 1.0

    # ----- fixed-fractional sizing ---------------------------------------- #
    def _size(self, broker, symbol: str, side: str, entry: float, sl: float,
              equity: float) -> tuple[float, float]:
        per_lot_risk = broker.estimate_risk(symbol, side, 1.0, entry, sl)
        if per_lot_risk <= 0:
            return 0.0, 0.0
        risk_budget = self.cfg.risk_pct * equity * self._dd_risk_multiplier(equity)
        raw = risk_budget / per_lot_risk
        # NOTIONAL CAP: never let a tight SL inflate the position beyond
        # max_notional_frac × equity worth of exposure.
        try:
            npl = broker.notional_per_lot(symbol, entry)
            if npl and npl > 0:
                raw = min(raw, self.cfg.max_notional_frac * equity / npl)
        except Exception:
            pass
        steps = math.floor(raw / self.cfg.lot_step)
        volume = round(steps * self.cfg.lot_step, 2)
        volume = max(self.cfg.min_lot, min(self.cfg.max_lot, volume))
        if volume < self.cfg.min_lot:
            return 0.0, 0.0
        return volume, per_lot_risk * volume


# --------------------------------------------------------------------------- #
# Self-test:                                                                   #
#   A) CONFLUENCE gate — needs >=3 strategies across >=2 families to trade     #
#   B) full path: bars -> strategies -> arbitrate -> execute on PaperBroker    #
#   ./env/Scripts/python.exe arbitration.py                                    #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone, timedelta
    from ftmo_compliance_engine import (
        FtmoComplianceEngine, AccountProfile, AccountSnapshot,
        Variant, Path, Phase,
    )
    from strategies import DEFAULT_STRATEGIES
    from paper_broker import PaperBroker
    from pg_state_store import PgStateStore
    from execution import ExecutionEngine
    from league import League
    from regime import detect_regime

    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)   # fixed Wednesday
    arb = Arbitrator()

    def sig(stratid, direction, conf):
        return Signal(stratid, "EURUSD", direction, conf, 1.1000, 1.0950, 1.1100,
                      "H1", Regime.TREND, now, now + timedelta(minutes=15)).validate()

    pb = PaperBroker(initial_balance=100_000.0)
    pb.set_price("EURUSD", 1.1000)          # no bars -> regime UNKNOWN, full weight

    # A1: 3 strategies across 3 families agree -> CONFLUENCE -> 1 order
    confluent = [sig("breakout_sr", Direction.BUY, 0.6),          # S/R
                 sig("roc_momentum", Direction.BUY, 0.5),         # Momentum
                 sig("order_block_retest", Direction.BUY, 0.7)]   # SMC/ICT
    r1 = arb.arbitrate(confluent, pb, 100_000.0, now)
    print("A1 confluence (3 fam) ->", [(x.side, x.volume) for x in r1])
    assert len(r1) == 1 and r1[0].side == "buy"

    # A2: a LONE signal -> rejected (the whole point)
    r2 = arb.arbitrate([sig("breakout_sr", Direction.BUY, 0.9)], pb, 100_000.0, now)
    print("A2 lone signal       ->", r2)
    assert r2 == []

    # A3: 3 agree but all SAME family -> rejected (need >=2 families)
    same_fam = [sig("breakout_sr", Direction.BUY, 0.8),
                sig("donchian_breakout", Direction.BUY, 0.8),
                sig("pivot_bounce", Direction.BUY, 0.8)]          # all S/R
    r3 = arb.arbitrate(same_fam, pb, 100_000.0, now)
    print("A3 one family        ->", r3)
    assert r3 == []

    # ---- B) full pipeline on a clean uptrend (regime=TREND) ---- #
    broker = PaperBroker(initial_balance=100_000.0)
    closes = [100 + i * 1.0 for i in range(26)]
    bars = [Bar(now + timedelta(minutes=i), c, c + 0.5, c - 0.5, c)
            for i, c in enumerate(closes)]
    broker.set_bars("XAUUSD", bars)
    assert detect_regime(bars) == Regime.TREND

    engine = FtmoComplianceEngine(
        AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.CHALLENGE, 100_000))
    engine.mark_reconciled()
    engine.roll_daily_baseline(now, 100_000)

    with PgStateStore() as store:
        ex = ExecutionEngine(broker, store, engine)
        sigs = arb.collect(DEFAULT_STRATEGIES, ["XAUUSD"], broker, now)
        print("\nB) signals  ->", [(s.strategy_id, s.direction.value,
                                    round(s.confidence, 2)) for s in sigs])
        reqs = arb.arbitrate(sigs, broker, broker.equity(), now)
        test_coid = None
        if not reqs:
            print("   (not enough confluence on this synthetic bar set — OK)")
        else:
            print("   RATIONALE:", reqs[0].rationale)
            snap = AccountSnapshot(broker.balance(), broker.equity(),
                                   broker.open_risk_to_sl())
            res = ex.submit(reqs[0], snap, now)
            test_coid = res.client_order_id          # clean up ONLY this row
            assert res.submitted and broker.open_positions()
            assert arb.arbitrate(sigs, broker, broker.equity(), now) == []  # cap
            row = store.conn.execute(
                "SELECT rationale FROM orders WHERE broker_ticket=%s",
                (res.ticket,)).fetchone()
            assert row and row[0] and "เห็นพ้องรวม" in row[0]   # Thai rationale persisted
            print("   persisted rationale in DB:", bool(row[0]))
            ex.close(next(iter(broker._pos)), reason="test")

        # Delete ONLY the order this self-test created — NEVER a broad
        # "strategy_id LIKE 'ensemble:%'", which also matches REAL production
        # orders (that broad DELETE once wiped live trade history when the test
        # was run against the live DB).
        if test_coid is not None:
            store.conn.execute(
                "DELETE FROM positions WHERE client_order_id = %s", (test_coid,))
            store.conn.execute(
                "DELETE FROM orders WHERE client_order_id = %s", (test_coid,))

    print("\nArbitration confluence + detailed rationale OK.")
