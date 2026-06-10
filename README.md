# IQ Option AI Auto-Trading System
## EMA + RSI + ATR + Volume Strategy

รันบนเครื่องด้วย Python — Dashboard ผ่าน Browser — แจ้งเตือน Telegram

Source repository: https://github.com/PeetNattawat/IQ-Option-AI-Auto-Trading-System.git

---

## 📁 โครงสร้างไฟล์

```
iq_trading/
├── backend/
│   ├── trading_engine.py   ← core: indicators, signals, trade manager, learning
│   ├── main.py             ← launcher + WebSocket server + Telegram integration
│   ├── telegram_bot.py     ← Telegram alert module
│   └── requirements.txt
├── frontend/
│   └── dashboard.html      ← เปิดด้วย browser ได้เลย
├── data/
│   ├── trades.json         ← ประวัติการเทรด (auto-created)
│   └── learning_rules.json ← กฎที่ AI เรียนรู้ (auto-created)
├── logs/
│   └── trading.log         ← log ทั้งหมด (auto-created)
└── .env.example            ← copy เป็น .env แล้วใส่ค่า
```

---

## ⚙️ การติดตั้ง

### 1. ติดตั้ง Python dependencies

```bash
cd backend
pip install -r requirements.txt

# ติดตั้ง iqoptionapi จาก GitHub โดยตรง
pip install git+https://github.com/iqoptionapi/iqoptionapi.git
```

### 2. ตั้งค่า .env

```bash
cp .env.example .env
# แก้ไขไฟล์ .env ใส่ email, password, Telegram token
```

ตัวอย่าง `.env`:
```
IQ_EMAIL=myemail@gmail.com
IQ_PASSWORD=mypassword123
IQ_ACCOUNT=PRACTICE
IQ_ASSETS=EURUSD,GBPUSD,AUDUSD
IQ_TIMEFRAME=300
IQ_AMOUNT=1.0
IQ_CONFIDENCE=70.0
IQ_MAX_LOSSES=3
TG_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
TG_CHAT_ID=-1001234567890
```

### 3. รัน Bot

```bash
cd backend
python main.py
```

### 4. เปิด Dashboard

เปิดไฟล์ `frontend/dashboard.html` ในเบราว์เซอร์ (Chrome / Edge แนะนำ)
หรือดับเบิลคลิกที่ไฟล์ได้เลย — dashboard จะ connect ไปที่ `ws://localhost:8765` อัตโนมัติ

---

## 📊 Signal Scoring System

| เงื่อนไข | คะแนนสูงสุด |
|---|---|
| EMA Stack Alignment (20>50>200) | 25 pts |
| RSI Zone (50-70 for CALL, 30-50 for PUT) | 20 pts |
| ATR Above Average (volatility confirm) | 15 pts |
| MACD Histogram direction | 15 pts |
| Volume above average | 10 pts |
| ADX > 25 (trend strength) | 10 pts |
| Bollinger Band context | 5 pts |
| **Total** | **100 pts** |

**เทรดเมื่อ score ≥ threshold (default 70)**

---

## 🤖 AI Learning Engine

ทุก 30 รอบ bot จะวิเคราะห์ประวัติการเทรดอัตโนมัติ:

- ถ้า CALL ตอน RSI > 75 มี win rate < 40% → สร้าง rule ห้ามเทรด
- ถ้า ADX < 20 (sideways) มี win rate < 45% → สร้าง rule ห้ามเทรด
- บันทึก rule ลง `data/learning_rules.json`
- rules จะถูก apply ในการเทรดรอบถัดไปทันที

---

## 📱 Telegram Commands

Bot จะส่งแจ้งเตือน:
- **Signal alert** — เมื่อ confidence ≥ 80%
- **Trade result** — หลังออเดอร์ปิด (WIN/LOSS)
- **Risk pause** — เมื่อ loss ติดต่อกัน ≥ max
- **Learning update** — เมื่อ AI สร้าง/ปิดการใช้งาน rule

วิธีหา Chat ID:
1. ส่งข้อความหา bot ของคุณ
2. เปิด `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. หา `"chat":{"id":...}` ในผลลัพธ์

---

## ⚠️ Risk Management

- **Max trades/hour**: 6 (ปรับได้ใน TradingConfig)
- **Max consecutive losses**: 3 → หยุดเทรดอัตโนมัติ
- **Min confidence**: 70% (dashboard แสดง HOLD ถ้าต่ำกว่า)
- **Telegram alert**: ส่งเมื่อ confidence ≥ 80% เท่านั้น

---

## 🔧 ปรับค่า Config

แก้ไขได้ที่ `.env` หรือใน `TradingConfig` ใน `trading_engine.py`:

```python
timeframe = 300        # M5 (แนะนำสำหรับ binary)
trade_amount = 1.0     # USD ต่อออเดอร์
confidence_threshold = 70.0  # คะแนนขั้นต่ำก่อนเทรด
max_consecutive_losses = 3   # หยุดหลัง loss ติดต่อกัน
```

---

## 📝 หมายเหตุ

> ระบบนี้ใช้เพื่อการศึกษาและวิจัยเท่านั้น
> Binary options มีความเสี่ยงสูง ควรเริ่มด้วย PRACTICE account เสมอ
> ไม่มีระบบใดการันตีกำไร 100%
