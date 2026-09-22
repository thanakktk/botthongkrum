"""
Trading-session detection (UTC)
======================================================================
XAUUSD moves most during the London and New York sessions; the Asian session is
quieter. Filtering entries to the right session is a real edge filter. Windows
are approximate UTC hours (DST shifts London/NY ~1h — tune if needed).
"""

from __future__ import annotations

from datetime import datetime

SESSIONS = {
    "asian":  (0, 9),     # Tokyo
    "london": (7, 16),
    "ny":     (12, 21),   # New York (overlaps London 12–16 = the most active)
}


def active_sessions(now_utc: datetime) -> set[str]:
    h = now_utc.hour + now_utc.minute / 60.0
    return {name for name, (a, b) in SESSIONS.items() if a <= h < b}


def in_allowed(now_utc: datetime, allowed) -> bool:
    """True if any allowed session is active (or no restriction was set)."""
    if not allowed:
        return True
    return bool(active_sessions(now_utc) & set(allowed))


def label(now_utc: datetime) -> str:
    act = active_sessions(now_utc)
    return "+".join(sorted(act)) if act else "off-session"
