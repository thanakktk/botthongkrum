"""
Monitoring Dashboard (read-only)
======================================================================
A single-page web monitor for the whole system. It reads ONLY from Postgres
(no MT5 connection, no order capability), so it is safe to leave open: account
state + floors (from the latest heartbeat), kill-switch status, open positions,
League standings, recent orders, and the audit trail — plus a liveness light
driven by heartbeat age.

Run:  ./env/Scripts/python.exe dashboard.py        # http://127.0.0.1:8000
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import news_feed
from strategies import CATALOG, ROBUST_TREND_IDS
from scorecards import score_all
from signals import Bar

_ROBUST = set(ROBUST_TREND_IDS)        # OOS-validated live roster; the rest run shadow
from sessions import label as session_label
from ftmo_compliance_engine import FTMO_TZ

app = FastAPI(title="FTMO Trading Monitor")

# Front-end is split into web/ (index.html + style.css + app.js) and served static.
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

KILL_MODES = ("running", "halt_new", "close_all_halt")

# Lazy MT5 connection for the LIVE (tick-by-tick) price chart. Read-only: it only
# reads quotes/bars, never trades. Falls back gracefully if MT5 isn't reachable.
_broker = None


def _live_broker():
    global _broker
    if _broker is None:
        try:
            from mt5_broker import Mt5Broker
            _broker = Mt5Broker().connect()
        except Exception as e:
            print(f"[dashboard] live MT5 unavailable: {e}")
            _broker = False
    return _broker or None

# Heartbeat older than this => the main loop is presumed stale/dead.
STALE_AFTER_SECS = 90


def _f(x):
    return float(x) if isinstance(x, Decimal) else x


def _iso(x):
    return x.isoformat() if isinstance(x, datetime) else x


def _recent_trades(c) -> list[dict]:
    rows = c.execute(
        "SELECT p.closed_at, p.symbol, p.side, p.volume, p.open_price, "
        "p.close_pnl, p.close_reason, o.strategy_id, o.regime "
        "FROM positions p LEFT JOIN orders o "
        "ON o.broker_ticket = p.broker_ticket "    # link by ticket (coid is NULL live)
        "WHERE p.status = 'closed' "
        "ORDER BY p.closed_at DESC NULLS LAST LIMIT 20").fetchall()
    return [{"closed_at": _iso(r[0]), "symbol": r[1], "side": r[2],
             "volume": _f(r[3]), "open_price": _f(r[4]), "pnl": _f(r[5]),
             "reason": r[6], "strategy": r[7], "regime": r[8]} for r in rows]


def _news_rows(c) -> list[dict]:
    try:
        evs = news_feed.upcoming(c, within_hours=96, only_relevant=True)
    except Exception:
        evs = []
    return [{"event_time": _iso(e["event_time"]), "currency": e["currency"],
             "title": e["title"], "impact": e["impact"],
             "forecast": e["forecast"], "previous": e["previous"],
             "actual": e["actual"]} for e in evs]


def _equity_curve(c) -> list[dict]:
    """Account equity over time, from the per-tick heartbeats (oldest -> newest)."""
    rows = c.execute(
        "SELECT ts, (payload->>'equity')::float FROM audit_log "
        "WHERE event_type = 'heartbeat' AND payload ? 'equity' "
        "ORDER BY id DESC LIMIT 300").fetchall()
    return [{"ts": _iso(t), "equity": _f(e)} for t, e in reversed(rows) if e is not None]


def _price_chart(c) -> Optional[dict]:
    """Recent M5 candles for the most-recently-updated symbol + the open
    position's levels (entry/SL/TP1/TP2) to overlay on the chart."""
    row = c.execute(
        "SELECT symbol FROM price_bars ORDER BY bar_time DESC LIMIT 1").fetchone()
    if not row:
        return None
    sym = row[0]
    bars = c.execute(
        "SELECT bar_time, o, h, l, c FROM price_bars WHERE symbol=%s "
        "ORDER BY bar_time DESC LIMIT 120", (sym,)).fetchall()
    pos = c.execute(
        "SELECT open_price, sl, tp1, tp2, side FROM positions "
        "WHERE symbol=%s AND status='open' ORDER BY opened_at DESC LIMIT 1",
        (sym,)).fetchone()
    levels = ({"entry": _f(pos[0]), "sl": _f(pos[1]), "tp1": _f(pos[2]),
               "tp2": _f(pos[3]), "side": pos[4]} if pos else None)
    return {
        "symbol": sym,
        "bars": [{"t": _iso(b[0]), "o": _f(b[1]), "h": _f(b[2]),
                  "l": _f(b[3]), "c": _f(b[4])} for b in reversed(bars)],
        "levels": levels,
    }


def _intel_symbols(c) -> list[str]:
    """Symbols that currently have price data — one intel section per symbol
    (so XAUUSD and BTCUSD are shown separately, not just the latest one)."""
    rows = c.execute("SELECT DISTINCT symbol FROM price_bars").fetchall()
    return sorted(r[0] for r in rows) or ["XAUUSD"]


def _performance_by_symbol(c) -> list[dict]:
    """Realized performance split per traded symbol (closed positions only) —
    trades / win-rate / net P&L, so XAU vs BTC can be compared at a glance."""
    rows = c.execute(
        "SELECT symbol, count(*), "
        "sum(CASE WHEN close_pnl > 0 THEN 1 ELSE 0 END), "
        "coalesce(sum(close_pnl), 0) "
        "FROM positions WHERE status = 'closed' GROUP BY symbol ORDER BY symbol"
    ).fetchall()
    out = []
    for sym, n, wins, pnl in rows:
        n, wins = int(n), int(wins or 0)
        out.append({"symbol": sym, "trades": n, "wins": wins,
                    "win_rate": (wins / n) if n else 0.0, "pnl": _f(pnl)})
    return out


def _strategy_intel(c, sym: str, panel_tf: str = "H1") -> tuple[str, list[dict]]:
    """Per-strategy readiness scorecards for ONE symbol on a HIGHER timeframe
    (default H1 — cleaner than M5), fetched live from MT5; falls back to stored
    bars. Plus each technique's live win-rate from the League + shadow stats."""
    bars = []
    b = _live_broker()
    if b is not None:
        try:
            bars = b.get_bars(sym, panel_tf, 250)
        except Exception:
            bars = []
    if len(bars) < 30:                       # fallback: stored bars
        rows = c.execute(
            "SELECT bar_time, o, h, l, c FROM price_bars WHERE symbol=%s "
            "ORDER BY bar_time DESC LIMIT 200", (sym,)).fetchall()
        bars = [Bar(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in reversed(rows)]
        panel_tf = "M5*"
    if not bars:
        return panel_tf, []
    scores = score_all(bars)
    lg = {r[0]: r for r in c.execute(
        "SELECT strategy_id, sum(trades), sum(wins), sum(gross_pnl) "
        "FROM strategy_league GROUP BY strategy_id").fetchall()}
    out = []
    for cat in CATALOG:
        sid = cat["id"]
        s = scores.get(sid, {"direction": "-", "pct": 0, "checklist": []})
        lr = lg.get(sid)
        out.append({"id": sid, "name": cat["name"], "family": cat["family"],
                    "direction": s["direction"], "pct": s["pct"],
                    "checklist": s["checklist"],
                    "roster": "active" if sid in _ROBUST else "shadow",
                    "trades": int(lr[1]) if lr else 0,
                    "wins": int(lr[2]) if lr else 0,
                    "pnl": float(lr[3]) if lr else 0.0})
    # attach rolling SHADOW stats (benched techniques paper-trade in the
    # background; this is how a regime shift that revives one becomes visible)
    srows = c.execute(
        "SELECT strategy_id, r_mult FROM shadow_trades WHERE symbol=%s "
        "ORDER BY closed_at DESC LIMIT 2000", (sym,)).fetchall()
    sby: dict = {}
    for sid, r in srows:
        lst = sby.setdefault(sid, [])
        if len(lst) < 30:
            lst.append(float(r))
    for item in out:
        rs = sby.get(item["id"])
        if rs:
            n = len(rs)
            item["sh_n"] = n
            item["sh_wr"] = sum(1 for x in rs if x > 0) / n
            item["sh_avgR"] = sum(rs) / n
    # active (OOS-validated) roster first, then shadow; each group by readiness %
    out.sort(key=lambda x: (x["roster"] != "active", -x["pct"]))
    return panel_tf, out


def _reasoning(c) -> list[dict]:
    """Detailed confluence reasoning per order — the case-study log."""
    rows = c.execute(
        "SELECT created_at, symbol, side, volume, regime, status, rationale "
        "FROM orders WHERE rationale IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 15").fetchall()
    return [{"created_at": _iso(r[0]), "symbol": r[1], "side": r[2],
             "volume": _f(r[3]), "regime": r[4], "status": r[5],
             "rationale": r[6]} for r in rows]


def build_status() -> dict:
    now = datetime.now(timezone.utc)
    with db.connect() as c:
        profile = c.execute(
            "SELECT login, variant, path, phase, initial_capital "
            "FROM account_profile WHERE id = 1").fetchone()
        hb = c.execute(
            "SELECT ts, decision, reason_code, payload FROM audit_log "
            "WHERE event_type = 'heartbeat' ORDER BY id DESC LIMIT 1").fetchone()
        ks = c.execute(
            "SELECT mode, reason, source, tripped_at FROM kill_switch "
            "WHERE id = 1").fetchone()
        cet_today = now.astimezone(FTMO_TZ).date()
        baseline = c.execute(
            "SELECT midnight_balance, source FROM day_baseline "
            "WHERE cet_date = %s", (cet_today,)).fetchone()
        positions = c.execute(
            "SELECT broker_ticket, symbol, side, volume, open_price, sl, tp, "
            "opened_at, tp1, tp2, mgmt FROM positions WHERE status = 'open' "
            "ORDER BY opened_at DESC NULLS LAST").fetchall()
        league = c.execute(
            "SELECT strategy_id, regime, trades, wins, gross_pnl, status "
            "FROM strategy_league ORDER BY strategy_id, regime").fetchall()
        orders = c.execute(
            "SELECT created_at, strategy_id, symbol, side, volume, status, "
            "regime, broker_ticket FROM orders ORDER BY created_at DESC LIMIT 15"
        ).fetchall()
        audit = c.execute(
            "SELECT ts, event_type, decision, reason_code, payload FROM audit_log "
            "ORDER BY id DESC LIMIT 30").fetchall()
        trades = _recent_trades(c)
        news = _news_rows(c)
        equity_curve = _equity_curve(c)
        reasoning = _reasoning(c)
        chart = _price_chart(c)
        intel_by_symbol = []
        for _sym in _intel_symbols(c):
            _tf, _rows = _strategy_intel(c, _sym)
            intel_by_symbol.append({"symbol": _sym, "tf": _tf, "rows": _rows})
        perf_by_symbol = _performance_by_symbol(c)

    hb_payload = hb[3] if hb else {}
    hb_ts = hb[0] if hb else None
    hb_age = (now - hb_ts).total_seconds() if hb_ts else None

    return {
        "now": now.isoformat(),
        "session": session_label(now),
        "alive": (hb_age is not None and hb_age <= STALE_AFTER_SECS),
        "heartbeat": {
            "age_secs": hb_age, "ts": _iso(hb_ts),
            "action": hb[1] if hb else None,
            "reason": hb[2] if hb else None,
            "equity": _f(hb_payload.get("equity")) if hb_payload else None,
            "balance": _f(hb_payload.get("balance")) if hb_payload else None,
            "daily_floor": _f(hb_payload.get("daily_floor")) if hb_payload else None,
            "daily_soft_floor": _f(hb_payload.get("daily_soft_floor")) if hb_payload else None,
            "overall_floor": _f(hb_payload.get("overall_floor")) if hb_payload else None,
            "open_risk_to_sl": _f(hb_payload.get("open_risk_to_sl")) if hb_payload else None,
        },
        "profile": {
            "login": profile[0], "variant": profile[1], "path": profile[2],
            "phase": profile[3], "initial_capital": _f(profile[4]),
        } if profile else None,
        "kill_switch": {
            "mode": ks[0], "reason": ks[1], "source": ks[2],
            "tripped_at": _iso(ks[3]),
        } if ks else None,
        "baseline": {
            "cet_date": str(cet_today),
            "midnight_balance": _f(baseline[0]) if baseline else None,
            "source": baseline[1] if baseline else None,
        },
        "positions": [
            {"ticket": p[0], "symbol": p[1], "side": p[2], "volume": _f(p[3]),
             "open_price": _f(p[4]), "sl": _f(p[5]), "tp": _f(p[6]),
             "opened_at": _iso(p[7]), "tp1": _f(p[8]), "tp2": _f(p[9]),
             "mgmt": p[10]} for p in positions
        ],
        "league": [
            {"strategy_id": r[0], "regime": r[1], "trades": r[2], "wins": r[3],
             "gross_pnl": _f(r[4]), "status": r[5]} for r in league
        ],
        "orders": [
            {"created_at": _iso(o[0]), "strategy_id": o[1], "symbol": o[2],
             "side": o[3], "volume": _f(o[4]), "status": o[5], "regime": o[6],
             "ticket": o[7]} for o in orders
        ],
        "audit": [
            {"ts": _iso(a[0]), "event_type": a[1], "decision": a[2],
             "reason": a[3], "payload": a[4]} for a in audit
        ],
        "trades": trades,
        "news": news,
        "catalog": [dict(c, roster="active" if c["id"] in _ROBUST else "shadow")
                    for c in CATALOG],
        "equity_curve": equity_curve,
        "reasoning": reasoning,
        "chart": chart,
        "intel_by_symbol": intel_by_symbol,
        "perf_by_symbol": perf_by_symbol,
    }


@app.get("/api/status")
def api_status() -> JSONResponse:
    return JSONResponse(build_status())


@app.get("/api/quote")
def api_quote(symbol: str = "", tf: str = "M1") -> JSONResponse:
    """Live quote + recent candles straight from MT5 — polled every ~1s for a
    MetaTrader-style ticking chart. The last candle is the live forming one."""
    b = _live_broker()
    if b is None:
        return JSONResponse({"ok": False, "error": "MT5 not connected"},
                            status_code=503)
    if not symbol:
        with db.connect() as c:
            row = c.execute("SELECT symbol FROM price_bars "
                            "ORDER BY bar_time DESC LIMIT 1").fetchone()
        symbol = row[0] if row else "BTCUSD"
    try:
        bid, ask = b.quote(symbol)
        bars = b.get_bars(symbol, tf, 120)
        return JSONResponse({
            "ok": True, "symbol": symbol, "tf": tf, "bid": bid, "ask": ask,
            "bars": [{"t": x.time.isoformat(), "o": x.open, "h": x.high,
                      "l": x.low, "c": x.close} for x in bars]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


class KillSwitchReq(BaseModel):
    mode: str
    reason: str = "dashboard"


@app.post("/api/kill_switch")
def set_kill_switch(req: KillSwitchReq) -> JSONResponse:
    """Write the shared kill-switch SIGNAL only. The main loop / watchdog perform
    the actual flatten/halt — the dashboard never touches the broker. Localhost-
    only and unauthenticated: add auth before exposing beyond 127.0.0.1."""
    if req.mode not in KILL_MODES:
        return JSONResponse({"ok": False, "error": f"mode must be one of {KILL_MODES}"},
                            status_code=400)
    with db.connect(autocommit=True) as c:
        c.execute(
            "UPDATE kill_switch SET mode = %s, reason = %s, source = 'dashboard', "
            "tripped_at = CASE WHEN %s = 'running' THEN NULL ELSE now() END, "
            "updated_at = now() WHERE id = 1",
            (req.mode, req.reason, req.mode))
    return JSONResponse({"ok": True, "mode": req.mode})


@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    print("Dashboard -> http://127.0.0.1:8000  (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
