# Deploy ลง Raspberry Pi

คู่มือนี้พา stack ทั้งชุด (dashboard + TimescaleDB + python-matter-server) ขึ้นบน Raspberry Pi
ให้ทำงานเองได้หลังไฟดับ

> **ใช้ Pi 3B / RAM 1GB?** stack เต็มไม่พอ (OOM แน่) — ใช้ **[DEPLOY-CLOUD.md](DEPLOY-CLOUD.md)**
> แทน: Pi รันแค่ matter-server ส่วน dashboard + DB ไปอยู่บน Oracle Cloud ฟรีผ่าน Tailscale

**ทำไมต้อง Pi ไม่ใช่เครื่อง Windows** — `python-matter-server` ต้องอยู่ใน host network namespace
พร้อม IPv6 + mDNS ที่มองเห็น LAN จริง ๆ ซึ่ง Docker Desktop บน Windows/macOS ทำไม่ได้
(อยู่หลัง NAT ของ VM) Pi จึงเป็นเครื่องที่ **ต้อง** อยู่วง L2 เดียวกับ Hub M1

---

## 0. ของที่ต้องมี

| | ข้อกำหนด | เหตุผล |
|---|---|---|
| บอร์ด | Pi 4 (4GB+) หรือ Pi 5 | TimescaleDB + Python 2 ตัว กิน RAM เกิน 2GB ตอน build |
| OS | **Raspberry Pi OS 64-bit** (Bookworm) | ตรวจแล้ว: `python-matter-server` มีแค่ `linux/amd64` กับ `linux/arm64` — **32-bit (armhf) รันไม่ได้** ส่วน `timescale/timescaledb:2.17.2-pg16` มี arm64 ให้ |
| ดิสก์ | **USB SSD** (แนะนำมาก) | ฐานข้อมูลเขียนตลอดเวลา SD card จะพังใน ~6–12 เดือน |
| เน็ต | สาย LAN, วง/VLAN เดียวกับ M1 | Matter ใช้ mDNS + IPv6 link-local — ข้าม subnet แล้วหากันไม่เจอ |

ตรวจว่าเป็น 64-bit จริง:

```bash
uname -m          # ต้องได้ aarch64  (ถ้าได้ armv7l = 32-bit ใช้ไม่ได้ ต้องลง OS ใหม่)
```

---

## 1. เตรียมเครื่อง

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git make curl
sudo timedatectl set-timezone Asia/Bangkok
timedatectl                     # ต้องเห็น "System clock synchronized: yes"
```

เวลาต้องตรง เพราะ timestamp ของ telemetry มาจากนาฬิกาเครื่องนี้ — Pi ไม่มี RTC
ถ้า NTP ไม่ทำงาน กราฟจะเพี้ยนทุกครั้งที่รีบูต

ตรวจว่า IPv6 เปิดอยู่ (Matter ใช้):

```bash
ip -6 addr show scope link      # ต้องเห็น inet6 fe80::... บน eth0
cat /proc/sys/net/ipv6/conf/all/disable_ipv6    # ต้องเป็น 0
```

ตั้ง IP ให้นิ่ง — จะจอง DHCP ที่เราเตอร์ (แนะนำ) หรือ fix ที่เครื่องก็ได้:

```bash
sudo nmcli con mod "Wired connection 1" ipv4.method manual \
  ipv4.addresses 192.168.1.50/24 ipv4.gateway 192.168.1.1 ipv4.dns "1.1.1.1,8.8.8.8"
sudo nmcli con up "Wired connection 1"
```

---

## 2. ติดตั้ง Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# ออกแล้ว login ใหม่ (หรือ newgrp docker) ให้ group มีผล
docker version --format '{{.Server.Arch}}'     # ต้องได้ arm64
docker compose version
```

> อย่าใช้ `apt install docker.io` — เวอร์ชันเก่าและมักไม่มี `docker compose` v2 (plugin)

### เปิด memory cgroup

`docker-compose.prod.yml` จำกัด CPU/RAM ของทุก service ไว้ แต่ถ้าไม่เปิด memory cgroup
Docker จะข้ามเพดานเหล่านั้นไปเงียบ ๆ

เปิด memory cgroup — Raspberry Pi OS ปิดไว้เป็นค่าเริ่มต้น ถ้าไม่เปิด Docker จะข้ามการจำกัด RAM
ของ container ทุกตัว (ขึ้นเตือน *Your kernel does not support memory limit capabilities*):

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
sudo sed -i '1 s/$/ cgroup_enable=cpuset cgroup_enable=memory cgroup_memory=1/' /boot/firmware/cmdline.txt
cat /boot/firmware/cmdline.txt        # ต้องยังเป็นบรรทัดเดียว ห้ามขึ้นบรรทัดใหม่
```

```bash
sudo reboot
grep -w memory /sys/fs/cgroup/cgroup.controllers   # ต้องเห็นคำว่า memory
```

### ถ้าใช้ SSD: ย้ายที่เก็บข้อมูลของ Docker ไป SSD

volume ของฐานข้อมูลอยู่ใต้ `/var/lib/docker` ถ้ายังอยู่บน SD card จะไม่ได้ประโยชน์จาก SSD เลย

```bash
sudo systemctl stop docker
sudo mkdir -p /mnt/ssd/docker
echo '{ "data-root": "/mnt/ssd/docker" }' | sudo tee /etc/docker/daemon.json
sudo rsync -aP /var/lib/docker/ /mnt/ssd/docker/
sudo systemctl start docker
docker info | grep "Docker Root Dir"      # ต้องชี้ไป /mnt/ssd/docker
```

---

## 3. เอาโค้ดลงเครื่องและตั้งค่า

```bash
cd ~
git clone <repo-url> iot-control
cd iot-control
make init
```

`make init` จะสุ่ม `JWT_SECRET` และ `POSTGRES_PASSWORD` ให้ (พร้อมแก้ `DATABASE_URL` ให้ตรงกัน)
แล้ว `chmod 600` — และ **จะไม่เขียนทับ `.env` เดิมถ้ามีอยู่แล้ว**

แก้ `.env` ต่ออีก 5 บรรทัด:

```ini
IOT_ADAPTER=matter

# เปิด matter-server ในเครื่องนี้ด้วย (ค่าเริ่มต้นปิดไว้ สำหรับแบบ cloud)
COMPOSE_PROFILES=matter

# app อยู่ใน container ส่วน matter-server อยู่ host network → ต้องผ่าน host gateway
MATTER_WS_URL=ws://host.docker.internal:5580/ws

PUBLIC_ORIGIN=https://iot.yourdomain.com
ALLOWED_HOSTS=iot.yourdomain.com,192.168.1.50,localhost
```

> `DATABASE_URL` ใน `.env` ชี้ `127.0.0.1` ไว้สำหรับเครื่องมือฝั่ง host (`make migrate`, `backup.sh`)
> ส่วนใน container `docker-compose.yml` จะเปลี่ยนให้เป็น `@db:5432` เองอัตโนมัติ ไม่ต้องแก้

---

## 4. Build และเปิด stack

```bash
make prod-up
```

ใช้ `prod-up` ไม่ใช่ `up` — เพราะ `make up` จะโหลด `docker-compose.override.yml` ซึ่งเป็นโหมด dev
(mount source, hot reload, `IOT_ADAPTER=mock`) ส่วน `prod-up` บังคับ overlay `docker-compose.prod.yml`:
read-only rootfs, จำกัด CPU/RAM, หมุน log

build ครั้งแรกบน Pi ใช้เวลา ~5–15 นาที (`asyncpg`/`aiohttp` มี wheel สำหรับ aarch64 อยู่แล้ว
จึงไม่ต้อง compile) ลำดับการขึ้นคือ `db (healthy) → migrate (จบงาน) → app`

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
make health
make migrate-status
```

`make health` ตอนนี้ควรได้ `"status": "degraded"` เพราะยังไม่ได้ commission hub — ปกติ

---

## 5. จับคู่ Hub M1 เข้า fabric

เปิด Tuya app → เลือก M1 → เมนู Matter / การควบคุมจากบุคคลที่สาม → สร้าง **pairing code 11 หลัก**
(เป็น multi-admin — M1 ยังอยู่ใน Tuya app ต่อได้)

รันจากใน container (บน Pi ไม่ได้ติดตั้ง venv ฝั่ง host):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec app python scripts/commission.py MT:Y.K90AFN00KA0648G00
```

ถ้าค้างนาน ให้ดู log ของ matter-server: `docker logs -f iot-matter-server`

ดูว่า bridge ปล่อยอะไรออกมาบ้าง:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  exec app python scripts/dump_nodes.py
```

จะเห็น endpoint/cluster ดิบ ๆ ก่อนผ่าน mapping ของเรา — ใช้จุดนี้ไล่เวลาอุปกรณ์ไม่โผล่หรือ
capability ผิด แล้วค่อยไปแก้ `ATTR_TO_CAP` / `DEVICE_TYPE_KIND` ใน `app/adapters/matter_adapter.py`

เสร็จแล้ว `make health` ควรเป็น `"status": "ok"` และเปิด `http://192.168.1.50:8000` เห็นอุปกรณ์

> compose เปิดพอร์ต `8000` ไว้ทุก interface เพื่อให้เข้าจาก LAN ได้
> ถ้าจะให้เข้าผ่าน Cloudflare Tunnel อย่างเดียว แก้ `ports` ของ service `app` เป็น
> `"127.0.0.1:${PORT:-8000}:8000"`

---

## 6. ให้ขึ้นเองหลังไฟดับ

Pi ไม่มี BIOS — บอร์ดจะบูตเองทันทีที่มีไฟอยู่แล้ว (Pi 5 เช็คได้ด้วย `rpi-eeprom-config`
ว่า `POWER_OFF_ON_HALT=0`) เหลือแค่ปลุก compose stack:

```bash
sudo cp ops/iot-control.service /etc/systemd/system/
sudo sed -i "s|/home/user/iot-control|$HOME/iot-control|" /etc/systemd/system/iot-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now iot-control
systemctl status iot-control
```

unit นี้เรียก `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d`
(prod overlay เหมือนกัน) ส่วน container ทุกตัวตั้ง `restart: unless-stopped` ไว้แล้ว
พังเองเมื่อไหร่ Docker ดึงกลับให้

**ทดสอบจริง — อย่าข้ามขั้นนี้:**

```bash
sudo reboot
# รอ ~2 นาที แล้ว ssh กลับเข้ามา
make health
```

---

## 7. เปิดจากภายนอก (Cloudflare Tunnel)

สร้าง tunnel ที่ Cloudflare Zero Trust → เอา token ใส่ `.env` เป็น `CF_TUNNEL_TOKEN=...`
แล้วเพิ่ม `tunnel` ใน `COMPOSE_PROFILES=matter,tunnel` (systemd จะเปิดให้เองทุกครั้งที่บูต) หรือเปิดทันทีด้วย:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tunnel up -d cloudflared
```

ตั้ง Access policy ให้ต้องล็อกอินก่อนถึงเว็บ — **ตอนนี้แอปยังไม่มี auth ของตัวเอง (เฟส 4)
ดังนั้น Cloudflare Access คือชั้นป้องกันเดียวที่มีอยู่** อย่าเปิด public โดยไม่มี Access policy

**ห้าม forward port พวกนี้ออกอินเทอร์เน็ตเด็ดขาด:**

| port | เหตุผล |
|---|---|
| `5580` matter-server | ไม่มี auth เลย ใครต่อได้ = สั่งอุปกรณ์ได้ |
| `5432` TimescaleDB | compose bind ไว้ที่ `127.0.0.1` แล้ว อย่าไปแก้ |
| `1883` MQTT | internal bus |

---

## 8. Backup

สองอย่างที่หายแล้วเจ็บคนละแบบ:

**ฐานข้อมูล**

```bash
make backup                     # ได้ ops/backups/iot-<เวลา>.dump
crontab -e
#   30 3 * * *  cd $HOME/iot-control && make backup >> $HOME/iot-backup.log 2>&1
```

`make backup` ใช้ `pg_dump` ที่อยู่ใน container ฐานข้อมูลเอง — **อย่าใช้ `postgresql-client` ของ
Raspberry Pi OS** เพราะเป็นเวอร์ชัน 15 ซึ่ง `pg_dump` จะปฏิเสธการ dump server PostgreSQL 16 ของเรา

dump ทั้งฐานในไฟล์เดียวโดยตั้งใจ — TimescaleDB เก็บแถวของ hypertable ไว้ใน chunk table ใต้
`_timescaledb_internal` การ dump แยกตามชื่อตาราง (`pg_dump -t telemetry`) จะได้ตารางแม่ที่ว่างเปล่า

**กุญแจ fabric ของ Matter** — อยู่ใน volume `matter-data` **ถ้าหายต้อง commission อุปกรณ์ใหม่ทั้งหมด**

```bash
docker volume ls | grep matter-data     # ชื่อ volume = <ชื่อโฟลเดอร์โปรเจกต์>_matter-data
docker run --rm -v iot-control_matter-data:/data -v "$PWD/ops/backups:/backup" \
  alpine tar czf /backup/matter-data-$(date +%F).tar.gz -C /data .
```

**เก็บ backup ไว้นอก Pi ด้วย** — SD card/SSD พังทีเดียวหายทั้งระบบและ backup ไปพร้อมกัน

**กู้คืนฐานข้อมูล** — ต้องลงบนฐานที่ว่างเท่านั้น (สคริปต์จะไม่ยอมทับของเดิม):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml down
docker volume rm iot-control_timescale-data            # ⚠️ ลบข้อมูลเดิมทิ้ง
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --wait db
make restore file=ops/backups/iot-<เวลา>.dump
make prod-up
```

`make restore` ครอบ `pg_restore` ด้วย `timescaledb_pre_restore()` / `timescaledb_post_restore()`
ตามที่ TimescaleDB กำหนด

---

## 9. เช็กลิสต์ก่อนถือว่าเสร็จ

- [ ] `uname -m` = `aarch64`
- [ ] `docker info | grep "Docker Root Dir"` ชี้ไป SSD
- [ ] `timedatectl` ขึ้น synchronized
- [ ] `.env` เป็น 600 และไม่มีคำว่า `CHANGE_ME` เหลือ
- [ ] `make migrate-status` ไม่มี pending และไม่มี DRIFT
- [ ] `make health` = `ok` (adapter connected + database connected)
- [ ] `curl -f http://127.0.0.1:8000/readyz` ได้ 200
- [ ] commission แล้ว และ `dump_nodes.py` เห็นอุปกรณ์ครบ
- [ ] **รีบูตจริงแล้ว stack กลับมาเอง**
- [ ] `make backup` และ backup `matter-data` รันผ่าน เก็บสำเนาไว้นอก Pi และ **ลอง `make restore` แล้ว**
- [ ] ไม่มี port 5580/5432/1883 โผล่ออกเน็ต (`sudo ss -tlnp`)

---

## 10. เจอปัญหาแบบนี้

| อาการ | สาเหตุที่พบบ่อย | ทางแก้ |
|---|---|---|
| `docker compose up` ฟ้อง `no matching manifest for linux/arm/v7` | ลง OS 32-bit | ต้องลง Raspberry Pi OS 64-bit ใหม่ |
| `migrate` ฟ้อง connection refused | ใช้ `DATABASE_URL` ที่ชี้ 127.0.0.1 ใน container | ต้องมาจาก `docker-compose.yml` ที่แทนเป็น `@db:5432` ให้ — อย่าไปตั้ง `DATABASE_URL` ทับใน `environment:` ของ service เอง |
| commission ค้าง / ไม่เจอ hub | Pi กับ M1 คนละ VLAN หรือ IPv6 ปิด | ต้องวง L2 เดียวกัน + `disable_ipv6=0`; AP ที่เปิด client isolation ก็บล็อก mDNS |
| อุปกรณ์โผล่ไม่ครบ | Tuya bridge ไม่ได้ export cluster นั้น | `dump_nodes.py` ดูของจริง; ที่ขาดจริง ๆ ต้องใช้ `IOT_ADAPTER=hybrid` ดึงผ่าน Tuya Cloud |
| app ขึ้นแต่โชว์ mock | เผลอใช้ `make up` (โหลด dev override) | ใช้ `make prod-up` |
| เว็บช้า / OOM ตอน build | RAM ไม่พอ | เพิ่ม swap ชั่วคราวตอน build: `sudo dphys-swapfile swapoff && sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile && sudo dphys-swapfile setup && sudo dphys-swapfile swapon` |
| SD card เต็ม/พัง | log + DB บน SD | ย้าย data-root ไป SSD (ข้อ 2); prod overlay หมุน log ที่ 10MB × 5 ให้แล้ว |

---

## หมายเหตุ

- ทุกคำสั่ง `make ...` ในไฟล์นี้ยังไม่เคยถูกรันจริงบน Pi — เครื่องที่พัฒนาอยู่เป็น Windows
  ที่ไม่มี `make` และ Docker อยู่โหมด Windows containers ที่ตรวจได้คือ **image ทั้งสองตัวมี arm64 จริง**
  (query registry แล้ว), compose ทั้ง base และ prod overlay validate ผ่าน, และ logic ของ
  `make init` ทดสอบด้วย shell จริงแล้ว
- ยังไม่มีระบบ login ของแอปเอง — ชั้นความปลอดภัยตอนนี้คือ LAN + Cloudflare Access เท่านั้น
  (`security.py` / `auth.py` / audit log เป็นเฟส 4 ดู README)
