"""
FTMO Compliance Engine
======================================================================
The single component with VETO power over every order. It enforces FTMO's
hard rules with a safety buffer so the bot stops BEFORE the real limit.

NOT every account is an FTMO account. Set RULES_MODE=none in .env for a
plain broker account and none of the rules below are enforced — see
"Env-driven construction" at the bottom of this module.

Rule provenance (FTMO official Academy / Trading Objectives, verified mid-2026):
  * Max Daily Loss limit (an EQUITY floor for the CET day):
        daily_floor = (balance at 00:00 CET that opened the day)
                      - daily_loss_pct * initial_capital
        daily_loss_pct = 0.05 (2-Step)  |  0.03 (1-Step)
        First day: the "midnight balance" is the initial capital.
        Equity = balance + floating P/L +/- swaps - commissions must stay
        ABOVE this floor at all times.  -> uses BALANCE at midnight, not equity.
  * Max (overall) Loss limit (an EQUITY floor for the whole account):
        2-Step  -> STATIC : floor = initial_capital - 0.10 * initial_capital
        1-Step  -> TRAILING (end-of-day): floor =
                   max(highest midnight balance ever, initial_capital)
                   - 0.10 * initial_capital     (monotonically non-decreasing)
  * Consistency rule (Standard variant only): no single CET day's profit may
    exceed 50% of total profit. (Swing exempt.)
  * Time rules: Swing has none. Standard funded must be flat through high-impact
    news windows; Standard must be flat over the weekend.

!! VERIFY all numeric values + the "balance vs equity at midnight" basis against
   FTMO's current official ruleset before going live. They change periodically. !!
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, date, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
from typing import Optional, Sequence

# FTMO server clock. CET/CEST, DST-aware. (Europe/Prague == CET zone.)
FTMO_TZ = ZoneInfo("Europe/Prague")


# --------------------------------------------------------------------------- #
# Enums                                                                       #
# --------------------------------------------------------------------------- #
class Variant(str, Enum):
    STANDARD = "standard"
    SWING = "swing"


class Path(str, Enum):
    ONE_STEP = "1-step"
    TWO_STEP = "2-step"


class Phase(str, Enum):
    CHALLENGE = "challenge"
    VERIFICATION = "verification"
    FUNDED = "funded"


class Action(str, Enum):
    NORMAL = "normal"          # trading allowed
    HALT_NEW = "halt_new"      # no new entries; keep managing open trades
    FLATTEN_ALL = "flatten_all"  # close everything and halt (kill-switch level)


class Reason(str, Enum):
    OK = "ok"
    DAILY_SOFT = "daily_loss_soft_buffer"
    DAILY_HARD = "daily_loss_hard_buffer"
    OVERALL_SOFT = "overall_loss_soft_buffer"
    OVERALL_HARD = "overall_loss_hard_buffer"
    PRETRADE_DAILY = "pretrade_worstcase_daily"
    PRETRADE_OVERALL = "pretrade_worstcase_overall"
    NEWS_BLACKOUT = "news_blackout"
    WEEKEND_FLATTEN = "weekend_flatten_window"
    SESSION_FLATTEN = "session_flatten_window"
    CONSISTENCY = "consistency_throttle"
    HALTED = "engine_halted"
    NOT_RECONCILED = "state_not_reconciled"


# --------------------------------------------------------------------------- #
# Rule parameters derived from the account path                               #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleParams:
    daily_loss_pct: float
    overall_loss_pct: float
    overall_trailing: bool        # True -> 1-Step EOD trailing; False -> 2-Step static
    min_trading_days: int

    @staticmethod
    def for_path(path: Path) -> "RuleParams":
        if path == Path.ONE_STEP:
            return RuleParams(0.03, 0.10, overall_trailing=True, min_trading_days=0)
        return RuleParams(0.05, 0.10, overall_trailing=False, min_trading_days=4)


@dataclass
class AccountProfile:
    variant: Variant
    path: Path
    phase: Phase
    initial_capital: float
    # Loss-budget overrides for an account NOT under FTMO's ruleset (a plain
    # broker demo/live account). None -> use FTMO's numbers for `path`.
    # See RULES_MODE / SELF_*_LOSS_PCT at the bottom of this module.
    daily_loss_pct: Optional[float] = None
    overall_loss_pct: Optional[float] = None

    @property
    def params(self) -> RuleParams:
        p = RuleParams.for_path(self.path)
        if self.daily_loss_pct is None and self.overall_loss_pct is None:
            return p
        return replace(
            p,
            daily_loss_pct=(p.daily_loss_pct if self.daily_loss_pct is None
                            else self.daily_loss_pct),
            overall_loss_pct=(p.overall_loss_pct if self.overall_loss_pct is None
                              else self.overall_loss_pct),
        )


# --------------------------------------------------------------------------- #
# Engine configuration (buffers + flatten windows)                            #
# --------------------------------------------------------------------------- #
@dataclass
class EngineConfig:
    # Fraction of the loss budget the bot is allowed to use before reacting.
    # soft -> halt new entries; hard -> flatten all. Both sit ABOVE the real floor.
    daily_soft_frac: float = 0.80     # 2-Step: halt at ~4% (0.8 * 5%)
    daily_hard_frac: float = 0.90     # 2-Step: flatten at ~4.5%
    overall_soft_frac: float = 0.80
    overall_hard_frac: float = 0.90

    # News blackout half-window in seconds. FTMO official = 2 min each side of a
    # high-impact release on the TARGETED instrument (funded Standard only).
    news_window_secs: int = 120

    # Standard weekend window: be flat / no new entries from this CET time on
    # Friday (weekday 4), through all of Saturday, until the Sunday reopen below.
    weekend_flatten_after: tuple[int, int] = (20, 0)   # 20:00 CET Friday (verify)
    # Sunday CET time the weekend window lifts (market reopen). None = block all
    # of Sunday too. (FX reopens ~23:00 CET Sun; crypto trades through — but a
    # Standard account must not carry weekend exposure regardless.) VERIFY.
    weekend_reopen_after: Optional[tuple[int, int]] = (23, 0)

    # OPTIONAL Standard daily flatten (instruments with a daily session close /
    # no-overnight policy). Leave as None for 24/5 FX where only weekend applies.
    # VERIFY whether your Standard account forbids weekday overnight holding.
    session_flatten_after: Optional[tuple[int, int]] = None

    # --- rule toggles ------------------------------------------------------ #
    # A prop-firm account is bound by all of these; a plain broker account by
    # none of them. Defaults keep the full FTMO ruleset ON; `config_from_env()`
    # switches them off when RULES_MODE says this is not an FTMO account.
    # Per-trade risk sizing and the kill switch are separate and stay active.
    enforce_daily_loss: bool = True
    enforce_overall_loss: bool = True
    enforce_consistency: bool = True
    enforce_weekend_flatten: bool = True
    enforce_news_blackout: bool = True


# --------------------------------------------------------------------------- #
# Live account snapshot fed in every tick                                     #
# --------------------------------------------------------------------------- #
@dataclass
class AccountSnapshot:
    balance: float                 # closed balance
    equity: float                  # balance + floating P/L +/- swaps - commissions
    # Total additional loss (positive $) if EVERY open position hit its stop loss
    # from the current price. Used for worst-case pre-trade gating.
    open_risk_to_sl: float = 0.0


@dataclass
class NewsEvent:
    when_utc: datetime             # scheduled high-impact release time (UTC)
    label: str = ""


@dataclass
class Verdict:
    action: Action
    reason: Reason
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == Action.NORMAL


# --------------------------------------------------------------------------- #
# The engine                                                                  #
# --------------------------------------------------------------------------- #
class FtmoComplianceEngine:
    def __init__(self, profile: AccountProfile, config: EngineConfig | None = None):
        self.profile = profile
        self.cfg = config or EngineConfig()

        # --- daily anchor: BALANCE at the 00:00 CET that opened the current day
        self.current_cet_date: Optional[date] = None
        self.midnight_balance: float = profile.initial_capital

        # --- overall anchor: highest midnight balance ever (for 1-Step trailing)
        self.highest_midnight_balance: float = profile.initial_capital

        # --- consistency tracking (Standard): per-day realized profit + total
        self.day_realized_profit: dict[date, float] = {}

        # Engine refuses to authorize anything until reconciliation has run.
        self.reconciled: bool = False
        # Manual / watchdog kill.
        self.halted: bool = False

    # ----- lifecycle ------------------------------------------------------- #
    def mark_reconciled(self) -> None:
        self.reconciled = True

    def halt(self) -> None:
        self.halted = True

    def resume(self) -> None:
        self.halted = False

    # ----- CET day handling ------------------------------------------------ #
    @staticmethod
    def cet_date(now_utc: datetime) -> date:
        return now_utc.astimezone(FTMO_TZ).date()

    def roll_daily_baseline(self, now_utc: datetime, midnight_balance: float) -> None:
        """Call exactly once per CET day at/after the 00:00 CET rollover, passing
        the closed BALANCE captured at that midnight. Also call after reconciliation
        to seed the current day if a baseline is missing.
        """
        d = self.cet_date(now_utc)
        self.current_cet_date = d
        self.midnight_balance = midnight_balance
        # Overall trailing anchor only ever increases.
        self.highest_midnight_balance = max(self.highest_midnight_balance,
                                            midnight_balance)

    # ----- floor math ------------------------------------------------------ #
    @property
    def daily_budget(self) -> float:
        return self.profile.params.daily_loss_pct * self.profile.initial_capital

    @property
    def overall_budget(self) -> float:
        return self.profile.params.overall_loss_pct * self.profile.initial_capital

    def real_daily_floor(self) -> float:
        return self.midnight_balance - self.daily_budget

    def real_overall_floor(self) -> float:
        if self.profile.params.overall_trailing:
            anchor = max(self.highest_midnight_balance, self.profile.initial_capital)
        else:
            anchor = self.profile.initial_capital
        return anchor - self.overall_budget

    def _buffered(self, anchor: float, budget: float, frac: float) -> float:
        return anchor - frac * budget

    def daily_soft_floor(self) -> float:
        return self._buffered(self.midnight_balance, self.daily_budget,
                              self.cfg.daily_soft_frac)

    def daily_hard_floor(self) -> float:
        return self._buffered(self.midnight_balance, self.daily_budget,
                              self.cfg.daily_hard_frac)

    def overall_soft_floor(self) -> float:
        base = self.real_overall_floor() + self.overall_budget  # the anchor
        return base - self.cfg.overall_soft_frac * self.overall_budget

    def overall_hard_floor(self) -> float:
        base = self.real_overall_floor() + self.overall_budget
        return base - self.cfg.overall_hard_frac * self.overall_budget

    # ----- continuous monitor (call every tick) ---------------------------- #
    def evaluate(self, snap: AccountSnapshot, now_utc: datetime) -> Verdict:
        if self.halted:
            return Verdict(Action.HALT_NEW, Reason.HALTED, "engine halted")
        if not self.reconciled:
            return Verdict(Action.HALT_NEW, Reason.NOT_RECONCILED,
                           "awaiting state reconciliation")

        eq = snap.equity

        # Hard floors first -> flatten everything (kill-switch territory).
        if self.cfg.enforce_overall_loss and eq <= self.overall_hard_floor():
            return Verdict(Action.FLATTEN_ALL, Reason.OVERALL_HARD,
                           f"equity {eq:.2f} <= overall hard floor "
                           f"{self.overall_hard_floor():.2f}")
        if self.cfg.enforce_daily_loss and eq <= self.daily_hard_floor():
            return Verdict(Action.FLATTEN_ALL, Reason.DAILY_HARD,
                           f"equity {eq:.2f} <= daily hard floor "
                           f"{self.daily_hard_floor():.2f}")

        # Soft floors -> stop opening new trades, keep managing open ones.
        if self.cfg.enforce_overall_loss and eq <= self.overall_soft_floor():
            return Verdict(Action.HALT_NEW, Reason.OVERALL_SOFT,
                           f"equity {eq:.2f} <= overall soft floor "
                           f"{self.overall_soft_floor():.2f}")
        if self.cfg.enforce_daily_loss and eq <= self.daily_soft_floor():
            return Verdict(Action.HALT_NEW, Reason.DAILY_SOFT,
                           f"equity {eq:.2f} <= daily soft floor "
                           f"{self.daily_soft_floor():.2f}")

        return Verdict(Action.NORMAL, Reason.OK)

    # ----- pre-trade veto (call before sending any order) ------------------ #
    def check_new_order(self, *, snap: AccountSnapshot, new_order_risk: float,
                        now_utc: datetime,
                        upcoming_news: Sequence[NewsEvent] = ()) -> Verdict:
        """new_order_risk: positive $ the proposed order loses if its SL is hit.
        Gating is conservative: assume every currently-open position AND the new
        order all hit their stops simultaneously, and require that worst-case
        equity still sits above the SOFT floors. This is what links position
        sizing to FTMO survival.
        """
        base = self.evaluate(snap, now_utc)
        if base.action != Action.NORMAL:
            return base  # already halted/flattening for some reason

        # Time-based gates (variant + phase aware).
        tgate = self._time_gate(now_utc, upcoming_news)
        if tgate is not None:
            return tgate

        # Worst-case: all open SLs hit + new order SL hit.
        worst_eq = snap.equity - snap.open_risk_to_sl - max(new_order_risk, 0.0)
        if self.cfg.enforce_overall_loss and worst_eq <= self.overall_soft_floor():
            return Verdict(Action.HALT_NEW, Reason.PRETRADE_OVERALL,
                           f"worst-case equity {worst_eq:.2f} would breach overall "
                           f"soft floor {self.overall_soft_floor():.2f}")
        if self.cfg.enforce_daily_loss and worst_eq <= self.daily_soft_floor():
            return Verdict(Action.HALT_NEW, Reason.PRETRADE_DAILY,
                           f"worst-case equity {worst_eq:.2f} would breach daily "
                           f"soft floor {self.daily_soft_floor():.2f}")

        # Consistency throttle (Standard only).
        cgate = self._consistency_gate(now_utc)
        if cgate is not None:
            return cgate

        return Verdict(Action.NORMAL, Reason.OK)

    # ----- time-based rules ------------------------------------------------ #
    def _time_gate(self, now_utc: datetime,
                   upcoming_news: Sequence[NewsEvent]) -> Optional[Verdict]:
        # Swing variant: no time restrictions at all.
        if self.profile.variant == Variant.SWING:
            return None

        now_cet = now_utc.astimezone(FTMO_TZ)

        # News blackout: Standard, FUNDED stage only (not during evaluation).
        if self.cfg.enforce_news_blackout and self.profile.phase == Phase.FUNDED:
            w = timedelta(seconds=self.cfg.news_window_secs)
            for ev in upcoming_news:
                if abs((now_utc - ev.when_utc).total_seconds()) <= w.total_seconds():
                    return Verdict(Action.HALT_NEW, Reason.NEWS_BLACKOUT,
                                   f"within news window of '{ev.label}'")

        # Weekend window (Standard): block new entries Fri-after-cutoff -> Sat ->
        # Sun-until-reopen. (Previously only blocked Friday — Sat/Sun leaked.)
        if self.cfg.enforce_weekend_flatten and self._in_weekend_window(now_cet):
            return Verdict(Action.HALT_NEW, Reason.WEEKEND_FLATTEN,
                           "weekend flatten window (Standard)")

        # Optional daily session flatten (verify per instrument).
        if self.cfg.session_flatten_after is not None:
            sa_h, sa_m = self.cfg.session_flatten_after
            if (now_cet.hour, now_cet.minute) >= (sa_h, sa_m):
                return Verdict(Action.HALT_NEW, Reason.SESSION_FLATTEN,
                               "daily session-flatten window (Standard)")
        return None

    def _in_weekend_window(self, now_cet: datetime) -> bool:
        """True when a Standard account must stay flat: from the Friday cutoff,
        through all of Saturday, until the Sunday reopen time."""
        wd = now_cet.weekday()                  # Mon=0 .. Fri=4, Sat=5, Sun=6
        hm = (now_cet.hour, now_cet.minute)
        if wd == 4:                             # Friday
            return hm >= self.cfg.weekend_flatten_after
        if wd == 5:                             # Saturday — all day
            return True
        if wd == 6:                             # Sunday — until reopen
            ra = self.cfg.weekend_reopen_after
            return ra is None or hm < ra
        return False

    def time_flatten_required(self, now_utc: datetime) -> Optional[Reason]:
        """For a Standard account, the reason it must be FLAT right now (weekend
        or daily-session window), else None. This is ROUTINE flattening of open
        positions — distinct from a kill-switch FLATTEN_ALL, so it must NOT latch
        the watchdog. Swing accounts are exempt."""
        if self.profile.variant == Variant.SWING:
            return None
        now_cet = now_utc.astimezone(FTMO_TZ)
        if self.cfg.enforce_weekend_flatten and self._in_weekend_window(now_cet):
            return Reason.WEEKEND_FLATTEN
        if self.cfg.session_flatten_after is not None and \
                (now_cet.hour, now_cet.minute) >= self.cfg.session_flatten_after:
            return Reason.SESSION_FLATTEN
        return None

    # ----- consistency rule (Standard) ------------------------------------- #
    def record_realized(self, now_utc: datetime, realized_pnl: float) -> None:
        d = self.cet_date(now_utc)
        self.day_realized_profit[d] = self.day_realized_profit.get(d, 0.0) + realized_pnl

    def _consistency_gate(self, now_utc: datetime) -> Optional[Verdict]:
        if not self.cfg.enforce_consistency:
            return None
        if self.profile.variant != Variant.STANDARD:
            return None
        total = sum(v for v in self.day_realized_profit.values() if v > 0)
        if total <= 0:
            return None
        today = self.day_realized_profit.get(self.cet_date(now_utc), 0.0)
        # If today's profit already exceeds 50% of total profit, throttle new risk
        # to avoid concentrating gains in a single day (fails the consistency check).
        if today > 0 and today >= 0.50 * total:
            return Verdict(Action.HALT_NEW, Reason.CONSISTENCY,
                           f"today's profit {today:.2f} >= 50% of total {total:.2f}")
        return None


# --------------------------------------------------------------------------- #
# Env-driven construction (single source of truth for every process)          #
# --------------------------------------------------------------------------- #
# RULES_MODE picks the rulebook this engine polices:
#   "ftmo" (default) -> the full FTMO prop-firm ruleset, unchanged.
#   "none"           -> a plain broker account (e.g. a VT Markets demo). FTMO's
#                       rules do not exist there, so the daily/overall loss
#                       floors, the consistency rule, the weekend flatten and
#                       the news blackout are all OFF.
# Active in BOTH modes: per-trade risk sizing (--risk-pct), the shared kill
# switch, and the reconciliation gate.
#
# Optional self-imposed floors, honoured in EITHER mode when set > 0 (fractions
# of ACCOUNT_INITIAL_CAPITAL). Under RULES_MODE=none these are the ONLY equity
# floors the engine has:
#   SELF_DAILY_LOSS_PCT=0.05     -> halt at 4% / flatten at 4.5% loss for the day
#   SELF_OVERALL_LOSS_PCT=0.10   -> same buffers against a 10% total loss
_NO_RULES = {"none", "off", "broker", "raw", "no", "false", "0"}


def rules_mode() -> str:
    return (os.getenv("RULES_MODE", "ftmo") or "ftmo").strip().lower()


def ftmo_rules_active() -> bool:
    return rules_mode() not in _NO_RULES


def _self_loss_limits() -> tuple[Optional[float], Optional[float]]:
    """(daily_pct, overall_pct) self-imposed floors from .env; None when unset."""
    def pct(key: str) -> Optional[float]:
        raw = (os.getenv(key) or "").strip()
        if not raw:
            return None
        try:
            val = float(raw)
        except ValueError:
            return None
        return val if val > 0 else None
    return pct("SELF_DAILY_LOSS_PCT"), pct("SELF_OVERALL_LOSS_PCT")


def profile_from_env() -> AccountProfile:
    daily, overall = _self_loss_limits()
    return AccountProfile(
        variant=Variant(os.getenv("ACCOUNT_VARIANT", "standard").strip()),
        path=Path(os.getenv("ACCOUNT_PATH", "2-step").strip()),
        phase=Phase(os.getenv("ACCOUNT_PHASE", "challenge").strip()),
        initial_capital=float(os.getenv("ACCOUNT_INITIAL_CAPITAL", "100000")),
        daily_loss_pct=daily,
        overall_loss_pct=overall,
    )


def apply_env_loss_limits(profile: AccountProfile) -> AccountProfile:
    """Stamp the self-imposed floors onto a profile loaded from the DB — the
    watchdog builds its profile from `account_profile`, not from .env."""
    profile.daily_loss_pct, profile.overall_loss_pct = _self_loss_limits()
    return profile


def config_from_env(base: EngineConfig | None = None) -> EngineConfig:
    cfg = base or EngineConfig()
    if ftmo_rules_active():
        return cfg
    daily, overall = _self_loss_limits()
    return replace(
        cfg,
        # Only floors the user opted into survive; everything FTMO-specific off.
        enforce_daily_loss=daily is not None,
        enforce_overall_loss=overall is not None,
        enforce_consistency=False,
        enforce_weekend_flatten=False,
        enforce_news_blackout=False,
    )


def rules_summary(profile: AccountProfile, cfg: EngineConfig) -> str:
    """One line describing what the engine will actually enforce."""
    bits = []
    if cfg.enforce_daily_loss:
        bits.append(f"daily-loss {profile.params.daily_loss_pct:.2%}")
    if cfg.enforce_overall_loss:
        bits.append(f"overall-loss {profile.params.overall_loss_pct:.2%}")
    if cfg.enforce_consistency:
        bits.append("consistency")
    if cfg.enforce_weekend_flatten:
        bits.append("weekend-flatten")
    if cfg.enforce_news_blackout:
        bits.append("news-blackout")
    label = "FTMO" if ftmo_rules_active() else "broker / no FTMO rules"
    return (f"RULES_MODE={rules_mode()} [{label}] -> "
            + (", ".join(bits) or "NO equity floors (per-trade risk sizing only)"))


# --------------------------------------------------------------------------- #
# Self-test of the core math (run: python ftmo_compliance_engine.py)          #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import timezone

    # $200k 2-Step funded Standard account.
    prof = AccountProfile(Variant.STANDARD, Path.TWO_STEP, Phase.FUNDED, 200_000)
    eng = FtmoComplianceEngine(prof)
    eng.mark_reconciled()

    now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)  # Thursday, mid-session
    # Day opened at balance 204,000 (carried profit from prior days).
    eng.roll_daily_baseline(now, midnight_balance=204_000)

    assert eng.daily_budget == 10_000             # 5% of 200k
    assert eng.real_daily_floor() == 194_000      # 204k - 10k  (matches FTMO example)
    assert eng.real_overall_floor() == 180_000    # static: 200k - 20k(10%)
    assert eng.daily_soft_floor() == 196_000      # halt new at 0.8*10k loss
    print("real daily floor   :", eng.real_daily_floor())
    print("daily soft (halt)  :", eng.daily_soft_floor())
    print("daily hard (flat)  :", eng.daily_hard_floor())
    print("overall floor      :", eng.real_overall_floor())

    # Equity comfortable -> NORMAL
    v = eng.evaluate(AccountSnapshot(balance=204_000, equity=203_000), now)
    assert v.action == Action.NORMAL, v

    # Equity drifts to the soft floor -> HALT_NEW
    v = eng.evaluate(AccountSnapshot(balance=200_000, equity=195_900), now)
    assert v.action == Action.HALT_NEW and v.reason == Reason.DAILY_SOFT, v

    # Pre-trade: a new order whose worst case would pierce the soft floor -> veto
    snap = AccountSnapshot(balance=202_000, equity=201_000, open_risk_to_sl=4_000)
    v = eng.check_new_order(snap=snap, new_order_risk=2_000, now_utc=now)
    assert v.action == Action.HALT_NEW and v.reason == Reason.PRETRADE_DAILY, v
    print("pre-trade veto ok  :", v.reason, "-", v.detail)

    # 1-Step trailing overall floor sanity
    prof1 = AccountProfile(Variant.SWING, Path.ONE_STEP, Phase.FUNDED, 100_000)
    eng1 = FtmoComplianceEngine(prof1)
    eng1.roll_daily_baseline(now, midnight_balance=100_000)
    assert eng1.real_overall_floor() == 90_000     # max(100k,100k) - 10k
    eng1.roll_daily_baseline(now + timedelta(days=1), midnight_balance=108_000)
    assert eng1.real_overall_floor() == 98_000     # trails up: 108k - 10k
    eng1.roll_daily_baseline(now + timedelta(days=2), midnight_balance=104_000)
    assert eng1.real_overall_floor() == 98_000     # never decreases
    print("1-step trailing ok : floor stays", eng1.real_overall_floor())

    # Weekend window (Standard): Fri-after-cutoff -> Sat -> Sun-until-reopen.
    # 2026-06-19 is a Friday, 06-20 Saturday, 06-21 Sunday (all in CET here).
    def at(y, m, d, h, mi=0):
        return datetime(y, m, d, h, mi, tzinfo=FTMO_TZ).astimezone(timezone.utc)
    healthy = AccountSnapshot(balance=200_000, equity=204_000)
    # Friday before cutoff -> allowed; after cutoff -> blocked
    assert eng.check_new_order(snap=healthy, new_order_risk=10,
                               now_utc=at(2026, 6, 19, 18)).action == Action.NORMAL
    assert eng.check_new_order(snap=healthy, new_order_risk=10,
                               now_utc=at(2026, 6, 19, 21)).reason == Reason.WEEKEND_FLATTEN
    # Saturday all day -> blocked (this was the leak)
    assert eng.time_flatten_required(at(2026, 6, 20, 12)) == Reason.WEEKEND_FLATTEN
    # Sunday before/after reopen
    assert eng.time_flatten_required(at(2026, 6, 21, 10)) == Reason.WEEKEND_FLATTEN
    assert eng.time_flatten_required(at(2026, 6, 21, 23, 30)) is None
    # Swing is exempt from all time rules
    swing = FtmoComplianceEngine(
        AccountProfile(Variant.SWING, Path.TWO_STEP, Phase.FUNDED, 200_000))
    assert swing.time_flatten_required(at(2026, 6, 20, 12)) is None
    print("weekend window ok  : Fri-cutoff/Sat/Sun blocked, Swing exempt")

    print("\nAll compliance-math self-tests passed.")
