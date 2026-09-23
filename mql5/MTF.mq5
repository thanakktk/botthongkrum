//+------------------------------------------------------------------+
//| MTF.mq5 - XAUUSD multi-timeframe (top-down) bot                  |
//|                                                                  |
//| Port of research/mtf_backtest.py. Attach to the ENTRY chart      |
//| (M15 by default, M5 also tested). Direction comes from the       |
//| higher timeframes: D1, H4, H1 each vote bull (EMA20 > EMA50 and  |
//| close > EMA50 on the last CLOSED bar), bear (mirror) or neutral; |
//| a trade needs `NeedVotes` of the enabled TFs agreeing and none   |
//| against. The entry is a PULLBACK on the chart TF: within the     |
//| last K closed bars price touched the EMA20, the bar that just    |
//| closed is back on the trend side of the EMA20 and closes beyond  |
//| the previous bar's high (low for sells) = trigger bar. One entry |
//| per pullback. Stop beyond the K-bar swing (clamped 0.5..2 ATR),  |
//| TP = RR x stop. Optional: max hold, exit when H1 flips, buy-only.|
//| Risk/alerts/daily summary: same plumbing as HourlySet.           |
//+------------------------------------------------------------------+
#property copyright "botthongkrum"
#property link      "https://github.com/thanakktk/botthongkrum"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

enum ENUM_SIDE { SIDE_BOTH = 0, SIDE_BUY = 1, SIDE_SELL = 2 };
enum ENUM_SL   { SL_SWING = 0, SL_ATR = 1, SL_POINTS = 2 };
enum ENUM_TP   { TP_RR = 0, TP_H4ATR = 1, TP_POINTS = 2 };

//--- higher-timeframe bias
input bool   UseD1              = true;
input bool   UseH4              = true;
input bool   UseH1              = true;
input int    NeedVotes          = 3;       // TFs that must agree (and none against)
input int    BiasFast           = 20;      // EMA fast on each HTF
input int    BiasSlow           = 50;      // EMA slow on each HTF
//--- entry on the chart timeframe
input int    K                  = 24;      // pullback window (bars) / swing length (24 on M15 = 6 h; best of 21 tested)
input int    EmaEntry           = 20;      // LTF EMA the price must pull back to
input ENUM_SL StopMode          = SL_SWING; // SL_POINTS = fixed SlPoints (500 = $5.00 on a 2-digit gold quote)
input int    SlPoints           = 500;     // used when StopMode = SL_POINTS
input double SwingPadAtr        = 0.1;     // swing stop padding, x ATR
input double MinStopAtr         = 0.5;     // clamp for the swing stop
input double MaxStopAtr         = 2.0;
input ENUM_TP TpMode            = TP_RR;   // TP_POINTS = fixed TpPoints (1000-1500 = $10-15)
input int    TpPoints           = 1500;    // used when TpMode = TP_POINTS
input double RR                 = 2.0;     // TP = RR x stop distance
input int    MaxHoldHours       = 24;      // 0 = no time exit
input bool   ExitOnH1Flip       = false;   // close when the H1 bias turns against the trade
input ENUM_SIDE Side            = SIDE_BOTH;
input int    MaxPositions       = 1;
input int    AtrN               = 14;
//--- daily goal (optional)
input double DailyTargetPct     = 0.0;     // stop for the day at +x% of the day-start balance (0 = off)
input double DailyStopPct       = 0.0;     // stop for the day at -x% (0 = off)
//--- risk
input double RiskPct            = 0.5;     // risk per trade, % of equity
input double MaxNotionalFrac    = 2.0;
input double DD1                = 0.20;
input double DD1_Mult           = 0.5;
input double DD2                = 0.35;
input double DD2_Mult           = 0.25;
input double HardStopPct        = 0.60;
input double InitialCapital     = 0;
//--- plumbing
input long   MagicNumber        = 747001;
input int    Slippage           = 30;
input bool   SignalDump         = false;
input string DumpFile           = "mtf_signals.csv";
input string DiscordWebhook     = "";      // paste your webhook or Load mql5/MTF_local.set (kept out of git); whitelist https://discord.com
input int    SummaryHour        = 0;       // server hour for the daily summary
input int    MaxSignalAgeMin    = 10;      // skip a closed-bar signal older than this

CTrade   trade;
datetime g_lastBar = 0;
int      g_dump = INVALID_HANDLE;
string   g_gvPrefix;
int      g_d1f = INVALID_HANDLE, g_d1s = INVALID_HANDLE, g_h4f = INVALID_HANDLE, g_h4s = INVALID_HANDLE,
         g_h1f = INVALID_HANDLE, g_h1s = INVALID_HANDLE, g_ltfEma = INVALID_HANDLE, g_h4atr = INVALID_HANDLE;

//+------------------------------------------------------------------+
string GV(const string key) { return g_gvPrefix + key; }
double GVget(const string key, double def = 0) { return GlobalVariableCheck(GV(key)) ? GlobalVariableGet(GV(key)) : def; }
void   GVset(const string key, double v)       { GlobalVariableSet(GV(key), v); }
void   GVdel(const string key)                 { if(GlobalVariableCheck(GV(key))) GlobalVariableDel(GV(key)); }

int OnInit()
{
   g_gvPrefix = "MTF_" + _Symbol + "_" + IntegerToString(MagicNumber) + "_";
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetExpertMagicNumber(MagicNumber);
   g_d1f = iMA(_Symbol, PERIOD_D1, BiasFast, 0, MODE_EMA, PRICE_CLOSE); g_d1s = iMA(_Symbol, PERIOD_D1, BiasSlow, 0, MODE_EMA, PRICE_CLOSE);
   g_h4f = iMA(_Symbol, PERIOD_H4, BiasFast, 0, MODE_EMA, PRICE_CLOSE); g_h4s = iMA(_Symbol, PERIOD_H4, BiasSlow, 0, MODE_EMA, PRICE_CLOSE);
   g_h1f = iMA(_Symbol, PERIOD_H1, BiasFast, 0, MODE_EMA, PRICE_CLOSE); g_h1s = iMA(_Symbol, PERIOD_H1, BiasSlow, 0, MODE_EMA, PRICE_CLOSE);
   g_ltfEma = iMA(_Symbol, _Period, EmaEntry, 0, MODE_EMA, PRICE_CLOSE);
   g_h4atr = iATR(_Symbol, PERIOD_H4, AtrN);
   if(g_d1f == INVALID_HANDLE || g_d1s == INVALID_HANDLE || g_h4f == INVALID_HANDLE || g_h4s == INVALID_HANDLE ||
      g_h1f == INVALID_HANDLE || g_h1s == INVALID_HANDLE || g_ltfEma == INVALID_HANDLE || g_h4atr == INVALID_HANDLE) return INIT_FAILED;
   if(GVget("cap0", 0) <= 0) GVset("cap0", InitialCapital > 0 ? InitialCapital : AccountInfoDouble(ACCOUNT_BALANCE));
   if(GVget("peak", 0) <= 0) GVset("peak", AccountInfoDouble(ACCOUNT_EQUITY));
   if(SignalDump)
   {
      g_dump = FileOpen(DumpFile, FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
      if(g_dump != INVALID_HANDLE) FileWrite(g_dump, "bar_time", "dir", "entry", "sl", "tp", "d1", "h4", "h1");
   }
   PrintFormat("MTF init: entry tf=%s bias D1=%d H4=%d H1=%d need=%d K=%d rr=%.2f hold=%dh side=%d cap0=%.2f",
               EnumToString(_Period), UseD1, UseH4, UseH1, NeedVotes, K, RR, MaxHoldHours, (int)Side, GVget("cap0"));
   if(!MQLInfoInteger(MQL_TESTER))
      Notify("EA เริ่มทำงาน (MTF)", StringFormat("%s เข้าที่ %s | ทิศจาก D1/H4/H1 (ต้องตรงกัน %d) | balance %.2f | เสี่ยง %.2f%%/ไม้ | ถือสูงสุด %d ชม.",
             _Symbol, EnumToString(_Period), NeedVotes, AccountInfoDouble(ACCOUNT_BALANCE), RiskPct, MaxHoldHours), 0x3498DB);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(g_dump != INVALID_HANDLE) FileClose(g_dump);
   int hs[8] = {g_d1f, g_d1s, g_h4f, g_h4s, g_h1f, g_h1s, g_ltfEma, g_h4atr};
   for(int i = 0; i < 8; i++) if(hs[i] != INVALID_HANDLE) IndicatorRelease(hs[i]);
}

//+------------------------------------------------------------------+
double Buf(int handle, int shift)
{
   double b[1];
   if(CopyBuffer(handle, 0, shift, 1, b) != 1) return 0;
   return b[0];
}

// bias of one HTF from its last CLOSED bar: +1 / -1 / 0
int Bias(ENUM_TIMEFRAMES tf, int hf, int hs)
{
   double f = Buf(hf, 1), s = Buf(hs, 1);
   double c = iClose(_Symbol, tf, 1);
   if(f <= 0 || s <= 0 || c <= 0) return 0;
   if(f > s && c > s) return 1;
   if(f < s && c < s) return -1;
   return 0;
}

int Direction(int &d1, int &h4, int &h1)
{
   d1 = UseD1 ? Bias(PERIOD_D1, g_d1f, g_d1s) : 0;
   h4 = UseH4 ? Bias(PERIOD_H4, g_h4f, g_h4s) : 0;
   h1 = UseH1 ? Bias(PERIOD_H1, g_h1f, g_h1s) : 0;
   int bull = (d1 == 1) + (h4 == 1) + (h1 == 1);
   int bear = (d1 == -1) + (h4 == -1) + (h1 == -1);
   if(bull >= NeedVotes && bear == 0) return 1;
   if(bear >= NeedVotes && bull == 0) return -1;
   return 0;
}

double AtrClosed(const MqlRates &r[], int n)
{
   double s = 0;
   for(int i = 1; i <= n; i++)
      s += MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - r[i + 1].close), MathAbs(r[i].low - r[i + 1].close)));
   return s / n;
}

//+------------------------------------------------------------------+
//| Pullback trigger on the closed chart bar r[1]; returns dir or 0  |
//+------------------------------------------------------------------+
int Trigger(int dir, const MqlRates &r[], double &swing)
{
   double e[]; ArraySetAsSeries(e, true);
   if(CopyBuffer(g_ltfEma, 0, 1, K + 1, e) != K + 1) return 0;     // e[0] = ema at r[1], e[j] at r[j+1]
   double c = r[1].close;
   bool touched = false;
   swing = dir > 0 ? r[1].low : r[1].high;
   for(int j = 1; j <= K; j++)                                        // bars r[2]..r[K+1]
   {
      if(dir > 0 && r[j + 1].low <= e[j]) touched = true;
      if(dir < 0 && r[j + 1].high >= e[j]) touched = true;
      swing = dir > 0 ? MathMin(swing, r[j + 1].low) : MathMax(swing, r[j + 1].high);
   }
   if(!touched) return 0;
   if(dir > 0 && c > e[0] && c > r[2].high) return 1;
   if(dir < 0 && c < e[0] && c < r[2].low) return -1;
   return 0;
}

//+------------------------------------------------------------------+
double DdMultiplier(double equity)
{
   double peak = GVget("peak", equity);
   if(equity > peak) { peak = equity; GVset("peak", peak); }
   if(peak <= 0) return 1.0;
   double dd = (peak - equity) / peak;
   if(DD2 > 0 && dd >= DD2) return DD2_Mult;
   if(DD1 > 0 && dd >= DD1) return DD1_Mult;
   return 1.0;
}

double SizeLots(int dir, double entry, double sl)
{
   double equity = AccountInfoDouble(ACCOUNT_EQUITY), lossPerLot = 0;
   if(!OrderCalcProfit(dir > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, entry, sl, lossPerLot)) return 0;
   lossPerLot = MathAbs(lossPerLot);
   if(lossPerLot <= 0) return 0;
   double raw = RiskPct / 100.0 * equity * DdMultiplier(equity) / lossPerLot;
   double notionalPerLot = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE) * entry;
   if(MaxNotionalFrac > 0 && notionalPerLot > 0) raw = MathMin(raw, MaxNotionalFrac * equity / notionalPerLot);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP), vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lots = MathMin(vmax, MathFloor(raw / step) * step);
   if(lots < vmin) return 0;
   return NormalizeDouble(lots, 2);
}

bool Ours(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return false;
   return PositionGetInteger(POSITION_MAGIC) == MagicNumber && PositionGetString(POSITION_SYMBOL) == _Symbol;
}

int CountOurs()
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--) { ulong t = PositionGetTicket(i); if(Ours(t)) n++; }
   return n;
}

void CloseAllOurs(const string why)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--) { ulong t = PositionGetTicket(i); if(Ours(t)) { trade.PositionClose(t); PrintFormat("close #%I64u (%s)", t, why); } }
}

//+------------------------------------------------------------------+
void OpenTrade(int dir, double swing, double atrv, int d1, int h4, int h1, datetime barTime)
{
   double px = dir > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double dist;
   if(StopMode == SL_SWING)
   {
      dist = dir > 0 ? px - swing : swing - px;
      dist = MathMin(MathMax(dist + SwingPadAtr * atrv, MinStopAtr * atrv), MaxStopAtr * atrv);
   }
   else if(StopMode == SL_POINTS) dist = SlPoints * _Point;
   else dist = 1.0 * atrv;
   double tpd = RR * dist;
   if(TpMode == TP_H4ATR) { double h4a = Buf(g_h4atr, 1); if(h4a > 0) tpd = h4a; }
   else if(TpMode == TP_POINTS) tpd = TpPoints * _Point;
   double sl = NormalizeDouble(px - dir * dist, _Digits), tp = NormalizeDouble(px + dir * tpd, _Digits);
   double lots = SizeLots(dir, px, sl);
   if(g_dump != INVALID_HANDLE)
      FileWrite(g_dump, TimeToString(barTime, TIME_DATE | TIME_MINUTES), dir, DoubleToString(px, 2), DoubleToString(sl, 2), DoubleToString(tp, 2), d1, h4, h1);
   if(lots <= 0) { Print("zero volume (risk too small for min lot)"); return; }
   string cmt = StringFormat("MTF:%d%d%d", d1, h4, h1);
   bool ok = dir > 0 ? trade.Buy(lots, _Symbol, 0, sl, tp, cmt) : trade.Sell(lots, _Symbol, 0, sl, tp, cmt);
   if(!ok || trade.ResultRetcode() != TRADE_RETCODE_DONE) { PrintFormat("order failed %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription()); return; }
   GVset("lastentry", (double)barTime);
   PrintFormat("%s %.2f @ %.2f sl %.2f tp %.2f (bias D1 %d H4 %d H1 %d)", dir > 0 ? "BUY" : "SELL", lots, px, sl, tp, d1, h4, h1);
   Notify(StringFormat("เปิดไม้ %s %s %.2f lot", dir > 0 ? "BUY" : "SELL", _Symbol, lots),
          StringFormat("ทิศจาก D1 %s / H4 %s / H1 %s\nจังหวะ: ย่อตัวแตะ EMA%d บน %s แล้วแท่งยืนยัน\nราคาเข้า: %.2f\nSL: %.2f  (%.2f จุด = %.1f ATR, เสี่ยง $%.2f)\nTP: %.2f  (%.1fR)\nถือได้สูงสุด: %d ชม.\nไม้ที่เปิดอยู่: %d",
                       BiasTxt(d1), BiasTxt(h4), BiasTxt(h1), EmaEntry, EnumToString(_Period), px, sl, dist, dist / atrv,
                       dist * lots * SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE), tp, tpd / dist, MaxHoldHours, CountOurs()),
          dir > 0 ? 0x2ECC71 : 0xE67E22);
}

string BiasTxt(int b) { return b > 0 ? "ขึ้น" : b < 0 ? "ลง" : "กลาง"; }

//+------------------------------------------------------------------+
void Manage()
{
   datetime now = TimeCurrent();
   int h1now = UseH1 ? Bias(PERIOD_H1, g_h1f, g_h1s) : 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Ours(t)) continue;
      int d = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
      if(MaxHoldHours > 0 && now - (datetime)PositionGetInteger(POSITION_TIME) >= MaxHoldHours * 3600)
      { trade.PositionClose(t); PrintFormat("close #%I64u (max hold %dh)", t, MaxHoldHours); continue; }
      if(ExitOnH1Flip && h1now == -d)
      { trade.PositionClose(t); PrintFormat("close #%I64u (H1 bias flipped)", t); }
   }
}

bool DailyBlocked()
{
   if(DailyTargetPct <= 0 && DailyStopPct <= 0) return false;
   MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
   double today = dt.year * 10000 + dt.mon * 100 + dt.day;
   if(GVget("dayd", 0) != today) { GVset("dayd", today); GVset("dayb", AccountInfoDouble(ACCOUNT_BALANCE)); GVset("dayblk", 0); }
   if(GVget("dayblk", 0) > 0) return true;
   double base = GVget("dayb", 0);
   if(base <= 0) return false;
   double pl = AccountInfoDouble(ACCOUNT_EQUITY) / base - 1.0;
   string state = "";
   if(DailyTargetPct > 0 && pl >= DailyTargetPct / 100.0) state = "TARGET";
   else if(DailyStopPct > 0 && pl <= -DailyStopPct / 100.0) state = "STOP";
   if(state == "") return false;
   GVset("dayblk", 1);
   CloseAllOurs("daily " + state);
   Notify(state == "TARGET" ? "ถึงเป้ารายวันแล้ว" : "ชนลิมิตขาดทุนรายวัน", StringFormat("กำไร/ขาดทุนวันนี้ %+.2f%% (ฐาน %.2f) -> ปิดทุกไม้ หยุดเทรดจนถึงวันถัดไป", pl * 100, base), state == "TARGET" ? 0x2ECC71 : 0xE74C3C);
   return true;
}

bool HardStopTripped()
{
   if(GVget("halted", 0) > 0) return true;
   double cap0 = GVget("cap0", 0);
   if(HardStopPct <= 0 || cap0 <= 0) return false;
   if(AccountInfoDouble(ACCOUNT_EQUITY) <= cap0 * (1.0 - HardStopPct))
   {
      CloseAllOurs("hard stop"); GVset("halted", 1);
      Notify("HARD STOP - EA หยุดทำงาน", StringFormat("equity %.2f <= %.2f -> ปิดทุกไม้ EA หยุดถาวร (ลบ Global Variable ...halted เพื่อเริ่มใหม่)", AccountInfoDouble(ACCOUNT_EQUITY), cap0 * (1.0 - HardStopPct)), 0xE74C3C);
      return true;
   }
   return false;
}

//+------------------------------------------------------------------+
void DailySummary()
{
   if(MQLInfoInteger(MQL_TESTER)) return;
   MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
   if(dt.hour < SummaryHour) return;
   double today = dt.year * 10000 + dt.mon * 100 + dt.day;
   if(GVget("sumd", 0) == today) return;
   if(GVget("sumd", 0) == 0) { GVset("sumd", today); return; }
   GVset("sumd", today);
   datetime end = StringToTime(TimeToString(TimeCurrent(), TIME_DATE)) + SummaryHour * 3600, start = end - 86400;
   int n = 0, wins = 0, losses = 0, opened = 0; double net = 0, best = 0, worst = 0;
   if(HistorySelect(start, end))
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(HistoryDealGetInteger(d, DEAL_MAGIC) != MagicNumber || HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
         long e = HistoryDealGetInteger(d, DEAL_ENTRY);
         if(e == DEAL_ENTRY_IN) { opened++; continue; }
         if(e != DEAL_ENTRY_OUT && e != DEAL_ENTRY_OUT_BY) continue;
         double p = HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP) + HistoryDealGetDouble(d, DEAL_COMMISSION);
         n++; net += p; if(p > 0) wins++; else losses++;
         best = MathMax(best, p); worst = MathMin(worst, p);
      }
   int openN = 0; double floating = 0; string openList = "";
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Ours(t)) continue;
      openN++;
      double fp = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP); floating += fp;
      long age = (TimeCurrent() - (datetime)PositionGetInteger(POSITION_TIME)) / 60;
      openList += StringFormat("\n  - #%I64u %s %.2f lot @ %.2f  ลอยตัว %s%.2f  (ถือมา %d ชม. %02d นาที)", t,
                               PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL", PositionGetDouble(POSITION_VOLUME),
                               PositionGetDouble(POSITION_PRICE_OPEN), fp >= 0 ? "+" : "", fp, (int)(age / 60), (int)(age % 60));
   }
   int d1, h4, h1; Direction(d1, h4, h1);
   string text = StringFormat("ช่วง %s -> %s\nเปิดไม้: %d | ปิดไม้: %d\nชนะ %d / แพ้ %d (win rate %.0f%%)\nกำไรสุทธิที่ปิดแล้ว: %s%.2f  (ดีสุด %+.2f / แย่สุด %+.2f)\nไม้ที่ยังค้าง: %d ไม้  ลอยตัว %s%.2f%s\nทิศตอนนี้: D1 %s / H4 %s / H1 %s\nbalance: %.2f | equity: %.2f",
                              TimeToString(start, TIME_DATE | TIME_MINUTES), TimeToString(end, TIME_DATE | TIME_MINUTES), opened, n, wins, losses,
                              n > 0 ? 100.0 * wins / n : 0.0, net >= 0 ? "+" : "", net, best, worst, openN, floating >= 0 ? "+" : "", floating,
                              openN > 0 ? openList : " (ไม่มี)", BiasTxt(d1), BiasTxt(h4), BiasTxt(h1), AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY));
   Print("DAILY SUMMARY: ", text);
   Notify(StringFormat("สรุปรายวัน %s  %s%.2f", TimeToString(start, TIME_DATE), net >= 0 ? "+" : "", net), text, net >= 0 ? 0x2ECC71 : (n == 0 ? 0x95A5A6 : 0xE74C3C));
}

//+------------------------------------------------------------------+
string JsonEscape(string s) { StringReplace(s, "\\", "\\\\"); StringReplace(s, "\"", "\\\""); StringReplace(s, "\n", "\\n"); return s; }

void Notify(const string title, const string text, const int clr)
{
   if(DiscordWebhook == "" || MQLInfoInteger(MQL_TESTER)) return;
   string body = StringFormat("{\"embeds\":[{\"title\":\"%s\",\"description\":\"%s\",\"color\":%d,\"footer\":{\"text\":\"MTF | %s %s | %s\"}}]}",
                              JsonEscape(title), JsonEscape(text), clr, _Symbol, EnumToString(_Period), TimeToString(TimeCurrent(), TIME_DATE | TIME_MINUTES));
   char data[], result[]; string headers;
   StringToCharArray(body, data, 0, StringLen(body), CP_UTF8);
   int rc = WebRequest("POST", DiscordWebhook, "Content-Type: application/json\r\n", 5000, data, result, headers);
   if(rc == -1) PrintFormat("Discord: WebRequest failed (%d) - whitelist https://discord.com in Tools>Options>Expert Advisors", GetLastError());
}

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != MagicNumber) return;
   long entryType = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entryType != DEAL_ENTRY_OUT && entryType != DEAL_ENTRY_OUT_BY) return;
   ulong posId = (ulong)HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   double px = HistoryDealGetDouble(trans.deal, DEAL_PRICE), vol = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
   double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) + HistoryDealGetDouble(trans.deal, DEAL_SWAP) + HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   long reason = HistoryDealGetInteger(trans.deal, DEAL_REASON), dtype = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   string side = (dtype == DEAL_TYPE_SELL) ? "BUY" : "SELL";
   double entry = 0; datetime opened = 0;
   if(HistorySelectByPosition(posId))
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_IN) { entry = HistoryDealGetDouble(d, DEAL_PRICE); opened = (datetime)HistoryDealGetInteger(d, DEAL_TIME); break; }
      }
   string why = reason == DEAL_REASON_SL ? "ชน Stop Loss" : reason == DEAL_REASON_TP ? "ถึง Take Profit" : reason == DEAL_REASON_SO ? "Stop out (margin)" :
                reason == DEAL_REASON_EXPERT ? "EA ปิดเอง (ครบเวลา / ทิศ H1 กลับ / เป้ารายวัน)" : "ปิดมือ / อื่น ๆ";
   long held = opened > 0 ? (TimeCurrent() - opened) / 60 : 0;
   Notify(StringFormat("ปิดไม้ #%I64u  %s  %s%.2f", posId, profit >= 0 ? "กำไร" : "ขาดทุน", profit >= 0 ? "+" : "", profit),
          StringFormat("%s %s %.2f lot\nเข้า: %.2f -> ออก: %.2f\nเหตุผล: %s\nถือ: %d ชม. %02d นาที\nไม้ที่ยังค้าง: %d\nbalance: %.2f",
                       side, _Symbol, vol, entry, px, why, (int)(held / 60), (int)(held % 60), CountOurs(), AccountInfoDouble(ACCOUNT_BALANCE)),
          profit >= 0 ? 0x2ECC71 : 0xE74C3C);
}

//+------------------------------------------------------------------+
void OnTick()
{
   if(HardStopTripped()) return;
   DailySummary();
   bool blocked = DailyBlocked();
   Manage();

   datetime bt = iTime(_Symbol, _Period, 0);
   if(bt == g_lastBar) return;
   g_lastBar = bt;
   if(blocked) return;
   if(MaxSignalAgeMin > 0 && !MQLInfoInteger(MQL_TESTER) && TimeCurrent() - bt > MaxSignalAgeMin * 60)
   { PrintFormat("closed-bar signal is %d min old (> %d) - skipped", (int)((TimeCurrent() - bt) / 60), MaxSignalAgeMin); return; }
   if(CountOurs() >= MaxPositions) return;

   int d1, h4, h1;
   int dir = Direction(d1, h4, h1);
   if(dir == 0) return;
   if((Side == SIDE_BUY && dir < 0) || (Side == SIDE_SELL && dir > 0)) return;

   MqlRates r[]; ArraySetAsSeries(r, true);
   int need = MathMax(K, AtrN) + 3;
   if(CopyRates(_Symbol, _Period, 0, need, r) < need) return;
   double atrv = AtrClosed(r, AtrN);
   if(atrv <= 0) return;
   // one entry per pullback: at least K bars since the last entry
   datetime last = (datetime)GVget("lastentry", 0);
   if(last > 0 && (r[1].time - last) < K * PeriodSeconds(_Period)) return;
   double swing;
   int trig = Trigger(dir, r, swing);
   if(trig == 0) return;
   OpenTrade(dir, swing, atrv, d1, h4, h1, r[1].time);
}
//+------------------------------------------------------------------+
