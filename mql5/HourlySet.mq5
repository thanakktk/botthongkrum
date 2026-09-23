//+------------------------------------------------------------------+
//| HourlySet.mq5 - XAUUSD "a trade every hour" multi-position bot   |
//|                                                                  |
//| Port of research/hourly_backtest.py. Attach to an H1 chart.      |
//| On every CLOSED H1 bar it opens a new SET (1 leg, or 2 legs for  |
//| the straddle hedge) by the chosen Mode; each leg has SL / TP in  |
//| ATR(14) multiples and is closed after MaxHoldHours. Several sets |
//| can be open at once (MaxPositions). Hedging is used by:          |
//|   Mode straddle  : BUY + SELL every hour; SetTpAtr closes the    |
//|                    whole set once its NET profit (realised legs  |
//|                    + floating legs) >= SetTpAtr x ATR;           |
//|                    LockTwinOnTp closes the twin when one leg TPs |
//|   Mode recovery  : trend leg; if it goes HedgeAtAtr against, the |
//|                    opposite hedge leg opens; the pair closes at  |
//|                    BasketTpAtr net profit or at MaxHoldHours     |
//| Daily goal: DailyTargetPct / DailyStopPct vs the server-day      |
//| starting balance -> close everything and idle until tomorrow.    |
//| Risk: RiskPct of equity per leg, notional cap, drawdown throttle,|
//| hard stop (same code as H4Trend).                                |
//| Needs a HEDGING account (VT Markets MT5 is) for straddle/recovery|
//+------------------------------------------------------------------+
#property copyright "botthongkrum"
#property link      "https://github.com/thanakktk/botthongkrum"
#property version   "1.00"
// Backtest (research/hourly_backtest.py, 2015-2026, $0.25 cost): EVERY mode loses
// (PF 0.80-0.92); default trend/BUY/1.5ATR/1R is the least bad: PF 0.92, 2022-26 PF 1.01.
// Hedging (straddle / recovery) does not create edge. Test on DEMO first.
#property strict

#include <Trade\Trade.mqh>

enum ENUM_MODE { MODE_TREND = 0, MODE_MOMO = 1, MODE_BREAKOUT = 2, MODE_STRADDLE = 3, MODE_RECOVERY = 4, MODE_MR = 5 };
enum ENUM_SIDE { SIDE_BOTH = 0, SIDE_BUY = 1, SIDE_SELL = 2 };

//--- rule
input ENUM_MODE Mode            = MODE_TREND;
input ENUM_SIDE Side            = SIDE_BUY;     // backtest: sells lose every era; BUY-only is the least bad
input double SlAtr              = 1.5;     // stop distance, x ATR(14) of H1
input double RR                 = 1.0;     // take-profit = RR x stop distance (1.0 with SlAtr 1.5 = best of 23 tested)
input int    MaxHoldHours       = 4;       // close a leg after this many hours
input int    MaxPositions       = 4;       // open legs allowed at once (sets x legs)
input double SetTpAtr           = 0.0;     // close a whole set at this NET profit (x ATR); 0 = off
input bool   LockTwinOnTp       = false;   // straddle: when one leg hits TP close the other
input double HedgeAtAtr         = 0.5;     // recovery: adverse move (x ATR) that opens the hedge leg
input double BasketTpAtr        = 0.2;     // recovery: net profit (x ATR) that closes lead + hedge
input double MomoKAtr           = 0.5;     // momo: |close - close[4]| must exceed this x ATR
input double MrZ                = 1.5;     // mr: |z| of the 24-bar mean to fade
input int    EmaFast            = 20;
input int    EmaMid             = 50;
input int    EmaSlow            = 200;
input int    AtrN               = 14;
//--- daily goal
input double DailyTargetPct     = 0.0;     // e.g. 1.0 = stop for the day at +1% of the day-start balance (0 = off)
input double DailyStopPct       = 0.0;     // e.g. 1.0 = stop for the day at -1% (0 = off)
//--- risk
input double RiskPct            = 0.25;    // risk per LEG, % of equity
input double MaxNotionalFrac    = 2.0;     // cap: leg notional <= this x equity
input double DD1                = 0.20;    // throttle tier 1: drawdown from peak
input double DD1_Mult           = 0.5;
input double DD2                = 0.35;
input double DD2_Mult           = 0.25;
input double HardStopPct        = 0.60;    // close all + halt at this loss of InitialCapital
input double InitialCapital     = 0;       // 0 = balance when first attached (stored)
//--- plumbing
input long   MagicBase          = 737000;  // + Mode
input int    Slippage           = 30;      // points
input bool   SignalDump         = false;   // tester: write every decision to a CSV
input string DumpFile           = "hourlyset_signals.csv";
input string DiscordWebhook     = "";      // Discord alerts: paste your webhook here or load mql5/HourlySet_local.set (kept out of git); whitelist https://discord.com in Tools>Options>Expert Advisors
input int    MaxSignalAgeMin    = 20;      // skip a closed-bar decision older than this (late attach / restart)
input int    SummaryHour        = 0;       // server hour to post the daily summary (covers the previous 24 h)

CTrade   trade;
datetime g_lastBar = 0;
int      g_dump    = INVALID_HANDLE;
string   g_gvPrefix;
int      g_hFast = INVALID_HANDLE, g_hMid = INVALID_HANDLE, g_hSlow = INVALID_HANDLE;

//+------------------------------------------------------------------+
string GV(const string key) { return g_gvPrefix + key; }
double GVget(const string key, double def = 0) { return GlobalVariableCheck(GV(key)) ? GlobalVariableGet(GV(key)) : def; }
void   GVset(const string key, double v)       { GlobalVariableSet(GV(key), v); }
void   GVdel(const string key)                 { if(GlobalVariableCheck(GV(key))) GlobalVariableDel(GV(key)); }
long   Magic() { return MagicBase + (long)Mode; }
string ModeName() { return Mode == MODE_TREND ? "trend" : Mode == MODE_MOMO ? "momo" : Mode == MODE_BREAKOUT ? "breakout" :
                           Mode == MODE_STRADDLE ? "straddle" : Mode == MODE_RECOVERY ? "recovery" : "mr"; }
bool   Hedging() { return Mode == MODE_STRADDLE || Mode == MODE_RECOVERY; }

//+------------------------------------------------------------------+
int OnInit()
{
   g_gvPrefix = "HS_" + _Symbol + "_" + IntegerToString(Magic()) + "_";
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetExpertMagicNumber(Magic());
   if(_Period != PERIOD_H1)
      Print("WARNING: attach to an H1 chart (decisions are taken on closed H1 bars); running on ", EnumToString(_Period));
   if(Hedging() && AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   { Print("ERROR: straddle/recovery need a hedging account"); return INIT_FAILED; }
   g_hFast = iMA(_Symbol, PERIOD_H1, EmaFast, 0, MODE_EMA, PRICE_CLOSE);
   g_hMid  = iMA(_Symbol, PERIOD_H1, EmaMid,  0, MODE_EMA, PRICE_CLOSE);
   g_hSlow = iMA(_Symbol, PERIOD_H1, EmaSlow, 0, MODE_EMA, PRICE_CLOSE);
   if(g_hFast == INVALID_HANDLE || g_hMid == INVALID_HANDLE || g_hSlow == INVALID_HANDLE) return INIT_FAILED;
   if(GVget("cap0", 0) <= 0) GVset("cap0", InitialCapital > 0 ? InitialCapital : AccountInfoDouble(ACCOUNT_BALANCE));
   if(GVget("peak", 0) <= 0) GVset("peak", AccountInfoDouble(ACCOUNT_EQUITY));
   if(SignalDump)
   {
      g_dump = FileOpen(DumpFile, FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
      if(g_dump != INVALID_HANDLE) FileWrite(g_dump, "bar_time", "mode", "dir", "entry", "sl", "tp", "atr");
   }
   PrintFormat("HourlySet init: mode=%s side=%d sl=%.2fATR rr=%.2f hold=%dh maxpos=%d setTp=%.2f daily=+%.2f%%/-%.2f%% cap0=%.2f",
               ModeName(), (int)Side, SlAtr, RR, MaxHoldHours, MaxPositions, SetTpAtr, DailyTargetPct, DailyStopPct, GVget("cap0"));
   if(!MQLInfoInteger(MQL_TESTER))
      Notify("EA เริ่มทำงาน", StringFormat("%s H1 | โหมด %s | balance %.2f | เสี่ยง %.2f%%/ขา | ถือสูงสุด %d ชม. | สรุปรายวันเวลา %02d:00 server", _Symbol, ModeName(),
             AccountInfoDouble(ACCOUNT_BALANCE), RiskPct, MaxHoldHours, SummaryHour), 0x3498DB);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(g_dump != INVALID_HANDLE) FileClose(g_dump);
   if(g_hFast != INVALID_HANDLE) IndicatorRelease(g_hFast);
   if(g_hMid  != INVALID_HANDLE) IndicatorRelease(g_hMid);
   if(g_hSlow != INVALID_HANDLE) IndicatorRelease(g_hSlow);
}

//+------------------------------------------------------------------+
//| Indicators on CLOSED bars (rates[] as series: r[1] = last closed)|
//+------------------------------------------------------------------+
double AtrClosed(const MqlRates &r[], int n)      // simple mean of TR over r[1..n], like the Python backtester
{
   double s = 0;
   for(int i = 1; i <= n; i++)
   {
      double tr = MathMax(r[i].high - r[i].low, MathMax(MathAbs(r[i].high - r[i + 1].close), MathAbs(r[i].low - r[i + 1].close)));
      s += tr;
   }
   return s / n;
}

double EmaAt(int handle, int shift)
{
   double b[1];
   if(CopyBuffer(handle, 0, shift, 1, b) != 1) return 0;
   return b[0];
}

//+------------------------------------------------------------------+
//| Decide the legs of a new set at the closed bar. Returns count.   |
//| dirs[]/sls[]/tps[] filled (up to 2 legs).                        |
//+------------------------------------------------------------------+
int Decide(const MqlRates &r[], double atrv, int &dirs[], double &sls[], double &tps[], string &tags[])
{
   double c = r[1].close;
   double slD = SlAtr * atrv, tpD = SlAtr * atrv * RR;
   int n = 0;
   int d = 0;
   if(Mode == MODE_TREND || Mode == MODE_RECOVERY)
   {
      double ef = EmaAt(g_hFast, 1), em = EmaAt(g_hMid, 1), es = EmaAt(g_hSlow, 1);
      if(ef <= 0 || em <= 0 || es <= 0) return 0;
      d = (ef > em && c > es) ? 1 : (ef < em && c < es) ? -1 : 0;
   }
   else if(Mode == MODE_MOMO)
   {
      double mv = c - r[5].close;
      if(MathAbs(mv) >= MomoKAtr * atrv) d = mv > 0 ? 1 : -1;
   }
   else if(Mode == MODE_BREAKOUT)
   {
      double hi = r[2].high, lo = r[2].low;
      for(int i = 3; i <= 5; i++) { hi = MathMax(hi, r[i].high); lo = MathMin(lo, r[i].low); }
      d = c > hi ? 1 : c < lo ? -1 : 0;
   }
   else if(Mode == MODE_MR)
   {
      double m = 0, v = 0;
      for(int i = 2; i <= 25; i++) m += r[i].close;
      m /= 24.0;
      for(int i = 2; i <= 25; i++) v += (r[i].close - m) * (r[i].close - m);
      double sd = MathSqrt(v / 24.0);
      if(sd <= 0) return 0;
      double z = (c - m) / sd;
      if(z < -MrZ) { d = 1;  tpD = MathMax(m - c, 0.2 * atrv); }
      else if(z > MrZ) { d = -1; tpD = MathMax(c - m, 0.2 * atrv); }
   }
   else if(Mode == MODE_STRADDLE)
   {
      dirs[0] = 1;  sls[0] = slD; tps[0] = tpD; tags[0] = "strad";
      dirs[1] = -1; sls[1] = slD; tps[1] = tpD; tags[1] = "strad";
      return 2;
   }
   if(d == 0) return 0;
   if((Side == SIDE_BUY && d < 0) || (Side == SIDE_SELL && d > 0)) return 0;
   dirs[0] = d; sls[0] = slD; tps[0] = tpD; tags[0] = Mode == MODE_RECOVERY ? "lead" : ModeName();
   return 1;
}

//+------------------------------------------------------------------+
//| Risk / sizing (same as H4Trend)                                  |
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
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double lossPerLot = 0;
   if(!OrderCalcProfit(dir > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, entry, sl, lossPerLot)) return 0;
   lossPerLot = MathAbs(lossPerLot);
   if(lossPerLot <= 0) return 0;
   double raw = RiskPct / 100.0 * equity * DdMultiplier(equity) / lossPerLot;
   double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   double notionalPerLot = contract * entry;
   if(MaxNotionalFrac > 0 && notionalPerLot > 0) raw = MathMin(raw, MaxNotionalFrac * equity / notionalPerLot);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lots = MathMin(vmax, MathFloor(raw / step) * step);
   if(lots < vmin) return 0;
   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Positions of this EA                                             |
//+------------------------------------------------------------------+
bool Ours(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return false;
   return PositionGetInteger(POSITION_MAGIC) == Magic() && PositionGetString(POSITION_SYMBOL) == _Symbol;
}

int CountOurs()
{
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--) { ulong t = PositionGetTicket(i); if(Ours(t)) n++; }
   return n;
}

// comment = "HS:<setid>:<tag>" ; setid = closed-bar time as integer
string SetOf(const string cmt) { int a = StringFind(cmt, ":"); int b = StringFind(cmt, ":", a + 1); return (a < 0 || b < 0) ? "" : StringSubstr(cmt, a + 1, b - a - 1); }
string TagOf(const string cmt) { int a = StringFind(cmt, ":"); int b = StringFind(cmt, ":", a + 1); return b < 0 ? "" : StringSubstr(cmt, b + 1); }

ulong OpenLeg(int dir, double lots, double sl, double tp, const string cmt)
{
   bool ok = dir > 0 ? trade.Buy(lots, _Symbol, 0, sl, tp, cmt) : trade.Sell(lots, _Symbol, 0, sl, tp, cmt);
   if(!ok || trade.ResultRetcode() != TRADE_RETCODE_DONE)
   { PrintFormat("order failed %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription()); return 0; }
   ulong deal = trade.ResultDeal();
   if(deal > 0 && HistoryDealSelect(deal)) return (ulong)HistoryDealGetInteger(deal, DEAL_POSITION_ID);
   // fallback: newest of ours
   for(int i = PositionsTotal() - 1; i >= 0; i--) { ulong t = PositionGetTicket(i); if(Ours(t) && PositionGetString(POSITION_COMMENT) == cmt) return t; }
   return 0;
}

void OpenSet(const MqlRates &r[], double atrv)
{
   int dirs[2]; double sls[2], tps[2]; string tags[2];
   int n = Decide(r, atrv, dirs, sls, tps, tags);
   if(n == 0) return;
   string setid = IntegerToString((long)r[1].time);
   double c = r[1].close;
   for(int i = 0; i < n; i++)
   {
      if(g_dump != INVALID_HANDLE)
         FileWrite(g_dump, TimeToString(r[1].time, TIME_DATE | TIME_MINUTES), ModeName(), dirs[i],
                   DoubleToString(c, 2), DoubleToString(c - dirs[i] * sls[i], 2), DoubleToString(c + dirs[i] * tps[i], 2), DoubleToString(atrv, 3));
      if(CountOurs() >= MaxPositions) { Print("max positions reached - leg skipped"); return; }
      double px = dirs[i] > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double sl = NormalizeDouble(px - dirs[i] * sls[i], _Digits), tp = NormalizeDouble(px + dirs[i] * tps[i], _Digits);
      double lots = SizeLots(dirs[i], px, sl);
      if(lots <= 0) { Print("zero volume (risk too small for min lot)"); return; }
      ulong t = OpenLeg(dirs[i], lots, sl, tp, "HS:" + setid + ":" + tags[i]);
      if(t == 0) continue;
      GVset("atr_" + IntegerToString(t), atrv);
      GVset("n_" + setid, GVget("n_" + setid, 0) + 1);
      PrintFormat("%s %s %.2f @ %.2f sl %.2f tp %.2f set %s", tags[i], dirs[i] > 0 ? "BUY" : "SELL", lots, px, sl, tp, setid);
      Notify(StringFormat("เปิดไม้ %s %s %.2f lot", dirs[i] > 0 ? "BUY" : "SELL", _Symbol, lots),
             StringFormat("โหมด: %s (%s)\nราคาเข้า: %.2f\nSL: %.2f  (เสี่ยง $%.2f)\nTP: %.2f\nถือได้สูงสุด: %d ชม.\nชุด: %s\nไม้ที่เปิดอยู่ตอนนี้: %d",
                          ModeName(), tags[i], px, sl, MathAbs(px - sl) * lots * SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE), tp, MaxHoldHours, setid, CountOurs()),
             dirs[i] > 0 ? 0x2ECC71 : 0xE67E22);
   }
}

//+------------------------------------------------------------------+
//| Per-tick management: max hold, set TP, recovery hedge, daily goal|
//+------------------------------------------------------------------+
void CloseSet(const string setid, const string why)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Ours(t)) continue;
      if(SetOf(PositionGetString(POSITION_COMMENT)) == setid) { trade.PositionClose(t); PrintFormat("close #%I64u (%s)", t, why); }
   }
}

void CloseAllOurs(const string why)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--) { ulong t = PositionGetTicket(i); if(Ours(t)) { trade.PositionClose(t); PrintFormat("close #%I64u (%s)", t, why); } }
}

void Manage()
{
   double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   datetime now = TimeCurrent();
   // ---- 1) max hold per leg (a recovery lead takes its hedge with it) ----
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Ours(t)) continue;
      string cmt = PositionGetString(POSITION_COMMENT);
      if(MaxHoldHours > 0 && now - (datetime)PositionGetInteger(POSITION_TIME) >= MaxHoldHours * 3600)
      {
         if(TagOf(cmt) == "lead") CloseSet(SetOf(cmt), "max hold (lead + hedge)");
         else { trade.PositionClose(t); PrintFormat("close #%I64u (max hold %dh)", t, MaxHoldHours); }
      }
   }
   // ---- 2) set-level take-profit / recovery basket ----
   string sets[]; double flt[], lots[]; double atrs[]; int cnt = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(!Ours(t)) continue;
      string sid = SetOf(PositionGetString(POSITION_COMMENT));
      int j = -1;
      for(int k = 0; k < cnt; k++) if(sets[k] == sid) { j = k; break; }
      if(j < 0)
      {
         ArrayResize(sets, cnt + 1); ArrayResize(flt, cnt + 1); ArrayResize(lots, cnt + 1); ArrayResize(atrs, cnt + 1);
         sets[cnt] = sid; flt[cnt] = 0; lots[cnt] = 0; atrs[cnt] = GVget("atr_" + IntegerToString(t), 0); j = cnt++;
      }
      flt[j] += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      lots[j] += PositionGetDouble(POSITION_VOLUME);
   }
   for(int j = 0; j < cnt; j++)
   {
      double net = flt[j] + GVget("sr_" + sets[j], 0);                 // + realised legs of the set
      double legs = GVget("n_" + sets[j], 1);
      double avgLots = legs > 0 ? lots[j] / MathMax(1.0, legs) : lots[j];
      if(Mode == MODE_RECOVERY)
      {
         if(legs >= 2 && atrs[j] > 0 && net >= BasketTpAtr * atrs[j] * contract * avgLots) CloseSet(sets[j], "basket profit");
      }
      else if(SetTpAtr > 0 && legs >= 2 && atrs[j] > 0 && net >= SetTpAtr * atrs[j] * contract * avgLots)
         CloseSet(sets[j], "set profit");
   }
   // ---- 3) recovery: open the hedge leg when the lead is HedgeAtAtr under water ----
   if(Mode == MODE_RECOVERY)
   {
      for(int i = PositionsTotal() - 1; i >= 0; i--)
      {
         ulong t = PositionGetTicket(i);
         if(!Ours(t)) continue;
         string cmt = PositionGetString(POSITION_COMMENT);
         if(TagOf(cmt) != "lead" || GVget("hd_" + IntegerToString(t), 0) > 0) continue;
         int d = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
         double a = GVget("atr_" + IntegerToString(t), 0);
         double px = d > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double adverse = -d * (px - PositionGetDouble(POSITION_PRICE_OPEN));
         if(a <= 0 || adverse < HedgeAtAtr * a) continue;
         if(CountOurs() >= MaxPositions) continue;
         double hp = d > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
         double hsl = NormalizeDouble(hp + d * 2 * a, _Digits), htp = NormalizeDouble(hp - d * 2 * a, _Digits);
         ulong h = OpenLeg(-d, PositionGetDouble(POSITION_VOLUME), hsl, htp, "HS:" + SetOf(cmt) + ":hedge");
         if(h > 0)
         {
            GVset("hd_" + IntegerToString(t), (double)h); GVset("atr_" + IntegerToString(h), a);
            GVset("n_" + SetOf(cmt), GVget("n_" + SetOf(cmt), 0) + 1);
            PrintFormat("hedge leg #%I64u for lead #%I64u (adverse %.2f >= %.2f)", h, t, adverse, HedgeAtAtr * a);
            Notify("เปิดขา HEDGE", StringFormat("ไม้หลัก #%I64u ติดลบ %.2f -> เปิดขาตรงข้าม %.2f lot (ปิดทั้งคู่เมื่อสุทธิ +%.1f ATR หรือครบ %d ชม.)", t, adverse, PositionGetDouble(POSITION_VOLUME), BasketTpAtr, MaxHoldHours), 0xF1C40F);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Daily goal: +Target / -Stop vs the server-day starting balance    |
//+------------------------------------------------------------------+
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
   PrintFormat("daily %s reached: %.2f%% vs day-start %.2f -> all closed, idle until tomorrow", state, pl * 100, base);
   Notify(state == "TARGET" ? "ถึงเป้ารายวันแล้ว" : "ชนลิมิตขาดทุนรายวัน", StringFormat("กำไร/ขาดทุนวันนี้ %+.2f%% (ฐาน %.2f) -> ปิดทุกไม้ หยุดเทรดจนถึงวันถัดไป", pl * 100, base), state == "TARGET" ? 0x2ECC71 : 0xE74C3C);
   return true;
}

//+------------------------------------------------------------------+
//| Daily summary to Discord: trades closed in the last 24 h, wins /   |
//| losses, net P/L, and what is still open (posted at SummaryHour)   |
//+------------------------------------------------------------------+
void DailySummary()
{
   if(MQLInfoInteger(MQL_TESTER)) return;
   MqlDateTime dt; TimeToStruct(TimeCurrent(), dt);
   if(dt.hour < SummaryHour) return;
   double today = dt.year * 10000 + dt.mon * 100 + dt.day;
   if(GVget("sumd", 0) == today) return;
   if(GVget("sumd", 0) == 0) { GVset("sumd", today); return; }     // first day after attach: nothing to summarise yet
   GVset("sumd", today);
   datetime end   = StringToTime(TimeToString(TimeCurrent(), TIME_DATE)) + SummaryHour * 3600;
   datetime start = end - 86400;
   int n = 0, wins = 0, losses = 0, opened = 0; double net = 0, best = 0, worst = 0;
   if(HistorySelect(start, end))
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(HistoryDealGetInteger(d, DEAL_MAGIC) != Magic() || HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
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
      double fp = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      floating += fp;
      long age = (TimeCurrent() - (datetime)PositionGetInteger(POSITION_TIME)) / 60;
      openList += StringFormat("\n  - #%I64u %s %.2f lot @ %.2f  ลอยตัว %s%.2f  (ถือมา %d ชม. %02d นาที)", t,
                               PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "BUY" : "SELL",
                               PositionGetDouble(POSITION_VOLUME), PositionGetDouble(POSITION_PRICE_OPEN), fp >= 0 ? "+" : "", fp, (int)(age / 60), (int)(age % 60));
   }
   string text = StringFormat("ช่วง %s -> %s\nเปิดไม้: %d ไม้ | ปิดไม้: %d ไม้\nชนะ %d / แพ้ %d (win rate %.0f%%)\nกำไรสุทธิที่ปิดแล้ว: %s%.2f  (ดีสุด %+.2f / แย่สุด %+.2f)\nไม้ที่ยังค้าง: %d ไม้  ลอยตัว %s%.2f%s\nbalance: %.2f | equity: %.2f",
                              TimeToString(start, TIME_DATE | TIME_MINUTES), TimeToString(end, TIME_DATE | TIME_MINUTES),
                              opened, n, wins, losses, n > 0 ? 100.0 * wins / n : 0.0, net >= 0 ? "+" : "", net, best, worst,
                              openN, floating >= 0 ? "+" : "", floating, openN > 0 ? openList : " (ไม่มี)",
                              AccountInfoDouble(ACCOUNT_BALANCE), AccountInfoDouble(ACCOUNT_EQUITY));
   Print("DAILY SUMMARY: ", text);
   Notify(StringFormat("สรุปรายวัน %s  %s%.2f", TimeToString(start, TIME_DATE), net >= 0 ? "+" : "", net), text, net >= 0 ? 0x2ECC71 : (n == 0 ? 0x95A5A6 : 0xE74C3C));
}

//+------------------------------------------------------------------+
bool HardStopTripped()
{
   if(GVget("halted", 0) > 0) return true;
   double cap0 = GVget("cap0", 0);
   if(HardStopPct <= 0 || cap0 <= 0) return false;
   if(AccountInfoDouble(ACCOUNT_EQUITY) <= cap0 * (1.0 - HardStopPct))
   {
      CloseAllOurs("hard stop");
      GVset("halted", 1);
      Print("HARD STOP: equity <= ", cap0 * (1.0 - HardStopPct), " -> all closed, halted. Delete GV '", GV("halted"), "' to resume.");
      Notify("HARD STOP - EA หยุดทำงาน", StringFormat("equity %.2f <= %.2f -> ปิดทุกไม้ EA หยุดถาวร (ลบ Global Variable ...halted เพื่อเริ่มใหม่)", AccountInfoDouble(ACCOUNT_EQUITY), cap0 * (1.0 - HardStopPct)), 0xE74C3C);
      return true;
   }
   return false;
}

void CleanupMeta()
{
   int total = GlobalVariablesTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      string name = GlobalVariableName(i);
      if(StringFind(name, g_gvPrefix + "atr_") == 0 || StringFind(name, g_gvPrefix + "hd_") == 0)
      {
         int p = StringFind(name, "_", StringLen(g_gvPrefix));
         ulong t = (ulong)StringToInteger(StringSubstr(name, p + 1));
         if(!PositionSelectByTicket(t)) GlobalVariableDel(name);
      }
   }
   // set-level GVs (n_/sr_) are dropped once no leg of the set is open
   total = GlobalVariablesTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      string name = GlobalVariableName(i);
      if(StringFind(name, g_gvPrefix + "n_") != 0) continue;
      string sid = StringSubstr(name, StringLen(g_gvPrefix) + 2);
      bool open = false;
      for(int k = PositionsTotal() - 1; k >= 0 && !open; k--) { ulong t = PositionGetTicket(k); if(Ours(t) && SetOf(PositionGetString(POSITION_COMMENT)) == sid) open = true; }
      if(!open) { GlobalVariableDel(name); GVdel("sr_" + sid); }
   }
}

//+------------------------------------------------------------------+
string JsonEscape(string s) { StringReplace(s, "\\", "\\\\"); StringReplace(s, "\"", "\\\""); StringReplace(s, "\n", "\\n"); return s; }

void Notify(const string title, const string text, const int clr)
{
   if(DiscordWebhook == "" || MQLInfoInteger(MQL_TESTER)) return;
   string body = StringFormat("{\"embeds\":[{\"title\":\"%s\",\"description\":\"%s\",\"color\":%d,\"footer\":{\"text\":\"HourlySet | %s | %s\"}}]}",
                              JsonEscape(title), JsonEscape(text), clr, _Symbol, TimeToString(TimeCurrent(), TIME_DATE | TIME_MINUTES));
   char data[], result[]; string headers;
   StringToCharArray(body, data, 0, StringLen(body), CP_UTF8);
   int rc = WebRequest("POST", DiscordWebhook, "Content-Type: application/json\r\n", 5000, data, result, headers);
   if(rc == -1) PrintFormat("Discord: WebRequest failed (%d) - whitelist https://discord.com in Tools>Options>Expert Advisors", GetLastError());
}

//+------------------------------------------------------------------+
//| Closes: book realised P/L into the set, straddle twin lock, alert |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD) return;
   if(!HistoryDealSelect(trans.deal)) return;
   if(HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != Magic()) return;
   long entryType = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   if(entryType != DEAL_ENTRY_OUT && entryType != DEAL_ENTRY_OUT_BY) return;
   ulong  posId  = (ulong)HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
   double px     = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
   double vol    = HistoryDealGetDouble(trans.deal, DEAL_VOLUME);
   double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) + HistoryDealGetDouble(trans.deal, DEAL_SWAP) + HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   long   reason = HistoryDealGetInteger(trans.deal, DEAL_REASON);
   long   dtype  = HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   string side   = (dtype == DEAL_TYPE_SELL) ? "BUY" : "SELL";
   // opening deal -> comment (set id / tag), entry, time
   string cmt = ""; double entry = 0; datetime opened = 0;
   if(HistorySelectByPosition(posId))
      for(int i = 0; i < HistoryDealsTotal(); i++)
      {
         ulong d = HistoryDealGetTicket(i);
         if(HistoryDealGetInteger(d, DEAL_ENTRY) == DEAL_ENTRY_IN)
         { cmt = HistoryDealGetString(d, DEAL_COMMENT); entry = HistoryDealGetDouble(d, DEAL_PRICE); opened = (datetime)HistoryDealGetInteger(d, DEAL_TIME); break; }
      }
   string sid = SetOf(cmt), tag = TagOf(cmt);
   if(sid != "") GVset("sr_" + sid, GVget("sr_" + sid, 0) + profit);       // realised part of the set
   // straddle: one leg took profit -> close the twin at market
   if(Mode == MODE_STRADDLE && LockTwinOnTp && reason == DEAL_REASON_TP && sid != "")
      CloseSet(sid, "twin lock after TP");
   string why = reason == DEAL_REASON_SL ? "ชน Stop Loss" : reason == DEAL_REASON_TP ? "ถึง Take Profit" : reason == DEAL_REASON_SO ? "Stop out (margin)" :
                reason == DEAL_REASON_EXPERT ? "EA ปิดเอง (ครบเวลา / ชุดกำไร / เป้ารายวัน / hedge)" : "ปิดมือ / อื่น ๆ";
   long held = opened > 0 ? (TimeCurrent() - opened) / 60 : 0;
   int openLeft = CountOurs();
   Notify(StringFormat("ปิดไม้ #%I64u  %s  %s%.2f", posId, profit >= 0 ? "กำไร" : "ขาดทุน", profit >= 0 ? "+" : "", profit),
          StringFormat("%s %s %.2f lot (%s/%s)\nเข้า: %.2f -> ออก: %.2f\nเหตุผล: %s\nถือ: %d ชม. %02d นาที\nกำไรสุทธิของชุดนี้: %.2f\nไม้ที่ยังค้าง: %d\nbalance: %.2f",
                       side, _Symbol, vol, ModeName(), tag, entry, px, why, (int)(held / 60), (int)(held % 60), GVget("sr_" + sid, 0), openLeft, AccountInfoDouble(ACCOUNT_BALANCE)),
          profit >= 0 ? 0x2ECC71 : 0xE74C3C);
}

//+------------------------------------------------------------------+
void OnTick()
{
   if(HardStopTripped()) return;
   DailySummary();
   bool blocked = DailyBlocked();
   Manage();

   datetime bt = iTime(_Symbol, PERIOD_H1, 0);
   if(bt == g_lastBar) return;
   g_lastBar = bt;
   CleanupMeta();
   if(blocked) return;
   if(MaxSignalAgeMin > 0 && !MQLInfoInteger(MQL_TESTER) && TimeCurrent() - bt > MaxSignalAgeMin * 60)
   { PrintFormat("closed-bar decision is %d min old (> %d) - skipped", (int)((TimeCurrent() - bt) / 60), MaxSignalAgeMin); return; }

   MqlRates r[];
   ArraySetAsSeries(r, true);
   int need = MathMax(AtrN, 26) + 3;
   if(CopyRates(_Symbol, PERIOD_H1, 0, need, r) < need) return;
   double atrv = AtrClosed(r, AtrN);
   if(atrv <= 0) return;
   if(CountOurs() >= MaxPositions) return;
   OpenSet(r, atrv);
}
//+------------------------------------------------------------------+
