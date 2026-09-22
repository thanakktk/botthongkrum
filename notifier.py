"""
Discord notifier
======================================================================
Fire-and-forget alerts to Discord webhooks. A notification failure must NEVER
affect trading, so the HTTP POST runs in a daemon thread and every error is
swallowed (logged only). Webhook URLs come from .env (secrets, gitignored).

Two channels:
  * trade webhook — order opened (with the WHY) and closed (with P&L)
  * news  webhook — high-impact XAUUSD-relevant economic events
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

GREEN = 3066993
RED = 15158332
BLUE = 3447003
GOLD = 15844367
GREY = 9807270

# market regime -> Thai (alerts are written for a Thai reader)
_TH_REGIME = {"trend": "เทรนด์", "range": "ออกข้าง", "unknown": "ไม่ชัด"}

# close-reason code -> Thai, so "ปิดเพราะอะไร" is readable in the alert
_TH_REASON = {
    "sl": "ชน Stop Loss (ตัดขาดทุนตามแผน)",
    "tp": "ถึงเป้า Take Profit",
    "tp2": "ถึงเป้าหมายสุดท้าย (TP2)",
    "tp1_hit": "ถึง TP1 — เก็บกำไรบางส่วน แล้วเลื่อน SL มาเสมอตัว",
    "be_locked": "ล็อกเสมอตัว (break-even) ก่อนถึง TP1",
    "trailed": "เลื่อน SL ตามกำไร (trailing)",
    "time_flatten": "ปิดตามเวลา (กฎปิดสุดสัปดาห์/ช่วงของบัญชี Standard)",
    "flatten_all": "ปิดทั้งหมด (คำสั่งความปลอดภัยจากระบบ Compliance)",
    "daily_loss_hard_buffer": "ใกล้ชนลิมิตขาดทุนรายวัน — ปิดกันทะลุกฎ FTMO",
    "manual": "ปิดด้วยมือ",
    "dev_flat": "ปิดเพื่อทดสอบระบบ",
    "end_of_data": "จบชุดข้อมูล (backtest)",
    "broker_close": "ปิดที่โบรกเกอร์ (ชน SL/TP)",
}


def thai_reason(code: Optional[str]) -> str:
    """Map an internal close-reason code to a Thai explanation for the alert."""
    if not code:
        return "—"
    if code.startswith("killswitch:"):
        return f"คำสั่งหยุดฉุกเฉิน (kill-switch): {code.split(':', 1)[1]}"
    return _TH_REASON.get(code, code)


def thai_regime(regime: Optional[str]) -> str:
    return _TH_REGIME.get(regime or "", regime or "—")


class DiscordNotifier:
    def __init__(self, webhook_url: Optional[str], enabled: bool = True,
                 timeout: float = 8.0):
        self.url = webhook_url or ""
        self.enabled = enabled and bool(self.url)
        self.timeout = timeout
        self._threads: list[threading.Thread] = []

    def send(self, content: Optional[str] = None,
             embeds: Optional[list[dict]] = None, block: bool = False) -> bool:
        if not self.enabled:
            return False
        payload: dict = {}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds
        data = json.dumps(payload).encode("utf-8")

        def _post():
            try:
                req = urllib.request.Request(
                    self.url, data=data,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "ftmo-bot/1.0"}, method="POST")
                urllib.request.urlopen(req, timeout=self.timeout)
            except Exception as e:                       # never break trading
                print(f"[discord] send failed: {e}")

        if block:
            _post()
        else:
            th = threading.Thread(target=_post, daemon=True)
            th.start()
            # keep a handle so flush() can wait for in-flight sends before exit —
            # a daemon thread is killed on process exit and would otherwise drop
            # the message (this is why one-off scripts lost their close alerts).
            self._threads = [t for t in self._threads if t.is_alive()] + [th]
        return True

    def flush(self, timeout: float = 10.0) -> None:
        """Wait for outstanding fire-and-forget sends. Call before a short-lived
        process exits so its last alert (e.g. a close) actually goes out."""
        for t in list(self._threads):
            t.join(timeout)
        self._threads = [t for t in self._threads if t.is_alive()]


def trade_notifier() -> DiscordNotifier:
    return DiscordNotifier(os.getenv("DISCORD_TRADE_WEBHOOK"))


def news_notifier() -> DiscordNotifier:
    return DiscordNotifier(os.getenv("DISCORD_NEWS_WEBHOOK"))


# --------------------------------------------------------------------------- #
# Embed builders                                                              #
# --------------------------------------------------------------------------- #
def _num(x) -> str:
    return "—" if x is None else (f"{x:,.5f}".rstrip("0").rstrip(".")
                                  if isinstance(x, float) else str(x))


def open_embed(*, symbol: str, side: str, volume: float, entry, sl, tp,
               risk: float, regime: Optional[str], strategy: str,
               rationale: str) -> dict:
    buy = side == "buy"
    return {
        "title": f"{'🟢 เปิดซื้อ (BUY)' if buy else '🔴 เปิดขาย (SELL)'} {symbol}",
        "color": GREEN if buy else RED,
        "fields": [
            {"name": "ปริมาณ (ล็อต)", "value": _num(volume), "inline": True},
            {"name": "ราคาเข้า", "value": _num(entry), "inline": True},
            {"name": "ความเสี่ยงถ้าชน SL", "value": f"${risk:,.2f}", "inline": True},
            {"name": "SL (ตัดขาดทุน)", "value": _num(sl), "inline": True},
            {"name": "TP (เป้ากำไร)", "value": _num(tp), "inline": True},
            {"name": "สภาพตลาด", "value": thai_regime(regime), "inline": True},
            {"name": "เทคนิคหลัก", "value": strategy, "inline": False},
            {"name": "เหตุผลที่เปิด", "value": rationale or "—", "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "FTMO bot · เปิดออเดอร์"},
    }


def close_embed(*, symbol: str, pnl: float, reason: str,
                strategy: Optional[str] = None, regime: Optional[str] = None) -> dict:
    win = pnl >= 0
    fields = [
        {"name": "กำไร/ขาดทุน", "value": f"${pnl:,.2f}", "inline": True},
        {"name": "เหตุผลที่ปิด", "value": thai_reason(reason), "inline": False},
    ]
    if regime:
        fields.append({"name": "สภาพตลาด", "value": thai_regime(regime),
                       "inline": True})
    if strategy:
        fields.append({"name": "เทคนิค", "value": strategy, "inline": False})
    return {
        "title": f"⚪ ปิดออเดอร์ {symbol} — {'🟩 กำไร' if win else '🟥 ขาดทุน'}",
        "color": GREEN if win else RED,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "FTMO bot · ปิดออเดอร์"},
    }


def mgmt_embed(*, symbol: str, event: str, detail: str) -> dict:
    return {
        "title": f"🎯 {symbol} — {event}",
        "color": BLUE,
        "description": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "FTMO bot · manage"},
    }


def news_embed(*, title: str, currency: str, impact: str, when: str,
               forecast=None, previous=None, actual=None) -> dict:
    fields = [
        {"name": "Currency", "value": currency, "inline": True},
        {"name": "Impact", "value": impact, "inline": True},
        {"name": "When", "value": when, "inline": True},
    ]
    if forecast is not None:
        fields.append({"name": "Forecast", "value": str(forecast), "inline": True})
    if previous is not None:
        fields.append({"name": "Previous", "value": str(previous), "inline": True})
    if actual is not None:
        fields.append({"name": "Actual", "value": str(actual), "inline": True})
    return {
        "title": f"📅 {title}",
        "color": GOLD,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "FTMO bot · news (XAUUSD focus)"},
    }


# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else "trade"
    if which == "news":
        n = news_notifier()
        ok = n.send(embeds=[news_embed(title="TEST — High Impact USD Event",
                                       currency="USD", impact="High",
                                       when="in 30 min", forecast="3.1%",
                                       previous="3.0%")], block=True)
    else:
        n = trade_notifier()
        ok = n.send(content="**FTMO bot connected** — trade alerts will appear here.",
                    embeds=[open_embed(symbol="XAUUSD", side="buy", volume=0.10,
                                       entry=2000.0, sl=1950.0, tp=2100.0, risk=500.0,
                                       regime="trend", strategy="ensemble:breakout_sr",
                                       rationale="TEST alert — breakout_sr fired BUY "
                                       "in TREND; net score 1.50")], block=True)
    print("sent:", ok, "(enabled:", n.enabled, ")")
