# Deploy แบบแยกบทบาท: Pi 3B + Cloud ฟรี

Pi 3B (RAM 1GB) รัน stack เต็มไม่ไหว จึงให้ทำหน้าที่เดียวคือ **กล่อง Matter bridge**
ส่วน dashboard + TimescaleDB ขึ้นไปอยู่บน **Oracle Cloud Always Free** และทุกเครื่องคุยกันผ่าน
**Tailscale** — ไม่เปิดพอร์ตออกอินเทอร์เน็ตเลยสักพอร์ต

> **ลองข้อ 2.5 ก่อนสมัคร Oracle** — ให้ Pi รัน dashboard เองแบบไม่เก็บประวัติ แล้ววัด RAM หนึ่งวัน
> ถ้ารับไหว ไม่ต้องมี server บน cloud เลย และได้ local-first กลับมา

```
 บ้าน (LAN)                                   Oracle Cloud — Singapore (Always Free, arm64)
 ─────────────                                ─────────────────────────────────────────────
 Zigbee ─► M1 Hub ──Matter──► Pi 3B            VM  iot-cloud
                              matter-server     ├─ db   TimescaleDB
                              :5580             ├─ app  FastAPI ──ws──► 100.x.x.x:5580 (Pi)
                                 ▲              └─ tailscale serve  https://iot-cloud.<tailnet>.ts.net
                                 │                        ▲
                                 └────── WireGuard (Tailscale) ──────┘
                                                          ▲
                                           มือถือ / โน้ตบุ๊ก (อยู่ใน tailnet เดียวกัน)
```

---

## ต้องรู้ก่อนเริ่ม

**ข้อแลกเปลี่ยนที่ได้มา** — เลือกแบบนี้แล้วเสียของบางอย่างไปจริง ๆ:

- **ขัดกับหลัก Local-first ของเอกสาร (§1)** — ถ้าเน็ตบ้านล่ม dashboard จะสั่งอุปกรณ์ไม่ได้
  (สวิตช์ที่ตัวอุปกรณ์และ Hub ยังทำงานปกติ) หน้าเว็บจะขึ้น "กำลังเชื่อมต่อ matter" จนเน็ตกลับมา
  ช่วงนั้นกราฟจะขาดหาย ซึ่งตรงกับความหมายที่ออกแบบไว้ว่า "เซนเซอร์เงียบ"
- **latency เพิ่ม** — ทุกคำสั่งวิ่ง ไทย → สิงคโปร์ → ไทย ประมาณการ +30–60 ms ต่อรอบ
  (ยังไม่ได้วัดจริง ดูได้จาก `latency_ms` ใน event `confirmed`)
- **"ฟรี" แต่ต้องมีบัตรเครดิต** — Oracle ใช้ยืนยันตัวตน ไม่ตัดเงินถ้าไม่อัปเกรด

**ความปลอดภัย** — แอปยังไม่มีระบบ login ของตัวเอง (เฟส 4) ดังนั้นคู่มือนี้ตั้งให้ dashboard
**เข้าได้เฉพาะเครื่องที่อยู่ใน tailnet ของคุณ** เท่านั้น ไม่มี URL สาธารณะ

### ฟรีจริงไหม — เช็คเงื่อนไข ณ 10 ก.ย. 2026

| บริการ | ได้ฟรีอะไร | เงื่อนไขที่ต้องระวัง |
|---|---|---|
| Oracle Cloud Always Free | Ampere A1 รวม **2 OCPU / 12 GB**, disk รวม 200 GB, ส่งข้อมูลออก 10 TB/เดือน | ต้องมีบัตรเครดิต · home region เลือกครั้งเดียวถาวร · VM ที่ "idle" 7 วันอาจโดนเก็บคืน (ดูข้อ 3) |
| Tailscale Personal | **6 users, อุปกรณ์ไม่จำกัด**, ฟรีถาวร | Serve (ใช้ในบ้าน tailnet) ใช้ได้ · Funnel (เปิด public) ต้องจ่าย — ซึ่งเราไม่ใช้อยู่แล้ว |

stack ของเรากินจริงราว 1 GB บน VM — ใช้โควตาไม่ถึง 10%

---

## 1. Tailscale — ทำก่อนอย่างอื่น

1. สมัครที่ <https://tailscale.com> (login ด้วย Google / GitHub / Microsoft ได้)
2. ติดตั้งแอป Tailscale บนโน้ตบุ๊กและมือถือที่จะใช้เปิด dashboard แล้ว login บัญชีเดียวกัน
3. Admin console → **DNS** → เปิด **MagicDNS** และ **HTTPS Certificates**
   (ข้อ 6 ต้องใช้ ถ้าไม่เปิด `tailscale serve` จะออก HTTPS ไม่ได้)

---

## 2. Pi 3B — กล่อง Matter

### 2.1 OS และ Docker

ลง **Raspberry Pi OS Lite (64-bit)** ด้วย Raspberry Pi Imager — ต้อง 64-bit เพราะ image ของ
`python-matter-server` มีแค่ `amd64` กับ `arm64` (Pi 3B เป็น ARMv8 จึงรองรับ)

```bash
uname -m                                   # ต้องได้ aarch64
sudo apt update && sudo apt full-upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

คืน RAM ให้ระบบ — Pi ตัวนี้ไม่มีจอ ไม่ต้องแบ่งให้ GPU:

```bash
echo "gpu_mem=16" | sudo tee -a /boot/firmware/config.txt
```

เปิด memory cgroup — Raspberry Pi OS ปิดไว้เป็นค่าเริ่มต้น ถ้าไม่เปิด Docker จะข้ามการจำกัด RAM
ของ container ทุกตัว (ขึ้นเตือน *Your kernel does not support memory limit capabilities*):

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
sudo sed -i '1 s/$/ cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1/' /boot/firmware/cmdline.txt
cat /boot/firmware/cmdline.txt        # ต้องยังเป็นบรรทัดเดียว ห้ามขึ้นบรรทัดใหม่
```

แล้วรีบูตทีเดียวให้มีผลทั้งสองอย่าง:

```bash
sudo reboot
grep -w memory /sys/fs/cgroup/cgroup.controllers   # หลังรีบูต ต้องเห็นคำว่า memory
```

### 2.2 Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname iot-pi
tailscale ip -4                            # จด 100.x.x.x ไว้ ใช้ในข้อ 5
```

### 2.3 matter-server

ใช้ไฟล์ [ops/pi/docker-compose.yml](ops/pi/docker-compose.yml) ไฟล์เดียว — ไม่ต้องมี repo ทั้งก้อน ไม่ต้องมี `.env`

```bash
mkdir -p ~/matter && cd ~/matter
curl -fsSLO https://raw.githubusercontent.com/<you>/<repo>/main/ops/pi/docker-compose.yml
#   (หรือ scp ไฟล์นี้ขึ้นมาจากเครื่องที่มี repo)
docker compose up -d
docker stats --no-stream matter-server     # ดู RAM จริง
```

ไฟล์นี้ตั้ง `--primary-interface eth0` (ให้ Matter ใช้ IPv6 link-local บนสาย LAN) และจำกัด log
ไว้ 5 MB × 3 ไฟล์ เพื่อไม่ให้เขียน SD card จนพัง

### 2.4 ปิด :5580 ไว้ให้เฉพาะ Tailscale

matter-server **ไม่มี auth เลย** ใครต่อถึงพอร์ตนี้ = สั่งอุปกรณ์ในบ้านได้ ตอนนี้คนที่ต้องต่อมีแค่ VM
บน cloud (ผ่าน Tailscale) จึงบล็อกพอร์ตนี้จากทางอื่นทั้งหมด:

```bash
sudo apt install -y iptables-persistent       # ถามว่าจะ save rule ตอนนี้ไหม → Yes
for ipt in iptables ip6tables; do
  sudo $ipt -A INPUT -p tcp --dport 5580 -i tailscale0 -j ACCEPT
  sudo $ipt -A INPUT -p tcp --dport 5580 -i lo         -j ACCEPT
  sudo $ipt -A INPUT -p tcp --dport 5580                -j DROP
done
sudo netfilter-persistent save
```

> **อย่าใช้ `ufw enable`** — ค่าเริ่มต้นของ ufw คือ deny ขาเข้าทั้งหมด ซึ่งจะตัด mDNS (UDP 5353)
> และ traffic ของ Matter ที่ Hub ส่งกลับมา ทำให้ commission ไม่ผ่านหรืออุปกรณ์หลุดแบบเงียบ ๆ
> rule ข้างบนแตะแค่ TCP 5580 พอร์ตเดียว

ทดสอบจากโน้ตบุ๊กใน LAN (ปิด Tailscale บนโน้ตบุ๊กก่อน):

```bash
curl -m 3 -s -o /dev/null -w '%{http_code}\n' http://<ip-LAN-ของ-pi>:5580/ws
# ต้องได้ 000 (timeout) = บล็อกแล้ว
```

### 2.5 ทางแยก: ลองรัน dashboard บน Pi ไปเลย (ยังไม่เก็บประวัติ)

ก่อนสมัคร Oracle ให้ลองแบบนี้ก่อน — ถ้า Pi 3B รับไหว **ไม่ต้องมี server บน cloud เลย** และได้
local-first กลับมา (เน็ตบ้านล่มก็ยังคุมอุปกรณ์ในบ้านได้) ผ่านข้อ 2.1–2.4 มาแล้วค่อยทำต่อตรงนี้

**เอา repo ลง Pi** (ต้องใช้ build image ของ dashboard):

```bash
cd ~
git clone <repo-url> iot-control
cd iot-control/ops/pi
```

ถ้าข้อ 2.3 รัน matter-server จาก `~/matter` ไปแล้ว ให้ย้ายมาใช้ที่นี่ที่เดียว — ห้ามรันสองชุดพร้อมกัน
(ชื่อ container ชนกัน) และต้องย้าย `matter-data` มาด้วย ไม่งั้นจะได้ fabric ใหม่ที่ว่างเปล่า:

```bash
(cd ~/matter && docker compose down)
sudo mv ~/matter/matter-data ~/iot-control/ops/pi/
```

**ตั้งค่า** — สร้าง `ops/pi/.env`:

```bash
cat > .env <<'EOF'
# ชื่อ tailnet ของ Pi + IP ในบ้าน — แอปยังไม่มี login ตัวนี้คือสิ่งที่กันเว็บอื่นยิงคำสั่งมา
ALLOWED_HOSTS=iot-pi.<tailnet>.ts.net,192.168.1.50
PUBLIC_ORIGIN=https://iot-pi.<tailnet>.ts.net
# ถ้าไม่อยากให้ทุกเครื่องในวง LAN เปิดได้ ให้เปิดบรรทัดนี้ แล้วเข้าผ่าน tailscale serve อย่างเดียว
# HOST=127.0.0.1
EOF
```

compose จะไม่ยอม start ถ้าไม่มี `ALLOWED_HOSTS` — ตั้งใจให้เป็นแบบนั้น

**Build และเปิด**:

```bash
docker compose -f docker-compose.yml -f docker-compose.dashboard.yml up -d --build
```

build บน Pi ไม่ต้อง compile อะไร (ตรวจแล้วว่าทุก dependency มี wheel สำหรับ aarch64) แต่ SD card ช้า
ครั้งแรกอาจกินเวลาราว 10–20 นาที (ค่าประมาณ) ครั้งต่อไปใช้ cache

dashboard ตัวนี้ถูกจำกัด RAM ไว้ที่ 256 MB — ถ้ามันรั่วเองจะโดน kill แล้ว restart เองตัวเดียว
ไม่ลาก matter-server ลงไปด้วย **ใช้ได้ก็ต่อเมื่อเปิด memory cgroup ในข้อ 2.1 แล้ว** ถ้าเห็น
`! app  Your kernel does not support memory limit capabilities...` แปลว่ายังไม่ได้เปิด — container
ยังรันได้ปกติ แค่ไม่มีเพดาน RAM (ตัว `memwatch.sh` วัดจาก `/proc` โดยตรง จึงยังวัดได้ถูกต้อง)

**เปิดใช้**:

- ในบ้าน: `http://192.168.1.50:8000`
- จากที่ไหนก็ได้: `sudo tailscale serve --bg --https=443 localhost:8000` → `https://iot-pi.<tailnet>.ts.net`

แถบบนสุดควรขึ้น **"ทำงานปกติ · ไม่บันทึกประวัติ"** — ถูกต้องสำหรับโหมดนี้
ถ้าขึ้น "กำลังเชื่อมต่อ matter" ค้างอยู่ แปลว่า dashboard ต่อ matter-server ไม่ถึง ให้ดู `docker logs matter-server`

**จับคู่ Hub** — ใช้ `make commission` ไม่ได้ในโหมดนี้ (มันชี้ไปที่ compose ชุดหลัก) สั่งตรงเข้า container แทน:

```bash
docker exec -it iot-app python scripts/commission.py MT:Y.K90AFN00KA0648G00
docker exec -it iot-app python scripts/dump_nodes.py
```

**วัดว่า Pi รับไหวไหม** — ต้องวัด *หลัง* commission แล้ว เพราะ matter-server จะกิน RAM เพิ่มเมื่อ
มี subscription ของอุปกรณ์จริง ปล่อยให้วัดไปหนึ่งวัน และเปิด dashboard ใช้งานตามปกติระหว่างนั้น:

```bash
cd ~/iot-control/ops/pi
nohup bash memwatch.sh > memwatch.log 2>&1 &     # เก็บทุก 60 วิ นาน 24 ชม.
tail -f memwatch.log                              # ดูสด (Ctrl+C ออกจาก tail ได้ ไม่หยุดการวัด)
```

ครบแล้วสรุปจะอยู่ท้าย `memwatch.log` (หรือสั่ง `bash memwatch.sh --report memwatch-*.csv` ทีหลังก็ได้)
[memwatch.sh](ops/pi/memwatch.sh) ตัดสินจาก MemAvailable ที่ต่ำที่สุดที่เจอ และนับ OOM kill ของ kernel ด้วย:

| ผลที่ได้ | ความหมาย | ต่อไป |
|---|---|---|
| **COMFORTABLE** — ต่ำสุด ≥ 200 MB, ไม่มี OOM | Pi รับไหวสบาย | บอกผม — จะทำโหมดเก็บประวัติลง Supabase ฟรีให้ (ไม่ต้องมี VM) |
| **TIGHT** — 100–200 MB | ใช้ได้แต่เหลือที่น้อย | ใช้แบบไม่มีประวัติต่อไปก่อน หรือย้ายไป cloud |
| **NOT VIABLE** — ต่ำกว่า 100 MB หรือมี OOM | Pi ไม่พอ | `docker rm -f iot-app` ให้เหลือแค่ matter-server แล้วไปข้อ 3 |

ส่ง `memwatch.log` มาให้ดูได้เลยไม่ว่าผลจะออกมาแบบไหน

ทั้งสอง container ตั้ง `restart: unless-stopped` ไว้ — รีบูต Pi แล้วกลับมาเองโดยไม่ต้องมี systemd unit

---

## 3. Oracle Cloud — สมัครและสร้าง VM

### 3.1 สมัคร

สมัครที่ <https://www.oracle.com/cloud/free/>

- **Home region เลือก Singapore (`ap-singapore-1`)** — ใกล้ไทยที่สุดที่เปิดให้ใช้ทั่วไป
  **เลือกแล้วเปลี่ยนไม่ได้** และ Always Free compute สร้างได้เฉพาะใน home region เท่านั้น
- ต้องใช้เบอร์มือถือ + บัตรเครดิตยืนยันตัวตน — ไม่ถูกตัดเงินจนกว่าจะอัปเกรด

### 3.2 เรื่อง VM ถูกเก็บคืน — ต้องตัดสินใจตรงนี้

Oracle ถือว่า VM บัญชี Always Free "idle" ถ้าตลอด 7 วัน **CPU (p95), network และ memory
ต่ำกว่า 20% ทั้งสามอย่าง** แล้วอาจเก็บคืน — stack นี้กินน้อยกว่านั้นทุกตัวแน่นอน

| ทางเลือก | ผล |
|---|---|
| **อัปเกรดเป็น Pay As You Go** (แนะนำ) | ไม่โดนเก็บคืนเพราะ idle · ยัง **ไม่เสียเงิน** ตราบที่ใช้อยู่ในโควตา Always Free — แต่ถ้าเผลอสร้างของที่ไม่ฟรีจะถูกเก็บเงินจริง |
| อยู่ Always Free ต่อ | ไม่มีทางถูกเก็บเงิน · แต่ต้องทำใจว่า VM อาจหายได้ และต้องมี backup นอก VM (ข้อ 9) |

ถ้าอัปเกรด ให้ตั้ง **Budget alert** ทันที: Billing & Cost Management → Budgets →
สร้างงบ $1 แจ้งเตือนที่ 100% — มีอะไรเริ่มคิดเงินเมื่อไหร่จะรู้ตัวภายในวันนั้น
และเวลาสร้าง resource ใด ๆ ให้ดูว่ามีป้าย **Always Free-eligible** กำกับทุกครั้ง

### 3.3 สร้าง VM

Compute → Instances → **Create instance**

| ช่อง | ค่า |
|---|---|
| Image | Canonical **Ubuntu 24.04** (เวอร์ชัน aarch64) |
| Shape | Ampere → **VM.Standard.A1.Flex** → **1 OCPU, 6 GB** (ต้องมีป้าย Always Free-eligible) |
| Boot volume | ค่าเริ่มต้น (~47–50 GB) — รวมทุก volume ต้องไม่เกิน 200 GB |
| SSH key | อัปโหลด public key ของคุณ |
| Networking | ค่าเริ่มต้น — **ไม่ต้องเปิดพอร์ตใน Security List** Tailscale ใช้แค่ขาออก |

1 OCPU / 6 GB เหลือเผื่ออีกครึ่งโควตาไว้ใช้ทำอย่างอื่น stack ของเราใช้ไม่ถึง 1 GB

> ถ้าเจอ **"Out of capacity"** — เป็นเรื่องปกติของ A1 ใน region ยอดนิยม ลองเปลี่ยน
> Availability Domain หรือกดใหม่ช่วงเวลาอื่น (ดึก ๆ มักได้)

---

## 4. เตรียม VM

```bash
ssh ubuntu@<public-ip-ของ-vm>

sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git make
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname iot-cloud
tailscale ping iot-pi                      # ต้องได้ pong กลับมา
```

image Ubuntu ของ Oracle มี iptables ที่บล็อกขาเข้าทุกพอร์ตยกเว้น SSH มาให้แล้ว — **ปล่อยไว้แบบนั้น**
เราไม่ต้องเปิดอะไรเพิ่ม

---

## 5. Deploy dashboard + TimescaleDB

```bash
cd ~
git clone <repo-url> iot-control
cd iot-control
make init
```

แก้ `.env`:

```ini
IOT_ADAPTER=matter

# tailscale IP ของ Pi จากข้อ 2.2 — ใช้ตัวเลข IP ไม่ใช่ชื่อ iot-pi
# (ชื่อ MagicDNS อาจ resolve ไม่ได้จากใน container)
MATTER_WS_URL=ws://100.x.x.x:5580/ws

# เว้นว่าง: VM นี้ไม่รัน matter-server
COMPOSE_PROFILES=

# เปิด dashboard เฉพาะ loopback — ออกไปข้างนอกผ่าน tailscale serve เท่านั้น
APP_BIND=127.0.0.1

PUBLIC_ORIGIN=https://iot-cloud.<tailnet>.ts.net
ALLOWED_HOSTS=iot-cloud.<tailnet>.ts.net
```

ชื่อ `<tailnet>` ดูได้ที่ Admin console → DNS (หน้าตาแบบ `tail1a2b3c.ts.net`)
ไม่ต้องใส่ `127.0.0.1` ใน `ALLOWED_HOSTS` — แอปเติมให้เองเพื่อให้ healthcheck ผ่าน

```bash
make prod-up
```

ขึ้นตามลำดับ `db (healthy) → migrate (จบงาน) → app` ครั้งแรกใช้เวลา build ไม่กี่นาที (A1 เร็วกว่า Pi มาก)

ตรวจว่า container ต่อถึง Pi ผ่าน Tailscale ได้จริง:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec app curl -m 5 -s -o /dev/null -w '%{http_code}\n' http://100.x.x.x:5580/ws
# ได้เลข HTTP กลับมา (เช่น 400) = ต่อถึง   ได้ 000 = ต่อไม่ถึง → ดูข้อ 11
make health
```

---

## 6. เปิดใช้ผ่าน HTTPS ใน tailnet

```bash
sudo tailscale serve --bg --https=443 localhost:8000
tailscale serve status
```

เปิด `https://iot-cloud.<tailnet>.ts.net` จากมือถือ/โน้ตบุ๊กที่ต่อ Tailscale อยู่ — ได้ HTTPS
พร้อม cert จริงโดยไม่ต้องมีโดเมน และเครื่องที่ไม่อยู่ใน tailnet เปิดไม่ได้เลย

ดูที่แถบบนของหน้าเว็บ: จุดสีเขียว + "ทำงานปกติ" แปลว่า WebSocket วิ่งผ่าน serve ได้แล้ว
การตั้งค่า serve ถูกเก็บไว้ใน tailscaled จึงอยู่รอดหลังรีบูต

---

## 7. จับคู่ Hub M1

Tuya app → เลือก M1 → เมนู Matter / การควบคุมจากบุคคลที่สาม → สร้าง pairing code
(multi-admin — M1 ยังอยู่ใน Tuya app ต่อได้)

รันจาก VM ได้เลย — คำสั่งวิ่งผ่าน Tailscale ไปที่ matter-server บน Pi ซึ่งเป็นตัวค้นหา Hub ใน LAN เอง:

```bash
make commission code=MT:Y.K90AFN00KA0648G00
make matter-nodes                          # ดู endpoint/cluster ที่ bridge ปล่อยออกมา
```

> `POST /api/devices/commission` ถูกปิดไว้เป็นค่าเริ่มต้น (`ALLOW_HTTP_COMMISSION=0`)
> เพราะหลัง reverse proxy ทุก request ดูเหมือนมาจาก IP ภายในหมด การเช็ค "LAN only"
> จึงพิสูจน์อะไรไม่ได้ — การ commission ต้องทำผ่าน shell บนเครื่องเท่านั้น

---

## 8. ให้ขึ้นเองหลังรีบูต

**Pi** — Docker, Tailscale และ `restart: unless-stopped` ขึ้นเองอยู่แล้ว ไม่ต้องทำอะไรเพิ่ม

**VM** — Oracle รีบูต VM ตอนบำรุงรักษาเป็นครั้งคราว ใช้ unit เดียวกับ Pi:

```bash
sudo cp ops/iot-control.service /etc/systemd/system/
sudo sed -i "s|/home/user/iot-control|$HOME/iot-control|" /etc/systemd/system/iot-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now iot-control
```

unit อ่าน `COMPOSE_PROFILES` จาก `.env` — บน VM ว่างไว้ จึงไม่มี matter-server โผล่มา

ทดสอบจริงทั้งสองเครื่อง: `sudo reboot` → รอ 2 นาที → เปิด dashboard ต้องกลับมาเขียวเอง

---

## 9. Backup

ของที่หายแล้วเจ็บมีสองอย่าง อยู่คนละเครื่อง

**ฐานข้อมูล (บน VM)**

```bash
make backup                                # ได้ ops/backups/iot-<เวลา>.dump
crontab -e
#   30 3 * * *  cd $HOME/iot-control && make backup >> $HOME/iot-backup.log 2>&1
```

`make backup` ใช้ `pg_dump` ของ container ฐานข้อมูลเอง (เวอร์ชันตรงกับ server เสมอ)
และ dump ทั้งฐานในไฟล์เดียว — ถ้า dump ล้มกลางทางจะเหลือแค่ไฟล์ `.partial` ไม่มีทางเห็นไฟล์พังหน้าตาเหมือน backup ดี

**เอาไฟล์ออกจาก VM ด้วย** — ถ้า VM โดนเก็บคืน backup ที่อยู่บน VM ก็หายไปพร้อมกัน
จากโน้ตบุ๊ก (ต่อ Tailscale อยู่):

```bash
scp "ubuntu@iot-cloud:iot-control/ops/backups/iot-*.dump" ~/iot-backups/
```

**กุญแจ fabric ของ Matter (บน Pi)** — **หายแล้วต้อง commission อุปกรณ์ใหม่ทั้งหมด**

```bash
cd ~/matter && sudo tar czf matter-data-$(date +%F).tar.gz matter-data
# แล้ว scp ออกไปเก็บที่อื่น — อย่าเก็บไว้บน SD card ใบเดียวกัน
```

**กู้คืนฐานข้อมูล** — ต้องลงบนฐานที่ว่างเท่านั้น (สคริปต์จะไม่ยอมทับของเดิม):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
docker volume rm iot-control_timescale-data            # ⚠️ ลบข้อมูลเดิมทิ้ง
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait db   # รอจน healthy
make restore file=ops/backups/iot-<เวลา>.dump
make prod-up
```

`make restore` เรียก `timescaledb_pre_restore()` / `timescaledb_post_restore()` ครอบ `pg_restore`
ตามที่ TimescaleDB กำหนด และในไฟล์ dump มี `schema_migrations` ติดมาด้วย ขั้น migrate ของ
`prod-up` จึงเห็นว่าไม่มีอะไรค้าง

---

## 10. เช็กลิสต์ก่อนถือว่าเสร็จ

- [ ] Pi: `uname -m` = `aarch64`, `docker stats` ของ matter-server ไม่เกิน ~400 MB
- [ ] Pi: `curl` ไป `:5580` จาก LAN ได้ `000` (บล็อกแล้ว)
- [ ] VM: สร้างด้วย shape ที่มีป้าย **Always Free-eligible**
- [ ] VM: ถ้าอัป PAYG แล้ว ตั้ง Budget alert แล้ว
- [ ] VM: `curl` จากใน container app ไป `100.x.x.x:5580` ได้เลข HTTP กลับมา
- [ ] `make health` = `ok` และ `make migrate-status` ไม่มี pending / DRIFT
- [ ] เปิด `https://iot-cloud.<tailnet>.ts.net` ได้ และ **เปิดไม่ได้** จากเครื่องที่ไม่อยู่ใน tailnet
- [ ] commission แล้ว `make matter-nodes` เห็นอุปกรณ์ครบ
- [ ] รีบูตทั้ง Pi และ VM แล้ว dashboard กลับมาเอง
- [ ] มี backup ทั้ง DB และ `matter-data` **อยู่นอก** VM/Pi และลอง restore แล้ว

---

## 11. เจอปัญหาแบบนี้

| อาการ | สาเหตุที่พบบ่อย | ทางแก้ |
|---|---|---|
| หน้าเว็บค้าง "กำลังเชื่อมต่อ matter" | app ต่อ Pi ไม่ถึง | ใช้ IP `100.x` ไม่ใช่ชื่อ MagicDNS ใน `MATTER_WS_URL` · `tailscale status` ทั้งสองเครื่อง · rule iptables ข้อ 2.4 ต้องมี `-i tailscale0 ACCEPT` ก่อน `DROP` |
| `tailscale serve` ฟ้องเรื่อง certificate | ยังไม่เปิด HTTPS ใน tailnet | Admin console → DNS → เปิด HTTPS Certificates (ข้อ 1) |
| เปิดหน้าเว็บได้ แต่จุดบนสุดเป็นสีแดง | WebSocket ไม่ผ่าน | `tailscale serve status` ต้องชี้ไป `localhost:8000` · ลองเปิดจากเครื่องอื่นใน tailnet |
| `400 Bad Request` ทุกหน้า | ชื่อโดเมนไม่อยู่ใน `ALLOWED_HOSTS` | ใส่ชื่อ `iot-cloud.<tailnet>.ts.net` ให้ตรงตัวอักษร แล้ว `make prod-up` ใหม่ |
| สร้าง VM ไม่ได้ "Out of capacity" | A1 ใน Singapore เต็ม | เปลี่ยน Availability Domain หรือลองใหม่ช่วงอื่น |
| VM หายไปทั้งเครื่อง | โดนเก็บคืนเพราะ idle (บัญชี Always Free) | สร้าง VM ใหม่ → ข้อ 4–6 → restore จาก backup นอก VM · พิจารณาอัป PAYG (ข้อ 3.2) |
| commission ไม่เจอ Hub | Pi กับ M1 คนละ VLAN / IPv6 ปิด / เผลอเปิด ufw | ต้องวงเดียวกัน · `cat /proc/sys/net/ipv6/conf/all/disable_ipv6` ต้องเป็น 0 · `sudo ufw status` ต้อง inactive |
| matter-server บน Pi โดน OOM kill | RAM 1GB ไม่พอ | ข้อ 2.1 `gpu_mem=16` · อย่ารันอะไรอื่นบน Pi ตัวนี้ |

ถ้าวันหนึ่งเปลี่ยนเป็น Pi 4/5 และอยากย้ายทุกอย่างกลับมาในบ้าน (local-first เต็มตัว)
ดู [DEPLOY-PI.md](DEPLOY-PI.md) — restore จาก backup ที่มีอยู่ได้เลย

---

## หมายเหตุ

**ยังไม่ได้ทดสอบบนเครื่องจริง** ทั้ง Pi, Oracle VM และ Tailscale — เครื่องที่พัฒนาอยู่เป็น Windows
ที่ไม่มี `make` และ Docker อยู่โหมด Windows containers

ที่ตรวจแล้ว: image ทั้งหมดมี `arm64` (query registry) · compose ทั้งสามแบบ resolve ถูก
(cloud = `app db migrate` ผูก `127.0.0.1`, Pi all-in-one = + `matter-server`, ไฟล์ Pi เดี่ยว validate ผ่าน
โดยไม่มี `.env`) · การปิด HTTP commission และ host filtering ทดสอบกับแอปที่รันจริงแล้ว ·
`backup.sh` / `restore.sh` ทดสอบกับ stub แทน Postgres แล้ว (ลำดับ hook, การปฏิเสธฐานที่ไม่ว่าง,
dump ที่ล้มไม่ทิ้งไฟล์ที่ดูเหมือน backup ดี) แต่ **ยังไม่เคย dump/restore กับ TimescaleDB จริง**

เงื่อนไข free tier ตรวจจากหน้าเว็บทางการ ณ 10 ก.ย. 2026 — เปลี่ยนได้ ควรเช็คอีกครั้งตอนสมัคร

แหล่งข้อมูล:
[Oracle — Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) ·
[Oracle — Free Tier](https://docs.oracle.com/iaas/Content/FreeTier/freetier.htm) ·
[Oracle — Singapore region](https://www.oracle.com/asean/cloud/cloud-regions/singapore/) ·
[Tailscale — Pricing](https://tailscale.com/pricing) ·
[Tailscale — Serve](https://tailscale.com/kb/1242/tailscale-serve) ·
[python-matter-server — CLI arguments](https://github.com/home-assistant-libs/python-matter-server/blob/main/matter_server/server/__main__.py) ·
[Oracle idle reclamation & PAYG (51sec)](https://blog.51sec.org/2023/02/oracle-cloud-cleaning-up-idle-compute.html)
