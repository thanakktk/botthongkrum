# H4Trend EA — ติดตั้งบน VPS (MT5 อย่างเดียว ไม่ต้องมี Python)

ไฟล์: `H4Trend.ex5` (พร้อมใช้) และ `H4Trend.mq5` (ซอร์ส)
ดาวน์โหลดตรง: https://github.com/thanakktk/botthongkrum/raw/main/mql5/H4Trend.ex5

## ขั้นตอน (ทำครั้งเดียว ~15 นาที)

1. **Remote เข้า VPS**: `Win+R` → `mstsc` → IP ของ VPS → user/password จากอีเมล
2. **ติดตั้ง MT5 ของ VT Markets** ใน VPS (ดาวน์โหลดจาก client portal ของ VT หรือ vtmarkets.com → Platforms → MT5 → Windows) → ล็อกอินบัญชี (demo `1292692` / `VTMarkets-Demo`)
3. **เปิด Algo Trading**: Tools → Options → Expert Advisors → ติ๊ก *Allow algorithmic trading* → OK → กดปุ่ม **Algo Trading** บนแถบเครื่องมือให้เป็นสีเขียว
4. **วางไฟล์ EA**: ใน MT5 กด File → **Open Data Folder** → เข้า `MQL5\Experts` → วาง `H4Trend.ex5` ที่ดาวน์โหลดไว้ → กลับมาที่ MT5 หน้าต่าง Navigator (Ctrl+N) คลิกขวา Expert Advisors → **Refresh**
5. **เปิดกราฟ**: Market Watch คลิกขวา → *Show All* → ลาก `XAUUSD-ECN` ออกมาเป็นกราฟ → เลือก timeframe **H4**
6. **แนบ EA**: ลาก `H4Trend` จาก Navigator ลงบนกราฟ → แท็บ *Common* ติ๊ก *Allow Algo Trading* → แท็บ *Inputs* ใช้ค่าเริ่มต้นได้เลย → OK
   - มุมขวาบนของกราฟต้องขึ้นชื่อ EA พร้อมหน้ายิ้ม/ไม่มีกากบาท และแท็บ *Experts* (Ctrl+T) ต้องมีบรรทัด `H4Trend init: cap0=... peak=...`
7. **ออกจาก VPS โดยปิดหน้าต่าง Remote Desktop** (disconnect) — ห้าม Sign out

MT5 บน VPS จะรันต่อเอง ถ้า VPS รีบูต MT5 ไม่เปิดเองอัตโนมัติ → ใส่ shortcut ของ `terminal64.exe` ไว้ใน Startup (`Win+R` → `shell:startup`) MT5 จำการล็อกอิน, กราฟ และ EA ที่แนบไว้ให้เอง

## Inputs สำคัญ (ค่าเริ่มต้น = ค่าที่ backtest)

| Input | ค่า | ความหมาย |
|---|---|---|
| RiskPct | 1.0 | เสี่ยง 1% ของ equity ต่อไม้ (ทุน $10k → ~$100/ไม้) |
| MaxNotionalFrac | 2.0 | เพดานขนาด position ≤ 2× equity |
| DD1 / DD1_Mult | 0.20 / 0.5 | ลด risk ครึ่งหนึ่งเมื่อ equity ต่ำกว่าจุดสูงสุด 20% |
| DD2 / DD2_Mult | 0.35 / 0.25 | เหลือ 1/4 เมื่อต่ำกว่า 35% |
| HardStopPct | 0.60 | ขาดทุน 60% ของทุนเริ่มต้น → ปิดทุกไม้ + หยุด |
| TP1_R / TP2_R | 2.0 / 2.5 | ปิดครึ่ง+ย้าย SL ไป break-even ที่ 2R, TP ที่ 2.5R |
| DiscordWebhook | ว่าง | ใส่ URL ถ้าต้องการแจ้งเตือน (ต้อง whitelist URL ใน Options → Expert Advisors → Allow WebRequest) |

**ทุนขั้นต่ำ**: ~$3,000 ที่ risk 1% (ต่ำกว่านั้นล็อตคำนวณได้ต่ำกว่า 0.01 → EA จะข้ามไม้) หรือใช้บัญชี cent

## สิ่งที่ EA ทำ / ไม่ทำ

- ตัดสินใจ**เฉพาะตอนแท่ง H4 ปิด** (00:00, 04:00, 08:00 … เวลา server) ระหว่างแท่งจะเห็นแค่การจัดการไม้ (TP1/trail)
- แต่ละกลยุทธ์ (magic 525601/2/3) ถือได้**ครั้งละ 1 ไม้** → สูงสุด 3 ไม้พร้อมกัน มักไปทางเดียวกัน
- ไม่มีกฎ FTMO, ไม่ปิดก่อนวันหยุด, ไม่หลบข่าว (ตามที่ backtest)
- ถ้า hard stop ทำงาน EA จะหยุดถาวร: แก้โดยลบ Global Variable `H4T_XAUUSD-ECN_525600_halted` (Tools → Global Variables, F3)

## ผลที่คาดหวัง (จาก backtest 22 ปี, risk 1% + throttle)

เฉลี่ย ~+2%/เดือน, CAGR ~22%, drawdown สูงสุดในอดีต 38% — **มีปีขาดทุน** (−9 ถึง −36%) และช่วงขาดทุนติดกัน 6–9 เดือน ช่วง มิ.ย. 2024→ก.ย. 2026 (ขาขึ้นแรง) ได้ดีกว่านี้มาก ห้ามใช้ช่วงนั้นเป็นความคาดหวัง

---

# HourlySet EA — บอท "มีไม้ทุกชั่วโมง" หลายไม้/hedge ได้ (H1)

ไฟล์: `HourlySet.ex5` (คอมไพล์แล้ว 0 errors) และ `HourlySet.mq5` · แนบบนกราฟ **XAUUSD-ECN H1** · ต้องเป็นบัญชี hedging (VT Markets เป็น)

## ผล backtest ก่อนใช้ (research/hourly_backtest.py, 2015–2026, ต้นทุน $0.25/oz, risk 0.25 %/ขา)

| Mode | ไม้/วัน | ชั่วโมงที่มีไม้ | WR | PF 2015–26 | PF 2022–26 |
|---|---|---|---|---|---|
| trend BUY-only 1.5 ATR / 1R (**default**) | 10.5 | 46 % | 48 % | **0.92** | **1.01** |
| trend สองทาง | 18.9 | 83 % | 47 % | 0.89 | 0.95 |
| straddle (BUY+SELL ทุกชั่วโมง) + ปิดทั้งชุดเมื่อกำไร | 25 | 55 % | 47 % | 0.87 | 0.92 |
| recovery (เปิด hedge เมื่อติดลบ 0.5 ATR, ปิดชุดที่ +0.2 ATR) | 21 | 61 % | 46 % | 0.87 | 0.93 |
| breakout / momo / mean-reversion | 4–15 | 15–65 % | 41–48 % | 0.80–0.90 | 0.86–0.95 |

**ทุกโหมดติดลบ** การ hedge ไม่สร้าง edge (BUY+SELL พร้อมกันหักลบกันเหลือแต่ต้นทุน 2 เท่า) ตัว default คือ "แพ้น้อยที่สุด" ใช้ทดสอบ demo ก่อน

## Inputs หลัก

| Input | default | ความหมาย |
|---|---|---|
| Mode | trend | trend / momo / breakout / straddle / recovery / mr |
| Side | BUY | both / buy / sell |
| SlAtr / RR | 1.5 / 1.0 | SL = 1.5×ATR(14,H1), TP = 1.0×SL |
| MaxHoldHours | 4 | ปิดไม้เมื่อครบ 4 ชม. (recovery: ปิด lead+hedge พร้อมกัน) |
| MaxPositions | 4 | ไม้เปิดพร้อมกันสูงสุด |
| SetTpAtr | 0 | ปิดทั้งชุด (ไม้ที่เปิดชั่วโมงเดียวกัน) เมื่อกำไรสุทธิ = ค่านี้ × ATR (นับขาที่ปิดไปแล้วด้วย) ใช้กับ straddle; 0 = ปิด |
| LockTwinOnTp | false | straddle: ขาหนึ่งถึง TP → ปิดอีกขาทันที |
| HedgeAtAtr / BasketTpAtr | 0.5 / 0.2 | recovery: เปิด hedge เมื่อติดลบ 0.5 ATR, ปิดคู่ที่กำไรสุทธิ +0.2 ATR |
| DailyTargetPct / DailyStopPct | 0 / 0 | เป้า/ลิมิตรายวัน % ของ balance ต้นวัน server → ถึงแล้วปิดหมด หยุดถึงพรุ่งนี้ (0 = ปิด) |
| RiskPct | 0.25 | เสี่ยงต่อขา % ของ equity (4 ไม้ = 1 %) |
| DD1/DD2, HardStopPct | 0.20/0.35, 0.60 | throttle และ hard stop เหมือน H4Trend |
| MagicBase | 737000 | + Mode |
| SignalDump | false | Strategy Tester: เขียนทุกการตัดสินใจลง Common\Files\hourlyset_signals.csv |

## วิธีทดสอบใน Strategy Tester

1. View → Strategy Tester (Ctrl+R) → Expert `HourlySet`, Symbol `XAUUSD-ECN`, Period **H1**, Model *Every tick based on real ticks*
2. Inputs: ค่า default หรือเปลี่ยน Mode; ใส่ DailyTargetPct/DailyStopPct ถ้าต้องการ
3. ดูแท็บ Journal ว่ามี `HourlySet init:` และไม่มี error; Results → Report เทียบกับ `reports/hourly_sweep_*.txt`
4. ถ้าจะเทียบสัญญาณตัวต่อตัวกับ Python ตั้ง SignalDump=true แล้วเทียบ CSV กับ `reports/hourly_trades.csv`

## สิ่งที่ EA ทำ / ไม่ทำ

- ตัดสินใจ**เฉพาะตอนแท่ง H1 ปิด**; ระหว่างแท่งจัดการ max hold / set TP / hedge / เป้ารายวัน ทุก tick
- ไม่ปิดไม้ตอนจบวันเอง (ตามที่ขอ) — ไม้ปิดด้วย SL/TP/ครบ 4 ชม./ชุดกำไร/เป้ารายวัน
- ต่อสัญญาณช้ากว่า 20 นาทีหลังแท่งปิด (แนบ EA กลางแท่ง) จะข้ามชั่วโมงนั้น
- หยุดถาวรเมื่อ hard stop → ลบ Global Variable `HS_XAUUSD-ECN_737000_halted`

## การแจ้งเตือน Discord (HourlySet)

Webhook ใส่เป็นค่า default ใน input `DiscordWebhook` แล้ว (เปลี่ยนได้) — **ต้องเปิด WebRequest ก่อน**: Tools → Options → Expert Advisors → ติ๊ก *Allow WebRequest for listed URL* → เพิ่ม `https://discord.com` มิฉะนั้น Journal จะขึ้น `Discord: WebRequest failed (4014)` และไม่มีข้อความออก (ใน Strategy Tester ไม่ส่งอยู่แล้ว)

| เหตุการณ์ | หัวข้อ | เนื้อหา |
|---|---|---|
| EA เริ่ม | `EA เริ่มทำงาน` | สัญลักษณ์, โหมด, balance, risk/ขา, ถือสูงสุด, เวลาสรุปรายวัน |
| เปิดไม้ | `เปิดไม้ BUY XAUUSD-ECN 0.10 lot` (เขียว/ส้ม) | โหมด, ราคาเข้า, SL + $ ที่เสี่ยง, TP, ถือได้สูงสุด, ชุด, จำนวนไม้ที่เปิดอยู่ |
| ปิดไม้ | `ปิดไม้ #ticket กำไร +12.30` / `ขาดทุน -8.10` (เขียว/แดง) | ทิศ, lot, เข้า→ออก, เหตุผล (SL / TP / EA ปิดเอง / มือ), เวลาที่ถือ, กำไรสุทธิของชุด, ไม้ที่ยังค้าง, balance |
| เปิดขา hedge (โหมด recovery) | `เปิดขา HEDGE` (เหลือง) | ไม้หลักติดลบเท่าไร, ขาตรงข้ามกี่ lot, เงื่อนไขปิดคู่ |
| ถึงเป้า / ชนลิมิตรายวัน | `ถึงเป้ารายวันแล้ว` / `ชนลิมิตขาดทุนรายวัน` | % ของวัน, ปิดทุกไม้ หยุดถึงพรุ่งนี้ |
| **สรุปรายวัน** (input `SummaryHour`, default 00:00 server) | `สรุปรายวัน 2026.09.23  +45.20` | ช่วงเวลา 24 ชม., เปิดกี่ไม้ / ปิดกี่ไม้, ชนะ/แพ้ + win rate, กำไรสุทธิ (ดีสุด/แย่สุด), **ไม้ที่ยังค้าง** รายตัว (ทิศ, lot, ราคา, ลอยตัว, ถือมานานเท่าไร), balance/equity |
| Hard stop | `HARD STOP - EA หยุดทำงาน` | equity ต่ำกว่าเส้น, วิธีเริ่มใหม่ |

สรุปรายวันจะเริ่มส่งตั้งแต่วันที่สองหลังแนบ EA (วันแรกไม่มีข้อมูลครบ 24 ชม.)

---

# MTF EA — D1/H4/H1 บอกทิศ, เข้าที่ M15 (ย่อตัว)

ไฟล์: `MTF.ex5` / `MTF.mq5` · แนบบนกราฟ **XAUUSD-ECN M15** · คู่มือเต็มและผลทดสอบ: `docs/mtf_bot.md`

- ทิศ: D1, H4, H1 ต้องขึ้นพร้อมกัน (EMA20 > EMA50 และ close > EMA50) → หา BUY; ลงพร้อมกัน → SELL
- เข้า: ใน 24 แท่ง M15 ราคาแตะ EMA20 แล้วแท่งล่าสุดปิดกลับเหนือ EMA20 และเหนือ high แท่งก่อน; SL ใต้ swing (0.5–2 ATR); TP 2R; ปิดเมื่อครบ 24 ชม.; 1 ไม้ต่อรอบย่อตัว
- backtest 2015–2026 M15 (ต้นทุนยุคปัจจุบัน): 2,622 ไม้ (0.9/วัน), WR 36 %, W/L 1.94, **PF 1.09**, IS 1.02 / OOS 1.17, +6.6 %/ปี ที่ risk 0.5 %, max DD 15 %; 2024–26 PF 1.2–1.4 · M5 แย่กว่าทุกแบบ (ไม่แนะนำ)
- Discord เหมือน HourlySet (เปิด/ปิด/สรุปรายวัน/เป้ารายวัน/hard stop) ใส่ webhook ใน input `DiscordWebhook` หรือ Load `MTF_local.set`
- **v2 (default ปัจจุบัน)**: gate คุณภาพ 4 ตัว (เข้าเฉพาะ 15:00–24:00 server, D1 EMA gap ≥ 0.7 ATR, H4 ทิศเดิม ≥ 6 แท่ง, H1 RSI ฝั่งเทรนด์ > 51) + TP 3R + break-even ที่ 1R → 2015–26: 728 ไม้ (~1.3/สัปดาห์), **WR 51 %, PF 1.44, IS 1.43 / OOS 1.44, max DD 5.8 %**, +7 %/ปี ที่ risk 0.5 %, 2024–26 PF 1.61 (docs/mtf_bot.md ข้อ 6)
- Inputs หลัก: `NeedVotes` 3, `K` 24, `RR` 3.0, `BreakEvenR` 1.0, `SessionStartHour/EndHour` 15/24, `MinD1StrengthAtr` 0.7, `MinH4BiasAgeBars` 6, `MinH1RsiDir` 51, `MaxHoldHours` 24, `Side` both, `RiskPct` 0.5, `MagicNumber` 747001
- SL/TP แบบจุดคงที่: `StopMode=SL_POINTS` + `SlPoints` (500 = $5), `TpMode=TP_POINTS` + `TpPoints` (1000–1500) — ทดสอบแล้ว SL 500/TP 1500 PF 1.10–1.15, SL 1000/TP 2000 PF 1.20–1.30 (ดีสุด), swing SL default PF 1.18–1.36 (ดู docs/mtf_bot.md ข้อ 5)
