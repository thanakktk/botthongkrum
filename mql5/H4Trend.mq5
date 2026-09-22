//+------------------------------------------------------------------+
//| H4Trend.mq5 — XAUUSD H4 trend/breakout bot (port of the Python   |
//| "H4 Trend bot": breakout_sr + donchian_breakout + roc_momentum)  |
//|                                                                  |
//| Each strategy trades ALONE (its own magic number, at most ONE    |
//| open position at a time), on CLOSED bars of the chart timeframe. |
//| Sizing: RiskPct % of equity per trade, notional cap, drawdown    |
//| throttle from the equity peak, hard stop at HardStopPct loss.    |
//| Management (per tick): at TP1 (2R) close PartialPct and move SL  |
//| to break-even (+0.05R); then trail the runner by TrailR behind   |
//| price; broker TP = TP2 (2.5R). Identical to research/backtester. |
//|                                                                  |
//| Signal parity: run in the Strategy Tester with SignalDump=true   |
//| -> Common\Files\<DumpFile> lists every signal per closed bar,     |
//| compared against research/parity_dump.py (Python side).          |
//+------------------------------------------------------------------+
#property copyright "botthongkrum"
#property link      "https://github.com/thanakktk/botthongkrum"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>

//--- inputs: risk
input double RiskPct         = 1.0;     // risk per trade, % of equity
input double MaxNotionalFrac = 2.0;     // cap: position notional <= this x equity
input double TP1_R           = 2.0;     // bank partial + break-even at this R
input double TP2_R           = 2.5;     // broker take-profit at this R
input double PartialPct      = 0.5;     // fraction closed at TP1
input double TrailR          = 1.0;     // trail distance after TP1, in R
input double BeBufferR       = 0.05;    // break-even nudged into profit, in R
input double DD1             = 0.20;    // throttle tier 1: drawdown from peak
input double DD1_Mult        = 0.5;     //   -> risk multiplier
input double DD2             = 0.35;    // throttle tier 2
input double DD2_Mult        = 0.25;    //   -> risk multiplier
input double HardStopPct     = 0.60;    // close all + halt at this loss of InitialCapital
input double InitialCapital  = 0;       // 0 = balance when first attached (stored)
//--- inputs: strategies (defaults = the backtested Python parameters)
input bool   UseBreakoutSR   = true;
input int    BrkLookback     = 20;
input bool   UseDonchian     = true;
input int    DonN            = 30;
input bool   UseRoc          = true;
input int    RocN            = 10;
input double RocThreshold    = 1.0;     // % change
input int    AtrN            = 14;
input double SlAtrMult       = 1.5;
//--- inputs: plumbing
input long   MagicBase       = 525600;  // +1 brk, +2 don, +3 roc
input int    Slippage        = 30;      // points
input bool   SignalDump      = false;   // tester: write every signal to a CSV
input string DumpFile        = "h4trend_signals.csv";
input string DiscordWebhook  = "";      // optional (whitelist the URL in Tools>Options>Expert Advisors)

//--- strategy ids
#define S_BRK 1
#define S_DON 2
#define S_ROC 3

CTrade   trade;
datetime g_lastBar = 0;
int      g_dump    = INVALID_HANDLE;
string   g_gvPrefix;

//+------------------------------------------------------------------+
string GV(const string key) { return g_gvPrefix + key; }
double GVget(const string key, double def = 0) { return GlobalVariableCheck(GV(key)) ? GlobalVariableGet(GV(key)) : def; }
void   GVset(const string key, double v)       { GlobalVariableSet(GV(key), v); }
void   GVdel(const string key)                 { if(GlobalVariableCheck(GV(key))) GlobalVariableDel(GV(key)); }

string StratName(int s) { return s == S_BRK ? "breakout_sr" : s == S_DON ? "donchian_breakout" : "roc_momentum"; }
long   StratMagic(int s) { return MagicBase + s; }

//+------------------------------------------------------------------+
int OnInit()
{
   g_gvPrefix = "H4T_" + _Symbol + "_" + IntegerToString(MagicBase) + "_";
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFillingBySymbol(_Symbol);
   if(GVget("cap0", 0) <= 0)
      GVset("cap0", InitialCapital > 0 ? InitialCapital : AccountInfoDouble(ACCOUNT_BALANCE));
   if(GVget("peak", 0) <= 0)
      GVset("peak", AccountInfoDouble(ACCOUNT_EQUITY));
   if(SignalDump)
   {
      g_dump = FileOpen(DumpFile, FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI, ',');
      if(g_dump != INVALID_HANDLE)
         FileWrite(g_dump, "bar_time", "strategy", "dir", "entry", "sl", "tp");
   }
   PrintFormat("H4Trend init: cap0=%.2f peak=%.2f halted=%d tf=%s", GVget("cap0"), GVget("peak"),
               (int)GVget("halted", 0), EnumToString(_Period));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(g_dump != INVALID_HANDLE) FileClose(g_dump);
}

//+------------------------------------------------------------------+
//| Indicators on CLOSED bars: rates[] is a series (index 0 = the    |
//| forming bar), so the last closed bar is rates[1].                |
//+------------------------------------------------------------------+
// simple-average ATR over the last n closed bars, TR uses the previous close
double AtrClosed(const MqlRates &r[], int n)
{
   if(ArraySize(r) < n + 2) return 0;
   double sum = 0;
   for(int i = 1; i <= n; i++)
   {
      double h = r[i].high, l = r[i].low, pc = r[i + 1].close;
      sum += MathMax(h - l, MathMax(MathAbs(h - pc), MathAbs(l - pc)));
   }
   return sum / n;
}
// highest high / lowest low of the n closed bars BEFORE the last closed one
double HighestPrior(const MqlRates &r[], int n)
{
   double v = -DBL_MAX;
   for(int i = 2; i <= n + 1; i++) v = MathMax(v, r[i].high);
   return v;
}
double LowestPrior(const MqlRates &r[], int n)
{
   double v = DBL_MAX;
   for(int i = 2; i <= n + 1; i++) v = MathMin(v, r[i].low);
   return v;
}

//+------------------------------------------------------------------+
//| Signal: dir = +1 buy / -1 sell / 0 none. entry/sl/tp as Python.  |
//+------------------------------------------------------------------+
int Signal(int s, const MqlRates &r[], double &entry, double &sl, double &tp)
{
   int need = (s == S_BRK ? BrkLookback + 2 : s == S_DON ? DonN + 2 : RocN + 2);
   need = MathMax(need, AtrN + 2);
   if(ArraySize(r) < need) return 0;
   double a = AtrClosed(r, AtrN);
   if(a <= 0) return 0;
   entry = r[1].close;
   int dir = 0;
   if(s == S_BRK || s == S_DON)
   {
      int n = (s == S_BRK ? BrkLookback : DonN);
      double hi = HighestPrior(r, n), lo = LowestPrior(r, n);
      if(entry > hi) dir = 1; else if(entry < lo) dir = -1;
   }
   else
   {
      double past = r[1 + RocN].close;
      if(past == 0) return 0;
      double roc = (entry - past) / past * 100.0;
      if(roc > RocThreshold) dir = 1; else if(roc < -RocThreshold) dir = -1;
   }
   if(dir == 0) return 0;
   double rdist = SlAtrMult * a;
   sl = entry - dir * rdist;
   tp = entry + dir * TP2_R * rdist;      // broker TP = TP2 (the arbitrator's tp2)
   return dir;
}

//+------------------------------------------------------------------+
//| Position bookkeeping                                              |
//+------------------------------------------------------------------+
bool FindPosition(int s, ulong &ticket)
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol && PositionGetInteger(POSITION_MAGIC) == StratMagic(s))
      { ticket = t; return true; }
   }
   return false;
}

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
   if(MaxNotionalFrac > 0 && notionalPerLot > 0)
      raw = MathMin(raw, MaxNotionalFrac * equity / notionalPerLot);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double lots = MathFloor(raw / step) * step;
   lots = MathMin(vmax, lots);
   if(lots < vmin) return 0;               // Python: below min lot -> no trade
   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
void OpenTrade(int s, int dir, double entry, double sl, double tp)
{
   double lots = SizeLots(dir, entry, sl);
   if(lots <= 0) { PrintFormat("%s: zero volume (risk too small for min lot)", StratName(s)); return; }
   trade.SetExpertMagicNumber(StratMagic(s));
   string cmt = "H4T:" + StratName(s);
   bool ok = dir > 0 ? trade.Buy(lots, _Symbol, 0, sl, tp, cmt) : trade.Sell(lots, _Symbol, 0, sl, tp, cmt);
   if(!ok || trade.ResultRetcode() != TRADE_RETCODE_DONE)
   { PrintFormat("%s: order failed %d %s", StratName(s), trade.ResultRetcode(), trade.ResultRetcodeDescription()); return; }
   // find the resulting position and record its management meta
   ulong ticket = 0;
   if(FindPosition(s, ticket))
   {
      double fill = PositionGetDouble(POSITION_PRICE_OPEN);
      double rdist = MathAbs(fill - sl);
      GVset("r_" + IntegerToString(ticket), rdist);
      GVset("st_" + IntegerToString(ticket), 0);              // 0 running, 1 tp1_hit
      GVset("v0_" + IntegerToString(ticket), lots);
      PrintFormat("%s %s %.2f @ %.2f sl %.2f tp %.2f (R=%.2f)", StratName(s), dir > 0 ? "BUY" : "SELL", lots, fill, sl, tp, rdist);
      Notify(StringFormat("%s %s %.2f lots @ %.2f | SL %.2f TP %.2f", StratName(s), dir > 0 ? "BUY" : "SELL", lots, fill, sl, tp));
   }
}

void ManagePosition(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return;
   string k = IntegerToString(ticket);
   double rdist = GVget("r_" + k, 0);
   if(rdist <= 0)   // adopted / unknown position: reconstruct R from the initial SL
   {
      double sl0 = PositionGetDouble(POSITION_SL);
      rdist = MathAbs(PositionGetDouble(POSITION_PRICE_OPEN) - sl0);
      if(rdist <= 0) return;
      GVset("r_" + k, rdist); GVset("st_" + k, 0); GVset("v0_" + k, PositionGetDouble(POSITION_VOLUME));
   }
   int    d     = PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? 1 : -1;
   double entry = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl    = PositionGetDouble(POSITION_SL);
   double tp    = PositionGetDouble(POSITION_TP);
   double vol   = PositionGetDouble(POSITION_VOLUME);
   double px    = d > 0 ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   int    state = (int)GVget("st_" + k, 0);
   double be    = NormalizeDouble(entry + d * BeBufferR * rdist, _Digits);

   if(state == 0)
   {
      double tp1 = entry + d * TP1_R * rdist;
      bool hit = d > 0 ? px >= tp1 : px <= tp1;
      if(!hit) return;
      double v0   = GVget("v0_" + k, vol);
      double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
      double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      double part = MathFloor(v0 * PartialPct / step) * step;
      if(part >= vmin && vol - part >= vmin)
         trade.PositionClosePartial(ticket, NormalizeDouble(part, 2));
      trade.PositionModify(ticket, be, tp);
      GVset("st_" + k, 1);
      PrintFormat("#%I64u TP1 hit: banked %.2f, SL->BE %.2f", ticket, part, be);
      Notify(StringFormat("#%I64u TP1 hit - banked %.2f lots, SL -> break-even %.2f", ticket, part, be));
      return;
   }
   // state 1: trail the runner
   double trail = NormalizeDouble(px - d * TrailR * rdist, _Digits);
   bool better = d > 0 ? trail > sl : trail < sl;
   if(better) trade.PositionModify(ticket, trail, tp);
}

void CleanupMeta()
{
   // drop GVs of positions that no longer exist (closed by SL/TP)
   int total = GlobalVariablesTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      string name = GlobalVariableName(i);
      if(StringFind(name, g_gvPrefix + "r_") != 0) continue;
      ulong t = (ulong)StringToInteger(StringSubstr(name, StringLen(g_gvPrefix) + 2));
      if(!PositionSelectByTicket(t))
      { string k = IntegerToString(t); GVdel("r_" + k); GVdel("st_" + k); GVdel("v0_" + k); }
   }
}

//+------------------------------------------------------------------+
bool HardStopTripped()
{
   if(GVget("halted", 0) > 0) return true;
   double cap0 = GVget("cap0", 0);
   if(HardStopPct <= 0 || cap0 <= 0) return false;
   if(AccountInfoDouble(ACCOUNT_EQUITY) <= cap0 * (1.0 - HardStopPct))
   {
      for(int s = S_BRK; s <= S_ROC; s++) { ulong t; if(FindPosition(s, t)) trade.PositionClose(t); }
      GVset("halted", 1);
      Print("HARD STOP: equity <= ", cap0 * (1.0 - HardStopPct), " -> all closed, trading halted. Delete GV '", GV("halted"), "' to resume.");
      Notify("HARD STOP tripped - all positions closed, EA halted");
      return true;
   }
   return false;
}

void Notify(const string text)
{
   if(DiscordWebhook == "") return;
   string body = "{\"content\":\"" + _Symbol + " H4Trend: " + text + "\"}";
   char data[], result[]; string headers;
   StringToCharArray(body, data, 0, StringLen(body));
   WebRequest("POST", DiscordWebhook, "Content-Type: application/json\r\n", 5000, data, result, headers);
}

//+------------------------------------------------------------------+
void OnTick()
{
   if(HardStopTripped()) return;

   // per-tick management of every strategy's position
   for(int s = S_BRK; s <= S_ROC; s++)
   { ulong t; if(FindPosition(s, t)) ManagePosition(t); }

   // entries once per closed bar
   datetime bt = iTime(_Symbol, _Period, 0);
   if(bt == g_lastBar) return;
   g_lastBar = bt;
   CleanupMeta();

   MqlRates r[];
   ArraySetAsSeries(r, true);
   int need = MathMax(MathMax(BrkLookback, DonN), MathMax(RocN, AtrN)) + 3;
   if(CopyRates(_Symbol, _Period, 0, need, r) < need) return;

   for(int s = S_BRK; s <= S_ROC; s++)
   {
      if((s == S_BRK && !UseBreakoutSR) || (s == S_DON && !UseDonchian) || (s == S_ROC && !UseRoc)) continue;
      double entry, sl, tp;
      int dir = Signal(s, r, entry, sl, tp);
      if(dir == 0) continue;
      if(g_dump != INVALID_HANDLE)
         FileWrite(g_dump, TimeToString(r[1].time, TIME_DATE | TIME_MINUTES), StratName(s), dir,
                   DoubleToString(entry, 5), DoubleToString(sl, 5), DoubleToString(tp, 5));
      ulong t;
      if(FindPosition(s, t)) continue;          // one position per strategy
      OpenTrade(s, dir, entry, NormalizeDouble(sl, _Digits), NormalizeDouble(tp, _Digits));
   }
}
//+------------------------------------------------------------------+
