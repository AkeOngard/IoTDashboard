/* Dashboard component (doc §10). Registered as a global factory so Alpine,
 * which loads deferred, finds it at init time.
 *
 * Display principle: show a number only where a number is the point, and show
 * secondary facts (battery, staleness) only when they are worth a glance.
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
      brightness: 'ความสว่างไฟ', color_temp: 'อุณหภูมิสี', switch: 'สวิตช์',
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

    // Endpoints of the Kelvin gradient used by lamp tints and the CT slider.
    K_WARM: [255, 172, 92],
    K_COOL: [198, 224, 255],
    RANGE: { brightness: [0, 100, 1], color_temp: [2200, 6500, 100] },
    RANGES: [
      { label: '1ชม', hours: 1 }, { label: '6ชม', hours: 6 },
      { label: '24ชม', hours: 24 }, { label: '7ว', hours: 168 },
      { label: '30ว', hours: 720 },
    ],
    // Thresholds that tint a reading amber. Tune per deployment.
    ALERT: { temperature: [null, 32], humidity: [30, 70], battery: [20, null] },
    // A sensor quieter than this has something wrong with it, not a stable value:
    // the recorder heartbeats every 5 minutes even when nothing changes.
    STALE_SECONDS: 600,

    devices: [], states: {}, pending: {}, toasts: [],
    socket: false, adapterConnected: false, adapterName: '—', historyEnabled: false,
    chart: { open: false, device: null, capability: null, hours: 24, loading: false, data: null, error: null },
    _ws: null, _backoff: 1000, _timers: {}, _toastSeq: 0, _tick: 0, _chartjs: null, _reqSeq: 0,

    init() {
      this.connect();
      // Drives the relative staleness labels.
      setInterval(() => { this._tick++; }, 15000);
    },

    connect() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      this._ws = ws;
      ws.onopen = () => { this.socket = true; this._backoff = 1000; };
      ws.onmessage = (e) => this.apply(JSON.parse(e.data));
      ws.onclose = () => {
        this.socket = false;
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

    /** One status line beats three indicators: report the worst thing that is
     *  true, and stay quiet when everything is fine. */
    get health() {
      if (!this.socket) return { tone: 'danger', text: 'ขาดการเชื่อมต่อเซิร์ฟเวอร์' };
      if (!this.adapterConnected) return { tone: 'warn', text: `กำลังเชื่อมต่อ ${this.adapterName}` };
      if (!this.historyEnabled) return { tone: 'muted', text: 'ทำงานปกติ · ไม่บันทึกประวัติ' };
      return { tone: 'on', text: 'ทำงานปกติ' };
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

    unit(id, cap) {
      const s = this.states[this.key(id, cap)];
      return s ? s.unit : (this.UNIT[cap] || '');
    },

    format(id, cap) {
      const v = this.shown(id, cap);
      if (v === null || v === undefined) return '—';
      if (cap === 'contact') return v ? 'ปิด' : 'เปิด';
      if (cap === 'occupancy') return v ? 'มีคน' : 'ว่าง';
      return typeof v === 'number' ? v.toLocaleString('th-TH') : v;
    },

    alert(id, cap) {
      const range = this.ALERT[cap], v = this.value(id, cap);
      if (!range || typeof v !== 'number') return false;
      return (range[0] !== null && v < range[0]) || (range[1] !== null && v > range[1]);
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
      if (cap === 'contact')   return v ? 'var(--text)' : 'var(--c-warm)';   // open is worth noticing
      if (cap === 'occupancy') return v ? 'var(--c-state)' : 'var(--faint)';
      if (this.FLAT[cap]) return this.rgb(this.FLAT[cap]);
      const stops = this.RAMP[cap];
      if (stops && typeof v === 'number') return this.rgb(this.ramp(stops, v));
      return 'var(--text)';
    },

    /** A lit lamp tints its own card with the colour it is actually showing. */
    cardTint(device) {
      if (!device.writable.includes('switch') || !this.shown(device.id, 'switch')) return '';
      if (device.kind !== 'light') {
        return 'background-image: radial-gradient(130% 110% at 88% -15%, rgba(75,189,133,.07), transparent 62%)';
      }
      const [r, g, b] = this.kelvinRgb(this.shown(device.id, 'color_temp'));
      const level = this.shown(device.id, 'brightness');
      const alpha = (0.05 + 0.11 * ((typeof level === 'number' ? level : 100) / 100)).toFixed(3);
      return `background-image: radial-gradient(130% 110% at 88% -15%, rgba(${r},${g},${b},${alpha}), transparent 62%)`;
    },

    /** An on lamp's toggle wears the lamp's colour; darkened so the white knob
     *  stays legible at 6500K. Returns '' when off, letting the class win. */
    toggleStyle(device) {
      if (!this.shown(device.id, 'switch')) return '';
      if (device.kind !== 'light') return '';
      const [r, g, b] = this.kelvinRgb(this.shown(device.id, 'color_temp'));
      return `background-color: rgb(${Math.round(r * .74)} ${Math.round(g * .74)} ${Math.round(b * .8)})`;
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
        return `--track-img: linear-gradient(90deg, rgba(255,255,255,.09), ${lamp})`;
      }
      return '';
    },

    statusLabel(id, cap) {
      if (this.isPending(id, cap)) return 'กำลังสั่ง…';
      const v = this.value(id, cap);
      if (v === null) return 'ไม่ทราบสถานะ';
      return v ? 'เปิดอยู่' : 'ปิดอยู่';
    },

    // ------------------------------------------------------------ layout

    heroCaps(device) {
      const hero = device.capabilities.filter(c => this.HERO.includes(c));
      return hero.length ? hero : device.capabilities.filter(c => this.STATE_READ.includes(c));
    },

    /** Small print under a card: the readings that did not earn large type,
     *  plus a staleness warning when the device has gone quiet. */
    footnote(device) {
      const hero = this.heroCaps(device);
      const notes = [];

      for (const cap of device.capabilities) {
        if (hero.includes(cap) || !this.READ.includes(cap)) continue;
        const v = this.value(device.id, cap);
        if (v === null) continue;
        const text = cap === 'battery'
          ? `แบต ${v}%`
          : `${this.LABEL[cap]} ${this.format(device.id, cap)}`;
        notes.push({ key: cap, text, color: this.readingColor(device.id, cap) });
      }

      const stale = this.staleness(device);
      if (stale) notes.push(stale);
      return notes;
    },

    /** Staleness only means something for devices that are supposed to report
     *  on their own. A lamp is silent between commands by design -- for those,
     *  `online` (Matter Reachable) is the signal, and it has its own dot. */
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

    async refresh() {
      try {
        await apiFetch('/api/devices/refresh', { method: 'POST' });
      } catch (err) {
        this.toast('error', 'สแกนไม่สำเร็จ', err.message);
      }
    },

    // ----------------------------------------------------------- history

    openChart(device) {
      const preferred = ['temperature', 'humidity', 'illuminance', 'brightness', 'switch'];
      this.chart.device = device;
      this.chart.capability = preferred.find(c => device.capabilities.includes(c))
        || device.capabilities[0];
      this.chart.open = true;
      this.loadChart();
    },

    closeChart() {
      this.chart.open = false;
      if (this._chartjs) { this._chartjs.destroy(); this._chartjs = null; }
    },

    pickCapability(cap) { this.chart.capability = cap; this.loadChart(); },
    pickRange(hours) { this.chart.hours = hours; this.loadChart(); },

    async loadChart() {
      const { device, capability, hours } = this.chart;
      if (!device || !capability) return;
      const seq = ++this._reqSeq;
      this.chart.loading = true;
      this.chart.error = null;
      try {
        const body = await apiFetch(`/api/devices/${encodeURIComponent(device.id)}/history`
          + `?capability=${encodeURIComponent(capability)}&hours=${hours}`);
        if (seq !== this._reqSeq) return;   // a newer request already won
        this.chart.data = body;
        this.draw();
      } catch (err) {
        if (seq === this._reqSeq) { this.chart.error = err.message; this.chart.data = null; }
      } finally {
        if (seq === this._reqSeq) this.chart.loading = false;
      }
    },

    draw() {
      const data = this.chart.data;
      if (!data) return;
      const rgb = this.COLOR[data.capability] || '90 168 124';
      const grid = 'rgba(255,255,255,.05)';
      const tick = 'rgb(92 92 102)';
      const labels = data.points.map(p => this.stamp(p.t, data.hours));
      const common = { pointRadius: 0, borderWidth: 0, tension: data.boolean ? 0 : 0.35 };

      const config = {
        type: 'line',
        data: {
          labels,
          datasets: [
            { ...common, label: 'max', data: data.points.map(p => p.hi), fill: 1,
              backgroundColor: `rgba(${rgb} / 0.13)` },
            { ...common, label: 'min', data: data.points.map(p => p.lo), fill: false },
            { ...common, label: this.LABEL[data.capability] || data.capability,
              data: data.points.map(p => p.v), borderColor: `rgb(${rgb})`, borderWidth: 1.75,
              stepped: data.boolean ? 'before' : false },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: '#1a1a1d',
              borderColor: 'rgba(255,255,255,.08)', borderWidth: 1,
              titleColor: tick, bodyColor: '#e9e9ec',
              padding: 10, displayColors: false,
              filter: (item) => item.datasetIndex === 2,
              callbacks: {
                label: (item) => ` ${item.formattedValue} ${this.CHART_UNIT[data.capability] || ''}`,
              },
            },
          },
          scales: {
            x: { border: { display: false }, grid: { display: false },
                 ticks: { color: tick, font: { size: 11 }, maxTicksLimit: 7, autoSkip: true } },
            y: { border: { display: false }, grid: { color: grid },
                 ticks: { color: tick, font: { size: 11 }, maxTicksLimit: 5, padding: 8 },
                 ...(data.boolean ? { min: 0, max: 100 } : {}) },
          },
        },
      };

      if (this._chartjs) { this._chartjs.destroy(); }
      this._chartjs = new Chart(this.$refs.canvas, config);
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
        item('ต่ำสุด', Math.min(...data.points.map(p => p.lo ?? p.v))),
        item('เฉลี่ย', values.reduce((a, b) => a + b, 0) / values.length),
        item('สูงสุด', Math.max(...data.points.map(p => p.hi ?? p.v))),
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
