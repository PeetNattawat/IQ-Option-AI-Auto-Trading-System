# Deploy บน Google Cloud (หรือ VPS อื่นๆ)

รันบอทแบบ 24/7 (headless) ดูผลผ่าน Telegram — ไม่ต้องเปิดแดชบอร์ดสู่อินเทอร์เน็ต

## 1. สร้าง VM บน Google Cloud

1. ไปที่ **Compute Engine → VM instances → Create instance**
2. ตั้งค่า:
   - **Region:** `us-central1` (ถ้าอยากให้เป็น Always Free หลังหมด trial) หรือที่ไหนก็ได้ระหว่าง trial
   - **Machine type:**
     - `e2-small` (2GB) — สบายช่วง trial $300
     - `e2-micro` (1GB) — ฟรีตลอดชีพถ้าอยู่ใน us-west1/us-central1/us-east1
   - **Boot disk:** Ubuntu 22.04 LTS (หรือ 24.04), ดิสก์ 30GB
   - **Firewall:** ไม่ต้องเปิด HTTP/HTTPS (บอทรัน headless ไม่เปิดพอร์ตสาธารณะ)
3. กด **Create** → รอ VM พร้อม → กด **SSH** (เปิด terminal ในเบราว์เซอร์)

## 2. ติดตั้งและตั้งค่า (รันใน SSH)

```bash
# clone โค้ด
git clone https://github.com/PeetNattawat/IQ-Option-AI-Auto-Trading-System.git iqoption-ai
cd iqoption-ai

# รันสคริปต์ตั้งค่าอัตโนมัติ (ติดตั้ง Python+deps, swap, timezone, systemd)
bash deploy/setup.sh
```

## 3. ใส่รหัสผ่าน (.env) แล้วสตาร์ท

```bash
cp .env.example .env
nano .env          # ใส่ IQ_EMAIL, IQ_PASSWORD, TG_TOKEN, TG_CHAT_ID (Ctrl+O บันทึก, Ctrl+X ออก)

sudo systemctl start iqbot
journalctl -u iqbot -f      # ดู log สด — เห็น "Tradable assets now: ..." = ทำงานแล้ว
```

> ⚠️ ไฟล์ `.env` ตั้งใจไม่ push ขึ้น GitHub (มีรหัสผ่าน) จึงต้องสร้างใหม่บน VM

## 4. คำสั่งที่ใช้บ่อย

```bash
sudo systemctl status iqbot      # สถานะ (running?)
sudo systemctl restart iqbot     # รีสตาร์ท (หลังแก้ .env หรือ git pull)
sudo systemctl stop iqbot        # หยุด
journalctl -u iqbot -f           # ดู log สด
journalctl -u iqbot -n 100       # ดู log ย้อนหลัง 100 บรรทัด
```

## 5. อัปเดตโค้ดในอนาคต

```bash
cd ~/iqoption-ai
git pull
./venv/bin/pip install -r requirements.txt   # เผื่อมี dependency ใหม่
sudo systemctl restart iqbot
```

## (ทางเลือก) ดูแดชบอร์ดแบบปลอดภัย — SSH tunnel

แดชบอร์ดไม่มีระบบล็อกอิน **ห้ามเปิดพอร์ต 8765 สู่อินเทอร์เน็ต** ถ้าอยากดูจากเครื่องตัวเอง:

```bash
# ใช้ gcloud CLI บนเครื่องคุณ
gcloud compute ssh <VM_NAME> --zone <ZONE> -- -L 8765:localhost:8765
```
แล้วเปิด `dashboard.html` บนเครื่องคุณ — จะต่อผ่าน tunnel ไป VM โดยไม่เปิดพอร์ตสาธารณะ

---
**หมายเหตุความปลอดภัย:** เริ่มจาก `IQ_ACCOUNT=PRACTICE` เสมอ ทดสอบให้นิ่งหลายวันก่อนพิจารณาเงินจริง
