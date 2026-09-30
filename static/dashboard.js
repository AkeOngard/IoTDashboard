/* Dashboard component (doc §10). Registered as a global factory so Alpine,
 * which loads deferred, finds it at init time.
 *
 * Display principle: show a number only where a number is the point, and show
 * secondary facts (battery, staleness) only when they are worth a glance.
 *
 * Everything on this page is worked out here, in the viewer's browser, from
 * the one WebSocket stream the server already sends: the pages, the groups,
 * the alerts and the charts cost the Pi nothing beyond the messages it was
 * sending anyway, plus one /history query when a chart is on screen.
 *
 * Pages are hash routes (#/devices, #/device/<id>, #/alerts) inside this one
 * document, so moving between them never asks the Pi for anything.
 */
function dashboard() {
  return {
    // Readings that deserve the large type.
    HERO: ['temperature', 'humidity', 'illuminance'],
    // Promoted to hero when a device has nothing else to show.
    STATE_READ: ['contact', 'occupancy'],
    READ: ['temperature', 'humidity', 'illuminance', 'battery', 'contact', 'occupancy'],
    // What the overview chart can show: the house's climate.
    CLIMATE: ['temperature', 'humidity'],

    LABEL: {
      temperature: 'อุณหภูมิ', humidity: 'ความชื้น', illuminance: 'ความสว่าง',
      battery: 'แบตเตอรี่', contact: 'หน้าต่าง/ประตู', occupancy: 'ตรวจจับคน',
      brightness: 'ความสว่างไฟ', color_temp: 'โทนแสง', switch: 'สวิตช์',
    },
    UNIT: { brightness: '%', color_temp: 'K' },
    CHART_UNIT: {
      temperature: '°C', humidity: '%', illuminance: 'lx', battery: '%',
      brightness: '%', color_temp: 'K', switch: '%', contact: '%', occupancy: '%',
    },
    // Chart line colours, matching the reading colours below.
    COLOR: {
      temperature: '230 180 79', humidity: '79 189 232', illuminance: '237 201 92',
      battery: '124 199 102', brightness: '139 124 246', color_temp: '169 139 245',
      switch: '139 124 246', contact: '169 139 245', occupancy: '169 139 245',
    },

    /* Value → colour ramps. A reading carries its own colour so "too hot" or
     * "battery nearly flat" registers before the number is even read. Stops are
     * [value, [r,g,b]] and everything between them is interpolated. */
    RAMP: {
      temperature: [[16, [98, 166, 232]], [22, [69, 196, 166]], [27, [124, 199, 102]],
                    [30, [230, 180, 79]], [34, [229, 111, 92]]],
      humidity:    [[25, [230, 180, 79]], [40, [124, 199, 102]], [55, [79, 189, 232]],
                    [70, [139, 143, 240]], [85, [169, 139, 245]]],
      battery:     [[10, [229, 111, 92]], [25, [230, 180, 79]], [50, [124, 199, 102]],
                    [100, [75, 189, 133]]],
    },
    FLAT: { illuminance: [237, 201, 92] },

    // Endpoints of the Kelvin gradient used by the colour-temperature slider.
    K_WARM: [255, 172, 92],
    K_COOL: [198, 224, 255],
    RANGE: { brightness: [1, 100, 1], color_temp: [2200, 6500, 100] },
    RANGES: [
      { label: '1ชม', hours: 1 }, { label: '6ชม', hours: 6 },
      { label: '24ชม', hours: 24 }, { label: '7วัน', hours: 168 },
      { label: '30วัน', hours: 720 },
    ],
    // At or below this a battery makes the alerts.
    LOW_BATTERY: 20,
    // A sensor quieter than this has something wrong with it, not a stable value:
    // the recorder heartbeats every 5 minutes even when nothing changes.
    STALE_SECONDS: 600,
    /* The chart shades a bucket's min-to-max only where the spread is wider
     * than this: a swing worth seeing, like a window opened for ten minutes.
     * Below it the spread is sensor noise, and a band there is just a glow
     * tracing the line. Capabilities not listed use 15% of the chart's span. */
    SWING: { temperature: 1, humidity: 5, battery: 2, illuminance: 50, brightness: 10, color_temp: 300 },
    // Gap between the commands of a group switch, so a room full of lamps
    // does not hit the Zigbee mesh in one burst.
    BULK_GAP_MS: 150,
    // How many devices the overview shows when none are pinned.
    DEFAULT_PINS: 6,

    // What the adapters call a device with no room. Matter has no rooms at
    // all, so on a Matter install every device starts here.
    UNASSIGNED: 'Unassigned',

    devices: [], states: {}, pending: {}, toasts: [],
    // Where we are: 'overview' | 'devices' | 'device' | 'alerts'.
    page: 'overview', detailId: null,
    // Devices page filter: 'all' | 'online' | 'attention'.
    filter: 'all',
    // Device ids pinned to the overview, kept in this browser.
    pins: [],
    // Which climate sensor the overview chart follows.
    overviewSensor: null,
    theme: 'dark', look: 'glass', canBlur: true,
    // The device being renamed, and the draft being typed into it.
    editing: null, draft: { name: '', room: '' }, saving: false, refreshing: false,
    socket: false, adapterConnected: false, adapterName: '—', historyEnabled: false,
    // `geo` is always an object -- empty until drawn, `drawn` says which --
    // so the plot's bindings never meet a null while the plot is torn down.
    chart: {
      device: null, capability: null, hours: 24,
      loading: false, data: null, error: null, geo: emptyGeo(), drawn: false, hover: null,
    },
    _ws: null, _backoff: 1000, _timers: {}, _toastSeq: 0, _tick: 0, _reqSeq: 0, _raf: 0, _plotEl: null,

    init() {
      const root = document.documentElement;
      this.theme = root.getAttribute('data-theme') || 'dark';
      this.look = root.getAttribute('data-look') || 'solid';
      this.canBlur = root.getAttribute('data-can-blur') !== 'no';
      this.syncThemeColor();
      try { this.pins = JSON.parse(localStorage.getItem('iot.pins') || '[]'); } catch (e) { this.pins = []; }
      this.connect();
      // Drives the clock, the greeting and the staleness labels.
      setInterval(() => { this._tick++; }, 15000);
      window.addEventListener('hashchange', () => this.route());
      this.route();
    },

    connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      this._ws = ws;
      ws.onopen = () => { this.socket = true; this._backoff = 1000; };
      ws.onmessage = (e) => this.apply(JSON.parse(e.data));
      ws.onclose = (event) => {
        this.socket = false;
        // 1008 is the server refusing the session. Reconnecting would spin
        // against the door for as long as the tab stays open.
        if (event.code === 1008) {
          window.location.replace('/login');
          return;
        }
        setTimeout(() => this.connect(), this._backoff);
        this._backoff = Math.min(this._backoff * 2, 15000);
      };
      ws.onerror = () => ws.close();
    },

    apply(msg) {
      if (msg.type === 'ping') {
        // Server heartbeat (doc §6): answering keeps Cloudflare from culling
        // the socket during a quiet stretch.
        if (this._ws && this._ws.readyState === WebSocket.OPEN) this._ws.send('pong');
      } else if (msg.type === 'snapshot') {
        this.devices = msg.devices;
        this.adapterName = msg.adapter;
        this.adapterConnected = msg.connected;
        if ('history' in msg) this.historyEnabled = msg.history;
        this.states = {};
        msg.states.forEach(s => { this.states[this.key(s.device_id, s.capability)] = s; });
        this.pending = {};
        msg.pending.forEach(p => { this.pending[this.key(p.device_id, p.capability)] = p.value; });
        // The chart may have been waiting for the device list or the news
        // that history is on.
        this.$nextTick(() => this.ensureChart());
      } else if (msg.type === 'devices') {
        // A rename: only the inventory changed, so leave state and pending
        // alone rather than replacing them with a whole new snapshot.
        this.devices = msg.devices;
      } else if (msg.type === 'state') {
        this.states[this.key(msg.device_id, msg.capability)] = msg;
      } else if (msg.type === 'adapter') {
        this.adapterConnected = msg.connected;
        this.adapterName = msg.adapter;
      } else if (msg.type === 'command') {
        const k = this.key(msg.device_id, msg.capability);
        if (msg.status === 'pending') {
          this.pending[k] = msg.value;
        } else {
          delete this.pending[k];
          if (msg.status === 'failed') {
            this.toast('error', 'สั่งงานไม่สำเร็จ', `${this.nameOf(msg.device_id)} — ${msg.reason || 'ไม่ทราบสาเหตุ'}`);
          }
        }
      }
    },

    key: (id, cap) => `${id}|${cap}`,
    nameOf(id) { return (this.devices.find(d => d.id === id) || {}).name || id; },
    byRoom(room) { return this.devices.filter(d => d.room === room); },

    // ------------------------------------------------------------ routing

    route() {
      const [page, id] = location.hash.replace(/^#\/?/, '').split('/');
      if (page === 'device' && id) {
        this.detailId = decodeURIComponent(id);
        this.page = 'device';
      } else {
        // detailId is left as it was: the device page's bindings run once
        // more while it is being torn down, and they need their device.
        this.page = ['devices', 'alerts'].includes(page) ? page : 'overview';
      }
      this.editing = null;
      window.scrollTo(0, 0);
      this.$nextTick(() => this.ensureChart());
    },

    deviceHref(device) { return '#/device/' + encodeURIComponent(device.id); },

    get detail() {
      return this.devices.find(d => d.id === this.detailId) || null;
    },

    // --------------------------------------------------------- appearance

    setTheme(theme) {
      this.theme = theme;
      document.documentElement.setAttribute('data-theme', theme);
      try { localStorage.setItem('iot.theme', theme); } catch (e) { /* private mode */ }
      this.syncThemeColor();
      // Grid lines and labels are drawn in theme colours.
      if (this.chart.data) this.$nextTick(() => this.draw());
    },

    setLook(look) {
      if (look === 'glass' && !this.canBlur) return;
      this.look = look;
      document.documentElement.setAttribute('data-look', look);
      try { localStorage.setItem('iot.look', look); } catch (e) { /* private mode */ }
    },

    /** The phone's own status bar follows the page. */
    syncThemeColor() {
      const meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute('content', this.theme === 'light' ? '#efeef6' : '#0f0f16');
    },

    // -------------------------------------------------------------- clock

    /** A greeting for the hour it is where the viewer is. "ราตรีสวัสดิ์" is a
     *  goodbye before sleep, not a hello, so late evening says good evening. */
    get greeting() {
      this._tick;
      const h = new Date().getHours();
      if (h >= 5 && h < 11) return 'อรุณสวัสดิ์';
      if (h >= 11 && h < 13) return 'สวัสดีตอนเที่ยง';
      if (h >= 13 && h < 17) return 'สวัสดีตอนบ่าย';
      if (h >= 17 && h < 21) return 'สวัสดีตอนเย็น';
      if (h >= 21) return 'สวัสดีตอนค่ำ';
      return 'สวัสดีตอนดึก';
    },

    get today() {
      this._tick;
      const d = new Date();
      return d.toLocaleDateString('th-TH', { weekday: 'short', day: 'numeric', month: 'short' })
        + ' · ' + d.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' });
    },

    /** "ออนไลน์ 9 จาก 10 อุปกรณ์ · ในบ้าน 21.5°C": the house in one line. */
    get houseLine() {
      const parts = [`ออนไลน์ ${this.onlineCount} จาก ${this.devices.length} อุปกรณ์`];
      const t = this.climate('temperature');
      if (t) parts.push(`ในบ้าน ${t.text}°C`);
      return parts.join(' · ');
    },

    /** "เมื่อสักครู่", "5 นาทีที่แล้ว": when a device last said anything. */
    lastSeen(device) {
      this._tick;
      const times = device.capabilities.map(c => this.states[this.key(device.id, c)]?.ts).filter(Boolean);
      if (!times.length) return '—';
      const age = Date.now() / 1000 - Math.max(...times);
      if (age < 60) return 'เมื่อสักครู่';
      if (age < 3600) return `${Math.floor(age / 60)} นาทีที่แล้ว`;
      if (age < 86400) return `${Math.floor(age / 3600)} ชม.ที่แล้ว`;
      return `${Math.floor(age / 86400)} วันที่แล้ว`;
    },

    // --------------------------------------------------------------- rooms

    /** Rooms in name order, with the no-room bucket last. */
    get rooms() {
      const names = [...new Set(this.devices.map(d => d.room))].sort();
      return names.filter(r => r !== this.UNASSIGNED)
        .concat(names.includes(this.UNASSIGNED) ? [this.UNASSIGNED] : []);
    },

    /** The placeholder is an English word from the adapter, not a room. */
    roomLabel(room) {
      return room === this.UNASSIGNED ? 'ยังไม่ระบุห้อง' : room;
    },

    /** One status line beats three indicators: report the worst thing that is
     *  true, and stay quiet when everything is fine. */
    get health() {
      if (!this.socket) return { tone: 'danger', text: 'ขาดการเชื่อมต่อเซิร์ฟเวอร์' };
      if (!this.adapterConnected) return { tone: 'warn', text: `กำลังเชื่อมต่อ ${this.adapterName}` };
      if (!this.historyEnabled) return { tone: 'muted', text: 'ทำงานปกติ · ไม่บันทึกประวัติ' };
      return { tone: 'ok', text: 'ทำงานปกติ' };
    },

    // --------------------------------------------------------------- state

    /** Confirmed value straight from the device. */
    value(id, cap) {
      const s = this.states[this.key(id, cap)];
      return s ? s.value : null;
    },

    /** What the UI shows: the optimistic target while a command is in flight,
     *  otherwise the last value the device actually reported. */
    shown(id, cap) {
      const k = this.key(id, cap);
      return k in this.pending ? this.pending[k] : this.value(id, cap);
    },

    isPending(id, cap) { return this.key(id, cap) in this.pending; },
    isSwitchable(device) { return device.writable.includes('switch'); },
    isOn(device) { return this.shown(device.id, 'switch') === true; },

    unit(id, cap) {
      const s = this.states[this.key(id, cap)];
      return s ? s.unit : (this.UNIT[cap] || '');
    },

    format(id, cap) {
      const v = this.shown(id, cap);
      if (v === null || v === undefined) return '—';
      if (cap === 'contact') return v ? 'ปิดสนิท' : 'เปิดอยู่';
      if (cap === 'occupancy') return v ? 'มีคน' : 'ว่าง';
      return typeof v === 'number' ? v.toLocaleString('th-TH') : v;
    },

    /** A reading with its unit, short: "27.1°C", "56%", "เปิดอยู่". */
    reading(id, cap) {
      const text = this.format(id, cap);
      if (text === '—' || this.STATE_READ.includes(cap)) return text;
      return text + (cap === 'temperature' ? '°C' : this.unit(id, cap));
    },

    /** The line under a device's name on a tile or a row. */
    summary(device) {
      if (!device.online) return 'ออฟไลน์';
      if (this.isSwitchable(device)) return this.stateText(device);
      const caps = this.heroCaps(device);
      return caps.length ? caps.map(c => this.reading(device.id, c)).join(' · ') : '—';
    },

    /** The line under a switchable device's name. */
    stateText(device) {
      if (!device.online) return 'ออฟไลน์';
      if (this.isPending(device.id, 'switch')) return 'กำลังสั่ง…';
      const v = this.shown(device.id, 'switch');
      if (v === null || v === undefined) return 'ไม่ทราบสถานะ';
      if (!v) return 'ปิดอยู่';
      const level = this.shown(device.id, 'brightness');
      return typeof level === 'number' ? `เปิด · ${level}%` : 'เปิดอยู่';
    },

    batteryText(device) {
      const v = this.value(device.id, 'battery');
      return typeof v === 'number' ? `${v}%` : '—';
    },

    // ---------------------------------------------------------- overview

    get onlineCount() { return this.devices.filter(d => d.online).length; },

    /** Online devices with an on/off switch. */
    get switchable() {
      return this.devices.filter(d => d.online && this.isSwitchable(d));
    },

    get onCount() { return this.switchable.filter(d => this.isOn(d)).length; },

    /** Average and extreme of one reading across the house, or null when no
     *  online device reports it. */
    climate(cap) {
      const rows = this.devices
        .filter(d => d.online)
        .map(d => ({ d, v: this.value(d.id, cap) }))
        .filter(r => typeof r.v === 'number');
      if (!rows.length) return null;
      const avg = rows.reduce((sum, r) => sum + r.v, 0) / rows.length;
      return {
        text: cap === 'temperature' ? avg.toFixed(1) : String(Math.round(avg)),
        color: this.rgb(this.ramp(this.RAMP[cap], avg)),
        many: rows.length > 1,
      };
    },

    /* Groups: every room with something to switch, plus the whole house.
     * A group reads as on while anything in it is on; switching it turns
     * everything off, or, when all of it is already off, everything on. */
    get groups() {
      this._tick;
      const all = this.devices.filter(d => this.isSwitchable(d));
      const out = [];
      if (all.length) out.push({ key: 'all', name: 'ทั้งหมด', kind: 'all', members: all });
      for (const room of this.rooms) {
        const members = all.filter(d => d.room === room);
        if (members.length) out.push({ key: 'room:' + room, name: this.roomLabel(room), kind: 'room', members });
      }
      return out.map(g => {
        const live = g.members.filter(d => d.online);
        const on = live.filter(d => this.isOn(d)).length;
        const sub = !live.length ? 'ออฟไลน์ทั้งหมด'
          : on === 0 ? 'ปิดทั้งหมด'
          : on === live.length ? 'เปิดทั้งหมด'
          : `เปิด ${on} จาก ${live.length}`;
        return { ...g, live, on, sub };
      });
    },

    toggleGroup(group) {
      this.setMany(group.live, group.on === 0);
    },

    /** Switch every device in `list` to `value`, one command at a time. */
    async setMany(list, value) {
      for (const d of list.filter(x => this.isOn(x) !== value)) {
        this.send(d.id, 'switch', value);
        await new Promise(r => setTimeout(r, this.BULK_GAP_MS));
      }
    },

    isPinned(device) { return this.pins.includes(device.id); },

    togglePin(device) {
      this.pins = this.isPinned(device)
        ? this.pins.filter(id => id !== device.id)
        : this.pins.concat(device.id);
      try { localStorage.setItem('iot.pins', JSON.stringify(this.pins)); } catch (e) { /* private mode */ }
    },

    /** Pinned devices, or -- until something is pinned -- the first few,
     *  controls before readings, so a new install is not an empty panel. */
    get pinned() {
      const chosen = this.pins.map(id => this.devices.find(d => d.id === id)).filter(Boolean);
      if (chosen.length) return chosen;
      const controls = this.devices.filter(d => this.isSwitchable(d));
      const rest = this.devices.filter(d => !this.isSwitchable(d));
      return controls.concat(rest).slice(0, this.DEFAULT_PINS);
    },

    get pinsChosen() {
      return this.pins.some(id => this.devices.some(d => d.id === id));
    },

    /** Current problems only. Nothing here is stored: when the device reports
     *  that the problem is gone, it simply drops off the list. */
    get issues() {
      this._tick; // staleness moves with the clock
      const out = [];
      const danger = { color: 'var(--danger)', bg: 'rgb(236 122 102 / .14)' };
      const warn = { color: 'var(--warn)', bg: 'rgb(230 169 78 / .16)' };
      for (const d of this.devices) {
        const base = { id: d.id, name: d.name, room: this.roomLabel(d.room) };
        if (!d.online) {
          out.push({ ...base, ...danger, key: d.id + ':offline', kind: 'offline',
                     title: `${d.name} ออฟไลน์`, detail: 'ไม่ตอบสนองจากเครือข่าย' });
          continue;
        }
        if (d.capabilities.includes('contact') && this.value(d.id, 'contact') === false) {
          out.push({ ...base, ...warn, key: d.id + ':open', kind: 'open',
                     title: `${d.name} เปิดอยู่`, detail: 'หน้าต่างหรือประตูยังเปิด' });
        }
        const battery = this.value(d.id, 'battery');
        if (typeof battery === 'number' && battery <= this.LOW_BATTERY) {
          out.push({ ...base, ...warn, key: d.id + ':battery', kind: 'battery',
                     title: `แบต ${d.name} เหลือ ${battery}%`, detail: 'ควรเปลี่ยนแบตเตอรี่เร็ว ๆ นี้' });
        }
        const stale = this.staleness(d);
        if (stale && stale.color === 'var(--warn)') {
          out.push({ ...base, ...warn, key: d.id + ':stale', kind: 'stale',
                     title: `${d.name} ${stale.text}`, detail: 'ไม่ได้ส่งค่ามาสักพัก' });
        }
      }
      return out;
    },

    hasIssue(device) { return this.issues.some(i => i.id === device.id); },

    /** Online / attention / offline, as a label and a pill style. */
    status(device) {
      if (!device.online) return { text: 'ออฟไลน์', cls: 'pill-muted' };
      if (this.hasIssue(device)) return { text: 'ต้องดูแล', cls: 'pill-warn' };
      return { text: 'ออนไลน์', cls: 'pill-ok' };
    },

    // ------------------------------------------------------------ devices

    /** The devices page list: by room, then name, through the filter. */
    get listed() {
      const order = this.rooms;
      return this.devices
        .filter(d => this.filter === 'online' ? d.online
          : this.filter === 'attention' ? (!d.online || this.hasIssue(d))
          : true)
        .slice()
        .sort((a, b) => (order.indexOf(a.room) - order.indexOf(b.room)) || a.name.localeCompare(b.name, 'th'));
    },

    // ------------------------------------------------------------- colour

    /** Linear interpolation across a [value, rgb] ramp. */
    ramp(stops, v) {
      if (v <= stops[0][0]) return stops[0][1];
      for (let i = 1; i < stops.length; i++) {
        if (v <= stops[i][0]) {
          const [v0, c0] = stops[i - 1], [v1, c1] = stops[i];
          const t = (v - v0) / (v1 - v0);
          return c0.map((c, j) => Math.round(c + (c1[j] - c) * t));
        }
      }
      return stops[stops.length - 1][1];
    },

    /** Approximate lamp colour for a colour temperature, warm 2200K → cool 6500K. */
    kelvinRgb(k) {
      const t = Math.max(0, Math.min(1, ((k ?? 2800) - 2200) / (6500 - 2200)));
      return this.K_WARM.map((w, i) => Math.round(w + (this.K_COOL[i] - w) * t));
    },

    rgb(c) { return `rgb(${c[0]} ${c[1]} ${c[2]})`; },

    /** The colour a reading should be drawn in. */
    readingColor(id, cap) {
      const v = this.shown(id, cap);
      if (v === null || v === undefined) return 'var(--faint)';
      if (cap === 'contact')   return v ? 'var(--text)' : 'var(--warn-text)';   // open is worth noticing
      if (cap === 'occupancy') return v ? 'var(--accent-text)' : 'var(--text)';
      if (this.FLAT[cap]) return this.rgb(this.FLAT[cap]);
      const stops = this.RAMP[cap];
      if (stops && typeof v === 'number') return this.rgb(this.ramp(stops, v));
      return 'var(--text)';
    },

    /** A tile lights up in the accent while its device is on or triggered. */
    tileOn(device) {
      return device.online && this.iconActive(device);
    },

    /** The icon's colour and how hard it glows. A lamp at 10% glows like one;
     *  an open window is amber; a climate sensor wears its temperature. */
    iconStyle(device) {
      if (!device.online) return '';
      const kind = this.iconKind(device);
      if (kind === 'contact' && this.iconActive(device)) return 'color: var(--warn-text); --glow: .2';
      if (kind === 'climate') {
        const t = this.value(device.id, 'temperature');
        if (typeof t === 'number') return `color: ${this.rgb(this.ramp(this.RAMP.temperature, t))}`;
      }
      if (device.kind === 'light' && this.iconActive(device)) {
        const level = this.shown(device.id, 'brightness');
        return `--glow: ${(0.14 + 0.32 * ((typeof level === 'number' ? level : 100) / 100)).toFixed(2)}`;
      }
      return '';
    },

    /** Brightness fades up to the accent; colour temperature is a real
     *  Kelvin gradient. Both are set as `--track-img` on the input. */
    sliderStyle(device, cap) {
      if (cap === 'color_temp') {
        const stops = [2200, 3000, 4000, 5000, 6500]
          .map(k => this.rgb(this.kelvinRgb(k))).join(', ');
        return `--track-img: linear-gradient(90deg, ${stops})`;
      }
      if (cap === 'brightness') return '--track-img: linear-gradient(90deg, var(--track), var(--accent))';
      return '';
    },

    // -------------------------------------------------------------- icons

    /* Which glyph a device gets. Actuators go by device type, sensors by what
     * they can read -- a bridge that calls everything a "sensor" still gets a
     * thermometer if it reports temperature. */
    iconKind(device) {
      if (device.kind === 'light' || device.kind === 'plug' || device.kind === 'switch') {
        return device.kind;
      }
      const caps = device.capabilities;
      if (caps.includes('contact')) return 'contact';
      if (caps.includes('occupancy')) return 'occupancy';
      if (caps.includes('temperature') || caps.includes('humidity')) return 'climate';
      if (caps.includes('illuminance')) return 'lux';
      return 'sensor';
    },

    /** Is this device in the state its icon animates for? Lamps and plugs: on.
     *  A contact sensor reports true for *closed*, so open is the active one. */
    iconActive(device) {
      const kind = this.iconKind(device);
      if (kind === 'contact') return this.shown(device.id, 'contact') === false;
      if (kind === 'occupancy') return this.shown(device.id, 'occupancy') === true;
      if (this.isSwitchable(device)) return this.isOn(device);
      return false;
    },

    // ------------------------------------------------------------ layout

    heroCaps(device) {
      const hero = device.capabilities.filter(c => this.HERO.includes(c));
      return hero.length ? hero : device.capabilities.filter(c => this.STATE_READ.includes(c));
    },

    /** A lamp's sliders only mean something while it is on. */
    sliderCaps(device) {
      if (!device.online) return [];
      if (this.isSwitchable(device) && !this.isOn(device)) return [];
      return device.writable.filter(c => c !== 'switch');
    },

    /** Staleness only means something for devices that are supposed to report
     *  on their own. A lamp is silent between commands by design -- for those,
     *  `online` (Matter Reachable) is the signal. */
    staleness(device) {
      this._tick; // reactive dependency so this re-renders on the interval
      const sensing = device.capabilities.filter(c => this.READ.includes(c));
      if (!sensing.length) return null;

      const times = sensing.map(c => this.states[this.key(device.id, c)]?.ts).filter(Boolean);
      if (!times.length) return { key: '_stale', text: 'ยังไม่มีข้อมูล', color: 'var(--faint)' };

      const age = Date.now() / 1000 - Math.max(...times);
      if (age < this.STALE_SECONDS) return null;   // fresh: say nothing
      const minutes = Math.floor(age / 60);
      const text = minutes < 90
        ? `เงียบมา ${minutes} นาที`
        : `เงียบมา ${Math.floor(age / 3600)} ชั่วโมง`;
      return { key: '_stale', text, color: 'var(--warn)' };
    },

    // ----------------------------------------------------------- control

    toggle(device) {
      this.send(device.id, 'switch', !this.shown(device.id, 'switch'));
    },

    /** Sliders fire on every pixel of drag; throttle so we do not flood the
     *  Zigbee mesh, which will drop commands (or the device) if hammered. */
    slide(device, cap, raw) {
      const k = this.key(device.id, cap), value = Number(raw);
      this.pending[k] = value;
      clearTimeout(this._timers[k]);
      this._timers[k] = setTimeout(() => this.send(device.id, cap, value), 220);
    },

    async send(id, cap, value) {
      this.pending[this.key(id, cap)] = value;
      try {
        await apiFetch(`/api/devices/${encodeURIComponent(id)}/command`,
          { json: { capability: cap, value } });
      } catch (err) {
        delete this.pending[this.key(id, cap)];
        this.toast('error', 'ส่งคำสั่งไม่ได้', err.message);
      }
    },

    // ------------------------------------------------------------ naming

    /* The hub only reports its own labels -- a product name at best, nothing
     * at all for the parts of a composed device -- so names are ours to keep.
     * "Unassigned" is a placeholder, not a room, so never seed the field
     * with it. */
    startEdit(device) {
      this.editing = device.id;
      this.draft = {
        name: device.name || '',
        room: device.room === this.UNASSIGNED ? '' : (device.room || ''),
      };
    },

    cancelEdit() { this.editing = null; this.saving = false; },

    async saveEdit(device) {
      const name = this.draft.name.trim();
      const room = this.draft.room.trim();
      // Sending the hub's own name back would store a pointless override that
      // then stops tracking the hub; treat "unchanged" as "no override".
      // hub_name only appears once an override exists, so on a device that has
      // none the hub's name is the one already on the card -- comparing against
      // hub_name alone would miss the commonest edit of all: set a room, leave
      // the name be.
      const hubName = device.hub_name ?? device.name ?? '';
      const hubRoom = device.hub_room ?? device.room ?? '';
      const body = {
        name: name === hubName ? '' : name,
        room: room === hubRoom ? '' : room,
      };
      this.saving = true;
      try {
        await apiFetch(`/api/devices/${encodeURIComponent(device.id)}/label`,
          { method: 'PATCH', json: body });
        this.editing = null;
      } catch (err) {
        this.toast('error', 'บันทึกชื่อไม่สำเร็จ', err.message);
      } finally {
        this.saving = false;
      }
    },

    /** Drop the name override only -- the room is the operator's either way,
     *  and clearing a field then saving is how you undo that one. */
    async resetLabel(device) {
      this.saving = true;
      try {
        await apiFetch(`/api/devices/${encodeURIComponent(device.id)}/label`,
          { method: 'PATCH', json: { name: '' } });
        this.editing = null;
      } catch (err) {
        this.toast('error', 'บันทึกชื่อไม่สำเร็จ', err.message);
      } finally {
        this.saving = false;
      }
    },

    // ------------------------------------------------------------ account

    account: { open: false, current: '', next: '', busy: false, error: '', done: false },
    loggingOut: false,

    /** End this session on the server and leave. */
    async logout() {
      await this._endSession('/api/auth/logout');
    },

    /** End every session, this one too, keeping the password. */
    async logoutAll() {
      if (!window.confirm('ให้ทุกเครื่องออกจากระบบ รวมเครื่องนี้ด้วย?')) return;
      await this._endSession('/api/auth/logout-all');
    },

    async _endSession(path) {
      this.loggingOut = true;
      // Stop the socket first: otherwise its close reads as a dropped
      // connection and the reconnect loop races the navigation below.
      if (this._ws) { this._ws.onclose = null; this._ws.close(); }
      try {
        await apiFetch(path, { method: 'POST' });
      } catch (err) {
        // 401 means there was no session to end, which is the goal anyway.
        // Anything else means the server never heard us: the session is still
        // live, and the cookie is HttpOnly, so this page cannot clear it
        // either. Saying "signed out" here would be the one lie that matters.
        if (err.status !== 401) {
          this.loggingOut = false;
          this.connect();
          this.toast('error', 'ยังไม่ได้ออกจากระบบ',
                     'ติดต่อเซิร์ฟเวอร์ไม่ได้ การเข้าสู่ระบบบนเครื่องนี้ยังใช้ได้อยู่ ลองใหม่อีกครั้ง');
          return;
        }
      }
      window.location.replace('/login');
    },

    async changePassword() {
      this.account.busy = true; this.account.error = ''; this.account.done = false;
      try {
        await apiFetch('/api/auth/password', {
          method: 'POST',
          json: { current: this.account.current, new: this.account.next },
        });
        this.account.done = true;
        this.account.current = ''; this.account.next = '';
      } catch (err) {
        // The server's reasons are English, shared with the CLI; say them in
        // the language of the rest of the panel. Anything unforeseen still
        // shows as sent rather than as a vague "failed".
        const detail = err.message || '';
        if (detail === 'not signed in') {
          window.location.replace('/login');
          return;
        }
        this.account.error =
            detail === 'current password is wrong' ? 'รหัสผ่านปัจจุบันไม่ถูกต้อง'
          : detail.startsWith('password must be at least') ? 'รหัสผ่านใหม่ต้องมีอย่างน้อย 10 ตัวอักษร'
          : detail.startsWith('password must not start or end') ? 'รหัสผ่านใหม่ต้องไม่ขึ้นต้นหรือลงท้ายด้วยช่องว่าง'
          : err.status === 0 ? 'ติดต่อเซิร์ฟเวอร์ไม่ได้'
          : detail || 'เปลี่ยนรหัสผ่านไม่สำเร็จ';
      } finally {
        this.account.busy = false;
      }
    },

    async refresh() {
      this.refreshing = true;
      try {
        await apiFetch('/api/devices/refresh', { method: 'POST' });
      } catch (err) {
        this.toast('error', 'สแกนไม่สำเร็จ', err.message);
      } finally {
        this.refreshing = false;
      }
    },

    // ----------------------------------------------------------- history

    /** Online devices that report temperature or humidity. */
    get climateSensors() {
      return this.devices.filter(d => d.online && this.CLIMATE.some(c => d.capabilities.includes(c)));
    },

    /** Capabilities worth a chart, for the chart's picker. */
    chartCaps(device) {
      if (!device) return [];
      if (this.page === 'overview') return this.CLIMATE.filter(c => device.capabilities.includes(c));
      return device.capabilities;
    },

    /** Point the chart at whatever the page on screen wants charted: the
     *  chosen climate sensor on the overview, the device on its own page. */
    ensureChart() {
      let target = null;
      if (this.historyEnabled) {
        if (this.page === 'overview') {
          target = this.climateSensors.find(d => d.id === this.overviewSensor) || this.climateSensors[0] || null;
        } else if (this.page === 'device') {
          target = this.detail;
        }
      }
      if (!target) {
        this.chart.device = null; this.chart.data = null;
        this.clearPlot();
        return;
      }
      const caps = this.chartCaps(target);
      if (this.chart.device?.id === target.id && caps.includes(this.chart.capability) && this.chart.data) {
        this.$nextTick(() => this.draw());
        return;
      }
      const preferred = ['temperature', 'humidity', 'illuminance', 'brightness', 'switch'];
      this.chart.device = target;
      this.chart.capability = preferred.find(c => caps.includes(c)) || caps[0];
      this.chart.data = null;
      this.clearPlot();
      this.loadChart();
    },

    clearPlot() {
      this.chart.drawn = false;
      this.chart.hover = null;
      this.chart.geo = emptyGeo();
    },

    pickSensor(device) { this.overviewSensor = device.id; this.ensureChart(); },
    pickCapability(cap) { this.chart.capability = cap; this.loadChart(); },
    pickRange(hours) { this.chart.hours = hours; this.loadChart(); },

    /** Redraw whenever the chart's box changes size -- including the moment
     *  it first lays out, which can be after the data arrived. */
    observePlot(el) {
      // Kept here rather than as an x-ref: the plot lives inside x-if
      // templates, and Alpine trips over refs removed with their template.
      this._plotEl = el;
      if (!window.ResizeObserver) return;
      new ResizeObserver(() => {
        if (!this.chart.data) return;
        cancelAnimationFrame(this._raf);
        this._raf = requestAnimationFrame(() => this.draw());
      }).observe(el);
    },

    async loadChart() {
      const { device, capability, hours } = this.chart;
      if (!device || !capability) return;
      const seq = ++this._reqSeq;
      this.chart.loading = true;
      this.chart.error = null;
      this.chart.hover = null;
      try {
        const body = await apiFetch(`/api/devices/${encodeURIComponent(device.id)}/history`
          + `?capability=${encodeURIComponent(capability)}&hours=${hours}`);
        if (seq !== this._reqSeq) return;   // a newer request already won
        this.chart.data = body;
        this.$nextTick(() => this.draw());
      } catch (err) {
        if (seq === this._reqSeq) { this.chart.error = err.message; this.chart.data = null; this.clearPlot(); }
      } finally {
        if (seq === this._reqSeq) this.chart.loading = false;
      }
    },

    /* The chart is plain SVG worked out here: a line, a band where the value
     * swung, a grid. It replaced Chart.js, 200 KB of script that every phone
     * had to download from the Pi and parse before the first card drew.
     *
     * Everything is computed into `chart.geo` and the template only binds it
     * (Alpine's x-for does not work inside <svg>, so the grid is one path and
     * the axis labels are HTML laid over it). */
    draw() {
      const data = this.chart.data;
      const box = this._plotEl;
      if (!data || !box || !box.isConnected) { this.clearPlot(); return; }
      // Not laid out yet: observePlot() calls again once the box has a size.
      if (!box.clientWidth) return;

      const W = Math.max(240, box.clientWidth);
      const H = box.clientHeight;
      const L = 44, R = W - 8, T = 10, B = H - 28;
      const end = Date.now() / 1000, start = end - data.hours * 3600;
      const x = (t) => L + ((t - start) / (end - start)) * (R - L);

      const pts = data.points;
      const all = pts.flatMap(p => [p.v, p.lo, p.hi]).filter(v => v !== null && v !== undefined);
      let lo, hi;
      if (data.boolean) { lo = 0; hi = 100; }
      else if (all.length) {
        lo = Math.min(...all); hi = Math.max(...all);
        const span = hi - lo || Math.abs(hi) * 0.1 || 1;
        lo -= span * 0.12; hi += span * 0.12;
      } else { lo = 0; hi = 1; }
      const y = (v) => B - ((v - lo) / (hi - lo)) * (B - T);
      const f = (n) => n.toFixed(1);

      // Line: broken where a bucket is empty, stepped for on/off values.
      const plotted = [];
      let line = '', prev = null;
      for (const p of pts) {
        if (p.v === null || p.v === undefined) { prev = null; continue; }
        const px = x(p.t), py = y(p.v);
        if (!prev) line += `M${f(px)} ${f(py)} `;
        else if (data.boolean) line += `L${f(prev.x)} ${f(py)} L${f(px)} ${f(py)} `;
        else line += `L${f(px)} ${f(py)} `;
        prev = { x: px, y: py };
        plotted.push({ x: px, y: py, v: p.v, t: p.t });
      }

      // Band: only runs of buckets whose spread beats the threshold, widened
      // by a bucket each side so it grows out of the line and settles back.
      let band = '';
      if (!data.boolean) {
        const limit = this.SWING[data.capability] ?? (hi - lo) * 0.15;
        const wide = (p) => p && p.lo !== null && p.hi !== null && p.hi - p.lo > limit;
        const usable = (p) => p && p.lo !== null && p.hi !== null;
        const runs = [];
        for (let i = 0; i < pts.length; i++) {
          if (!wide(pts[i])) continue;
          let j = i;
          while (j + 1 < pts.length && wide(pts[j + 1])) j++;
          const a = usable(pts[i - 1]) ? i - 1 : i;
          const b = usable(pts[j + 1]) ? j + 1 : j;
          if (runs.length && a <= runs[runs.length - 1][1]) runs[runs.length - 1][1] = b;
          else runs.push([a, b]);
          i = j;
        }
        for (const [a, b] of runs) {
          for (let k = a; k <= b; k++) band += `${k === a ? 'M' : 'L'}${f(x(pts[k].t))} ${f(y(pts[k].hi))} `;
          for (let k = b; k >= a; k--) band += `L${f(x(pts[k].t))} ${f(y(pts[k].lo))} `;
          band += 'Z ';
        }
      }

      const unit = this.CHART_UNIT[data.capability] || '';
      const ticks = data.boolean ? [100, 50, 0] : [0, 1, 2, 3].map(k => hi - ((hi - lo) * k) / 3);
      const digits = data.boolean || hi - lo >= 10 ? 0 : 1;
      const grid = ticks.map(v => `M${L} ${f(y(v))} H${R}`).join(' ');
      const yLabels = ticks.map(v => ({ key: v, top: y(v), text: v.toFixed(digits) + (data.boolean ? '%' : '') }));
      const xLabels = [0, 0.25, 0.5, 0.75, 1].map(frac => ({
        key: frac,
        left: L + frac * (R - L),
        align: frac === 0 ? 'start' : frac === 1 ? 'end' : 'center',
        text: frac === 1 ? 'ตอนนี้' : this.stamp(start + frac * (end - start), data.hours),
      }));

      this.chart.geo = {
        w: W, h: H, L, R, T, B, grid, line, band: band.trim(), yLabels, xLabels, unit,
        pts: plotted, last: plotted[plotted.length - 1] || null,
        stroke: `rgb(${this.COLOR[data.capability] || '139 124 246'})`,
        fill: `rgb(${this.COLOR[data.capability] || '139 124 246'} / .22)`,
      };
      this.chart.drawn = true;
    },

    /** Nearest plotted point to the pointer, for the readout. */
    hoverChart(event) {
      const g = this.chart.geo;
      if (!g || !g.pts.length) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const px = (event.clientX - rect.left) * (g.w / rect.width);
      let best = g.pts[0];
      for (const p of g.pts) if (Math.abs(p.x - px) < Math.abs(best.x - px)) best = p;
      const when = new Date(best.t * 1000).toLocaleString('th-TH', {
        day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
      });
      this.chart.hover = {
        x: best.x, y: best.y, when,
        text: `${Math.round(best.v * 10) / 10}${g.unit}`,
        // Keep the readout inside the box at either edge.
        left: Math.min(Math.max(best.x, 64), g.w - 64),
      };
    },

    stamp(epoch, hours) {
      const d = new Date(epoch * 1000);
      if (hours <= 24) return d.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit' });
      return d.toLocaleDateString('th-TH', { day: 'numeric', month: 'short' });
    },

    get chartStats() {
      const data = this.chart.data;
      if (!data || !data.points.length) return [];
      const values = data.points.map(p => p.v).filter(v => v !== null);
      if (!values.length) return [];
      const unit = this.CHART_UNIT[data.capability] || '';
      const stops = this.RAMP[data.capability];
      // Same ramp as the readings, so the min/max here read like the tiles do.
      const item = (label, n) => ({
        label,
        value: `${Math.round(n * 10) / 10}${unit}`,
        color: stops ? this.rgb(this.ramp(stops, n))
          : this.FLAT[data.capability] ? this.rgb(this.FLAT[data.capability])
          : 'var(--text)',
      });
      return [
        item('ต่ำสุด', Math.min(...data.points.map(p => p.lo ?? p.v).filter(v => v !== null))),
        item('เฉลี่ย', values.reduce((a, b) => a + b, 0) / values.length),
        item('สูงสุด', Math.max(...data.points.map(p => p.hi ?? p.v).filter(v => v !== null))),
      ];
    },

    get chartSource() {
      const data = this.chart.data;
      if (!data) return '';
      const bucket = data.bucket_seconds >= 3600
        ? `${Math.round(data.bucket_seconds / 3600)} ชม.`
        : `${Math.round(data.bucket_seconds / 60) || 1} นาที`;
      return `${data.points.length} จุด · ช่วงละ ${bucket}`;
    },

    toast(kind, title, body) {
      const id = ++this._toastSeq;
      this.toasts.push({ id, kind, title, body });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 6000);
    },

    // -------------------------------------------------------------- icons

    /** An icon's markup by name, for x-html. Only these constants ever go
     *  through x-html -- never a device name or anything else that arrives
     *  over the wire. */
    icon(name) { return ICONS[name] || ''; },
    devIcon(device) { return ICONS['dev-' + this.iconKind(device)]; },
  };
}

/** A chart with nothing drawn yet. */
function emptyGeo() {
  return { w: 0, h: 0, L: 0, R: 0, T: 0, B: 0, grid: '', line: '', band: '', yLabels: [], xLabels: [],
           unit: '', pts: [], last: null, stroke: 'none', fill: 'none' };
}

/* Icons, one copy each. Device glyphs carry the classes base.html animates
 * (glow, rays, lever, panel, ping); the rest are plain 24px strokes. */
const ICONS = (() => {
  const svg = (body) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
  return {
    'dev-light': svg('<g class="rays"><path d="M12 1.5v2M3.9 4.4l1.4 1.4M1.5 12.5h2M20.1 4.4l-1.4 1.4M22.5 12.5h-2"/></g>'
      + '<path class="glow" fill="currentColor" stroke="none" d="M12 4.5a6.2 6.2 0 0 0-3.7 11.2v2.1h7.4v-2.1A6.2 6.2 0 0 0 12 4.5Z"/>'
      + '<path d="M12 4.5a6.2 6.2 0 0 0-3.7 11.2v2.1h7.4v-2.1A6.2 6.2 0 0 0 12 4.5Z"/><path d="M9.8 20.5h4.4"/>'),
    'dev-plug': svg('<path class="glow" fill="currentColor" stroke="none" d="M6.5 9.5h11v3.2a5.5 5.5 0 0 1-11 0V9.5Z"/>'
      + '<path d="M6.5 9.5h11v3.2a5.5 5.5 0 0 1-11 0V9.5Z"/><path d="M9.3 9.5V4.2M14.7 9.5V4.2M12 18.2v2.6"/>'),
    'dev-switch': svg('<rect class="glow" x="6" y="3" width="12" height="18" rx="3" fill="currentColor" stroke="none"/>'
      + '<rect x="6" y="3" width="12" height="18" rx="3"/>'
      + '<rect class="lever" x="9" y="12.6" width="6" height="5.4" rx="1.6" fill="currentColor" stroke="none"/>'),
    'dev-contact': svg('<path d="M4 3.5h16v17H4z"/><path class="panel" d="M12 3.5h8v17h-8z" fill="currentColor" fill-opacity=".18"/>'
      + '<circle cx="13.6" cy="12" r=".9" fill="currentColor" stroke="none"/>'),
    'dev-occupancy': svg('<circle class="ping" cx="12" cy="12" r="9.5"/><circle cx="12" cy="8.4" r="2.9"/><path d="M5.8 20.2a6.2 6.2 0 0 1 12.4 0"/>'),
    'dev-climate': svg('<path d="M9.5 13.8V5a2.2 2.2 0 1 1 4.4 0v8.8a4.6 4.6 0 1 1-4.4 0Z"/><path d="M11.7 9.5v6.8" stroke-width="2.6"/>'),
    'dev-lux': svg('<circle class="glow" cx="12" cy="12" r="4.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="4.6"/>'
      + '<g class="rays"><path d="M12 1.8v2.4M12 19.8v2.4M1.8 12h2.4M19.8 12h2.4M4.8 4.8l1.7 1.7M17.5 17.5l1.7 1.7M19.2 4.8l-1.7 1.7M6.5 17.5l-1.7 1.7"/></g>'),
    'dev-sensor': svg('<circle cx="12" cy="12" r="2.6" fill="currentColor" stroke="none"/><path d="M6.6 6.6a7.6 7.6 0 0 0 0 10.8M17.4 6.6a7.6 7.6 0 0 1 0 10.8"/>'),

    home: svg('<path d="M3.5 10.5 12 3.8l8.5 6.7V20H3.5z"/><circle cx="12" cy="14" r="2.2" fill="currentColor" stroke="none"/>'),
    overview: svg('<rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/>'),
    devices: svg('<rect x="3" y="5" width="13" height="10" rx="1.8"/><path d="M7 19h5M9.5 15v4"/><rect x="17.5" y="9" width="4" height="10" rx="1.2"/>'),
    bell: svg('<path d="M6 16.5V11a6 6 0 1 1 12 0v5.5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>'),
    gear: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 13.5a7.6 7.6 0 0 0 0-3l2-1.6-2-3.4-2.4.9a7.6 7.6 0 0 0-2.6-1.5L14 2.5h-4l-.4 2.4A7.6 7.6 0 0 0 7 6.4l-2.4-.9-2 3.4 2 1.6a7.6 7.6 0 0 0 0 3l-2 1.6 2 3.4 2.4-.9a7.6 7.6 0 0 0 2.6 1.5l.4 2.4h4l.4-2.4a7.6 7.6 0 0 0 2.6-1.5l2.4.9 2-3.4z"/>'),
    sun: svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/>'),
    moon: svg('<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z"/>'),
    logout: svg('<path d="M9 4H5.5A1.5 1.5 0 0 0 4 5.5v13A1.5 1.5 0 0 0 5.5 20H9M15.5 16.5 20 12l-4.5-4.5M19.5 12H9.5"/>'),
    refresh: svg('<path d="M20 12a8 8 0 1 1-2.4-5.7M20 4v4.5h-4.5"/>'),
    pencil: svg('<path d="M14 5.5 18.5 10M4 20h3.6l10.6-10.6a2.2 2.2 0 0 0 0-3.1l-.5-.5a2.2 2.2 0 0 0-3.1 0L4 16.4z"/>'),
    pin: svg('<path d="M9 3.5h6l-1 5.5 3.5 3.5v1.5h-11V12.5L10 9z"/><path d="M12 14v6.5"/>'),
    back: svg('<path d="M15 5l-7 7 7 7"/>'),
    chevron: svg('<path d="M9 5l7 7-7 7"/>'),
    power: svg('<path d="M12 3v8"/><path d="M6.4 6.6a8 8 0 1 0 11.2 0"/>'),
    room: svg('<path d="M4 20V9.5L12 4l8 5.5V20"/><path d="M9.5 20v-6h5v6"/>'),
    wifi: svg('<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.8 16a4.8 4.8 0 0 1 6.4 0"/><circle cx="12" cy="19.2" r="1" fill="currentColor" stroke="none"/>'),
    thermo: svg('<path d="M9.5 13.8V5a2.2 2.2 0 1 1 4.4 0v8.8a4.6 4.6 0 1 1-4.4 0Z"/><path d="M11.7 9.5v6.8" stroke-width="2.6"/>'),
    'alert-offline': svg('<path d="M2.5 9a14 14 0 0 1 6-3.5M21.5 9a14 14 0 0 0-8.5-3.9M5.5 12.5a9.5 9.5 0 0 1 3-2M8.8 16a4.8 4.8 0 0 1 6.4 0M3 3l18 18"/>'),
    'alert-open': svg('<path d="M4 3.5h16v17H4z"/><path d="M12 3.5h8v17h-8z" fill="currentColor" fill-opacity=".18"/>'),
    'alert-battery': svg('<rect x="2.5" y="7" width="17" height="10" rx="2"/><path d="M21.5 10.5v3"/><path d="M5.5 10v4" stroke-width="2.4"/>'),
    'alert-stale': svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    check: svg('<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.2 2.4 2.4 4.8-5"/>'),
  };
})();
