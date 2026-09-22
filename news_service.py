"""
News service (separate process)
======================================================================
Polls the ForexFactory feed hourly into Postgres and fires a Discord alert
ahead of each high-impact XAUUSD-relevant (USD) event. Dedup via the
`alerted` flag so each event pings once. The dashboard reads the same table.

Run:  ./env/Scripts/python.exe news_service.py
      ./env/Scripts/python.exe news_service.py --once        # one cycle, exit
      ./env/Scripts/python.exe news_service.py --test-alert  # send a sample ping
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

import db
import news_feed
from notifier import news_notifier, news_embed


def _alert_due(conn, notifier, lead_minutes: float, now: datetime) -> int:
    sent = 0
    for ev in news_feed.due_for_alert(conn, lead_minutes, now):
        mins = (ev["event_time"] - now).total_seconds() / 60.0
        notifier.send(embeds=[news_embed(
            title=ev["title"], currency=ev["currency"], impact=ev["impact"],
            when=f"in {mins:.0f} min", forecast=ev["forecast"],
            previous=ev["previous"], actual=ev["actual"])], block=True)
        news_feed.mark_alerted(conn, ev["id"])
        print(f"[news] ALERT {ev['title']} ({ev['currency']}) in {mins:.0f}m")
        sent += 1
    return sent


def run(refresh_secs: float = 3600, check_secs: float = 60,
        lead_minutes: float = 15) -> None:
    notifier = news_notifier()
    print(f"[news] up. refresh={refresh_secs}s check={check_secs}s "
          f"lead={lead_minutes}m discord={'on' if notifier.enabled else 'off'}")
    last_refresh = 0.0
    while True:
        try:
            with db.connect(autocommit=True) as c:
                now = datetime.now(timezone.utc)
                if time.time() - last_refresh >= refresh_secs:
                    n = news_feed.refresh(c)
                    print(f"[news] refreshed {n} high-impact events")
                    last_refresh = time.time()
                _alert_due(c, notifier, lead_minutes, now)
        except Exception as e:
            print(f"[news] cycle error: {e}")
        time.sleep(check_secs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--lead", type=float, default=15.0)
    args = ap.parse_args()

    if args.test_alert:
        ok = news_notifier().send(embeds=[news_embed(
            title="TEST — FOMC Statement", currency="USD", impact="High",
            when="in 15 min", forecast="3.75%", previous="3.75%")], block=True)
        print("test news alert sent:", ok)
        return 0

    if args.once:
        with db.connect(autocommit=True) as c:
            n = news_feed.refresh(c)
            now = datetime.now(timezone.utc)
            ups = news_feed.upcoming(c, within_hours=24 * 14)
            sent = _alert_due(c, news_notifier(), args.lead, now)
            print(f"[news] once: refreshed {n}, upcoming relevant {len(ups)}, "
                  f"alerts sent {sent}")
        return 0

    run(lead_minutes=args.lead)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
