/* The house in 3D: a dollhouse drawn from the floor plan, with every placed
 * device on it, lit, coloured and labelled from the same WebSocket stream
 * the rest of the dashboard reads.
 *
 * All of it runs in the viewer's browser. The Pi serves this file and
 * three.js once (both cached for good under their versioned URLs) and the
 * plan as a few kilobytes of JSON; it never draws anything. dashboard.js
 * imports this module only when the house page first opens, so the other
 * pages never fetch three.js at all.
 *
 * Plan coordinates are metres: x across the front from left to right as seen
 * from the street, z from the front wall towards the back. The world negates
 * plan z, so a camera standing in the street sees the house the right way
 * round.
 *
 * Nothing renders continuously. A frame is drawn when something changed --
 * the camera, a device, the theme -- and only an animation that is actually
 * running (someone moving in a room) keeps frames coming. On a phone that is
 * the difference between a page that sits idle and one that warms the hand.
 *
 * This file holds no wording: every label's text arrives from dashboard.js,
 * already in the viewer's language.
 */

let T = null;   // three.js, imported on first use

/** Build a view inside `el` (which must be position: relative). */
export async function createHouse(el, options) {
  if (!T) T = await import(options.threeUrl);
  return new HouseView(el, options);
}

// ---------------------------------------------------------------- constants

const SLAB = 0.2;            // between storeys
const SPREAD = 2.5;          // metres between storeys laid side by side
const WALL_T = 0.12;
const DOOR_W = 0.9;
const DOOR_H = 2.1;
const LOW = 0.32;            // a wall cut away to show the room behind it
const HALF = 1.1;            // inner walls: enough to read the rooms, low enough to see in
const FENCE_H = 1.5;
const EPS = 0.01;
const INDOOR = new Set(['living', 'bed', 'bath', 'kitchen', 'hall', 'room']);

// What sits on a wall, and how high (metres above its floor). An AC's height
// is taken from the ceiling instead (see _layoutPin).
const ON_WALL = { ac: -0.45, plug: 0.3, switch: 1.2, contact: 1.1, climate: 1.5 };

const PALETTE = {
  dark: {
    sky: 0xd9d4ff, under: 0x231f38, hemi: 1.55, sun: 1.7,
    ground: 0x17161f, wall: 0xd7d3e6, fence: 0x9c97b5, gate: 0x5f5a80,
    stair: 0xb7b1d0, car: 0x7f75cf, glass: 0x2b2f48, wheel: 0x1b1a24,
    device: 0xeeecf6, dim: 0x6b6880, accent: 0x8b7cf6, focus: 0x8b7cf6,
    floor: {
      living: 0x7a6a58, bed: 0x5f5a7d, bath: 0x4d6e78, kitchen: 0x7d725c,
      hall: 0x5e5c6c, room: 0x67646f, yard: 0x2d4733, carport: 0x4f4f59,
    },
    additive: true,
  },
  light: {
    sky: 0xffffff, under: 0xa9a3c9, hemi: 1.9, sun: 2.0,
    ground: 0xdedcea, wall: 0xffffff, fence: 0x8d88a8, gate: 0x5f5a80,
    stair: 0xd5d1e6, car: 0x7d72d6, glass: 0x3a4060, wheel: 0x2b2a36,
    device: 0xffffff, dim: 0xa3a0b6, accent: 0x6c5ce0, focus: 0x6c5ce0,
    floor: {
      living: 0xd8c6ad, bed: 0xd2cdea, bath: 0xc3dde3, kitchen: 0xe4d8bf,
      hall: 0xdcdae6, room: 0xe1dfe8, yard: 0xb3d1a2, carport: 0xcac9d3,
    },
    additive: false,
  },
};

// ------------------------------------------------------------- plan helpers

const isIndoor = (room) => !!room && INDOOR.has(room.kind);
const pairKey = (a, b) => (a < b ? a + '|' + b : b + '|' + a);
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const num = (v, fallback) => (Number.isFinite(+v) && v !== '' && v !== null ? +v : fallback);

/** A room as numbers, whatever a half-typed editor field holds. */
function cleanRoom(r) {
  return {
    ...r,
    x: num(r.x, 0), z: num(r.z, 0),
    w: Math.max(0.5, num(r.w, 0.5)), d: Math.max(0.5, num(r.d, 0.5)),
  };
}

/** The smallest room holding the point: a bathroom inside a kitchen wins. */
function innermost(rooms, x, z) {
  for (const r of rooms) {
    if (x > r.x && x < r.x + r.w && z > r.z && z < r.z + r.d) return r;
  }
  return null;
}

/* Walls, worked out from where room rectangles meet. Along every line a room
 * edge lies on, each stretch between two cut points gets the room on either
 * side of it (the smallest one holding a point just off the line). Where the
 * two differ and at least one is indoors there is a wall -- unless the plan
 * says those two rooms are open to each other. The plan's doors become one
 * opening in the longest stretch the two rooms share. */
function wallsOf(floor, rooms) {
  const lines = new Map();
  const add = (axis, c, a, b) => {
    const key = axis + ':' + c.toFixed(3);
    let line = lines.get(key);
    if (!line) lines.set(key, line = { axis, c, edges: [] });
    line.edges.push([a, b]);
  };
  for (const r of rooms) {
    add('x', r.z, r.x, r.x + r.w);
    add('x', r.z + r.d, r.x, r.x + r.w);
    add('z', r.x, r.z, r.z + r.d);
    add('z', r.x + r.w, r.z, r.z + r.d);
  }
  const cutsX = rooms.flatMap(r => [r.x, r.x + r.w]);
  const cutsZ = rooms.flatMap(r => [r.z, r.z + r.d]);
  const open = new Set((floor.open || []).map(p => pairKey(p[0], p[1])));
  const doors = new Set((floor.doors || []).map(p => pairKey(p[0], p[1])));

  const segs = [];
  for (const line of lines.values()) {
    const cuts = (line.axis === 'x' ? cutsX : cutsZ).concat(line.edges.flat());
    const pts = [...new Set(cuts.map(v => Math.round(v * 1000) / 1000))].sort((p, q) => p - q);
    let run = null;
    for (let i = 0; i + 1 < pts.length; i++) {
      const p = pts[i], q = pts[i + 1];
      const covered = line.edges.some(e => e[0] <= p + 1e-4 && e[1] >= q - 1e-4);
      if (q - p < 1e-3 || !covered) { run = null; continue; }
      const m = (p + q) / 2;
      const neg = line.axis === 'x' ? innermost(rooms, m, line.c - EPS) : innermost(rooms, line.c - EPS, m);
      const pos = line.axis === 'x' ? innermost(rooms, m, line.c + EPS) : innermost(rooms, line.c + EPS, m);
      const inNeg = isIndoor(neg), inPos = isIndoor(pos);
      if (neg === pos || (!inNeg && !inPos) || (neg && pos && open.has(pairKey(neg.id, pos.id)))) {
        run = null;
        continue;
      }
      if (run && run.neg === neg && run.pos === pos && Math.abs(run.b - p) < 1e-4) { run.b = q; continue; }
      run = {
        axis: line.axis, c: line.c, a: p, b: q, neg, pos,
        exterior: !(inNeg && inPos),
        // Which way is out, along the axis across the wall.
        out: inNeg && !inPos ? 1 : !inNeg && inPos ? -1 : 0,
        door: false,
      };
      segs.push(run);
    }
  }
  for (const key of doors) {
    let best = null;
    for (const s of segs) {
      if (s.neg && s.pos && pairKey(s.neg.id, s.pos.id) === key && (!best || s.b - s.a > best.b - best.a)) best = s;
    }
    if (best) best.door = true;
  }
  return segs;
}

/** A soft white disc or a fade, tinted per use through the material colour. */
function gradientTexture(kind) {
  const c = document.createElement('canvas');
  c.width = 128; c.height = 128;
  const g = c.getContext('2d');
  let grad;
  if (kind === 'fade') {
    grad = g.createLinearGradient(0, 0, 0, 128);
    grad.addColorStop(0, 'rgba(255,255,255,0.75)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
  } else {
    grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
    const stops = kind === 'glow' ? [[0, 1], [0.22, 0.55], [1, 0]] : [[0, 0.85], [0.45, 0.4], [1, 0]];
    for (const [at, alpha] of stops) grad.addColorStop(at, `rgba(255,255,255,${alpha})`);
  }
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const tex = new T.CanvasTexture(c);
  tex.colorSpace = T.SRGBColorSpace;
  return tex;
}

// --------------------------------------------------------------------- view

class HouseView {
  constructor(el, options) {
    this.el = el;
    this.cb = options;
    this.theme = options.theme === 'light' ? 'light' : 'dark';
    this.state = { layoutJson: '', floor: 'all', mode: 'view', selected: null, placing: null, focus: null };
    this.layout = null;
    this.floors = new Map();
    this.pins = new Map();
    this.roomTags = [];
    this.yaw = -0.5; this.pitch = 0.92; this.dist = 26; this.minDist = 3; this.maxDist = 90;
    this._frame = 0;
    this._fitted = false;
    this._worldMats = [];
    this._ptrs = new Map();
    this._gesture = null;
    this._drag = null;

    const renderer = this.renderer = new T.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'low-power' });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x000000, 0);
    this.canvas = renderer.domElement;
    this.canvas.className = 'house-canvas';
    this.overlay = document.createElement('div');
    this.overlay.className = 'house-overlay';
    el.prepend(this.canvas, this.overlay);

    this.scene = new T.Scene();
    this.camera = new T.PerspectiveCamera(36, 1, 0.1, 400);
    this.target = new T.Vector3();
    this.hemi = new T.HemisphereLight();
    this.sun = new T.DirectionalLight(0xffffff, 1);
    this.sun.position.set(-9, 18, 14);
    this.world = new T.Group();
    this.devices = new T.Group();
    this.scene.add(this.hemi, this.sun, this.world, this.devices);

    this.geo = {
      box: new T.BoxGeometry(1, 1, 1),
      plane: new T.PlaneGeometry(1, 1),
      bulb: new T.SphereGeometry(0.12, 18, 12),
      dot: new T.SphereGeometry(0.028, 10, 8),
      ball: new T.SphereGeometry(0.075, 14, 10),
      dome: new T.SphereGeometry(0.09, 16, 8, 0, Math.PI * 2, Math.PI / 2, Math.PI / 2),
      rod: new T.CylinderGeometry(0.012, 0.012, 1, 6),
      pole: new T.CylinderGeometry(0.045, 0.06, 1, 10),
      wheel: new T.CylinderGeometry(0.34, 0.34, 0.24, 18),
      ring: new T.RingGeometry(0.86, 1, 48),
    };
    this.geo.ring.rotateX(-Math.PI / 2);
    this.tex = { glow: gradientTexture('glow'), pool: gradientTexture('pool'), fade: gradientTexture('fade') };
    this.raycaster = new T.Raycaster();
    this._v = new T.Vector3();
    this._c = new T.Color();

    this._bind();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(el);
    // A browser may drop what a hidden tab drew; draw again on return.
    this._onShow = () => { if (!document.hidden) this.invalidate(); };
    document.addEventListener('visibilitychange', this._onShow);
    this._resize();
  }

  // ------------------------------------------------------------ public API

  /** Bring the view in line with the page. Cheap to call often: each part
   *  is only redone when it actually changed. */
  update(next) {
    const s = this.state;
    const layoutJson = next.layout ? JSON.stringify(next.layout) : '';
    const themeChanged = (next.theme === 'light' ? 'light' : 'dark') !== this.theme;
    const rebuild = layoutJson !== s.layoutJson || next.floor !== s.floor || themeChanged;
    const refit = !this._fitted || next.floor !== s.floor;
    this.theme = next.theme === 'light' ? 'light' : 'dark';
    s.layoutJson = layoutJson;
    s.floor = next.floor;
    s.mode = next.mode;
    s.placing = next.placing || null;
    s.focus = next.focus || null;
    this.el.classList.toggle('is-arranging', s.mode === 'arrange');
    this.el.classList.toggle('is-placing', !!s.placing);
    if (rebuild) {
      this.layout = next.layout
        ? { floors: next.layout.floors.map(f => ({ ...f, rooms: f.rooms.map(cleanRoom) })) }
        : null;
      this._build();
      if (themeChanged) this.pins.forEach(pin => this._paint(pin));
    }
    this._paintFocus();
    this._setPins(next.pins || []);
    if (s.selected !== next.selected) {
      s.selected = next.selected || null;
      this.pins.forEach(pin => pin.label.classList.toggle('is-selected', pin.data.id === s.selected));
    }
    if (rebuild && refit && this._w) this.fit();
    else this._updateCamera();
  }

  /** Frame everything on show, from the street and a little to the left. */
  fit() {
    const box = this._bounds();
    if (!box) return;
    const size = new T.Vector3(), center = new T.Vector3();
    box.getSize(size);
    box.getCenter(center);
    this.target.copy(center);
    const radius = size.length() / 2;
    const vfov = (this.camera.fov * Math.PI) / 180;
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    this.dist = clamp((radius * 0.95) / Math.sin(Math.min(vfov, hfov) / 2), 4, 120);
    this.minDist = Math.max(2.5, radius * 0.35);
    this.maxDist = this.dist * 2.2;
    this.yaw = -0.5;
    this.pitch = 0.92;
    this._fitted = true;
    // The sphere round the box is generous, most of all on a tall phone
    // screen. Tighten on the box's actual outline on screen: centre it, then
    // pull in until it spans most of the view. A few passes settle it.
    const corners = [];
    for (const x of [box.min.x, box.max.x]) {
      for (const y of [box.min.y, box.max.y]) {
        for (const z of [box.min.z, box.max.z]) corners.push(new T.Vector3(x, y, z));
      }
    }
    const right = new T.Vector3(), up = new T.Vector3();
    for (let pass = 0; pass < 4; pass++) {
      this._place();
      this.camera.updateMatrixWorld();
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      for (const c of corners) {
        const v = this._v.copy(c).project(this.camera);
        x0 = Math.min(x0, v.x); x1 = Math.max(x1, v.x); y0 = Math.min(y0, v.y); y1 = Math.max(y1, v.y);
      }
      const halfH = this.dist * Math.tan(vfov / 2), halfW = halfH * this.camera.aspect;
      right.setFromMatrixColumn(this.camera.matrixWorld, 0);
      up.setFromMatrixColumn(this.camera.matrixWorld, 1);
      this.target.addScaledVector(right, ((x0 + x1) / 2) * halfW).addScaledVector(up, ((y0 + y1) / 2) * halfH);
      const span = Math.max((x1 - x0) / 2, (y1 - y0) / 2);
      this.dist = clamp(this.dist * (span / 0.86), 3, 120);
    }
    this.minDist = Math.min(this.minDist, this.dist * 0.4);
    this.maxDist = this.dist * 2.5;
    this._updateCamera();
  }

  destroy() {
    cancelAnimationFrame(this._frame);
    this._ro.disconnect();
    document.removeEventListener('visibilitychange', this._onShow);
    this._unbind();
    this.pins.forEach(pin => this._dropPin(pin));
    this.pins.clear();
    this._clearWorld();
    Object.values(this.geo).forEach(g => g.dispose());
    Object.values(this.tex).forEach(t => t.dispose());
    this.renderer.dispose();
    this.canvas.remove();
    this.overlay.remove();
  }

  // ----------------------------------------------------------------- world

  _mat(color, extra) {
    const mat = new T.MeshLambertMaterial({ color, ...extra });
    this._worldMats.push(mat);
    return mat;
  }

  _clearWorld() {
    this._worldMats.forEach(m => m.dispose());
    this._worldMats = [];
    this.world.clear();
    this.roomTags.forEach(t => t.el.remove());
    this.roomTags = [];
    this.roomMeshes = [];
  }

  /** A box between two heights over a plan rectangle. */
  _box(group, mat, x, z, w, d, y0, y1) {
    const mesh = new T.Mesh(this.geo.box, mat);
    mesh.scale.set(w, Math.max(0.001, y1 - y0), d);
    mesh.position.set(x + w / 2, (y0 + y1) / 2, -(z + d / 2));
    group.add(mesh);
    return mesh;
  }

  _build() {
    this._clearWorld();
    const pal = PALETTE[this.theme];
    this.hemi.color.set(pal.sky);
    this.hemi.groundColor.set(pal.under);
    this.hemi.intensity = pal.hemi;
    this.sun.intensity = pal.sun;
    this.floors = new Map();
    if (!this.layout) { this.invalidate(); return; }

    /* The whole house: stacked storeys would hide the ground floor under
     * the one above, so each storey stands beside the last instead, a
     * storey higher -- to the right on a wide screen, behind on a phone
     * held upright -- and the camera sees into every room at once. */
    const floors = this.layout.floors;
    const all = this.state.floor === 'all' || !floors.some(f => f.id === this.state.floor);
    this._wide = (this._w || 1) >= (this._h || 1);
    let y = 0, edge = null;
    floors.forEach((floor, index) => {
      const visible = all || floor.id === this.state.floor;
      const rooms = floor.rooms.slice().sort((a, b) => a.w * a.d - b.w * b.d);
      const height = clamp(num(floor.height, 2.7), 2, 6);
      let ox = 0, oz = 0;
      if (all && rooms.length) {
        const lo = this._wide ? Math.min(...rooms.map(r => r.x)) : Math.min(...rooms.map(r => r.z));
        const hi = this._wide ? Math.max(...rooms.map(r => r.x + r.w)) : Math.max(...rooms.map(r => r.z + r.d));
        const shift = edge === null ? 0 : edge + SPREAD - lo;
        if (this._wide) ox = shift; else oz = shift;
        edge = hi + shift;
      }
      const info = {
        floor, index, base: all ? y : 0, ox, oz, visible, rooms, height,
        walls: wallsOf(floor, rooms), wallMeshes: [], fences: floor.fences || [],
      };
      y += height + SLAB;
      this.floors.set(floor.id, info);
      if (visible) this._buildFloor(info, pal, all && floors.length > 1);
    });
    this.pins.forEach(pin => this._layoutPin(pin));
  }

  _buildFloor(info, pal, tagFloor) {
    const { floor, base, rooms } = info;
    const group = new T.Group();
    group.position.set(info.ox, 0, -info.oz);
    this.world.add(group);
    // Labels sit in the world, so they take the storey's offset themselves.
    const at = (x, y, z) => new T.Vector3(x + info.ox, y, -(z + info.oz));

    // Ground under the lowest storey only.
    if (info.index === 0 && rooms.length) {
      const x0 = Math.min(...rooms.map(r => r.x)) - 4, x1 = Math.max(...rooms.map(r => r.x + r.w)) + 4;
      const z0 = Math.min(...rooms.map(r => r.z)) - 4, z1 = Math.max(...rooms.map(r => r.z + r.d)) + 4;
      const ground = new T.Mesh(this.geo.plane, this._mat(pal.ground));
      ground.rotation.x = -Math.PI / 2;
      ground.scale.set(x1 - x0, z1 - z0, 1);
      ground.position.set((x0 + x1) / 2, base - 0.1, -(z0 + z1) / 2);
      group.add(ground);
    }

    // Floors: big rooms first, and a room inside another a hair above it.
    rooms.slice().reverse().forEach((r, k) => {
      const indoor = isIndoor(r);
      const thick = indoor ? SLAB : 0.06;
      const top = base + (indoor ? 0 : -0.03) + k * 0.004;
      const mat = this._mat(pal.floor[r.kind] || pal.floor.room);
      const mesh = this._box(group, mat, r.x, r.z, r.w, r.d, top - thick, top);
      mesh.userData = { room: r.id, floor: floor.id };
      this.roomMeshes.push(mesh);
      if (r.kind === 'carport') this._car(group, r, top, pal);
      const tag = document.createElement('span');
      tag.className = 'house-room';
      tag.textContent = r.name;
      this.overlay.prepend(tag);
      this.roomTags.push({ el: tag, at: at(r.x + r.w / 2, top + 0.02, r.z + r.d / 2) });
    });

    if (tagFloor) {
      const inside = rooms.filter(isIndoor);
      if (inside.length) {
        const tag = document.createElement('span');
        tag.className = 'house-floor';
        tag.textContent = floor.name;
        this.overlay.prepend(tag);
        const x0 = Math.min(...inside.map(r => r.x)), z0 = Math.min(...inside.map(r => r.z));
        this.roomTags.push({ el: tag, at: at(x0 - 0.4, base, z0 - 0.4) });
      }
    }

    // Wall pieces get their height from the camera in _cutaway().
    const wallMat = this._mat(pal.wall);
    for (const seg of info.walls) {
      const pieces = [];
      if (seg.door) {
        const len = seg.b - seg.a;
        const w = Math.min(DOOR_W, len - 0.3);
        if (w < 0.5) continue;    // too short for a frame: leave the gap open
        const mid = (seg.a + seg.b) / 2;
        pieces.push([seg.a, mid - w / 2, 'wall'], [mid + w / 2, seg.b, 'wall'], [mid - w / 2, mid + w / 2, 'lintel']);
      } else {
        pieces.push([seg.a, seg.b, 'wall']);
      }
      for (let [a, b, piece] of pieces) {
        // Run whole walls past their ends so corners close.
        if (piece === 'wall' && a === seg.a) a -= WALL_T / 2;
        if (piece === 'wall' && b === seg.b) b += WALL_T / 2;
        const mesh = new T.Mesh(this.geo.box, wallMat);
        mesh.userData = { seg, piece, a, b };
        group.add(mesh);
        info.wallMeshes.push(mesh);
      }
    }

    const stairMat = this._mat(pal.stair);
    for (const s of floor.stairs || []) this._stairs(group, s, base, info.height + SLAB, stairMat);
    for (const f of info.fences) this._fence(group, f, base, pal);
  }

  _stairs(group, s, base, rise, mat) {
    const n = Math.max(6, Math.round(rise / 0.18));
    const step = rise / n;
    const alongX = s.up === '+x' || s.up === '-x';
    const run = (alongX ? num(s.w, 1) : num(s.d, 1)) / n;
    for (let i = 0; i < n; i++) {
      let x = num(s.x, 0), z = num(s.z, 0), w = num(s.w, 1), d = num(s.d, 1);
      if (alongX) { w = run; x = s.up === '+x' ? x + i * run : x + num(s.w, 1) - (i + 1) * run; }
      else { d = run; z = s.up === '+z' ? z + i * run : z + num(s.d, 1) - (i + 1) * run; }
      this._box(group, mat, x, z, w, d, base, base + step * (i + 1));
    }
  }

  /** A low wall with posts and a rail, and a barred gate in each gap. */
  _fence(group, f, base, pal) {
    const wall = this._mat(pal.wall), post = this._mat(pal.fence), gate = this._mat(pal.gate);
    const z = num(f.z, 0), t = 0.14;
    const gates = (f.gates || []).map(g => [num(g[0], 0), num(g[1], 0)]).sort((a, b) => a[0] - b[0]);
    const runs = [];
    let at = num(f.x0, 0);
    for (const [a, b] of gates) { if (a > at) runs.push([at, a]); at = Math.max(at, b); }
    if (num(f.x1, 0) > at) runs.push([at, num(f.x1, 0)]);
    for (const [a, b] of runs) {
      this._box(group, wall, a, z - t / 2, b - a, t, base, base + 0.55);
      this._box(group, post, a, z - 0.03, b - a, 0.06, base + FENCE_H - 0.06, base + FENCE_H);
      const n = Math.max(1, Math.round((b - a) / 1.6));
      for (let i = 0; i <= n; i++) {
        const x = a + ((b - a) * i) / n;
        this._box(group, wall, x - 0.1, z - 0.1, 0.2, 0.2, base, base + FENCE_H + 0.1);
      }
      for (let x = a + 0.15; x < b - 0.1; x += 0.22) {
        this._box(group, post, x - 0.015, z - 0.015, 0.03, 0.03, base + 0.55, base + FENCE_H);
      }
    }
    for (const [a, b] of gates) {
      this._box(group, gate, a, z - 0.03, b - a, 0.06, base + 0.08, base + 0.14);
      this._box(group, gate, a, z - 0.03, b - a, 0.06, base + 1.3, base + 1.36);
      for (let x = a + 0.12; x < b - 0.06; x += 0.16) {
        this._box(group, gate, x - 0.012, z - 0.012, 0.024, 0.024, base + 0.08, base + 1.36);
      }
    }
  }

  /** A car, lengthwise in its carport. */
  _car(group, r, top, pal) {
    const alongZ = r.d >= r.w;
    const len = Math.min(4.3, (alongZ ? r.d : r.w) - 0.6), wid = Math.min(1.8, (alongZ ? r.w : r.d) - 0.6);
    if (len < 2 || wid < 1) return;
    const car = new T.Group();
    const body = new T.Mesh(this.geo.box, this._mat(pal.car));
    body.scale.set(wid, 0.55, len);
    body.position.y = 0.55;
    const cabin = new T.Mesh(this.geo.box, this._mat(pal.glass));
    cabin.scale.set(wid * 0.84, 0.46, len * 0.5);
    cabin.position.set(0, 1.05, -len * 0.06);
    car.add(body, cabin);
    const wheelMat = this._mat(pal.wheel);
    for (const sx of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const wheel = new T.Mesh(this.geo.wheel, wheelMat);
        wheel.rotation.z = Math.PI / 2;
        wheel.position.set((sx * wid) / 2, 0.34, sz * len * 0.31);
        car.add(wheel);
      }
    }
    car.position.set(r.x + r.w / 2, top, -(r.z + r.d / 2));
    if (!alongZ) car.rotation.y = Math.PI / 2;
    group.add(car);
  }

  /* Dollhouse cut-away: an outside wall between the camera and the rooms
   * drops to a kerb, the far ones stand full height, and walls inside stay
   * half height so a lamp's pool of light on the floor shows from anywhere.
   * Editing the plan drops every wall, to read it like a drawing. */
  _cutaway() {
    const dx = this.camera.position.x - this.target.x, dz = this.camera.position.z - this.target.z;
    const len = Math.hypot(dx, dz) || 1;
    const cx = dx / len, cz = dz / len;
    const plan = this.state.mode === 'plan';
    for (const info of this.floors.values()) {
      if (!info.visible) continue;
      for (const mesh of info.wallMeshes) {
        const { seg, piece, a, b } = mesh.userData;
        let h;
        if (plan) h = LOW;
        else if (!seg.exterior) h = HALF;
        else {
          // Outward normal in the world: plan z is negated.
          const nx = seg.axis === 'z' ? seg.out : 0, nz = seg.axis === 'x' ? -seg.out : 0;
          h = nx * cx + nz * cz > 0.2 ? LOW : info.height;
        }
        let y0 = info.base;
        if (piece === 'lintel') {
          if (h <= DOOR_H) { mesh.visible = false; continue; }
          y0 += DOOR_H;
        }
        const hh = info.base + h - y0;
        mesh.visible = true;
        if (seg.axis === 'x') {
          mesh.scale.set(b - a, hh, WALL_T);
          mesh.position.set((a + b) / 2, y0 + hh / 2, -seg.c);
        } else {
          mesh.scale.set(WALL_T, hh, b - a);
          mesh.position.set(seg.c, y0 + hh / 2, -(a + b) / 2);
        }
      }
    }
  }

  _paintFocus() {
    const pal = PALETTE[this.theme];
    for (const mesh of this.roomMeshes || []) {
      const on = this.state.focus === mesh.userData.floor + ':' + mesh.userData.room;
      mesh.material.emissive.set(on ? pal.focus : 0x000000);
      mesh.material.emissiveIntensity = on ? 0.45 : 0;
    }
    this.invalidate();
  }

  _bounds() {
    const box = new T.Box3();
    let any = false;
    for (const info of this.floors.values()) {
      if (!info.visible) continue;
      for (const r of info.rooms) {
        box.expandByPoint(this._v.set(r.x + info.ox, info.base - 0.1, -(r.z + info.oz)));
        box.expandByPoint(this._v.set(r.x + r.w + info.ox, info.base + info.height, -(r.z + r.d + info.oz)));
        any = true;
      }
    }
    return any ? box : null;
  }

  // ------------------------------------------------------------------ pins

  _setPins(list) {
    const seen = new Set();
    for (const d of list) {
      seen.add(d.id);
      let pin = this.pins.get(d.id);
      if (pin && pin.data.kind !== d.kind) { this._dropPin(pin); pin = null; }
      if (!pin) { pin = this._makePin(d); this.pins.set(d.id, pin); }
      const dragging = this._drag && this._drag.id === d.id;
      if (!dragging) { pin.x = num(d.x, 0); pin.z = num(d.z, 0); pin.floor = d.floor; }
      const changed = pin.json !== (pin.json = JSON.stringify(d));
      pin.data = d;
      if (changed) this._paint(pin);
      this._layoutPin(pin);
    }
    for (const [id, pin] of this.pins) {
      if (!seen.has(id)) { this._dropPin(pin); this.pins.delete(id); }
    }
    this.invalidate();
  }

  _makePin(d) {
    const group = new T.Group();
    group.userData.pin = d.id;
    const parts = {}, mats = [];
    const solid = (geo, color) => {
      const mat = new T.MeshLambertMaterial({ color });
      mats.push(mat);
      const mesh = new T.Mesh(geo, mat);
      group.add(mesh);
      return mesh;
    };
    const effect = (geo, map) => {
      const mat = new T.MeshBasicMaterial({ map, transparent: true, depthWrite: false, side: T.DoubleSide });
      const mesh = new T.Mesh(geo, mat);
      mesh.renderOrder = 2;
      group.add(mesh);
      return mesh;
    };
    const pal = PALETTE[this.theme];

    switch (d.kind) {
      case 'light': {
        parts.bulb = solid(this.geo.bulb, pal.device);
        parts.cord = solid(this.geo.rod, pal.dim);
        parts.cord.scale.y = 0.3;
        parts.cord.position.y = 0.27;
        parts.pole = solid(this.geo.pole, pal.fence);
        const glow = new T.Sprite(new T.SpriteMaterial({ map: this.tex.glow, transparent: true, depthWrite: false }));
        glow.renderOrder = 3;
        group.add(glow);
        parts.glow = glow;
        parts.pool = effect(this.geo.plane, this.tex.pool);
        parts.pool.rotation.x = -Math.PI / 2;
        break;
      }
      case 'ac': {
        parts.body = solid(this.geo.box, pal.device);
        parts.body.scale.set(0.9, 0.3, 0.22);
        parts.strip = solid(this.geo.box, pal.dim);
        parts.strip.scale.set(0.66, 0.035, 0.02);
        parts.strip.position.set(0, -0.07, 0.115);
        parts.fan = effect(this.geo.plane, this.tex.fade);
        parts.fan.scale.set(0.8, 1, 1);
        parts.fan.rotation.x = -0.75;
        parts.fan.position.set(0, -0.5, 0.42);
        break;
      }
      case 'plug':
      case 'switch': {
        parts.body = solid(this.geo.box, pal.device);
        parts.body.scale.set(d.kind === 'plug' ? 0.16 : 0.12, d.kind === 'plug' ? 0.16 : 0.18, 0.05);
        parts.led = solid(this.geo.dot, pal.dim);
        parts.led.position.set(0, d.kind === 'plug' ? 0.05 : 0.06, 0.03);
        break;
      }
      case 'climate': {
        parts.body = solid(this.geo.box, pal.device);
        parts.body.scale.set(0.16, 0.16, 0.05);
        parts.face = solid(this.geo.box, pal.dim);
        parts.face.scale.set(0.11, 0.11, 0.01);
        parts.face.position.z = 0.03;
        break;
      }
      case 'contact': {
        parts.body = solid(this.geo.box, pal.device);
        parts.body.scale.set(0.15, 0.05, 0.035);
        parts.body.position.x = -0.06;
        parts.mag = solid(this.geo.box, pal.device);
        parts.mag.scale.set(0.06, 0.05, 0.035);
        parts.mag.position.x = 0.07;
        break;
      }
      case 'occupancy': {
        parts.body = solid(this.geo.dome, pal.device);
        parts.ring = effect(this.geo.ring, null);
        parts.ring.material.color.set(pal.accent);
        break;
      }
      default:
        parts.body = solid(this.geo.ball, pal.device);
    }
    this.devices.add(group);

    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'house-pin';
    label.dataset.pin = d.id;
    const icon = document.createElement('span');
    icon.className = 'house-pin-ic';
    const text = document.createElement('span');
    text.className = 'house-pin-txt';
    const name = document.createElement('span');
    name.className = 'house-pin-name';
    const value = document.createElement('span');
    value.className = 'house-pin-val';
    text.append(name, value);
    label.append(icon, text);
    this.overlay.append(label);
    return { data: d, group, parts, mats, label, icon, name, value, x: 0, z: 0, floor: d.floor, json: '', iconHtml: null };
  }

  _dropPin(pin) {
    this.devices.remove(pin.group);
    pin.mats.forEach(m => m.dispose());
    pin.group.traverse(o => { if (o.material && !pin.mats.includes(o.material)) o.material.dispose(); });
    pin.label.remove();
  }

  /** Colour and glow from the device's state. */
  _paint(pin) {
    const d = pin.data, p = pin.parts, pal = PALETTE[this.theme];
    const live = d.online !== false;
    const rgb = this._c.setRGB(...(d.rgb || [255, 255, 255]).map(v => v / 255), T.SRGBColorSpace);
    const level = clamp(num(d.level, 1), 0, 1);
    const blend = pal.additive ? T.AdditiveBlending : T.NormalBlending;
    for (const mat of pin.mats) {
      mat.transparent = !live;
      mat.opacity = live ? 1 : 0.45;
      mat.emissive.set(0x000000);
    }
    const lit = live && d.active;
    switch (d.kind) {
      case 'light':
        p.bulb.material.color.set(lit ? 0xffffff : pal.device);
        if (lit) p.bulb.material.emissive.copy(rgb).multiplyScalar(0.45 + 0.55 * level);
        p.glow.visible = lit;
        p.glow.material.color.copy(rgb);
        p.glow.material.blending = blend;
        p.glow.material.opacity = 0.35 + 0.5 * level;
        p.glow.scale.setScalar(0.8 + 0.9 * level);
        p.pool.visible = lit;
        p.pool.material.color.copy(rgb);
        p.pool.material.blending = blend;
        p.pool.material.opacity = (pal.additive ? 0.22 : 0.16) + (pal.additive ? 0.5 : 0.3) * level;
        pin.poolRadius = 1.2 + 0.9 * level;
        break;
      case 'ac':
        if (lit) p.strip.material.emissive.copy(rgb);
        p.fan.visible = lit;
        p.fan.material.color.copy(rgb);
        p.fan.material.blending = blend;
        p.fan.material.opacity = pal.additive ? 0.55 : 0.4;
        break;
      case 'plug':
      case 'switch':
        p.led.material.color.set(lit ? 0xffffff : pal.dim);
        if (lit) p.led.material.emissive.set(pal.accent);
        break;
      case 'climate':
        if (live && d.rgb) { p.face.material.color.copy(rgb); p.face.material.emissive.copy(rgb).multiplyScalar(0.35); }
        else p.face.material.color.set(pal.dim);
        break;
      case 'contact':
        if (lit) { p.body.material.emissive.copy(rgb).multiplyScalar(0.6); p.mag.material.emissive.copy(rgb).multiplyScalar(0.6); }
        p.mag.position.x = lit ? 0.16 : 0.07;
        break;
      case 'occupancy':
        if (lit) p.body.material.emissive.set(pal.accent).multiplyScalar(0.5);
        p.ring.visible = lit;
        break;
      default:
        if (lit) p.body.material.emissive.set(pal.accent).multiplyScalar(0.4);
    }

    const label = pin.label;
    if (pin.iconHtml !== d.icon) { pin.icon.innerHTML = d.icon || ''; pin.iconHtml = d.icon; }
    pin.name.textContent = d.name || '';
    pin.value.textContent = d.text || '';
    pin.value.style.color = d.textColor || '';
    label.title = d.name || '';
    label.setAttribute('aria-label', (d.name || '') + (d.text ? ' · ' + d.text : ''));
    label.classList.toggle('is-on', !!lit && d.kind !== 'contact');
    label.classList.toggle('is-alert', !!d.alert);
    label.classList.toggle('is-offline', !live);
    label.classList.toggle('is-pending', !!d.pending);
    label.classList.toggle('is-selected', d.id === this.state.selected);
    label.style.setProperty('--pin', lit && d.rgb ? `rgb(${d.rgb.join(' ')})` : '');
  }

  /** Where a device stands: lamps hang below the ceiling, wall things go
   *  to the nearest wall of their room facing in, a lamp by the fence sits
   *  on it, and a lamp out in the open gets a post. */
  _layoutPin(pin) {
    const info = this.floors.get(pin.floor);
    const show = !!info && info.visible;
    pin.group.visible = show;
    pin.shown = show;
    if (!show) { pin.label.hidden = true; return; }
    pin.label.hidden = false;
    const kind = pin.data.kind, p = pin.parts, H = info.height;
    let x = pin.x, z = pin.z, y = 1.4, rot = 0;
    const room = innermost(info.rooms, x, z);

    if (kind in ON_WALL && isIndoor(room)) {
      const r = room, depth = kind === 'ac' ? 0.13 : 0.05;
      const sides = [
        [x - r.x, () => { x = r.x + depth; rot = Math.PI / 2; }],
        [r.x + r.w - x, () => { x = r.x + r.w - depth; rot = -Math.PI / 2; }],
        [z - r.z, () => { z = r.z + depth; rot = Math.PI; }],
        [r.z + r.d - z, () => { z = r.z + r.d - depth; rot = 0; }],
      ];
      sides.sort((a, b) => a[0] - b[0])[0][1]();
      y = ON_WALL[kind] < 0 ? H + ON_WALL[kind] : ON_WALL[kind];
    } else if (kind === 'light') {
      const fence = info.fences.find(f => Math.abs(num(f.z, 0) - z) < 0.8 && x >= num(f.x0, 0) - 0.3 && x <= num(f.x1, 0) + 0.3);
      const outside = !isIndoor(room);
      if (fence) { z = num(fence.z, 0); y = FENCE_H + 0.24; }
      else y = outside ? 2.5 : H - 0.32;
      p.cord.visible = !outside;
      p.pole.visible = outside && !fence;
      // The post stands from the ground to the lamp; the fence pillar is drawn already.
      p.pole.scale.y = y - 0.12;
      p.pole.position.y = -(y + 0.12) / 2;
      const r = pin.poolRadius || 1.6;
      p.pool.scale.set(r * 2, r * 2, 1);
      p.pool.position.y = -y + (outside ? -0.01 : 0.03);
    } else if (kind === 'occupancy') {
      y = H - 0.05;
      p.ring.position.y = -y + 0.04;
    } else if (kind in ON_WALL) {
      y = ON_WALL[kind] < 0 ? 2.2 : ON_WALL[kind];
    }
    x += info.ox; z += info.oz;
    pin.group.position.set(x, info.base + y, -z);
    pin.group.rotation.y = rot;
    pin.anchor = (pin.anchor || new T.Vector3()).set(x, info.base + y + (kind === 'light' ? 0.32 : 0.22), -z);
  }

  // ------------------------------------------------------------- rendering

  invalidate() {
    if (!this._frame) this._frame = requestAnimationFrame((now) => this._tick(now));
  }

  _tick(now) {
    this._frame = 0;
    let animating = false;
    for (const pin of this.pins.values()) {
      const ring = pin.parts.ring;
      if (ring && ring.visible && pin.shown) {
        // Someone in the room: one slow ring outwards, as on the icon.
        const t = (now % 2600) / 2600;
        const r = 0.35 + 1.25 * t;
        ring.scale.set(r, 1, r);
        ring.material.opacity = 0.65 * (1 - t);
        animating = true;
      }
    }
    this.renderer.render(this.scene, this.camera);
    this._placeLabels();
    if (animating) this.invalidate();
  }

  /** Lay the HTML labels over the drawing: crisp text, in the page's own
   *  font and colours, and real buttons to tap. */
  _placeLabels() {
    const w = this._w, h = this._h, v = this._v;
    // Device labels always stack above room names; within each, nearer on top.
    const put = (el, at, lift, layer) => {
      v.copy(at).project(this.camera);
      if (v.z > 1 || v.x < -1.2 || v.x > 1.2 || v.y < -1.2 || v.y > 1.2) { el.style.visibility = 'hidden'; return; }
      el.style.visibility = '';
      const sx = ((v.x + 1) / 2) * w, sy = ((1 - v.y) / 2) * h;
      el.style.transform = `translate3d(${sx.toFixed(1)}px, ${sy.toFixed(1)}px, 0) translate(-50%, ${lift})`;
      el.style.zIndex = String(layer + Math.round((1 - v.z) * 100000));
    };
    for (const tag of this.roomTags) put(tag.el, tag.at, '-50%', 0);
    for (const pin of this.pins.values()) {
      if (pin.shown && pin.anchor) put(pin.label, pin.anchor, '-100%', 200000);
    }
  }

  _resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this._w = w; this._h = h;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (this.layout && this._wide !== undefined && this._wide !== (w >= h)) {
      // A phone turned on its side: lay the storeys out the other way.
      this._build();
      this.fit();
    } else if (!this._fitted && this.layout) this.fit();
    this.invalidate();
  }

  _place() {
    const cp = Math.cos(this.pitch);
    this.camera.position.set(
      this.target.x + this.dist * Math.sin(this.yaw) * cp,
      this.target.y + this.dist * Math.sin(this.pitch),
      this.target.z + this.dist * Math.cos(this.yaw) * cp,
    );
    this.camera.lookAt(this.target);
  }

  _updateCamera() {
    this._place();
    this._cutaway();
    this.invalidate();
  }

  // ----------------------------------------------------------------- input

  _ray(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const ndc = new T.Vector2(((clientX - rect.left) / rect.width) * 2 - 1, -((clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    return this.raycaster;
  }

  /** Where on a floor the pointer is: upper storeys first, and only where
   *  that storey has a room -- unless `floorId` pins the floor (a drag). A
   *  tap that just misses a room (a hair past the fence) lands inside the
   *  nearest one, up to SNAP metres away. */
  _floorAt(clientX, clientY, floorId) {
    const SNAP = 2;
    const ray = this._ray(clientX, clientY).ray;
    const infos = [...this.floors.values()].filter(i => i.visible && (!floorId || i.floor.id === floorId))
      .sort((a, b) => b.base - a.base);
    const out = (info, x, z) => ({ floor: info.floor.id, x: Math.round(x * 100) / 100, z: Math.round(z * 100) / 100 });
    let near = null;
    for (const info of infos) {
      const hit = ray.intersectPlane(new T.Plane(new T.Vector3(0, 1, 0), -info.base), new T.Vector3());
      if (!hit) continue;
      const x = hit.x - info.ox, z = -hit.z - info.oz;
      if (floorId || innermost(info.rooms, x, z)) return out(info, x, z);
      for (const r of info.rooms) {
        const cx = clamp(x, r.x + 0.15, r.x + r.w - 0.15), cz = clamp(z, r.z + 0.15, r.z + r.d - 0.15);
        const d = Math.hypot(cx - x, cz - z);
        if (d < SNAP && (!near || d < near.d)) near = { d, at: out(info, cx, cz) };
      }
    }
    return near ? near.at : null;
  }

  _tap(e) {
    if (this.state.placing) {
      const at = this._floorAt(e.clientX, e.clientY);
      if (at && this.cb.onPlace) this.cb.onPlace(this.state.placing, at.floor, at.x, at.z);
      return;
    }
    const caster = this._ray(e.clientX, e.clientY);
    const hits = caster.intersectObjects(this.devices.children, true);
    for (const hit of hits) {
      let o = hit.object;
      while (o && !o.userData.pin) o = o.parent;
      if (o && o.visible) { this.cb.onSelect && this.cb.onSelect(o.userData.pin); return; }
    }
    if (this.state.mode === 'plan') {
      const room = caster.intersectObjects(this.roomMeshes || [], false)[0];
      if (room && this.cb.onRoom) { this.cb.onRoom(room.object.userData.floor, room.object.userData.room); return; }
    }
    this.cb.onSelect && this.cb.onSelect(null);
  }

  _bind() {
    const c = this.canvas;
    this._h_down = (e) => {
      c.setPointerCapture(e.pointerId);
      this._ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (this._ptrs.size === 1) {
        this._gesture = { kind: e.button === 2 || e.shiftKey ? 'pan' : 'orbit', x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, moved: false };
      } else if (this._ptrs.size === 2) {
        const [a, b] = [...this._ptrs.values()];
        this._gesture = { kind: 'pinch', d: Math.hypot(a.x - b.x, a.y - b.y), dist: this.dist, mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2, moved: true };
      }
    };
    this._h_move = (e) => {
      if (!this._ptrs.has(e.pointerId)) return;
      this._ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const g = this._gesture;
      if (!g) return;
      if (g.kind === 'pinch' && this._ptrs.size === 2) {
        const [a, b] = [...this._ptrs.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        this.dist = clamp((g.dist * g.d) / Math.max(d, 1), this.minDist, this.maxDist);
        const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
        this._pan(mx - g.mx, my - g.my);
        g.mx = mx; g.my = my;
        this._updateCamera();
        return;
      }
      const dx = e.clientX - g.x, dy = e.clientY - g.y;
      g.x = e.clientX; g.y = e.clientY;
      if (!g.moved && Math.hypot(e.clientX - g.sx, e.clientY - g.sy) > 5) g.moved = true;
      if (!g.moved) return;
      if (g.kind === 'pan') this._pan(dx, dy);
      else {
        this.yaw -= dx * 0.006;
        this.pitch = clamp(this.pitch + dy * 0.005, 0.18, 1.45);
      }
      this._updateCamera();
    };
    this._h_up = (e) => {
      if (!this._ptrs.has(e.pointerId)) return;
      this._ptrs.delete(e.pointerId);
      const g = this._gesture;
      if (this._ptrs.size === 1) {
        // One finger left after a pinch: carry on turning from where it is.
        const [p] = [...this._ptrs.values()];
        this._gesture = { kind: 'orbit', x: p.x, y: p.y, sx: p.x, sy: p.y, moved: true };
        return;
      }
      this._gesture = null;
      if (g && !g.moved && e.type === 'pointerup') this._tap(e);
    };
    this._h_wheel = (e) => {
      e.preventDefault();
      this.dist = clamp(this.dist * Math.exp(e.deltaY * 0.0012), this.minDist, this.maxDist);
      this._updateCamera();
    };
    this._h_menu = (e) => e.preventDefault();
    c.addEventListener('pointerdown', this._h_down);
    c.addEventListener('pointermove', this._h_move);
    c.addEventListener('pointerup', this._h_up);
    c.addEventListener('pointercancel', this._h_up);
    c.addEventListener('wheel', this._h_wheel, { passive: false });
    c.addEventListener('contextmenu', this._h_menu);

    // Labels: a click selects; while arranging, a drag moves the device.
    const o = this.overlay;
    this._l_down = (e) => {
      const el = e.target.closest('[data-pin]');
      if (!el || this.state.mode !== 'arrange' || e.button > 0) return;
      const pin = this.pins.get(el.dataset.pin);
      if (!pin) return;
      e.preventDefault();
      el.setPointerCapture(e.pointerId);
      this._drag = { id: pin.data.id, el, sx: e.clientX, sy: e.clientY, moved: false };
    };
    this._l_move = (e) => {
      const drag = this._drag;
      if (!drag) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.sx, e.clientY - drag.sy) < 5) return;
      drag.moved = true;
      drag.el.classList.add('is-dragging');
      const pin = this.pins.get(drag.id);
      const at = pin && this._floorAt(e.clientX, e.clientY, pin.floor);
      if (!at) return;
      pin.x = at.x; pin.z = at.z;
      this._layoutPin(pin);
      this.invalidate();
    };
    this._l_up = (e) => {
      const drag = this._drag;
      if (!drag) return;
      this._drag = null;
      drag.el.classList.remove('is-dragging');
      const pin = this.pins.get(drag.id);
      if (drag.moved && pin && e.type === 'pointerup') {
        this.cb.onMove && this.cb.onMove(drag.id, pin.floor, Math.round(pin.x * 100) / 100, Math.round(pin.z * 100) / 100);
      } else if (!drag.moved && e.type === 'pointerup') {
        this.cb.onSelect && this.cb.onSelect(drag.id);
      }
    };
    this._l_click = (e) => {
      const el = e.target.closest('[data-pin]');
      // While arranging, pointer clicks are the drag handler's; a keyboard
      // "click" (detail 0) still selects.
      if (!el || (this.state.mode === 'arrange' && e.detail !== 0)) return;
      this.cb.onSelect && this.cb.onSelect(el.dataset.pin);
    };
    o.addEventListener('pointerdown', this._l_down);
    o.addEventListener('pointermove', this._l_move);
    o.addEventListener('pointerup', this._l_up);
    o.addEventListener('pointercancel', this._l_up);
    o.addEventListener('click', this._l_click);
  }

  _unbind() {
    const c = this.canvas, o = this.overlay;
    c.removeEventListener('pointerdown', this._h_down);
    c.removeEventListener('pointermove', this._h_move);
    c.removeEventListener('pointerup', this._h_up);
    c.removeEventListener('pointercancel', this._h_up);
    c.removeEventListener('wheel', this._h_wheel);
    c.removeEventListener('contextmenu', this._h_menu);
    o.removeEventListener('pointerdown', this._l_down);
    o.removeEventListener('pointermove', this._l_move);
    o.removeEventListener('pointerup', this._l_up);
    o.removeEventListener('pointercancel', this._l_up);
    o.removeEventListener('click', this._l_click);
  }

  /** Slide the target across the ground, by screen pixels. */
  _pan(dx, dy) {
    const scale = (this.dist * 1.6) / Math.max(this._h || 1, 1);
    // Screen right, and away from the camera, along the ground. What was
    // under the finger stays under it.
    const rx = Math.cos(this.yaw), rz = -Math.sin(this.yaw);
    const fx = -Math.sin(this.yaw), fz = -Math.cos(this.yaw);
    this.target.x += (-rx * dx + fx * dy) * scale;
    this.target.z += (-rz * dx + fz * dy) * scale;
  }
}
