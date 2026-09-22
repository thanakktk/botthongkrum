"""
Context-Aware League System (Pillar 4)
======================================================================
Tracks each strategy's realized outcomes PER REGIME and turns that standing into
a weight multiplier for the Arbitrator. The regime conditioning is the point:
a trend strategy is judged on its trend-regime trades, not punished for a bad
run while the market was ranging.

Lifecycle (per strategy_id x regime):
  active     -> normal weight, scaled mildly by win rate
  benched    -> weight 0 for a probation window (poor record once we have data)
  probation  -> reduced weight to re-test; promote back to active if it recovers,
                otherwise bench again. (A safe re-entry path, not a permanent ban.)

State is persisted in strategy_league so standings survive restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from signals import Regime


@dataclass
class LeagueConfig:
    min_sample: int = 8           # trades before we judge an active strategy
    bench_winrate: float = 0.35   # below this (with data) -> bench
    probation_days: float = 3.0   # benched duration before a re-test
    probation_weight: float = 0.3 # reduced weight while on probation
    promote_winrate: float = 0.45 # beat this during probation -> back to active
    probation_sample: int = 4     # extra trades needed to judge a probation run


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


class League:
    def __init__(self, store, cfg: Optional[LeagueConfig] = None):
        self.conn = store.conn          # PgStateStore (autocommit) connection
        self.cfg = cfg or LeagueConfig()

    # ----- persistence ----------------------------------------------------- #
    def _row(self, sid: str, regime: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT strategy_id, regime, trades, wins, gross_pnl, status, "
            "probation_until FROM strategy_league "
            "WHERE strategy_id = %s AND regime = %s", (sid, regime)).fetchone()
        if not r:
            return None
        return {"strategy_id": r[0], "regime": r[1], "trades": int(r[2]),
                "wins": int(r[3]), "gross_pnl": float(r[4]), "status": r[5],
                "probation_until": r[6]}

    def _set_status(self, sid: str, regime: str, status: str,
                    probation_until: Optional[datetime]) -> None:
        self.conn.execute(
            "UPDATE strategy_league SET status = %s, probation_until = %s, "
            "benched_at = CASE WHEN %s = 'benched' THEN now() ELSE benched_at END, "
            "updated_at = now() WHERE strategy_id = %s AND regime = %s",
            (status, probation_until, status, sid, regime))

    def _reset_stats(self, sid: str, regime: str) -> None:
        # Probation is a FRESH re-test: zero the counters so the old losing
        # record doesn't drag the win rate down and force an instant re-bench.
        self.conn.execute(
            "UPDATE strategy_league SET trades = 0, wins = 0, gross_pnl = 0, "
            "updated_at = now() WHERE strategy_id = %s AND regime = %s",
            (sid, regime))

    # ----- outcome recording ---------------------------------------------- #
    def record_outcome(self, sid: str, regime: Regime | str, pnl: float,
                       now: datetime) -> None:
        rg = regime.value if isinstance(regime, Regime) else regime
        win = 1 if pnl > 0 else 0
        self.conn.execute(
            """
            INSERT INTO strategy_league (strategy_id, regime, trades, wins, gross_pnl)
            VALUES (%s, %s, 1, %s, %s)
            ON CONFLICT (strategy_id, regime) DO UPDATE SET
                trades    = strategy_league.trades + 1,
                wins      = strategy_league.wins + EXCLUDED.wins,
                gross_pnl = strategy_league.gross_pnl + EXCLUDED.gross_pnl,
                updated_at = now()
            """, (sid, rg, win, pnl))
        self._reevaluate(sid, rg, now)

    def _reevaluate(self, sid: str, rg: str, now: datetime) -> None:
        row = self._row(sid, rg)
        if not row or row["trades"] == 0:
            return
        wr = row["wins"] / row["trades"]
        status = row["status"]

        if status == "probation":
            # stats were reset on probation entry, so judge the re-test alone
            if row["trades"] >= self.cfg.probation_sample:
                if wr >= self.cfg.promote_winrate:
                    self._set_status(sid, rg, "active", None)
                elif wr < self.cfg.bench_winrate:
                    self._set_status(sid, rg, "benched",
                                     now + timedelta(days=self.cfg.probation_days))
        else:  # active
            if row["trades"] >= self.cfg.min_sample and \
                    (wr < self.cfg.bench_winrate or row["gross_pnl"] < 0):
                self._set_status(sid, rg, "benched",
                                 now + timedelta(days=self.cfg.probation_days))

    # ----- weighting (consumed by the Arbitrator) -------------------------- #
    def weight_multiplier(self, sid: str, regime: Regime | str,
                          now: datetime) -> float:
        rg = regime.value if isinstance(regime, Regime) else regime
        row = self._row(sid, rg)
        if not row:
            return 1.0                      # no history -> neutral
        status = row["status"]
        if status == "benched":
            if row["probation_until"] and now >= row["probation_until"]:
                self._reset_stats(sid, rg)                     # fresh re-test
                self._set_status(sid, rg, "probation", None)
                return self.cfg.probation_weight
            return 0.0
        if status == "probation":
            return self.cfg.probation_weight
        if row["trades"] < self.cfg.min_sample:
            return 1.0
        wr = row["wins"] / row["trades"]
        return _clamp(0.5 + wr, 0.5, 1.5)

    def standings(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT strategy_id, regime, trades, wins, gross_pnl, status "
            "FROM strategy_league ORDER BY strategy_id, regime").fetchall()
        return [{"strategy_id": r[0], "regime": r[1], "trades": int(r[2]),
                 "wins": int(r[3]), "gross_pnl": float(r[4]), "status": r[5]}
                for r in rows]


# --------------------------------------------------------------------------- #
# Self-test: bench a loser, let it serve probation, promote it on recovery.    #
#   ./env/Scripts/python.exe league.py                                         #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone
    from pg_state_store import PgStateStore

    SID = "test-league"
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)

    with PgStateStore() as store:
        store.conn.execute("DELETE FROM strategy_league WHERE strategy_id = %s", (SID,))
        lg = League(store)

        # 10 mostly-losing TREND trades -> should bench
        for i in range(10):
            lg.record_outcome(SID, Regime.TREND, pnl=(50 if i < 2 else -40), now=now)
        st = lg._row(SID, "trend")
        w = lg.weight_multiplier(SID, Regime.TREND, now)
        print(f"after losses: trades={st['trades']} wins={st['wins']} "
              f"status={st['status']} weight={w}")
        assert st["status"] == "benched" and w < 1e-9

        # still benched within the probation window
        assert lg.weight_multiplier(SID, Regime.TREND, now + timedelta(days=1)) < 1e-9

        # after the window -> probation (reduced weight, allowed to re-test)
        later = now + timedelta(days=4)
        w2 = lg.weight_multiplier(SID, Regime.TREND, later)
        print(f"after window: status={lg._row(SID,'trend')['status']} weight={w2}")
        assert lg._row(SID, "trend")["status"] == "probation" and abs(w2 - 0.3) < 1e-9

        # a winning probation run -> promoted back to active
        for _ in range(6):
            lg.record_outcome(SID, Regime.TREND, pnl=80, now=later)
        st3 = lg._row(SID, "trend")
        w3 = lg.weight_multiplier(SID, Regime.TREND, later)
        print(f"after recovery: trades={st3['trades']} wins={st3['wins']} "
              f"status={st3['status']} weight={round(w3,2)}")
        assert st3["status"] == "active" and w3 > 0.3

        store.conn.execute("DELETE FROM strategy_league WHERE strategy_id = %s", (SID,))
        print("\nLeague bench -> probation -> promote cycle OK.")
