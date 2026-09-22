"""
Economic-calendar feed (ForexFactory weekly JSON)
======================================================================
Source: nfs.faireconomy.media — free, keyless, JSON, the de-facto ForexFactory
calendar feed. We keep only HIGH-impact events and flag the XAUUSD-driving ones
(high-impact in NEWS_CURRENCIES, default USD) as `relevant`.

Feed quirks honored: the currency field is named **country**; `impact` is a
string ("High"/"Medium"/"Low"/"Holiday"); `date` is ISO-8601 with an embedded
US-Eastern offset (parse offset-aware -> UTC); `actual` is usually only present
in lastweek. Poll hourly — it refreshes ~weekly.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional, Sequence

from dotenv import load_dotenv

load_dotenv()

# Only the current-week file is published on this CDN (nextweek/lastweek 404).
# It carries the full Mon–Sun week; next week's events appear when it rolls over.
# Poll HOURLY — the CDN returns HTTP 429 if hammered.
_BASE = "https://nfs.faireconomy.media/ff_calendar_{}.json"
FEEDS = ("thisweek",)
RELEVANT_CCYS = {c.strip().upper()
                 for c in os.getenv("NEWS_CURRENCIES", "USD").split(",") if c.strip()}


def fetch(which: str = "thisweek", timeout: float = 30.0) -> list[dict]:
    req = urllib.request.Request(
        _BASE.format(which),
        headers={"User-Agent": "Mozilla/5.0 (compatible; XAUUSD-bot/1.0)"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _to_utc(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def parse(raw: Sequence[dict]) -> list[dict]:
    out = []
    for e in raw:
        t = _to_utc(e.get("date", ""))
        if t is None:
            continue
        ccy = (e.get("country") or "").upper()     # feed field is `country`
        impact = e.get("impact")
        out.append({
            "event_time": t, "currency": ccy, "title": e.get("title", ""),
            "impact": impact,
            "forecast": e.get("forecast") or None,
            "previous": e.get("previous") or None,
            "actual": e.get("actual") or None,
            "relevant": impact == "High" and ccy in RELEVANT_CCYS,
        })
    return out


def refresh(conn, which: Sequence[str] = FEEDS) -> int:
    """Fetch the weekly feeds and upsert HIGH-impact events. Returns the count
    upserted. Never resets the `alerted` flag (so we don't re-ping)."""
    events: list[dict] = []
    for w in which:
        try:
            events += parse(fetch(w))
        except Exception as e:                      # a feed hiccup must not crash
            print(f"[news] fetch {w} failed: {e}")
    high = [e for e in events if e["impact"] == "High"]
    for e in high:
        conn.execute(
            """
            INSERT INTO news_events (event_time, currency, title, impact,
                                     forecast, previous, actual, relevant)
            VALUES (%(event_time)s, %(currency)s, %(title)s, %(impact)s,
                    %(forecast)s, %(previous)s, %(actual)s, %(relevant)s)
            ON CONFLICT (event_time, currency, title) DO UPDATE SET
                forecast = EXCLUDED.forecast,
                previous = EXCLUDED.previous,
                actual   = EXCLUDED.actual,
                impact   = EXCLUDED.impact,
                relevant = EXCLUDED.relevant,
                fetched_at = now()
            """, e)
    return len(high)


def upcoming(conn, within_hours: float = 72.0, only_relevant: bool = True,
             now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    upper = now + timedelta(hours=within_hours)
    q = ("SELECT event_time, currency, title, impact, forecast, previous, "
         "actual, relevant FROM news_events "
         "WHERE event_time >= %s AND event_time <= %s")
    if only_relevant:
        q += " AND relevant = TRUE"
    q += " ORDER BY event_time LIMIT 50"
    rows = conn.execute(q, (now, upper)).fetchall()
    return [{"event_time": r[0], "currency": r[1], "title": r[2], "impact": r[3],
             "forecast": r[4], "previous": r[5], "actual": r[6], "relevant": r[7]}
            for r in rows]


def due_for_alert(conn, lead_minutes: float = 15.0,
                  now: Optional[datetime] = None) -> list[dict]:
    """Relevant events firing within `lead_minutes` that haven't been alerted."""
    now = now or datetime.now(timezone.utc)
    upper = now + timedelta(minutes=lead_minutes)
    rows = conn.execute(
        "SELECT id, event_time, currency, title, impact, forecast, previous, actual "
        "FROM news_events WHERE relevant = TRUE AND alerted = FALSE "
        "AND event_time >= %s AND event_time <= %s ORDER BY event_time",
        (now, upper)).fetchall()
    return [{"id": r[0], "event_time": r[1], "currency": r[2], "title": r[3],
             "impact": r[4], "forecast": r[5], "previous": r[6], "actual": r[7]}
            for r in rows]


def mark_alerted(conn, event_id: int) -> None:
    conn.execute("UPDATE news_events SET alerted = TRUE WHERE id = %s", (event_id,))


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import db
    print("currencies of interest:", RELEVANT_CCYS)
    with db.connect(autocommit=True) as c:
        n = refresh(c)
        print(f"upserted {n} high-impact events")
        ups = upcoming(c, within_hours=24 * 14)
        print(f"upcoming relevant (next 14d): {len(ups)}")
        for e in ups[:8]:
            print(f"  {e['event_time']}  {e['currency']:4s} {e['impact']:6s} "
                  f"{e['title']}  (f={e['forecast']} p={e['previous']})")
