"""
Historical data loader (backtest/ folder)
======================================================================
Two sources live under backtest/:
  * XAU_15m_data.csv  — one continuous M15 series, 2004->2026, ';'-separated,
        header "Date;Open;High;Low;Close;Volume", date "YYYY.MM.DD HH:MM".
  * XAUUSD (YEAR)/[XAUUSD/]XAUUSD_YYYY_MM.csv — per-month M1 files, ','-separated,
        no header, "YYYY.MM.DD,HH:MM,O,H,L,C,V".

This gives ~22 years of gold to validate the edge across many market cycles —
far more than the ~7 months MetaTrader's terminal cache returns.

NOTE on timezone: these stamps are the data vendor's server time (not guaranteed
UTC), so SESSION-based analysis on this data is approximate. REGIME/trend analysis
(our main filter) is timezone-independent, so it is unaffected.
"""

from __future__ import annotations

# --- allow importing project-root modules when run from this subfolder ---
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))

import csv
import glob
import os
from datetime import datetime, timezone

from signals import Bar

# backtest/ data lives at the PROJECT ROOT (this module sits in research/)
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "backtest")
M15_CSV = os.path.join(BASE, "XAU_15m_data.csv")

# M15-based resample factors (vs the live M5-based map in backtester._TF_FACTOR)
M15_TF_FACTOR = {"M15": 1, "M30": 2, "H1": 4, "H4": 16, "D1": 96}


def _bar(t: datetime, o, h, l, c, v) -> Bar:
    return Bar(time=t, open=float(o), high=float(h), low=float(l),
               close=float(c), volume=float(v))


def load_m15(path: str = M15_CSV, start_year: int | None = None,
             end_year: int | None = None) -> list[Bar]:
    """Load the continuous M15 CSV, optionally filtered to [start_year, end_year]."""
    out: list[Bar] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f, delimiter=";"):
            if not row or row[0].lower().startswith("date"):
                continue                          # header / blank
            try:
                t = datetime.strptime(row[0], "%Y.%m.%d %H:%M").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            if start_year and t.year < start_year:
                continue
            if end_year and t.year > end_year:
                continue
            out.append(_bar(t, row[1], row[2], row[3], row[4],
                            row[5] if len(row) > 5 else 0))
    out.sort(key=lambda b: b.time)
    return out


def load_m1_year(year: int) -> list[Bar]:
    """Concatenate every monthly M1 file for `year` (handles the 2015-style nested
    'XAUUSD/' subfolder and the flat 2026-style layout)."""
    pats = [os.path.join(BASE, f"XAUUSD ({year})", f"XAUUSD_{year}_*.csv"),
            os.path.join(BASE, f"XAUUSD ({year})", "XAUUSD",
                         f"XAUUSD_{year}_*.csv")]
    files = sorted({p for pat in pats for p in glob.glob(pat)})
    out: list[Bar] = []
    for fp in files:
        with open(fp, newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if len(row) < 7:
                    continue
                try:
                    t = datetime.strptime(f"{row[0]} {row[1]}",
                                          "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                out.append(_bar(t, row[2], row[3], row[4], row[5], row[6]))
    out.sort(key=lambda b: b.time)
    return out


if __name__ == "__main__":
    m15 = load_m15()
    print(f"M15 CSV: {len(m15):,} bars  {m15[0].time:%Y-%m-%d} -> "
          f"{m15[-1].time:%Y-%m-%d}")
    yrs = sorted({b.time.year for b in m15})
    print(f"years covered: {yrs[0]}..{yrs[-1]} ({len(yrs)} years)")
    print(f"sample bar: {m15[0]}")
