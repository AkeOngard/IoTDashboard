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
    HERO: ['temperature', 'outdoor_temperature', 'humidity', 'illuminance'],
    // Promoted to hero when a device has nothing else to show.
    STATE_READ: ['contact', 'occupancy'],
    READ: ['temperature', 'humidity', 'illuminance', 'battery', 'contact', 'occupancy'],
    // What the overview chart can show: the house's climate.
    CLIMATE: ['temperature', 'humidity'],

    LABEL: {
      temperature: tr('อุณหภูมิ'), humidity: tr('ความชื้น'), illuminance: tr('ความสว่าง'),
      battery: tr('แบตเตอรี่'), contact: tr('หน้าต่าง/ประตู'), occupancy: tr('ตรวจจับคน'),
      brightness: tr('ความสว่างไฟ'), color_temp: tr('โทนแสง'), switch: tr('สวิตช์'),
      hvac_mode: tr('โหมดแอร์'), target_temperature: tr('อุณหภูมิที่ตั้ง'), fan_mode: tr('ความแรงลม'),
      swing_mode: tr('ทิศทางลม'), outdoor_temperature: tr('อุณหภูมินอกบ้าน'),
    },
    UNIT: { brightness: '%', color_temp: 'K', target_temperature: '°C' },
    /* Settings that are one word from a list -- an air conditioner's mode,
     * fan and louvre -- and what each word is called on screen. A device's own
     * `choices` says which of them it actually supports. */
    CHOICE_LABEL: {
      hvac_mode: { auto: tr('อัตโนมัติ##mode'), cool: tr('เย็น'), heat: tr('ร้อน'), dry: tr('ลดความชื้น'), fan: tr('พัดลม') },
      fan_mode: {
        auto: tr('อัตโนมัติ##mode'), quiet: tr('เงียบ'), low: tr('เบา'), medium_low: tr('ค่อนข้างเบา'),
        medium: tr('กลาง'), medium_high: tr('ค่อนข้างแรง'), high: tr('แรง'),
      },
      swing_mode: {
        off: tr('หยุดนิ่ง'), vertical: tr('ส่ายขึ้นลง'), horizontal: tr('ส่ายซ้ายขวา'), both: tr('ส่ายทุกทิศ'),
        fixed_1: tr('ตำแหน่ง {n}', { n: 1 }), fixed_2: tr('ตำแหน่ง {n}', { n: 2 }), fixed_3: tr('ตำแหน่ง {n}', { n: 3 }),
        fixed_4: tr('ตำแหน่ง {n}', { n: 4 }), fixed_5: tr('ตำแหน่ง {n}', { n: 5 }),
      },
    },
    // What an AC's icon glows in, by mode.
    MODE_COLOR: { cool: 'rgb(98 166 232)', heat: 'rgb(229 111 92)', dry: 'rgb(79 189 232)', fan: 'var(--text)', auto: 'var(--accent-text)' },
    // Only these get a slider; every other writable setting has its own control.
    SLIDERS: ['brightness', 'color_temp'],
    CHART_UNIT: {
      temperature: '°C', humidity: '%', illuminance: 'lx', battery: '%',
      brightness: '%', color_temp: 'K', switch: '%', contact: '%', occupancy: '%',
      target_temperature: '°C', outdoor_temperature: '°C',
    },
    // Chart line colours, matching the reading colours below.
    COLOR: {
      temperature: '230 180 79', humidity: '79 189 232', illuminance: '237 201 92',
      battery: '124 199 102', brightness: '139 124 246', color_temp: '169 139 245',
      switch: '139 124 246', contact: '169 139 245', occupancy: '169 139 245',
      target_temperature: '98 166 232', outdoor_temperature: '229 111 92',
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
    RANGE: { brightness: [1, 100, 1], color_temp: [2200, 6500, 100], target_temperature: [17, 30, 1] },
    RANGES: [
      { label: tr('1ชม'), hours: 1 }, { label: tr('6ชม'), hours: 6 },
      { label: tr('24ชม'), hours: 24 }, { label: tr('7วัน'), hours: 168 },
      { label: tr('30วัน'), hours: 720 },
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
    SWING: {
      temperature: 1, humidity: 5, battery: 2, illuminance: 50, brightness: 10, color_temp: 300,
      target_temperature: 1, outdoor_temperature: 1,
    },
    // How many devices the overview shows when none are pinned.
    DEFAULT_PINS: 6,

    // What the adapters call a device with no room. Matter has no rooms at
    // all, so on a Matter install every device starts here.
    UNASSIGNED: 'Unassigned',

    devices: [], states: {}, pending: {}, toasts: [],
    // Groups the operator made, as the server keeps them: {id, name, devices}.
    customGroups: [],
    // The group being created or edited (id null for a new one).
    groupEdit: { open: false, id: null, name: '', devices: [], busy: false, error: '' },
    // Automation rules, their recent runs and run counts, as the Pi has them.
    automations: { enabled: true, rules: [], log: [], stats: {} },
    // The rule open in the editor: a copy, so a broadcast never clobbers typing.
    ruleEdit: { open: false, id: null, draft: null, busy: false, error: '' },
    // Where we are: 'overview' | 'devices' | 'device' | 'groups' | 'automations' | 'alerts'.
    page: 'overview', detailId: null,
    // Devices page filter: 'all' | 'online' | 'attention'.
    filter: 'all',
    // Device ids pinned to the overview, kept in this browser.
    pins: [],
    // Which climate sensor the overview chart follows.
    overviewSensor: null,
    // Appearance, per browser: theme and how see-through the panels are (0-100).
    theme: 'dark', glass: 100, canBlur: true,
    // The device being renamed, and the draft being typed into it.
    editing: null, draft: { name: '', room: '' }, saving: false, refreshing: false,
    socket: false, adapterConnected: false, adapterName: '—', historyEnabled: false,
    // `geo` is always an object -- empty until drawn, `drawn` says which --
    // so the plot's bindings never meet a null while the plot is torn down.
    chart: {
      device: null, capability: null, hours: 24,
      loading: false, data: null, error: null, geo: emptyGeo(), drawn: false, hover: null,
    },
    _ws: null, _backoff: 1000, _timers: {}, _toastSeq: 0, _tick: 0, _reqSeq: 0, _raf: 0, _plotEl: null, _rowSeq: 0,

    init() {
      const root = document.documentElement;
      this.theme = root.getAttribute('data-theme') || 'dark';
      this.glass = parseInt(root.getAttribute('data-glass'), 10) || 0;
      this.canBlur = root.getAttribute('data-can-blur') !== 'no';
      this.syncThemeColor();
      try { this.pins = JSON.parse(localStorage.getItem('iot.pins') || '[]'); } catch (e) { this.pins = []; }
      this.connect();
      // Drives the clock, the greeting and the staleness labels. The first
      // tick lands on the next whole minute so the big clock turns over on
      // time; after that every 15 s stays on the :00/:15/:30/:45 grid.
      const tick = () => { this._tick++; };
      setTimeout(() => { tick(); setInterval(tick, 15000); },
                 60000 - (Date.now() % 60000));
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
        this.customGroups = msg.groups || [];
        if (msg.automations) this.automations = msg.automations;
        // A link straight to a rule (#/automations/<id>) waited for the list.
        if (this.page === 'automations' && !this.ruleEdit.open) {
          const id = location.hash.split('/')[2];
          if (id) this.openRule(decodeURIComponent(id));
        }
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
      } else if (msg.type === 'groups') {
        // Made, changed or deleted on some dashboard -- maybe this one.
        this.customGroups = msg.groups;
      } else if (msg.type === 'automations') {
        this.automations = { enabled: msg.enabled, rules: msg.rules, log: msg.log, stats: msg.stats };
      } else if (msg.type === 'automation_run') {
        this.automations.log = [msg.entry].concat(this.automations.log).slice(0, 100);
        this.automations.stats = msg.stats;
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
            this.toast('error', tr('สั่งงานไม่สำเร็จ'), `${this.nameOf(msg.device_id)} — ${msg.reason || tr('ไม่ทราบสาเหตุ')}`);
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
        this.page = ['devices', 'groups', 'automations', 'alerts'].includes(page) ? page : 'overview';
        // #/automations/<id> opens that rule.
        if (page === 'automations' && id) this.$nextTick(() => this.openRule(decodeURIComponent(id)));
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

    /** 0 is solid, 100 full glass, anything between a mix. Dragging only
     *  changes one CSS variable, so it follows the thumb without re-laying
     *  out the page; at 0 the blur switches off altogether. */
    setGlass(raw) {
      const level = this.canBlur ? Math.max(0, Math.min(100, Math.round(Number(raw)))) : 0;
      this.glass = level;
      const root = document.documentElement;
      root.style.setProperty('--glass', String(level / 100));
      root.setAttribute('data-look', level > 0 ? 'glass' : 'solid');
      root.setAttribute('data-glass', String(level));
      try {
        localStorage.setItem('iot.glass', String(level));
        localStorage.removeItem('iot.look');
      } catch (e) { /* private mode */ }
    },

    get glassLabel() {
      if (this.glass === 0) return tr('ทึบ');
      if (this.glass === 100) return tr('ใสเต็มที่');
      return this.glass + '%';
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
      if (h >= 5 && h < 11) return tr('อรุณสวัสดิ์');
      if (h >= 11 && h < 13) return tr('สวัสดีตอนเที่ยง');
      if (h >= 13 && h < 17) return tr('สวัสดีตอนบ่าย');
      if (h >= 17 && h < 21) return tr('สวัสดีตอนเย็น');
      if (h >= 21) return tr('สวัสดีตอนค่ำ');
      return tr('สวัสดีตอนดึก');
    },

    /** "21:14": the big clock on the desktop overview. */
    get clockNow() {
      this._tick;
      return new Date().toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' });
    },

    /** "พฤ. 2 ต.ค.": the date beside that clock. */
    get today() {
      this._tick;
      return new Date().toLocaleDateString(LOCALE, { weekday: 'short', day: 'numeric', month: 'short' });
    },

    /** "ออนไลน์ 9 จาก 10 อุปกรณ์ · ในบ้าน 21.5°C": the house in one line. */
    get houseLine() {
      const parts = [tr('ออนไลน์ {on} จาก {n} อุปกรณ์', { on: this.onlineCount, n: this.devices.length })];
      const t = this.climate('temperature');
      if (t) parts.push(tr('{place} {t}°C', { place: t.label, t: t.text }));
      return parts.join(' · ');
    },

    /** "เมื่อสักครู่", "5 นาทีที่แล้ว": when a device last said anything. */
    lastSeen(device) {
      this._tick;
      const times = device.capabilities.map(c => this.states[this.key(device.id, c)]?.ts).filter(Boolean);
      if (!times.length) return '—';
      const age = Date.now() / 1000 - Math.max(...times);
      if (age < 60) return tr('เมื่อสักครู่');
      if (age < 3600) return tr('{n} นาทีที่แล้ว', { n: Math.floor(age / 60) });
      if (age < 86400) return tr('{n} ชม.ที่แล้ว', { n: Math.floor(age / 3600) });
      return tr('{n} วันที่แล้ว', { n: Math.floor(age / 86400) });
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
      return room === this.UNASSIGNED ? tr('ยังไม่ระบุห้อง') : room;
    },

    /** One status line beats three indicators: report the worst thing that is
     *  true, and stay quiet when everything is fine. */
    get health() {
      if (!this.socket) return { tone: 'danger', text: tr('ขาดการเชื่อมต่อเซิร์ฟเวอร์') };
      if (!this.adapterConnected) return { tone: 'warn', text: tr('กำลังเชื่อมต่อ {name}', { name: this.adapterName }) };
      if (!this.historyEnabled) return { tone: 'muted', text: tr('ทำงานปกติ · ไม่บันทึกประวัติ') };
      return { tone: 'ok', text: tr('ทำงานปกติ') };
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
    isAc(device) { return device.kind === 'ac'; },
    isChoice(cap) { return cap in this.CHOICE_LABEL; },
    /** What a reading is called on a device's page. An AC's own thermometer
     *  reads the room, beside the outdoor one. */
    capLabel(device, cap) {
      if (cap === 'temperature' && device && this.isAc(device)) return tr('อุณหภูมิในห้อง');
      return this.LABEL[cap] || cap;
    },
    choiceText(cap, v) { return (this.CHOICE_LABEL[cap] || {})[v] || v; },
    /** The words a device takes for a choice setting, as it reported them. */
    choicesFor(device, cap) {
      return (device && device.choices && device.choices[cap]) || Object.keys(this.CHOICE_LABEL[cap] || {});
    },
    isOn(device) { return this.shown(device.id, 'switch') === true; },

    unit(id, cap) {
      const s = this.states[this.key(id, cap)];
      return s ? s.unit : (this.UNIT[cap] || '');
    },

    format(id, cap) {
      const v = this.shown(id, cap);
      if (v === null || v === undefined) return '—';
      if (cap === 'contact') return v ? tr('ปิดสนิท') : tr('เปิดอยู่##contact');
      if (cap === 'occupancy') return v ? tr('มีคน') : tr('ว่าง');
      if (this.isChoice(cap)) return this.choiceText(cap, v);
      return typeof v === 'number' ? v.toLocaleString(LOCALE) : v;
    },

    /** A reading with its unit, short: "27.1°C", "56%", "เปิดอยู่". */
    reading(id, cap) {
      const text = this.format(id, cap);
      if (text === '—' || this.STATE_READ.includes(cap)) return text;
      return text + (cap === 'temperature' ? '°C' : this.unit(id, cap));
    },

    /** The line under a device's name on a tile or a row. */
    summary(device) {
      if (!device.online) return tr('ออฟไลน์');
      if (this.isSwitchable(device)) return this.stateText(device);
      const caps = this.heroCaps(device);
      return caps.length ? caps.map(c => this.reading(device.id, c)).join(' · ') : '—';
    },

    /** The line under a switchable device's name. */
    stateText(device) {
      if (!device.online) return tr('ออฟไลน์');
      if (this.isPending(device.id, 'switch')) return tr('กำลังสั่ง…');
      const v = this.shown(device.id, 'switch');
      if (v === null || v === undefined) return tr('ไม่ทราบสถานะ');
      if (!v) return tr('ปิดอยู่');
      if (this.isAc(device)) {
        const t = this.shown(device.id, 'target_temperature');
        const mode = this.format(device.id, 'hvac_mode');
        return typeof t === 'number' ? tr('{mode} · {t}°C', { mode, t }) : mode;
      }
      const level = this.shown(device.id, 'brightness');
      return typeof level === 'number' ? tr('เปิด · {level}%', { level }) : tr('เปิดอยู่');
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
      let rows = this.devices
        .filter(d => d.online)
        .map(d => ({ d, v: this.value(d.id, cap) }))
        .filter(r => typeof r.v === 'number');
      // An AC's own thermometer sits in its air intake near the ceiling,
      // reads whole degrees and runs cold while the unit cools: a stand-in
      // while nothing else measures the room, never averaged with a sensor.
      const sensors = rows.filter(r => !this.isAc(r.d));
      if (sensors.length) rows = sensors;
      if (!rows.length) return null;
      const avg = rows.reduce((sum, r) => sum + r.v, 0) / rows.length;
      const coarse = rows.every(r => this.isAc(r.d));
      return {
        // No decimal a whole-degree sensor never measured.
        text: cap === 'temperature' && !coarse ? avg.toFixed(1) : String(Math.round(avg)),
        color: this.rgb(this.ramp(this.RAMP[cap], avg)),
        many: rows.length > 1,
        label: this.placeOf(rows.map(r => r.d)),
      };
    },

    /** Where readings come from, said as plainly as it can be: one room's
     *  name, one device's when it has no room, or "the house". */
    placeOf(devices) {
      const rooms = [...new Set(devices.map(d => d.room))];
      if (rooms.length === 1 && rooms[0] && rooms[0] !== this.UNASSIGNED) return this.roomLabel(rooms[0]);
      if (devices.length === 1) return devices[0].name;
      return tr('ในบ้าน');
    },

    /* Groups: the whole house, every room with something to switch, and the
     * groups the operator made. A group reads as on while anything in it is
     * on; switching it turns everything off, or, when all of it is already
     * off, everything on. */
    get groups() {
      return this.roomGroups.concat(this.madeGroups);
    },

    get roomGroups() {
      this._tick;
      const all = this.devices.filter(d => this.isSwitchable(d));
      const out = [];
      if (all.length) out.push(this.describeGroup({ key: 'all', name: tr('ทั้งหมด'), kind: 'all', members: all }));
      for (const room of this.rooms) {
        const members = all.filter(d => d.room === room);
        if (members.length) {
          out.push(this.describeGroup({ key: 'room:' + room, name: this.roomLabel(room), kind: 'room', members }));
        }
      }
      return out;
    },

    get madeGroups() {
      this._tick;
      return this.customGroups.map(g => {
        // Ids of devices that have left the fabric stay in the group, so
        // they count in "members" but not here.
        const devices = g.devices.map(id => this.devices.find(d => d.id === id)).filter(Boolean);
        return this.describeGroup({
          key: 'group:' + g.id, id: g.id, name: g.name, kind: 'custom',
          devices, members: devices.filter(d => this.isSwitchable(d)),
        });
      });
    },

    describeGroup(g) {
      const live = g.members.filter(d => d.online);
      const on = live.filter(d => this.isOn(d)).length;
      const sub = !g.members.length ? tr('ไม่มีอุปกรณ์ที่เปิดปิดได้')
        : !live.length ? tr('ออฟไลน์ทั้งหมด')
        : on === 0 ? tr('ปิดทั้งหมด')
        : on === live.length ? tr('เปิดทั้งหมด')
        : tr('เปิด {on} จาก {n}', { on, n: live.length });
      return { ...g, live, on, sub };
    },

    toggleGroup(group) {
      this.switchGroup(group, group.on === 0);
    },

    /** Switch a group on or off. The Pi sends the commands one at a time;
     *  this page only makes one request and then follows the usual
     *  pending -> confirmed messages for each device. */
    async switchGroup(group, value) {
      try {
        if (group.kind === 'custom') {
          await apiFetch(`/api/groups/${encodeURIComponent(group.id)}/switch`, { json: { value } });
        } else {
          await apiFetch('/api/devices/switch', { json: { devices: group.members.map(d => d.id), value } });
        }
      } catch (err) {
        this.toast('error', tr('สั่งทั้งกลุ่มไม่ได้'), err.message);
      }
    },

    // --------------------------------------------------------- group edit

    newGroup() {
      this.groupEdit = { open: true, id: null, name: '', devices: [], busy: false, error: '' };
    },

    editGroup(group) {
      const stored = this.customGroups.find(g => g.id === group.id);
      if (!stored) return;
      this.groupEdit = { open: true, id: stored.id, name: stored.name, devices: [...stored.devices], busy: false, error: '' };
    },

    toggleMember(device) {
      const ids = this.groupEdit.devices;
      this.groupEdit.devices = ids.includes(device.id) ? ids.filter(id => id !== device.id) : ids.concat(device.id);
    },

    /** Devices to choose from, by room. */
    get memberChoices() {
      return this.rooms.map(room => ({
        room, label: this.roomLabel(room),
        devices: this.byRoom(room).slice().sort((a, b) => a.name.localeCompare(b.name, 'th')),
      }));
    },

    async saveGroup() {
      const edit = this.groupEdit;
      const name = edit.name.trim();
      if (!name) { edit.error = tr('ตั้งชื่อกลุ่มก่อน'); return; }
      edit.busy = true; edit.error = '';
      try {
        const body = { name, devices: edit.devices };
        if (edit.id) {
          await apiFetch(`/api/groups/${encodeURIComponent(edit.id)}`, { method: 'PATCH', json: body });
        } else {
          await apiFetch('/api/groups', { json: body });
        }
        // The new list arrives over the socket, for every open dashboard.
        this.groupEdit.open = false;
      } catch (err) {
        edit.error = err.message;
      } finally {
        edit.busy = false;
      }
    },

    async deleteGroup() {
      const edit = this.groupEdit;
      if (!edit.id || !window.confirm(tr('ลบกลุ่ม "{name}"? อุปกรณ์ในกลุ่มไม่ได้หายไปไหน', { name: edit.name }))) return;
      edit.busy = true;
      try {
        await apiFetch(`/api/groups/${encodeURIComponent(edit.id)}`, { method: 'DELETE' });
        this.groupEdit.open = false;
      } catch (err) {
        edit.error = err.message;
      } finally {
        edit.busy = false;
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
                     title: tr('{name} ออฟไลน์', { name: d.name }), detail: tr('ไม่ตอบสนองจากเครือข่าย') });
          continue;
        }
        if (d.capabilities.includes('contact') && this.value(d.id, 'contact') === false) {
          out.push({ ...base, ...warn, key: d.id + ':open', kind: 'open',
                     title: tr('{name} เปิดอยู่##contact', { name: d.name }), detail: tr('หน้าต่างหรือประตูยังเปิด') });
        }
        const battery = this.value(d.id, 'battery');
        if (typeof battery === 'number' && battery <= this.LOW_BATTERY) {
          out.push({ ...base, ...warn, key: d.id + ':battery', kind: 'battery',
                     title: tr('แบต {name} เหลือ {n}%', { name: d.name, n: battery }), detail: tr('ควรเปลี่ยนแบตเตอรี่เร็ว ๆ นี้') });
        }
        const stale = this.staleness(d);
        if (stale && stale.color === 'var(--warn)') {
          out.push({ ...base, ...warn, key: d.id + ':stale', kind: 'stale',
                     title: `${d.name} ${stale.text}`, detail: tr('ไม่ได้ส่งค่ามาสักพัก') });
        }
      }
      return out;
    },

    hasIssue(device) { return this.issues.some(i => i.id === device.id); },

    /** Online / attention / offline, as a label and a pill style. */
    status(device) {
      if (!device.online) return { text: tr('ออฟไลน์'), cls: 'pill-muted' };
      if (this.hasIssue(device)) return { text: tr('ต้องดูแล'), cls: 'pill-warn' };
      return { text: tr('ออนไลน์'), cls: 'pill-ok' };
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
      const stops = this.RAMP[cap === 'outdoor_temperature' ? 'temperature' : cap];
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
      if (kind === 'ac' && this.iconActive(device)) {
        return `color: ${this.MODE_COLOR[this.shown(device.id, 'hvac_mode')] || 'var(--accent-text)'}; --glow: .22`;
      }
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
      if (['light', 'plug', 'switch', 'ac'].includes(device.kind)) return device.kind;
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
      return device.writable.filter(c => this.SLIDERS.includes(c));
    },

    /** Staleness only means something for devices that are supposed to report
     *  on their own. A lamp is silent between commands by design -- for those,
     *  `online` (Matter Reachable) is the signal. */
    staleness(device) {
      this._tick; // reactive dependency so this re-renders on the interval
      // An AC's thermometer is the unit's own and only reports when it moves
      // a whole degree; whether the unit is reachable is what `online` says.
      if (this.isAc(device)) return null;
      const sensing = device.capabilities.filter(c => this.READ.includes(c));
      if (!sensing.length) return null;

      const times = sensing.map(c => this.states[this.key(device.id, c)]?.ts).filter(Boolean);
      if (!times.length) return { key: '_stale', text: tr('ยังไม่มีข้อมูล'), color: 'var(--faint)' };

      const age = Date.now() / 1000 - Math.max(...times);
      if (age < this.STALE_SECONDS) return null;   // fresh: say nothing
      const minutes = Math.floor(age / 60);
      const text = minutes < 90
        ? tr('เงียบมา {n} นาที', { n: minutes })
        : tr('เงียบมา {n} ชั่วโมง', { n: Math.floor(age / 3600) });
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

    /** The AC set point, one degree at a time. Taps are gathered the same way
     *  as a slider's drag, so five quick taps send one command, not five. */
    nudge(device, delta) {
      const [lo, hi] = this.RANGE.target_temperature;
      const now = this.shown(device.id, 'target_temperature');
      const next = Math.max(lo, Math.min(hi, (typeof now === 'number' ? now : 25) + delta));
      if (next !== now) this.slide(device, 'target_temperature', next);
    },

    async send(id, cap, value) {
      this.pending[this.key(id, cap)] = value;
      try {
        await apiFetch(`/api/devices/${encodeURIComponent(id)}/command`,
          { json: { capability: cap, value } });
      } catch (err) {
        delete this.pending[this.key(id, cap)];
        this.toast('error', tr('ส่งคำสั่งไม่ได้'), err.message);
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
        this.toast('error', tr('บันทึกชื่อไม่สำเร็จ'), err.message);
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
        this.toast('error', tr('บันทึกชื่อไม่สำเร็จ'), err.message);
      } finally {
        this.saving = false;
      }
    },

    // -------------------------------------------------------- automations

    // Weekday chips, Monday first; values match Python's weekday().
    WEEKDAYS: [
      { d: 0, label: tr('จ') }, { d: 1, label: tr('อ') }, { d: 2, label: tr('พ') }, { d: 3, label: tr('พฤ') },
      { d: 4, label: tr('ศ') }, { d: 5, label: tr('ส') }, { d: 6, label: tr('อา') },
    ],
    // On/off readings and what each side is called.
    BINARY: {
      switch: { true: tr('เปิด##state'), false: tr('ปิด##state') },
      contact: { true: tr('ปิดสนิท'), false: tr('เปิดอยู่##contact') },
      occupancy: { true: tr('มีคน'), false: tr('ว่าง') },
    },
    // A sensible first value when a number capability is picked.
    NUMBER_DEFAULT: {
      temperature: 30, humidity: 70, illuminance: 100, battery: 20, brightness: 100, color_temp: 3000,
      target_temperature: 25, outdoor_temperature: 35,
    },
    OP_LABEL: { gt: tr('มากกว่า'), lt: tr('น้อยกว่า'), eq: tr('เท่ากับ'), ne: tr('ไม่เท่ากับ') },

    isBinary(cap) { return cap in this.BINARY; },

    get rules() { return this.automations.rules; },

    ruleStats(rule) { return this.automations.stats[rule.id] || { runs: 0, last: null }; },

    /** Rules that read or drive this device, for its page. */
    rulesFor(device) {
      const uses = (r) => r.trigger.device === device.id
        || r.conditions.some(c => c.device === device.id)
        || r.actions.some(a => a.device === device.id
          || (a.type === 'group' && (this.groupDeviceIds(a.group) || []).includes(device.id)));
      return this.rules.filter(uses);
    },

    groupDeviceIds(target) {
      if (target === 'all') return this.devices.map(d => d.id);
      if (target.startsWith('room:')) return this.byRoom(target.slice(5)).map(d => d.id);
      const g = this.customGroups.find(x => x.id === target);
      return g ? g.devices : null;
    },

    /** Everything an action can switch as a group. */
    get groupTargets() {
      return [{ value: 'all', label: tr('ทั้งบ้าน') }]
        .concat(this.rooms.map(r => ({ value: 'room:' + r, label: tr('ห้อง ') + this.roomLabel(r) })))
        .concat(this.customGroups.map(g => ({ value: g.id, label: tr('กลุ่ม ') + g.name })));
    },

    targetLabel(target) {
      const t = this.groupTargets.find(x => x.value === target);
      return t ? t.label : tr('กลุ่มที่ถูกลบไปแล้ว');
    },

    deviceById(id) { return this.devices.find(d => d.id === id) || null; },

    /** Capabilities to offer: anything readable for a test, only writable
     *  ones for an action. */
    capsFor(deviceId, writable) {
      const d = this.deviceById(deviceId);
      if (!d) return [];
      return writable ? d.writable : d.capabilities;
    },

    /** A new rule starts as the commonest case: a time, switching a group off. */
    newRule() {
      this.openEditor(null, {
        name: '', enabled: true,
        trigger: { type: 'time', at: '22:00', days: [] },
        conditions: [],
        actions: [{ type: 'group', group: 'all', value: false }],
      });
    },

    openRule(id) {
      const rule = this.rules.find(r => r.id === id);
      if (rule) this.openEditor(rule.id, rule);
    },

    openEditor(id, rule) {
      const draft = JSON.parse(JSON.stringify(rule));
      // A key per row, so removing one never makes the row below change
      // type in place. The server ignores the extra field.
      draft.conditions.forEach(c => { c._k = ++this._rowSeq; });
      draft.actions.forEach(a => { a._k = ++this._rowSeq; });
      this.ruleEdit = { open: true, id, draft, busy: false, error: '' };
      // On a phone the editor sits under the list: bring it into view once
      // its template has rendered (a tick later than $nextTick).
      if (window.innerWidth < 1024) {
        setTimeout(() => document.getElementById('rule-editor')?.scrollIntoView({ block: 'start' }), 60);
      }
    },

    closeEditor() { this.ruleEdit.open = false; },

    setTriggerType(type) {
      const first = this.devices[0];
      this.ruleEdit.draft.trigger = type === 'time'
        ? { type: 'time', at: '22:00', days: [] }
        : this.fixTest({ type: 'device', device: first ? first.id : '', capability: '', op: 'eq', value: true });
    },

    toggleDay(list, d) {
      if (!list) return;
      const i = list.indexOf(d);
      if (i >= 0) list.splice(i, 1); else list.push(d);
      list.sort();
    },

    /** After the device or capability of a test changes, keep it valid. */
    fixTest(test) {
      const caps = this.capsFor(test.device, false);
      if (!caps.includes(test.capability)) test.capability = caps[0] || '';
      if (this.isChoice(test.capability)) {
        if (!['eq', 'ne'].includes(test.op)) test.op = 'eq';
        const words = this.choicesFor(this.deviceById(test.device), test.capability);
        if (!words.includes(test.value)) test.value = words[0];
      } else if (this.isBinary(test.capability)) {
        test.op = 'eq';
        if (typeof test.value !== 'boolean') test.value = test.capability !== 'contact';
      } else {
        if (!['gt', 'lt'].includes(test.op)) test.op = 'gt';
        if (typeof test.value !== 'number') test.value = this.NUMBER_DEFAULT[test.capability] ?? 0;
      }
      return test;
    },

    fixAction(action) {
      const caps = this.capsFor(action.device, true);
      if (!caps.includes(action.capability)) action.capability = caps[0] || 'switch';
      if (action.capability === 'switch') {
        if (typeof action.value !== 'boolean') action.value = true;
      } else if (this.isChoice(action.capability)) {
        const words = this.choicesFor(this.deviceById(action.device), action.capability);
        if (!words.includes(action.value)) action.value = words[0];
      } else if (typeof action.value !== 'number') {
        action.value = this.NUMBER_DEFAULT[action.capability] ?? 50;
      }
      return action;
    },

    addCondition(type) {
      const c = this.ruleEdit.draft.conditions, _k = ++this._rowSeq;
      if (type === 'time_between') c.push({ _k, type, from: '18:00', to: '06:00' });
      else if (type === 'weekday') c.push({ _k, type, days: [0, 1, 2, 3, 4] });
      else {
        const first = this.devices[0];
        c.push(this.fixTest({ _k, type: 'device', device: first ? first.id : '', capability: '', op: 'eq', value: true }));
      }
    },

    addAction(type) {
      const a = this.ruleEdit.draft.actions, _k = ++this._rowSeq;
      if (type === 'group') a.push({ _k, type, group: 'all', value: false });
      else if (type === 'delay') a.push({ _k, type, seconds: 300 });
      else {
        const first = this.devices.find(d => d.writable.length);
        a.push(this.fixAction({ _k, type: 'device', device: first ? first.id : '', capability: '', value: true }));
      }
    },

    // ----- words

    daysText(days) {
      if (!days || !days.length || days.length === 7) return tr('ทุกวัน');
      if (days.join() === '0,1,2,3,4') return tr('วันธรรมดา');
      if (days.join() === '5,6') return tr('เสาร์-อาทิตย์');
      return days.map(d => this.WEEKDAYS[d].label).join(' ');
    },

    /** "หน้าต่างครัว เปิดอยู่", "เซนเซอร์ อุณหภูมิ มากกว่า 30°C". */
    testText(t) {
      const d = this.deviceById(t.device);
      const name = d ? d.name : tr('อุปกรณ์ที่หายไป');
      if (this.isBinary(t.capability)) {
        const word = this.BINARY[t.capability][String(t.value)];
        return t.op === 'ne' ? tr('{name} ไม่{word}', { name, word }) : tr('{name} {word}##is', { name, word });
      }
      if (this.isChoice(t.capability)) {
        const what = this.LABEL[t.capability], value = this.choiceText(t.capability, t.value);
        return t.op === 'ne' ? tr('{name} {what} ไม่ใช่ {value}', { name, what, value })
                             : tr('{name} {what} เป็น {value}', { name, what, value });
      }
      return `${name} ${this.LABEL[t.capability] || t.capability} ${this.OP_LABEL[t.op]} ${t.value}${this.CHART_UNIT[t.capability] || ''}`;
    },

    actionText(a) {
      if (a.type === 'delay') return tr('รอ {n} นาที', { n: Math.round(a.seconds / 60) || 1 });
      if (a.type === 'group') return tr(a.value ? 'เปิด{target}' : 'ปิด{target}', { target: this.targetLabel(a.group) });
      const d = this.deviceById(a.device);
      const name = d ? d.name : tr('อุปกรณ์ที่หายไป');
      if (a.capability === 'switch') return tr(a.value ? 'เปิด {name}' : 'ปิด {name}', { name });
      if (a.capability === 'target_temperature') {
        return tr('ตั้งอุณหภูมิ {name} เป็น {value}', { name, value: a.value + '°C' });
      }
      const value = this.isChoice(a.capability)
        ? this.choiceText(a.capability, a.value) : a.value + (this.UNIT[a.capability] || '');
      return tr('ตั้ง{what} {name} เป็น {value}', { what: this.LABEL[a.capability], name, value });
    },

    /** The whole rule in one sentence. */
    ruleText(rule) {
      const t = rule.trigger;
      const when = t.type === 'time' ? tr('เวลา {at} {days}', { at: t.at, days: this.daysText(t.days) })
        : tr('เมื่อ {test}', { test: this.testText(t) });
      const ifs = rule.conditions.map(c => c.type === 'time_between' ? tr('ช่วง {from}–{to}', { from: c.from, to: c.to })
        : c.type === 'weekday' ? tr('เป็น{days}', { days: this.daysText(c.days) }) : this.testText(c));
      const thens = rule.actions.map(a => this.actionText(a));
      return when + (ifs.length ? tr(' และถ้า ') + ifs.join(', ') : '') + ' → ' + (thens.join(' → ') || '…');
    },

    runText(entry) {
      const why = { time: tr('ตามเวลา'), device: tr('อุปกรณ์เปลี่ยน'), test: tr('ลองสั่ง') }[entry.why] || entry.why;
      const skipped = { time_between: tr('นอกช่วงเวลา'), weekday: tr('ไม่ใช่วันที่กำหนด'), device: tr('สถานะอุปกรณ์ไม่ตรง') };
      const result = {
        ran: tr('ทำงานแล้ว'),
        skipped: tr('ข้าม — ') + (skipped[entry.detail] || tr('เงื่อนไขไม่ตรง')),
        failed: tr('ไม่สำเร็จ — ') + entry.detail,
        limited: tr('หยุดไว้ — ทำงานถี่เกินไป อาจวนกับกฎอื่น'),
        cancelled: tr('ยกเลิก — กฎถูกแก้ระหว่างรอ'),
      }[entry.result] || entry.result;
      return `${why} · ${result}`;
    },

    runColor(entry) {
      return { ran: 'var(--ok)', skipped: 'var(--faint)', cancelled: 'var(--faint)' }[entry.result] || 'var(--warn)';
    },

    clock(ts) {
      return new Date(ts * 1000).toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' });
    },

    // ----- saving

    async saveRule() {
      const edit = this.ruleEdit;
      if (!edit.draft.name.trim()) { edit.error = tr('ตั้งชื่อกฎก่อน'); return; }
      edit.busy = true; edit.error = '';
      try {
        const body = { ...edit.draft, name: edit.draft.name.trim() };
        const saved = edit.id
          ? await apiFetch(`/api/automations/${encodeURIComponent(edit.id)}`, { method: 'PATCH', json: body })
          : await apiFetch('/api/automations', { json: body });
        // Stay on the rule, now saved: the list itself arrives over the socket.
        this.openEditor(saved.id, saved);
        this.toast('ok', tr('บันทึกกฎแล้ว'), saved.name);
      } catch (err) {
        edit.error = err.message;
      } finally {
        edit.busy = false;
      }
    },

    async setRuleEnabled(rule, enabled) {
      try {
        await apiFetch(`/api/automations/${encodeURIComponent(rule.id)}`, { method: 'PATCH', json: { enabled } });
        if (this.ruleEdit.id === rule.id && this.ruleEdit.draft) this.ruleEdit.draft.enabled = enabled;
      } catch (err) {
        this.toast('error', tr('เปลี่ยนสถานะกฎไม่ได้'), err.message);
      }
    },

    async testRule() {
      try {
        await apiFetch(`/api/automations/${encodeURIComponent(this.ruleEdit.id)}/run`, { method: 'POST' });
        // The log entry only lands when the run ends, which a "wait" can
        // put minutes away: say now that it started.
        const waits = this.ruleEdit.draft.actions.some(a => a.type === 'delay');
        this.toast('ok', tr('เริ่มทำตามกฎแล้ว'),
                   waits ? tr('กฎนี้มีขั้นรอ ผลจะขึ้นในบันทึกเมื่อทำครบ') : tr('ผลจะขึ้นในบันทึกในอีกครู่'));
      } catch (err) {
        this.toast('error', tr('ลองสั่งไม่ได้'), err.message);
      }
    },

    async deleteRule() {
      const edit = this.ruleEdit;
      if (!edit.id || !window.confirm(tr('ลบกฎ "{name}"?', { name: edit.draft.name }))) return;
      edit.busy = true;
      try {
        await apiFetch(`/api/automations/${encodeURIComponent(edit.id)}`, { method: 'DELETE' });
        this.ruleEdit.open = false;
      } catch (err) {
        edit.error = err.message;
      } finally {
        edit.busy = false;
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
      if (!window.confirm(tr('ให้ทุกเครื่องออกจากระบบ รวมเครื่องนี้ด้วย?'))) return;
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
          this.toast('error', tr('ยังไม่ได้ออกจากระบบ'),
                     tr('ติดต่อเซิร์ฟเวอร์ไม่ได้ การเข้าสู่ระบบบนเครื่องนี้ยังใช้ได้อยู่ ลองใหม่อีกครั้ง'));
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
            detail === 'current password is wrong' ? tr('รหัสผ่านปัจจุบันไม่ถูกต้อง')
          : detail.startsWith('password must be at least') ? tr('รหัสผ่านใหม่ต้องมีอย่างน้อย 10 ตัวอักษร')
          : detail.startsWith('password must not start or end') ? tr('รหัสผ่านใหม่ต้องไม่ขึ้นต้นหรือลงท้ายด้วยช่องว่าง')
          : err.status === 0 ? tr('ติดต่อเซิร์ฟเวอร์ไม่ได้')
          : detail || tr('เปลี่ยนรหัสผ่านไม่สำเร็จ');
      } finally {
        this.account.busy = false;
      }
    },

    async refresh() {
      this.refreshing = true;
      try {
        await apiFetch('/api/devices/refresh', { method: 'POST' });
      } catch (err) {
        this.toast('error', tr('สแกนไม่สำเร็จ'), err.message);
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
      // A mode is a word, not a line on a chart; history only keeps numbers.
      return device.capabilities.filter(c => !this.isChoice(c));
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
        text: frac === 1 ? tr('ตอนนี้') : this.stamp(start + frac * (end - start), data.hours),
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
      const when = new Date(best.t * 1000).toLocaleString(LOCALE, {
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
      if (hours <= 24) return d.toLocaleTimeString(LOCALE, { hour: '2-digit', minute: '2-digit' });
      return d.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short' });
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
        item(tr('ต่ำสุด'), Math.min(...data.points.map(p => p.lo ?? p.v).filter(v => v !== null))),
        item(tr('เฉลี่ย'), values.reduce((a, b) => a + b, 0) / values.length),
        item(tr('สูงสุด'), Math.max(...data.points.map(p => p.hi ?? p.v).filter(v => v !== null))),
      ];
    },

    get chartSource() {
      const data = this.chart.data;
      if (!data) return '';
      const bucket = data.bucket_seconds >= 3600
        ? tr('{n} ชม.', { n: Math.round(data.bucket_seconds / 3600) })
        : tr('{n} นาที', { n: Math.round(data.bucket_seconds / 60) || 1 });
      return tr('{n} จุด · ช่วงละ {bucket}', { n: data.points.length, bucket });
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
    // A wall unit: its body glows, the airflow springs out when it runs.
    'dev-ac': svg('<rect class="glow" x="2.5" y="4.5" width="19" height="9" rx="2.2" fill="currentColor" stroke="none"/>'
      + '<rect x="2.5" y="4.5" width="19" height="9" rx="2.2"/><path d="M6 10.5h12"/>'
      + '<g class="rays"><path d="M7.5 16.5c-.6 1.2-.6 2.2 0 3.5M12 16.5v4M16.5 16.5c.6 1.2.6 2.2 0 3.5"/></g>'),
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
    group: svg('<path d="m12 3.5 8.5 4.3-8.5 4.3-8.5-4.3z"/><path d="m3.5 12 8.5 4.3 8.5-4.3"/><path d="m3.5 16.2 8.5 4.3 8.5-4.3"/>'),
    plus: svg('<path d="M12 5v14M5 12h14"/>'),
    minus: svg('<path d="M5 12h14"/>'),
    flow: svg('<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H14a3.5 3.5 0 0 1 3.5 3.5V15.5"/><path d="m15 13 2.5 2.5L20 13"/>'),
    play: svg('<path d="M8 5.5v13l10.5-6.5z"/>'),
    close: svg('<path d="M6 6l12 12M18 6 6 18"/>'),
    wifi: svg('<path d="M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.8 16a4.8 4.8 0 0 1 6.4 0"/><circle cx="12" cy="19.2" r="1" fill="currentColor" stroke="none"/>'),
    thermo: svg('<path d="M9.5 13.8V5a2.2 2.2 0 1 1 4.4 0v8.8a4.6 4.6 0 1 1-4.4 0Z"/><path d="M11.7 9.5v6.8" stroke-width="2.6"/>'),
    'alert-offline': svg('<path d="M2.5 9a14 14 0 0 1 6-3.5M21.5 9a14 14 0 0 0-8.5-3.9M5.5 12.5a9.5 9.5 0 0 1 3-2M8.8 16a4.8 4.8 0 0 1 6.4 0M3 3l18 18"/>'),
    'alert-open': svg('<path d="M4 3.5h16v17H4z"/><path d="M12 3.5h8v17h-8z" fill="currentColor" fill-opacity=".18"/>'),
    'alert-battery': svg('<rect x="2.5" y="7" width="17" height="10" rx="2"/><path d="M21.5 10.5v3"/><path d="M5.5 10v4" stroke-width="2.4"/>'),
    'alert-stale': svg('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'),
    check: svg('<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.2 2.4 2.4 4.8-5"/>'),
  };
})();
