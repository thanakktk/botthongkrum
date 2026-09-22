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
