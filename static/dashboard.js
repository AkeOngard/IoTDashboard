/* Dashboard component (doc §10). Registered as a global factory so Alpine,
 * which loads deferred, finds it at init time.
 *
 * Display principle: show a number only where a number is the point, and show
 * secondary facts (battery, staleness) only when they are worth a glance.
 *
 * Everything on this page is worked out here, in the viewer's browser, from
 * the one WebSocket stream the server already sends: the overview, the
 * "needs attention" list and the history chart cost the Pi nothing beyond
 * the messages it was sending anyway.
 */
function dashboard() {
  return {
    // Readings that deserve the large type.
    HERO: ['temperature', 'humidity', 'illuminance'],
    // Promoted to hero when a device has nothing else to show.
    STATE_READ: ['contact', 'occupancy'],
    READ: ['temperature', 'humidity', 'illuminance', 'battery', 'contact', 'occupancy'],

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
      battery: '124 199 102', brightness: '75 189 133', color_temp: '169 139 245',
      switch: '75 189 133', contact: '169 139 245', occupancy: '169 139 245',
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
    // State colours as rgb triples, for tints that need an alpha.
    TONE: { on: [75, 189, 133], open: [230, 169, 78], occupied: [169, 139, 245] },

    // Endpoints of the Kelvin gradient used by lamp tints and the CT slider.
    K_WARM: [255, 172, 92],
    K_COOL: [198, 224, 255],
    RANGE: { brightness: [1, 100, 1], color_temp: [2200, 6500, 100] },
    RANGES: [
      { label: '1ชม', hours: 1 }, { label: '6ชม', hours: 6 },
      { label: '24ชม', hours: 24 }, { label: '7วัน', hours: 168 },
      { label: '30วัน', hours: 720 },
    ],
    // Thresholds that tint a reading amber. Tune per deployment.
    ALERT: { temperature: [null, 32], humidity: [30, 70], battery: [20, null] },
    // At or below this a battery makes the "needs attention" list.
    LOW_BATTERY: 20,
    // A sensor quieter than this has something wrong with it, not a stable value:
    // the recorder heartbeats every 5 minutes even when nothing changes.
    STALE_SECONDS: 600,
    /* The chart shades a bucket's min-to-max only where the spread is wider
     * than this: a swing worth seeing, like a window opened for ten minutes.
     * Below it the spread is sensor noise, and a band there is just a glow
     * tracing the line. Capabilities not listed use 15% of the chart's span. */
    SWING: { temperature: 1, humidity: 5, battery: 2, illuminance: 50, brightness: 10, color_temp: 300 },
    // Gap between the commands of "turn everything off", so a room full of
    // lamps does not hit the Zigbee mesh in one burst.
    BULK_GAP_MS: 150,

    devices: [], states: {}, pending: {}, toasts: [],
    // Which room the chips show; 'all' for every room.
    room: 'all',
    // The card being renamed, and the draft being typed into it.
    editing: null, draft: { name: '', room: '' }, saving: false, refreshing: false,
    socket: false, adapterConnected: false, adapterName: '—', historyEnabled: false,
    chart: {
      open: false, device: null, capability: null, hours: 24,
      loading: false, data: null, error: null, geo: null, hover: null,
    },
    _ws: null, _backoff: 1000, _timers: {}, _toastSeq: 0, _tick: 0, _reqSeq: 0, _raf: 0,

    init() {
      try { this.room = localStorage.getItem('iot.room') || 'all'; } catch (e) { /* private mode */ }
      this.connect();
      // Drives the relative staleness labels.
      setInterval(() => { this._tick++; }, 15000);
      // The chart is drawn to its box's real size, so redraw whenever that
      // changes -- including the moment the dialog first lays out, which is
      // after the data may already have arrived.
      this.$nextTick(() => {
        if (!window.ResizeObserver || !this.$refs.plot) return;
        new ResizeObserver(() => {
          if (!this.chart.open || !this.chart.data) return;
          cancelAnimationFrame(this._raf);
          this._raf = requestAnimationFrame(() => this.draw());
        }).observe(this.$refs.plot);
      });
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

    get rooms() {
      return [...new Set(this.devices.map(d => d.room))].sort();
    },

    /** The rooms the chips leave on screen. A remembered room that has since
     *  been emptied or renamed falls back to showing everything. */
    get visibleRooms() {
      return this.rooms.includes(this.room) ? [this.room] : this.rooms;
    },

    pickRoom(room) {
      this.room = room;
      try { localStorage.setItem('iot.room', room); } catch (e) { /* private mode */ }
    },

    /** Controls first, readings after: on a phone the controls pair up two to
     *  a row and the sensors take the full width underneath. */
    roomDevices(room) {
      const list = this.byRoom(room);
      return list.filter(d => this.isSwitchable(d)).concat(list.filter(d => !this.isSwitchable(d)));
    },

    /** "2 on · 27.4°C · 58%": what a glance at a room heading should tell you. */
    roomMeta(room) {
      const list = this.byRoom(room);
      const parts = [];
      if (list.some(d => this.isSwitchable(d))) {
        const on = list.filter(d => d.online && this.isOn(d)).length;
        parts.push(on ? `เปิดอยู่ ${on}` : 'ปิดหมด');
      }
      const climate = list.find(d => typeof this.value(d.id, 'temperature') === 'number');
      if (climate) {
        let text = `${this.value(climate.id, 'temperature')}°C`;
        const h = this.value(climate.id, 'humidity');
        if (typeof h === 'number') text += ` · ${h}%`;
        parts.push(text);
      }
      return parts.join(' · ');
    },

    /** One status line beats three indicators: report the worst thing that is
     *  true, and stay quiet when everything is fine. */
    get health() {
      if (!this.socket) return { tone: 'danger', text: 'ขาดการเชื่อมต่อเซิร์ฟเวอร์' };
      if (!this.adapterConnected) return { tone: 'warn', text: `กำลังเชื่อมต่อ ${this.adapterName}` };
      if (!this.historyEnabled) return { tone: 'muted', text: 'ทำงานปกติ · ไม่บันทึกประวัติ' };
      return { tone: 'on', text: 'ทำงานปกติ' };
    },

    get healthStyle() {
      return {
        on:     'background-color: rgb(75 189 133 / .12); color: var(--on-text)',
        warn:   'background-color: rgb(230 169 78 / .12); color: var(--warn-text)',
        danger: 'background-color: rgb(236 122 102 / .12); color: var(--danger-text)',
        muted:  'background-color: var(--surface-2); color: var(--muted)',
      }[this.health.tone];
    },

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

    alert(id, cap) {
      const range = this.ALERT[cap], v = this.value(id, cap);
      if (!range || typeof v !== 'number') return false;
      return (range[0] !== null && v < range[0]) || (range[1] !== null && v > range[1]);
    },

    // ----------------------------------------------------------- overview

    /** Online devices with an on/off switch: what "turn everything off" acts on. */
    get switchable() {
      return this.devices.filter(d => d.online && this.isSwitchable(d));
    },

    get onCount() {
      return this.switchable.filter(d => this.isOn(d)).length;
    },

    /** Average and extreme of one reading across the house, or null when no
     *  online device reports it. */
    climate(cap) {
      const rows = this.devices
        .filter(d => d.online)
        .map(d => ({ d, v: this.value(d.id, cap) }))
        .filter(r => typeof r.v === 'number');
      if (!rows.length) return null;
      const avg = rows.reduce((sum, r) => sum + r.v, 0) / rows.length;
      const top = rows.reduce((m, r) => (r.v > m.v ? r : m));
      const color = (v) => this.rgb(this.ramp(this.RAMP[cap], v));
      return {
        text: cap === 'temperature' ? avg.toFixed(1) : String(Math.round(avg)),
        color: color(avg),
        many: rows.length > 1,
        room: rows[0].d.room,
        topRoom: top.d.room,
        topText: cap === 'temperature' ? top.v.toFixed(1) : String(Math.round(top.v)),
        topColor: color(top.v),
      };
    },

    /** Everything worth a look, gathered in one place so nobody has to scan
     *  every card for it: a window left open, a device gone quiet or
     *  unreachable, a battery about to die. */
    get issues() {
      this._tick; // staleness moves with the clock
      const out = [];
      for (const d of this.devices) {
        if (!d.online) {
          out.push({ key: d.id + ':offline', text: `${d.name} ออฟไลน์`, room: d.room, color: 'var(--danger)' });
          continue;
        }
        if (d.capabilities.includes('contact') && this.value(d.id, 'contact') === false) {
          out.push({ key: d.id + ':open', text: `${d.name} เปิดอยู่`, room: d.room, color: 'var(--warn)' });
        }
        const battery = this.value(d.id, 'battery');
        if (typeof battery === 'number' && battery <= this.LOW_BATTERY) {
          out.push({ key: d.id + ':battery', text: `แบต ${d.name} เหลือ ${battery}%`, room: d.room, color: 'var(--warn)' });
        }
        const stale = this.staleness(d);
        if (stale && stale.color === 'var(--warn)') {
          out.push({ key: d.id + ':stale', text: `${d.name} ${stale.text}`, room: d.room, color: 'var(--warn)' });
        }
      }
      return out;
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
    rgba(c, a) { return `rgb(${c[0]} ${c[1]} ${c[2]} / ${a})`; },

    /** The colour a reading should be drawn in. */
    readingColor(id, cap) {
      const v = this.shown(id, cap);
      if (v === null || v === undefined) return 'var(--faint)';
      if (cap === 'contact')   return v ? 'var(--text)' : 'var(--warn)';   // open is worth noticing
      if (cap === 'occupancy') return v ? 'var(--c-state)' : 'var(--text)';
      if (this.FLAT[cap]) return this.rgb(this.FLAT[cap]);
      const stops = this.RAMP[cap];
      if (stops && typeof v === 'number') return this.rgb(this.ramp(stops, v));
      return 'var(--text)';
    },

    /* The one colour a card is "about" right now, or null when it is at rest:
     * the light a lamp is actually making, green for a live plug, amber for
     * an open window, the temperature for a climate sensor. The icon, its
     * box, the card's tint and the toggle all draw from this. */
    tone(device) {
      if (!device.online) return null;
      if (this.isSwitchable(device)) {
        if (!this.isOn(device)) return null;
        return device.kind === 'light'
          ? this.kelvinRgb(this.shown(device.id, 'color_temp'))
          : this.TONE.on;
      }
      const kind = this.iconKind(device);
      if (kind === 'contact') return this.shown(device.id, 'contact') === false ? this.TONE.open : null;
      if (kind === 'occupancy') return this.shown(device.id, 'occupancy') === true ? this.TONE.occupied : null;
      if (kind === 'climate') {
        const t = this.value(device.id, 'temperature');
        if (typeof t === 'number') return this.ramp(this.RAMP.temperature, t);
        const h = this.value(device.id, 'humidity');
        return typeof h === 'number' ? this.ramp(this.RAMP.humidity, h) : null;
      }
      if (kind === 'lux') {
        return typeof this.value(device.id, 'illuminance') === 'number' ? this.FLAT.illuminance : null;
      }
      return null;
    },

    /** A lit lamp tints its own card with the colour it is making; an open
     *  window only outlines it. Low alpha, so a room full of lights still
     *  reads as a calm grid rather than a set of coloured billboards. */
    cardStyle(device) {
      const tone = this.tone(device);
      if (!tone) return '';
      if (this.isSwitchable(device)) {
        return `background-color: ${this.rgba(tone, .08)}; border-color: ${this.rgba(tone, .32)}`;
      }
      if (this.iconKind(device) === 'contact') return `border-color: ${this.rgba(tone, .32)}`;
      return '';
    },

    iconBoxStyle(device) {
      const tone = this.tone(device);
      return tone ? `background-color: ${this.rgba(tone, .16)}` : '';
    },

    /** The icon's colour and how hard it glows. A warm lamp at 10% looks like
     *  a warm lamp at 10%. */
    iconStyle(device) {
      const tone = this.tone(device);
      if (!tone) return '';
      let glow = 0.2;
      if (device.kind === 'light') {
        const level = this.shown(device.id, 'brightness');
        glow = 0.12 + 0.30 * ((typeof level === 'number' ? level : 100) / 100);
      }
      return `color: ${this.rgb(tone)}; --glow: ${glow.toFixed(2)}`;
    },

    /** An on toggle wears the device's colour, darkened so the white knob
     *  stays legible at 6500K. Returns '' when off, letting the class win. */
    toggleStyle(device) {
      const tone = this.tone(device);
      if (!tone || !this.isSwitchable(device)) return '';
      return `background-color: rgb(${Math.round(tone[0] * .74)} ${Math.round(tone[1] * .74)} ${Math.round(tone[2] * .8)})`;
    },

    /** Brightness fades up to the lamp's colour; colour temperature is a real
     *  Kelvin gradient. Both are set as `--track-img` on the input. */
    sliderStyle(device, cap) {
      if (cap === 'color_temp') {
        const stops = [2200, 3000, 4000, 5000, 6500]
          .map(k => this.rgb(this.kelvinRgb(k))).join(', ');
        return `--track-img: linear-gradient(90deg, ${stops})`;
      }
      if (cap === 'brightness') {
        const lamp = device.capabilities.includes('color_temp')
          ? this.rgb(this.kelvinRgb(this.shown(device.id, 'color_temp')))
          : this.rgb(this.K_WARM);
        return `--track-img: linear-gradient(90deg, var(--track), ${lamp})`;
      }
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

    /** Small print under a card: the readings that did not earn large type,
     *  plus a staleness warning when the device has gone quiet. */
    footnote(device) {
      if (!device.online) return [{ key: '_offline', text: 'ออฟไลน์ — ไม่ตอบสนอง', color: 'var(--danger)' }];
      const hero = this.heroCaps(device);
      const notes = [];

      for (const cap of device.capabilities) {
        if (hero.includes(cap) || !this.READ.includes(cap)) continue;
        const v = this.value(device.id, cap);
        if (v === null) continue;
        const low = cap === 'battery' && typeof v === 'number' && v <= this.LOW_BATTERY;
        const text = cap === 'battery'
          ? (low ? `แบตใกล้หมด ${v}%` : `แบต ${v}%`)
          : `${this.LABEL[cap]} ${this.format(device.id, cap)}`;
        notes.push({ key: cap, text, color: low ? 'var(--warn)' : 'var(--faint)' });
      }

      const stale = this.staleness(device);
      if (stale) notes.push(stale);
      return notes;
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

    /** Switch off every device in `list` that is on, one at a time. */
    async turnOff(list) {
      for (const d of list.filter(x => this.isOn(x))) {
        this.send(d.id, 'switch', false);
        await new Promise(r => setTimeout(r, this.BULK_GAP_MS));
      }
    },

    allOff() { this.turnOff(this.switchable); },
    roomOff(room) { this.turnOff(this.switchable.filter(d => d.room === room)); },

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
        room: device.room === 'Unassigned' ? '' : (device.room || ''),
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

    openChart(device) {
      const preferred = ['temperature', 'humidity', 'illuminance', 'brightness', 'switch'];
      this.chart.device = device;
      this.chart.capability = preferred.find(c => device.capabilities.includes(c))
        || device.capabilities[0];
      this.chart.data = null;
      this.chart.geo = null;
      this.chart.open = true;
      this.loadChart();
    },

    closeChart() {
      this.chart.open = false;
      this.chart.hover = null;
    },

    pickCapability(cap) { this.chart.capability = cap; this.loadChart(); },
    pickRange(hours) { this.chart.hours = hours; this.loadChart(); },

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
        // The box only has a width once the dialog is on screen.
        this.$nextTick(() => this.draw());
      } catch (err) {
        if (seq === this._reqSeq) { this.chart.error = err.message; this.chart.data = null; this.chart.geo = null; }
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
      const box = this.$refs.plot;
      if (!data || !box) { this.chart.geo = null; return; }
      // Not laid out yet (the dialog is still opening): the observer in
      // init() calls again once the box has a size.
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
        stroke: `rgb(${this.COLOR[data.capability] || '90 168 124'})`,
        fill: `rgb(${this.COLOR[data.capability] || '90 168 124'} / .22)`,
      };
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

    get summary() {
      const data = this.chart.data;
      if (!data || !data.points.length) return [];
      const values = data.points.map(p => p.v).filter(v => v !== null);
      if (!values.length) return [];
      const unit = this.CHART_UNIT[data.capability] || '';
      const stops = this.RAMP[data.capability];
      // Same ramp as the cards, so the min/max here read like the tiles do.
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
  };
}
