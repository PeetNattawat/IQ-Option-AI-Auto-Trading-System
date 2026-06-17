# คำสั่งใช้งานบน VM (SSH Operations Cheatsheet)

คำสั่งที่ใช้บ่อยสำหรับดูแล/วินิจฉัยบอทบน Google Cloud VM
บอทรันเป็น systemd service ชื่อ **`iqbot`** — log อยู่ใน **journald** (ไม่ใช่ไฟล์ `logs/trading.log`)

> เข้า VM: ปุ่ม **SSH** ใน Compute Engine หรือ `gcloud compute ssh <VM_NAME> --zone <ZONE>`

---

## 1. สถานะบอท

```bash
sudo systemctl status iqbot        # running อยู่ไหม + เริ่มเมื่อไหร่ + กิน RAM เท่าไหร่
sudo systemctl is-active iqbot     # ตอบสั้นๆ: active / inactive / failed
uptime                             # โหลดเครื่อง + เปิดมานานเท่าไหร่
```

## 2. ดู log

```bash
journalctl -u iqbot -f             # log สด (ออกด้วย Ctrl+C)
journalctl -u iqbot -n 200         # ย้อนหลัง 200 บรรทัดล่าสุด
journalctl -u iqbot --no-pager     # ทั้งหมด (ไม่ต้องกด space ทีละหน้า)

# เฉพาะช่วงเวลา (ปรับวันที่ตามต้องการ)
journalctl -u iqbot --since "2026-06-16 00:00" --until "2026-06-17 00:00" --no-pager
journalctl -u iqbot --since "today"            # ตั้งแต่เที่ยงคืนวันนี้
journalctl -u iqbot --since "1 hour ago"       # ชั่วโมงที่ผ่านมา
```

## 3. วินิจฉัยปัญหา (กรอง log เฉพาะเรื่อง)

```bash
# ── ออกออเดอร์น้อย: ดูว่าโดนบล็อกด้วยกฎ risk ตัวไหน ──
journalctl -u iqbot --since "2026-06-16" | grep -E "RISK|blocked|open positions|cooling|daily"

# ── ดูว่าบอทเจอสัญญาณไหม / รอสัญญาณอยู่เฉยๆ ──
journalctl -u iqbot --since "2026-06-16" | grep -E "เข้าเงื่อนไข|รอสัญญาณ|ยังไม่มีคู่|ออกออเดอร์"

# ── ผลการเทรด (ปิดไม้ WIN/LOSS) ──
journalctl -u iqbot --since "2026-06-16" | grep "RESULT"

# ── การแจ้งเตือน Telegram (ส่งสำเร็จไหม) ──
journalctl -u iqbot --since "2026-06-16" | grep -E "\[TG\]|alert|Send error"

# ── การเชื่อมต่อ IQ หลุดไหม + หลุดกี่ครั้ง ──
journalctl -u iqbot --since "2026-06-16" | grep -E "Connection|reconnect|หลุด"
journalctl -u iqbot --since "2026-06-16" | grep -c "Connection lost"

# ── ออเดอร์ค้าง / ถูกบังคับปิด (หลังอัปเดตโค้ด deadlock-breaker) ──
journalctl -u iqbot --since "2026-06-16" | grep -E "EXPIRED|stale"

# ── error/exception ทั้งหมด ──
journalctl -u iqbot -p warning --since "2026-06-16" --no-pager
```

## 4. อัปเดตโค้ด แล้ว restart  ← ใช้หลังผมแก้โค้ดทุกครั้ง

```bash
cd ~/iqoption-ai
git pull
./venv/bin/pip install -r requirements.txt   # เผื่อมี dependency ใหม่ (ปกติข้ามได้)
sudo systemctl restart iqbot
journalctl -u iqbot -f                        # ดูว่าบูตขึ้นปกติ — เห็น "Tradable assets now: ..." = โอเค
```

## 5. start / stop / restart

```bash
sudo systemctl start iqbot
sudo systemctl stop iqbot
sudo systemctl restart iqbot
```

> สั่งจาก Telegram ก็ได้: `/start` `/stop` `/restart` `/status` `/summary` `/dashboard`

## 6. ดูข้อมูลบอทบน VM

```bash
cd ~/iqoption-ai
cat data/config.json                                   # ค่าตั้งความเสี่ยง/กลยุทธ์ปัจจุบัน
python3 -c "import json;d=json.load(open('data/trades.json'));\
from collections import Counter;print('ทั้งหมด',len(d),Counter(t.get('status') for t in d))"   # นับไม้ตามสถานะ
python3 -c "import json;d=json.load(open('data/trades.json'));\
[print(t['open_time'],t['asset'],t['direction'],t['status'],t.get('result')) for t in d if t['status']=='open']"  # ไม้ที่ยังค้าง
```

## 6.1 แก้ค่ากลยุทธ์/ความเสี่ยง (config) บน VM

> ⚠️ `data/config.json` ถูก gitignore — **`git pull` จะไม่แก้ให้** ต้องแก้บน VM โดยตรงแล้ว restart

```bash
cd ~/iqoption-ai

# ตัวอย่าง: ลด adx_min เป็น 25 (แก้ทีละค่าได้ ปลอดภัยกว่า nano)
python3 -c "import json,io;p='data/config.json';d=json.load(open(p));d['adx_min']=25.0;json.dump(d,open(p,'w'),indent=2);print('saved adx_min=',d['adx_min'])"

sudo systemctl restart iqbot
journalctl -u iqbot -f          # ยืนยันบูตขึ้นปกติ
```

คีย์ config ที่ปรับบ่อย: `adx_min` `dir_margin` `confidence_threshold` `rsi_call_min/max` `rsi_put_min/max`
`max_open_positions` `max_trades_per_day` `daily_loss_limit` `trade_amount` `expiry_minutes`

## 7. (ทางเลือก) ดู dashboard ผ่าน SSH tunnel — ไม่เปิดพอร์ตสาธารณะ

```bash
# รันบนเครื่องตัวเอง (ต้องมี gcloud CLI)
gcloud compute ssh <VM_NAME> --zone <ZONE> -- -L 8765:localhost:8765
# แล้วเปิด dashboard.html บนเครื่องตัวเอง — จะต่อผ่าน tunnel ไป VM
```

---

## เช็กลิสต์เร็วๆ เวลาบอทมีปัญหา

| อาการ | คำสั่งเช็คก่อน |
|-------|----------------|
| ออกออเดอร์น้อย/ไม่ออก | `... \| grep -E "RISK\|blocked\|open positions"` + ดู `data/trades.json` ว่ามีไม้ค้างไหม |
| ไม่แจ้งผลใน Telegram | `... \| grep "RESULT"` (ปิดผลไหม) เทียบกับ `... \| grep "\[TG\]"` (ส่งไหม) |
| บอทเงียบไปเลย | `systemctl status iqbot` + `... \| grep -E "Connection\|Traceback"` |
| สงสัยบอทตาย | `systemctl is-active iqbot` — ถ้าไม่ active ให้ `restart` แล้วดู log สด |
