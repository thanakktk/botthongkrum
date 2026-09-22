"""
Bar Replay — manual trading practice on historical gold
======================================================================
TradingView-style bar replay over the local XAUUSD M15 history (backtest/
XAU_15m_data.csv, 2004->2026). Candles are revealed one at a time; you place
market / pending orders with SL/TP and the browser simulates the fills. An
"ask the bot" button runs the SAME robust strategies + confluence arbitrator the
live bot uses, as of the current replay candle, so you can compare your read
with the bot's.

Fully offline: no MT5, no Postgres, never touches the live account or DB.

    ./env/Scripts/python.exe replay.py            -> http://127.0.0.1:8050   [--port N]
"""

from __future__ import annotations

import bisect
import csv
import os
import random
import sys
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "research"))

from signals import Bar                                        # noqa: E402
from paper_broker import PaperBroker                           # noqa: E402
from arbitration import Arbitrator, ArbitrationConfig          # noqa: E402
from strategies import select_strategies, ROBUST_TREND_IDS     # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
M15_CSV = os.path.join(ROOT, "backtest", "XAU_15m_data.csv")
SYMBOL = "XAUUSD"
TF_SECS = {"M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}

app = FastAPI(title="Bar Replay")
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


# --------------------------------------------------------------------------- #
# Data: the M15 series in memory, clock-aligned resamples cached per TF        #
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _m15() -> list[tuple]:
    """[(epoch, o, h, l, c, v), ...] sorted. Stamps are the vendor's server time,
    treated as UTC (see research/histdata.py)."""
    out = []
    with open(M15_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f, delimiter=";"):
            if len(row) < 5 or row[0][:1] == "D":
                continue
            try:
                t = datetime.strptime(row[0], "%Y.%m.%d %H:%M").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            out.append((int(t.timestamp()), float(row[1]), float(row[2]),
                        float(row[3]), float(row[4]),
                        float(row[5]) if len(row) > 5 else 0.0))
    out.sort()
    return out


def _bucket(rows: list[tuple], secs: int) -> list[tuple]:
    """Aggregate M15 rows into clock-aligned buckets of `secs` (like MT5 bars)."""
    if secs <= 900:
        return rows
    out: list[list] = []
    for t, o, h, l, c, v in rows:
        b = t - t % secs
        if out and out[-1][0] == b:
            cur = out[-1]
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = c
            cur[5] += v
        else:
            out.append([b, o, h, l, c, v])
    return [tuple(x) for x in out]


@lru_cache(maxsize=8)
def _series(tf: str) -> list[tuple]:
    return _bucket(_m15(), TF_SECS[tf])


@lru_cache(maxsize=8)
def _times(tf: str) -> list[int]:
    return [r[0] for r in _series(tf)]


def _tf(tf: str) -> str:
    tf = tf.upper()
    if tf not in TF_SECS:
        raise HTTPException(400, f"tf must be one of {list(TF_SECS)}")
    return tf


def _pack(rows) -> list[list]:
    return [[r[0], r[1], r[2], r[3], r[4]] for r in rows]


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "replay.html"))


@app.get("/api/meta")
def meta():
    rows = _m15()
    return {"symbol": SYMBOL, "first": rows[0][0], "last": rows[-1][0],
            "tfs": list(TF_SECS), "contract_size": 100.0}


@app.get("/api/bars")
def bars(tf: str, start: int, before: int = 300, after: int = 3000):
    """`before` bars ending just before `start` (the visible history) plus up
    to `after` bars from `start` on (revealed one by one by the client)."""
    tf = _tf(tf)
    s, ts = _series(tf), _times(tf)
    i = bisect.bisect_left(ts, start)
    lo = max(0, i - max(before, 1))
    return {"tf": tf, "bars": _pack(s[lo:i + after]), "cursor": i - lo}


@app.get("/api/random_start")
def random_start():
    """A random weekday start, leaving room for history and forward bars."""
    rows = _m15()
    lo, hi = rows[0][0] + 90 * 86400, rows[-1][0] - 30 * 86400
    while True:
        t = random.randint(lo, hi)
        if datetime.fromtimestamp(t, timezone.utc).weekday() < 5:
            return {"start": t - t % 3600}


@lru_cache(maxsize=1)
def _bot():
    """Mirror of run_xau_robust.ps1: robust-4, M30/H1/H4 confluence."""
    arb = Arbitrator(ArbitrationConfig(
        timeframes=("M30", "H1", "H4"),
        tf_weights={"M30": 1.0, "H1": 1.6, "H4": 2.4},
        risk_pct=0.003, tp1_r=2.0, tp2_r=2.5,
        min_agree=2, min_families=2, min_agreement=0.70, min_conviction=1.0))
    return select_strategies(ROBUST_TREND_IDS), arb


@app.get("/api/bot")
def bot(asof: int, equity: float = 100_000.0):
    """What would the live bot do with only the candles CLOSED by `asof`?"""
    rows = _m15()
    m15_t = _times("M15")
    # M15 bars whose close (open + 15m) is <= asof; ~250 H4 bars of history
    end = bisect.bisect_right(m15_t, asof - 900)
    window = rows[max(0, end - 250 * 16):end]
    if len(window) < 200:
        raise HTTPException(400, "not enough history before this point")
    broker = PaperBroker(equity, specs={SYMBOL: 100.0})
    for tf in ("M30", "H1", "H4"):
        secs = TF_SECS[tf]
        # only buckets that have fully closed by `asof`
        agg = [r for r in _bucket(window, secs) if r[0] + secs <= asof]
        broker.set_bars(SYMBOL, [Bar(
            time=datetime.fromtimestamp(r[0], timezone.utc), open=r[1], high=r[2],
            low=r[3], close=r[4], volume=r[5]) for r in agg], timeframe=tf)
    broker.set_price(SYMBOL, window[-1][4])

    strategies, arb = _bot()
    now = datetime.fromtimestamp(asof, timezone.utc)
    sigs = arb.collect(strategies, [SYMBOL], broker, now)
    reqs = arb.arbitrate(sigs, broker, equity, now)
    consider = arb.last_consider.get(SYMBOL) or arb.last_consider.get("*") or {}
    out = {
        "asof": asof,
        "signals": [{"strategy": s.strategy_id, "tf": s.timeframe,
                     "dir": s.direction.value if hasattr(s.direction, "value")
                     else str(s.direction),
                     "conf": round(s.confidence, 2), "entry": s.entry,
                     "sl": s.sl, "tp": s.tp} for s in sigs],
        "decision": consider.get("decision", "none"),
        "why": consider.get("why", ""),
        "order": None,
    }
    if reqs:
        r = reqs[0]
        out["order"] = {"side": r.side, "volume": r.volume, "entry": r.entry,
                        "sl": r.sl, "tp": r.tp, "tp1": r.tp1, "risk": r.risk_to_sl,
                        "regime": r.regime, "rationale": r.rationale}
    return out


if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser()
    # 8001 is taken by Docker on this machine; 8000 is the live dashboard.
    ap.add_argument("--port", type=int, default=8050)
    port = ap.parse_args().port
    print("loading M15 history ...", flush=True)
    print(f"{len(_m15()):,} bars loaded -> http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
