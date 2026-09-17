# IoT Control Gateway

Monitor เซนเซอร์และควบคุมอุปกรณ์ Zigbee ผ่าน **Matter** (local control)
FastAPI + WebSocket ฝั่งหลัง, Tailwind CDN + Alpine.js + Chart.js ฝั่งหน้า ไม่มี build step

โครงสร้างและชื่อไฟล์อ้างอิงจาก `IoT-project.html` (Project Documentation v1.0)
ส่วนที่ยังไม่ตรงกับเอกสารมีสรุปไว้ในหัวข้อ [ส่วนที่ต่างจากเอกสาร](#ส่วนที่ต่างจากเอกสาร)

```
Zigbee devices ──► Zemismart M1 (Zigbee→Matter bridge)
                        │  Matter over IP (LAN · IPv6 · mDNS)
                        ▼
                 python-matter-server :5580
                        │
            MatterAdapter ─┐         ┌─ TuyaAdapter (fallback)
                           └► DeviceRouter ◄┘
                                 │
                        Hub (state + command lifecycle)
                        │                        │
          Recorder (deadband + heartbeat)   FastAPI (REST + WebSocket)
                        │                        │
             TimescaleDB hypertable      templates/ + static/
             + telemetry_5m rollup ─────► Chart.js
```

## โครงสร้างโปรเจกต์

```
iot-control/
├── Makefile                    # single entry point ทุกคำสั่ง
├── Dockerfile                  # multi-stage, non-root, HEALTHCHECK
├── docker-compose.yml          # base: db → migrate → app  (profiles: matter, tunnel)
├── docker-compose.override.yml # dev: hot reload + mock adapter
├── docker-compose.prod.yml     # prod: read-only rootfs, limits, log rotation
├── .env.example
│
├── app/
│   ├── main.py                 # FastAPI, lifespan, middleware, WS hub, healthz/readyz
│   ├── config.py               # pydantic-settings
│   ├── db.py                   # asyncpg pool + reconnect loop
│   ├── models.py               # Device / Capability / StateEvent / Command
│   ├── hub.py                  # registry + state + command lifecycle
│   ├── telemetry.py            # Recorder: deadband, heartbeat, batched writes
│   ├── history.py              # raw ↔ rollup query selection
│   ├── routes_devices.py       # list, onoff, level, command, history, commission
│   └── adapters/
│       ├── base.py             # DeviceAdapter protocol
│       ├── matter_adapter.py   # cluster map, subscribe, device_command
│       ├── tuya_adapter.py     # DP mapping, cloud fallback
│       ├── mock_adapter.py     # bridge จำลอง สำหรับ dev ไม่ต้องมีฮาร์ดแวร์
│       └── router.py           # DeviceRouter (hybrid)
│
├── migrations/
│   ├── 001_init.sql            # extensions + helpers
│   └── 004_telemetry.sql       # devices, hypertable, CAGG   (002/003 จองไว้ให้ auth)
│
├── scripts/
│   ├── migrate.py              # up / status / verify / new
│   ├── entrypoint.sh           # migrate | serve | dev | shell | psql
│   ├── healthcheck.py          # Docker HEALTHCHECK
│   ├── commission.py           # จับคู่ Hub M1
│   ├── dump_nodes.py           # dump endpoint/cluster ที่ bridge ปล่อยออกมา
│   ├── init_env.py             # make init: สุ่ม secret ลง .env
│   ├── backup.sh               # full pg_dump + retention
│   └── restore.sh              # pg_restore พร้อม timescaledb pre/post hooks
│
├── templates/                  # base.html · dashboard.html
├── static/                     # api.js · dashboard.js · manifest.json
└── ops/                        # mosquitto.conf · iot-control.service · backups/
    └── pi/
        ├── docker-compose.yml            # Pi 3B: matter-server อย่างเดียว
        ├── docker-compose.dashboard.yml  # overlay: + dashboard บน Pi, ไม่มี DB
        └── memwatch.sh                   # วัด RAM หนึ่งวัน แล้วตัดสินว่า Pi รับไหวไหม
```

## เริ่มใช้งานเร็วสุด (mock — ไม่ต้องมีฮาร์ดแวร์)

```bash
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
```

> `make` ไม่มีมาให้บน Windows — คำสั่ง `make ...` ในเอกสารนี้ใช้บน Linux host
> (เครื่องที่รัน matter-server / production) ถ้าอยากใช้บน Windows ติดตั้งผ่าน
> `winget install GnuWin32.Make` หรือใช้คำสั่งเต็มตามที่ Makefile เขียนไว้

เปิด <http://127.0.0.1:8000> จะเจออุปกรณ์จำลอง 6 ตัว เซนเซอร์ค่าขยับเองทุก 5 วินาที
และคำสั่งจะถูก "ทำหาย" ประมาณ 8% เพื่อให้เห็น path `pending → failed` ของจริง

> ยังไม่ต้องมี database ก็รันได้ — เว้น `DATABASE_URL` ว่างไว้ จะได้ระบบ monitor/control
> ครบทุกอย่าง แค่ไม่มีกราฟย้อนหลัง

## Deploy จริง

| คู่มือ | เมื่อไหร่ |
|---|---|
| **[DEPLOY-CLOUD.md §2.5](DEPLOY-CLOUD.md)** | **กำลังทดลอง** — Pi 3B รัน matter-server + dashboard เอง ไม่เก็บประวัติ แล้ววัด RAM ด้วย `ops/pi/memwatch.sh` ถ้ารับไหวไม่ต้องใช้ cloud |
| [DEPLOY-CLOUD.md](DEPLOY-CLOUD.md) | Pi 3B รับไม่ไหว — Pi รันแค่ matter-server, dashboard + DB อยู่บน Oracle Cloud Always Free, เชื่อมกันด้วย Tailscale |
| [DEPLOY-PI.md](DEPLOY-PI.md) | Pi 4/5 (4GB+) — ทุกอย่างอยู่ในบ้านเครื่องเดียว local-first เต็มตัว |

สองแบบใช้ compose ชุดเดียวกัน ต่างกันแค่ `.env`: `COMPOSE_PROFILES=matter` เปิด matter-server
ในเครื่อง, `APP_BIND=127.0.0.1` ซ่อน dashboard ไว้หลัง `tailscale serve`

## ขึ้น stack เต็ม

```bash
make init          # .env + สุ่ม JWT_SECRET
make up            # build → migrate → start → health
make migrate-status
```

Startup order คือ `db (healthy) → migrate (completed) → app` — แอปไม่มีทางแข่งกับ schema

> **Docker Desktop บน Windows ต้องอยู่โหมด Linux containers** (คลิกขวาไอคอน →
> Switch to Linux containers) — image ของ TimescaleDB เป็น Linux
> ส่วน `matter-server` **รันบน Windows ไม่ได้เลย** ต้องอยู่บน Linux host วง L2 เดียวกับ M1

## ต่อของจริง (Matter)

```bash
docker compose --profile matter up -d matter-server   # บน Linux box/Pi เท่านั้น
make commission code=MT:Y.K90AFN00KA0648G00
make matter-nodes                      # ดูว่า bridge ปล่อย endpoint/cluster อะไรออกมาบ้าง
```

pairing code เอามาจาก Tuya app → เลือก M1 → เมนู Matter / การควบคุมจากบุคคลที่สาม
เป็น **multi-admin** — M1 ยังอยู่ใน Tuya app ต่อได้ตามเดิม

`commission.py` ใช้ `network_only=True` เพราะ M1 อยู่บน IP network แล้ว ไม่ต้องใช้ Bluetooth

> สำรอง volume `matter-data` ไว้เสมอ — ถ้าหาย = ต้อง commission ใหม่ทั้งหมด

แล้วชี้ dashboard มาที่ matter-server:

```ini
IOT_ADAPTER=matter
MATTER_WS_URL=ws://<ip-ของ-linux-box>:5580/ws
```

## Config (`.env`)

| ตัวแปร | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `COMPOSE_PROFILES` | *(ว่าง)* | `matter` = รัน matter-server ในเครื่องนี้ · `tunnel` = Cloudflare Tunnel |
| `IOT_ADAPTER` | `mock` | `matter` · `tuya` · `hybrid` · `mock` |
| `MATTER_WS_URL` | `ws://127.0.0.1:5580/ws` | endpoint ของ python-matter-server |
| `COMMAND_TIMEOUT` | `5.0` | วินาทีที่รอ state echo ก่อนถือว่าคำสั่งล้มเหลว |
| `TUYA_ACCESS_ID` / `_SECRET` / `_UID` | *(ว่าง)* | ใช้เมื่อ adapter เป็น `tuya`/`hybrid` |
| `TUYA_POLL_SECONDS` | `15` | บังคับขั้นต่ำ 10 วินาที (rate limit) |
| `DATABASE_URL` | *(ว่าง)* | เว้นว่าง = ปิด history ทั้งหมด · พอร์ต 6543 (transaction pooler) แอปปิด prepared statement ให้เอง |
| `HISTORY_RETENTION_DAYS` | `400` | ใช้เฉพาะบน Postgres ธรรมดา · บน TimescaleDB policy ใน migration 005 เป็นคนจัดการ · `0` = ไม่ลบเลย |
| `TELEMETRY_FLUSH_SECONDS` | `2` | หน่วงก่อน INSERT เป็นชุด · ตั้งสูงขึ้นเมื่อ database อยู่ไกล |
| `RUN_MIGRATIONS` | `0` | ปกติ container `migrate` เป็นคนรัน |
| `DEVICE_LABELS_PATH` | `data/labels.json` | ชื่อ/ห้องที่ตั้งเองในหน้าเว็บ · เว้นว่าง = ปิดการเปลี่ยนชื่อ |
| `PUBLIC_ORIGIN` | `http://localhost:8000` | CORS |
| `ALLOWED_HOSTS` | `*` | TrustedHost (คั่นด้วย comma) — `127.0.0.1`/`localhost` ถูกเติมให้เองเพื่อ healthcheck |
| `APP_BIND` | `0.0.0.0` | interface ที่เปิดพอร์ต dashboard · `127.0.0.1` = เข้าได้ผ่าน proxy เท่านั้น |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | proxy ที่ยอมเชื่อ `X-Forwarded-For` (เดิม `*` = ปลอม IP ได้) |
| `AUTH_PATH` | `data/auth.json` | ที่เก็บ hash รหัสผ่าน · **เว้นว่าง = ปิดล็อกอิน** (log จะเตือนทุกครั้ง) |
| `SESSION_SECRET` | *(ว่าง)* | เว้นว่าง = สุ่มเก็บไว้ใน `AUTH_PATH` ให้เอง session ข้ามการรีสตาร์ตได้ |
| `SESSION_HOURS` | `720` | ค้างล็อกอินนานเท่าไร (30 วัน) |
| `LOGIN_ATTEMPTS` / `LOGIN_WINDOW_MINUTES` | `10` / `15` | จำกัดการเดารหัสต่อ IP |
| `ALLOW_HTTP_COMMISSION` | `0` | เปิด `POST /api/devices/commission` — ปกติใช้ `make commission` |
| `WS_PING_SECONDS` | `25` | heartbeat กัน Cloudflare ตัด WS |
| `CF_TUNNEL_TOKEN` | *(ว่าง)* | `make tunnel` |

## API

| Method | Path | หมายเหตุ |
|---|---|---|
| `POST` | `/api/auth/login` | `{"password": "..."}` → ตั้ง cookie · 401 ผิด · 429 เดาถี่เกินไป |
| `POST` | `/api/auth/logout` | ยกเลิก session นี้ที่เซิร์ฟเวอร์ แล้วล้าง cookie — cookie ที่ถูกคัดลอกไปก่อนหน้าก็ใช้ไม่ได้ |
| `POST` | `/api/auth/logout-all` | ต้องล็อกอินอยู่ · ให้ทุกเครื่องหลุดรวมเครื่องนี้ โดยไม่เปลี่ยนรหัสผ่าน |
| `POST` | `/api/auth/password` | `{"current": "...", "new": "..."}` · เปลี่ยนแล้วเบราว์เซอร์อื่นหลุดทั้งหมด |
| `GET` | `/api/auth/status` | `{required, configured, authenticated}` |
| `GET` | `/healthz` | `degraded` เมื่อต่อ hub หรือ DB ไม่ได้ · ไม่ได้ล็อกอินจะเห็นแค่ `status` |
| `GET` | `/readyz` | 503 เมื่อยังไม่พร้อมรับงานจริง |
| `GET` | `/api/devices` | snapshot: devices + states + pending |
| `POST` | `/api/devices/refresh` | สแกน fabric ใหม่ |
| `POST` | `/api/devices/{id}/onoff` | `{"value": true}` |
| `POST` | `/api/devices/{id}/level` | `{"value": 60, "capability": "brightness"}` |
| `POST` | `/api/devices/{id}/command` | รูปทั่วไป — ที่หน้าเว็บเรียกจริง |
| `PATCH` | `/api/devices/{id}/label` | `{"name": "โคมห้องนอน", "room": "ห้องนอน"}` · `""` = กลับไปใช้ชื่อจาก hub |
| `GET` | `/api/devices/{id}/history` | `?capability=temperature&hours=24&points=240` |
| `POST` | `/api/devices/commission` | ปิดเป็นค่าเริ่มต้น (`ALLOW_HTTP_COMMISSION`) · ใช้ `make commission` แทน |
| `WS` | `/ws` | push: `snapshot` · `devices` · `state` · `command` · `adapter` · `ping` |

## สิ่งที่ตั้งใจออกแบบไว้แบบนี้

**คำสั่งเป็น pending → confirmed | failed** — ไม่มีจุดไหนที่ถือว่ากดแล้วสำเร็จ
UI แสดงค่าที่สั่ง (optimistic) แต่จะยืนยันก็ต่อเมื่ออุปกรณ์ echo ค่ากลับมาเองเท่านั้น
ถ้าเงียบเกิน `COMMAND_TIMEOUT` จะเด้ง toast แดงและ rollback ค่าที่แสดง
`latency_ms` ที่ติดมากับ event `confirmed` คือ latency จริงตั้งแต่กดจนอุปกรณ์ตอบ

**Adapter เป็น seam เดียวของระบบ** ([app/adapters/base.py](app/adapters/base.py)) —
`DeviceRouter` ทำให้ Matter/Tuya อยู่ร่วมกันได้โดย API และ UI ไม่รู้เรื่องเลย
และ **backend ตัวหนึ่งล่มไม่ทำให้ทั้ง router ขึ้นว่า offline**

**Slider throttle 220 ms** ฝั่ง client — ยิงทุก pixel ที่ลากจะถล่ม Zigbee mesh จนอุปกรณ์ค้าง

**Deadband + heartbeat ก่อนเขียนลง DB** เซนเซอร์ที่รายงานทุก 30 วินาที = 2,880 แถว/วัน
ซึ่งเกือบทั้งหมดเหมือนแถวก่อนหน้า จะเขียนก็ต่อเมื่อค่าขยับเกิน deadband
(อุณหภูมิ 0.2°C, ความชื้น 1%, ความสว่าง 10% แบบสัมพัทธ์, boolean ทุกครั้งที่เปลี่ยน)
แต่บังคับเขียนอย่างน้อยทุก 5 นาที — **ช่องว่างในกราฟจึงแปลว่า "เซนเซอร์เงียบ" ไม่ใช่ "ค่านิ่ง"**

**DB ล่มไม่ทำให้คุมอุปกรณ์ไม่ได้** ส่วน control คือส่วนที่สำคัญต่อความปลอดภัย ไม่ใช่ history —
เมื่อ DB หลุด telemetry จะ buffer ไว้ใน memory (ทิ้งของเก่าสุดถ้าล้น) `/healthz` ขึ้น `degraded`,
`/history` ตอบ 503, ปุ่มกราฟหายไปจาก UI แต่ toggle/slider ยังทำงานปกติ แล้ว reconnect เองเมื่อ DB กลับมา

**HEALTHCHECK ไม่ถือว่า `degraded` = ไม่สุขภาพดี** เพราะรีสตาร์ท container ตอน DB ล่มไม่ได้แก้อะไร
มีแต่จะทำให้คนที่กำลังดูหน้าเว็บอยู่ใช้งานไม่ได้ไปด้วย

**หน้าเว็บพูดเฉพาะเรื่องที่ต้องรู้** — timestamp จะโผล่ก็ต่อเมื่อเซนเซอร์เงียบเกิน 10 นาที
(recorder heartbeat ทุก 5 นาที ดังนั้นเงียบ = ผิดปกติจริง) และ **staleness ใช้กับเซนเซอร์เท่านั้น**
หลอดไฟไม่ได้มีหน้าที่รายงานเป็นระยะ — ของพวกนี้ดูที่ `online` (Matter Reachable) แทน
สถานะระบบเหลือบรรทัดเดียวที่บอกเรื่องแย่ที่สุดที่เป็นจริงอยู่ ไม่ใช่ไฟสามดวงให้ตีความเอง
ค่าที่ไม่ใช่พระเอก (แบตเตอรี่) ลงไปเป็นตัวเล็กท้ายการ์ด

**Design system เป็น CSS custom properties ชุดเดียว** ใน [templates/base.html](templates/base.html) —
Tailwind ใช้ผ่าน arbitrary value (`bg-[var(--surface)]`) ดังนั้นเปลี่ยนธีมทั้งระบบ = แก้บล็อกเดียว
ตอนนี้เลือกยึด dark ตัวเดียวโดยตั้งใจ (แผงติดผนัง + มือถือตอนกลางคืน) ไม่ตาม `prefers-color-scheme`

**สีบนหน้าเว็บทุกจุดแปลว่าอะไรบางอย่าง ไม่ได้ทาเล่น** ([static/dashboard.js](static/dashboard.js)) —

- *ค่าเซนเซอร์เปลี่ยนสีตามค่าจริง* ผ่าน ramp ที่ interpolate ระหว่างจุดที่กำหนด
  อุณหภูมิไล่ ฟ้า→เขียว→เหลือง→แดง, ความชื้นไล่ ส้ม(แห้ง)→เขียว→ฟ้า→ม่วง(ชื้น), แบตเตอรี่ แดง→เขียว
  อ่านออกว่า "ร้อนไป" ก่อนจะทันอ่านตัวเลข ตัวเลขเดียวกันนี้ในกราฟย้อนหลังก็ใช้ ramp เดียวกัน
- *slider อุณหภูมิสีเป็น gradient เคลวินจริง* 2200K→6500K และ *slider ความสว่างไล่ไปหาสีที่หลอดเปิดอยู่จริง*
  — เลื่อนแล้วเห็นผลลัพธ์ก่อนกด
- *หลอดไฟที่เปิดอยู่จะเรืองแสงบนการ์ดตัวเอง* ด้วยสีเคลวินที่มันเปิดอยู่ และความเข้มตามความสว่าง
  ปลั๊ก/สวิตช์ทั่วไปเป็นเขียว มองปราดเดียวรู้ว่าห้องไหนเปิดอะไรอยู่

**Migration มี checksum + advisory lock** แก้ไฟล์ที่ apply ไปแล้วจะ error ทันที
และสอง instance ที่ start พร้อมกันจะไม่ชนกัน — ไฟล์ที่ขึ้นต้นด้วย `-- migrate:no-transaction`
จะถูกแยกทีละ statement เพราะ continuous aggregate ของ TimescaleDB รันใน transaction ไม่ได้

**History ทำงานได้ทั้งบน TimescaleDB และ Postgres ธรรมดา** — แอปถามตัว database เองตอนต่อ
ไม่ได้อ่านจาก config: มี TimescaleDB ก็ใช้ hypertable + continuous aggregate 5 นาที ไม่มีก็อ่าน
raw ด้วย `date_bin()` แล้วลบของเก่าเองเป็นรอบ ทำให้ managed free tier (Supabase, Neon) ที่ไม่มี
TimescaleDB ใช้ image เดียวกันได้โดยไม่ต้องแก้อะไร — migration 005 มี `-- migrate:requires-extension`
กำกับไว้ ตัวรันจะข้ามและบันทึกว่าข้าม ถ้าวันหนึ่งย้าย dump ไปลง TimescaleDB มันจะ apply ให้เอง

## ส่วนที่ต่างจากเอกสาร

| เอกสารว่า | ของจริง | เหตุผล |
|---|---|---|
| `002_auth_audit` · `003_user_mgmt` | ยังไม่มี (จองเลขไว้) | เป็นเฟส security — และ checksum ทำให้แก้ไฟล์ที่ apply แล้วไม่ได้ จึงไม่ควรสร้างไฟล์ครึ่ง ๆ ไว้ก่อน |
| `main.py` รวม WS hub | แยกเป็น `hub.py` · `telemetry.py` · `history.py` | เอกสารไม่ได้ห้าม และ command lifecycle ยาวเกินกว่าจะยัดใน `main.py` |
| WS `{"type":"devices"}` | มีทั้ง `snapshot` และ `devices` | snapshot ส่ง devices + states + pending พร้อมกันตอนเชื่อมต่อ · `devices` ส่งเฉพาะรายชื่อเวลาเปลี่ยนชื่ออุปกรณ์ จะได้ไม่ล้าง state ที่ client ถืออยู่ |
| `device_id` = `"1:3"` | `"matter:1:3"` | DeviceRouter ต้องรู้ backend จาก id |
| auth เก็บใน migration 002/003 | เก็บเป็นไฟล์ `AUTH_PATH` | ผู้ใช้คนเดียว และล็อกอินต้องทำงานได้แม้ database ล่ม ไม่งั้นเน็ตหลุดแล้วเข้าไปปิดไฟในบ้านตัวเองไม่ได้ · เลข 002/003 ยังจองไว้ถ้าวันหนึ่งมีหลายผู้ใช้ |
| SlowAPI rate limit | จำกัดการเดารหัสในโค้ดเอง | ต้องกันเฉพาะหน้าล็อกอิน และเก็บสถานะในหน่วยความจำก็พอสำหรับผู้ใช้คนเดียว — ไม่คุ้มกับ dependency เพิ่ม |
| palette `slate-950` · `emerald-500` · `amber-500` | CSS token ชุดเดียว + ramp ตามค่าที่อ่านได้ | สีถูกผูกกับความหมาย (ค่าสูง/ต่ำ, สีหลอดไฟจริง) แทนที่จะเป็นสีคงที่ต่อ component และเปลี่ยนธีมได้จากที่เดียว |
| `rounded-lg` input · `xl` card · `2xl` modal | `2xl` card · `2xl` modal · pill สำหรับปุ่ม | ทรงโค้งชุดเดียวทั้งหน้าอ่านสงบกว่า |

## ยังไม่ได้ทำ

- **เฟส 3** — MQTT bus (Mosquitto) คั่นระหว่าง adapter กับ hub · `ops/mosquitto.conf` พร้อมแล้ว
- **เฟส 4 (security)** — ทำแล้วเท่าที่ระบบผู้ใช้คนเดียวต้องใช้: [app/auth.py](app/auth.py) ·
  [app/routes_auth.py](app/routes_auth.py) · [templates/login.html](templates/login.html) ·
  [scripts/set_password.py](scripts/set_password.py) · ด่านกั้นทุก route และ WebSocket ·
  จำกัดการเดารหัส
  ยังไม่ได้ทำ (ต้องมีเมื่อมีผู้ใช้หลายคน): 2FA · audit log · จัดการผู้ใช้ ·
  `routes_users.py` · `routes_audit.py` · migrations 002/003 · HTTP 428 gate

## หมายเหตุเรื่อง Matter bridge ของ Tuya

M1 จะ expose เฉพาะ cluster มาตรฐาน — OnOff (6), LevelControl (8), ColorControl (768),
TemperatureMeasurement (1026), RelativeHumidityMeasurement (1029) เป็นต้น
ฟังก์ชันเฉพาะรุ่นที่เห็นใน Tuya app (scene, preset, พลังงาน) จะไม่โผล่ผ่าน Matter
ถ้าจำเป็นต้องใช้ ให้ตั้ง `IOT_ADAPTER=hybrid` แล้วใส่ credential ของ Tuya —
`DeviceRouter` จะพาอุปกรณ์ที่ Matter มองไม่เห็นไปทาง Tuya Cloud ให้เอง

**ชื่ออุปกรณ์** — M1 ส่งมาแค่ชื่อที่ตัว hub รู้จัก ซึ่งมักเป็นชื่อรุ่น (`2 Gang Switch`)
ไม่ใช่ชื่อที่ตั้งไว้ใน Tuya app เพราะชื่อนั้นอยู่บน Tuya cloud ไม่ได้อยู่ในตัว hub
(`FixedLabel` ของ M1 ก็ใช้ไม่ได้ — มันใส่ค่าตัวอย่างจากสเปคมาตรง ๆ คือ
`room='bedroom 2', orientation='North', floor='2', direction='up'` เหมือนกันทุก endpoint)
Matter ไม่มีช่องทางขอชื่อนั้น จึงตั้งชื่อและกำหนดห้องเองได้จากปุ่มดินสอบนการ์ด
เก็บเป็นไฟล์ JSON (`DEVICE_LABELS_PATH`) ไม่ได้เก็บใน database — โหมด Pi-only
ที่ไม่มี DB ก็ต้องจำชื่อได้ ตั้งชื่อในแอป Tuya ใหม่ชื่อที่นี่ก็ไม่หาย

**อุปกรณ์หลายช่อง** — สวิตช์สองทางมาแบบ composed device: endpoint หนึ่งถือชื่อกับ
สถานะออนไลน์ และ `PartsList` ชี้ไปยัง endpoint ลูกที่ถือ cluster ที่กดได้จริง
ระบบไต่ขึ้นไปเอาชื่อจาก parent แล้วไล่เลข (`2 Gang Switch 1`, `2 Gang Switch 2`)

ใช้ `make matter-nodes` ดูว่า bridge ปล่อยอะไรออกมาจริง ๆ ก่อนเดา — มันพิมพ์
device type, ทุก label cluster, `PartsList` และชื่อที่หน้าเว็บจะเลือกใช้
