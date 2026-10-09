// Targhette — ONE plate for /view's three kinds: the user's ⚑ targhetta, the
// agent's tags (tags.json) and the value of a ↔ metro dimension.
//
// A plate is a THREE.Sprite sized in MILLIMETRES (sizeAttenuation): it grows as
// you get closer, like the part it labels, and it is drawn twice — depth-tested
// at full opacity, and a 0.35 ghost with no depth test on top — so the part
// covers it the way it covers anything, yet it never disappears. That stays.
//
// What this module adds (PLAN_VIEW_TOOLS §3, quill: «se zoommi vicino tanto da
// vedere le loro lettere e non più l'intera parola si allontanano da dove
// zoommi e/o diventano trasparenti»): when a plate on screen is too big to be
// read as a WORD — wider than ~60% of the viewport, letters far past reading
// size, or a big plate running off the screen — it moves out of the way of the
// point you are zooming at (the wheel's cursor, the pinch's centre, else the
// middle of the screen): it SLIDES, preferring the direction its stem points,
// until it clears that point; when sliding would push it off the screen it
// FADES to 0.15 instead. ~150 ms either way, with hysteresis (in at 1.0, out at
// 0.8) so it does not blink while you orbit. The anchor dot never moves — it
// still says where. Hover (or tap, on a phone) a plate that has stepped aside
// and it comes back to full opacity while the pointer stays on it.
//
// The viewer draws on demand, so all of this runs in scene.onBeforeRender —
// i.e. only when the camera (or anything else) asks for a frame — and asks for
// more frames itself only while a plate is still on its way.
//
// The top half is pure (numbers in, numbers out — tests/ui/plates.test.cjs);
// the bottom half takes THREE as an argument, so node can load this file.

// ── pure ──────────────────────────────────────────────────────────────────
export const PLATE = {
  WIDTH: 0.92,         // × viewport width. The plan said ~0.6; tuned by eye on
                       //   creepyfinger-v4 g51/g64: an agent tag at 62% or 84% of the
                       //   viewport still reads whole at a glance (28-40 px letters),
                       //   so width alone only counts once it barely fits
  LETTER_PX: 56,       // letter height in CSS px (≈ 4× a 14 px reading size)
  OFF_WIDTH: 0.35,     // a plate this wide that runs off the screen is "unreadable" too
  ENTER: 1.0, EXIT: 0.8,
  ZONE: 0.16,          // the zoom zone: a disk of this × min(viewport side)…
  ZONE_MIN: 44,        // …and at least this many px
  FADE: 0.15,
  MS: 150,
};

// The screen rectangle (CSS px, y down) of a camera-facing sprite: `mv` its
// position in VIEW space, `w`×`h` its size, `cx,cy` its center (THREE's
// Sprite.center), `P` the projection matrix's elements (column-major, THREE's).
export function spriteRect(mv, w, h, cx, cy, P, vw, vh) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const u of [0, 1]) for (const v of [0, 1]) {
    const x = mv[0] + (u - cx) * w, y = mv[1] + (v - cy) * h, z = mv[2];
    const cw = P[3] * x + P[7] * y + P[11] * z + P[15];
    if (!(cw > 1e-9)) return null;                 // behind the camera
    const nx = (P[0] * x + P[4] * y + P[8] * z + P[12]) / cw;
    const ny = (P[1] * x + P[5] * y + P[9] * z + P[13]) / cw;
    const sx = (nx + 1) / 2 * vw, sy = (1 - ny) / 2 * vh;
    x0 = Math.min(x0, sx); x1 = Math.max(x1, sx); y0 = Math.min(y0, sy); y1 = Math.max(y1, sy);
  }
  return { x0, y0, x1, y1 };
}

// px on screen → a VIEW-space offset at depth mv.z (so a slid plate keeps its size)
export function pxToView(dx, dy, z, P, vw, vh) {
  const w = P[11] * z + P[15];
  return [dx * 2 / vw * w / P[0], -dy * 2 / vh * w / P[5]];
}

// How far past "readable as a word" a plate is: ≥ 1 is too big. Continuous in
// the plate's size, so the hysteresis has something to bite on.
export function crowding(r, vw, vh, letter = 0.5, o = PLATE) {
  if (!r) return 0;
  const w = r.x1 - r.x0, h = r.y1 - r.y0;
  let s = Math.max(w / (o.WIDTH * vw), (h * letter) / o.LETTER_PX);
  const off = r.x0 < 0 || r.y0 < 0 || r.x1 > vw || r.y1 > vh;
  const onScreen = r.x1 > 0 && r.y1 > 0 && r.x0 < vw && r.y0 < vh;
  // a plate partly off the screen cannot be read whole — but a SMALL one at the
  // edge is just a plate at the edge (you panned): only a big one counts
  if (off && onScreen) s = Math.max(s, w / (o.OFF_WIDTH * vw), (h * letter) / (0.6 * o.LETTER_PX));
  return s;
}

export function hysteresis(on, s, o = PLATE) {
  return on ? s >= o.EXIT : s >= o.ENTER;
}

const distToRect = (r, x, y) => Math.hypot(Math.max(r.x0 - x, 0, x - r.x1), Math.max(r.y0 - y, 0, y - r.y1));
const shift = (r, dx, dy) => ({ x0: r.x0 + dx, y0: r.y0 + dy, x1: r.x1 + dx, y1: r.y1 + dy });

// Where a too-big plate goes. `f` the zoom point (px), `hint` the plate's stem
// as seen on screen (px, y down) or null, `keep` the points it must not land
// on (px: its own anchor — a plate slid over the place it points at would
// defeat itself, measured on g64#a1 seen from above). Returns
//   { mode: 'stay' }               — it does not cover the zoom zone and it is
//                                    whole on screen: leave it
//   { mode: 'slide', dx, dy }      — move it by this many px, it stays on screen
//   { mode: 'fade' }               — no room to slide: fade where it is
export function escape(r, f, vw, vh, hint = null, keep = [], o = PLATE) {
  const R = Math.max(o.ZONE_MIN, o.ZONE * Math.min(vw, vh)), tol = 2;
  const cut = s => s.x0 < -tol || s.y0 < -tol || s.x1 > vw + tol || s.y1 > vh + tol;
  // clear of the zoom point: a whole plate stays; a big one cut by the edge of
  // the screen shows only giant pieces of letters (and its stem across the
  // part), so it fades
  if (distToRect(r, f[0], f[1]) >= R) return cut(r) ? { mode: 'fade' } : { mode: 'stay' };
  const cx = (r.x0 + r.x1) / 2, cy = (r.y0 + r.y1) / 2;
  let ax = cx - f[0], ay = cy - f[1], al = Math.hypot(ax, ay);
  if (al < 1e-6) { ax = 0; ay = -1; al = 1; }       // dead centre: up
  ax /= al; ay /= al;
  let ux = ax, uy = ay;
  const hl = hint ? Math.hypot(hint[0], hint[1]) : 0;
  // «lungo lo stelo, verso fuori»: the stem's direction when it leads away from
  // the zoom point, else straight away from it
  if (hl > 1e-6 && (hint[0] * ax + hint[1] * ay) / hl > 0.2) { ux = hint[0] / hl; uy = hint[1] / hl; }
  const span = Math.hypot(r.x1 - r.x0, r.y1 - r.y0) + 2 * R;
  let t = -1;
  for (let k = 4; k <= span * 1.5; k += 4)
    if (distToRect(shift(r, ux * k, uy * k), f[0], f[1]) >= R) { t = k; break; }
  if (t < 0) return { mode: 'fade' };
  for (let lo = t - 4, hi = t, i = 0; i < 8; i++) {   // refine to the px
    const m = (lo + hi) / 2;
    if (distToRect(shift(r, ux * m, uy * m), f[0], f[1]) >= R) { hi = m; t = m; } else lo = m;
  }
  const s = shift(r, ux * t, uy * t), m = 0.25 * R;
  if (cut(s) || keep.some(q => distToRect(s, q[0], q[1]) < m)) return { mode: 'fade' };
  return { mode: 'slide', dx: ux * t, dy: uy * t };
}

// one step of an exponential approach that lands in ~`ms`
export function ease(cur, target, dt, ms = PLATE.MS) {
  const k = 1 - Math.exp(-dt / (ms / 3));
  return cur + (target - cur) * Math.min(1, Math.max(0, k));
}

export const inRect = (r, x, y) => !!r && x >= r.x0 && x <= r.x1 && y >= r.y0 && y <= r.y1;

// ── three ─────────────────────────────────────────────────────────────────
export const GHOST = 0.35;
// No registry: the plates are found in the scene every frame (it holds a few
// dozen objects), so a plate erased, redrawn or hidden needs no bookkeeping.
let plates = [];                 // those seen in the last frame
const S = { THREE: null, viewer: null, focus: null, ptr: null, touches: new Map(), tap: null, last: 0, debug: [] };

// The plate itself: two sprites (solid + ghost) and a leader line drawn while it
// is slid away from where it belongs. `pos` is where the plate belongs; `stem`
// (world vector) the direction it may slide along; `fade` extra materials that
// fade WITH the plate (the stem — never the anchor dot); `place(cam)` returns
// the sprite center for this frame (the metro's value stands beside its line
// and decides which side every frame). Returns the Group.
export function plateSprites(THREE, { tex, w, h, pos, center = [0.5, 0], ink = '#fff', order = [3, 10],
                                       stem = null, fade = [], place = null, letter = null, keep = [] }) {
  const g = new THREE.Group();
  const sprites = [];
  for (const ghost of [false, true]) {
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, toneMapped: false, transparent: true,
      opacity: ghost ? GHOST : 1, depthTest: !ghost, depthWrite: false, sizeAttenuation: true }));
    sp.center.set(center[0], center[1]);
    sp.position.copy(pos); sp.scale.set(w, h, 1);
    sp.renderOrder = ghost ? order[1] : order[0];
    sp.userData.ghost = ghost;
    sprites.push(sp); g.add(sp);
  }
  const lineGeo = new THREE.BufferGeometry().setFromPoints([pos.clone(), pos.clone()]);
  const line = new THREE.Line(lineGeo, new THREE.LineBasicMaterial({ color: ink, toneMapped: false,
    transparent: true, opacity: 0, depthTest: false, depthWrite: false }));
  line.renderOrder = order[1]; line.visible = false; line.raycast = () => {};
  g.add(line);
  const st = {
    sprites, line, base: pos.clone(), w, h, stem: stem ? stem.clone().normalize() : null,
    fade: fade.map(m => ({ m, op: m.opacity })), place, keep: keep.map(k => k.clone()),
    letter: letter ?? (tex && tex.userData && tex.userData.letter) ?? 0.5,
    on: false, mode: 'stay', dx: 0, dy: 0, tdx: 0, tdy: 0, op: 1, top: 1, held: false, hover: false, tapHeld: false,
    shown: null, rect: null, group: g,
  };
  g.userData.plate = st;
  return g;
}

// ⚑ — the anchor dot on the surface, a stem along the normal, the plate on the
// tip of the stem. One builder for the user's targhetta and the agent's tag.
export function makePlate(THREE, { at, n, stem, tex, w, h, ink, letter = null }) {
  const g = new THREE.Group();
  n = n.clone().normalize();
  const tip = at.clone().addScaledVector(n, stem);
  const r = Math.max(h * 0.09, 0.03);                // the anchor dot
  const dotG = new THREE.SphereGeometry(r, 12, 8);
  const stemG = new THREE.CylinderGeometry(r * 0.35, r * 0.35, stem, 8);
  stemG.translate(0, stem / 2, 0);
  const qn = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), n);
  const fade = [];
  for (const ghost of [false, true]) {
    const opt = { color: ink, toneMapped: false, transparent: true, opacity: ghost ? GHOST : 1,
                  depthTest: !ghost, depthWrite: !ghost };
    const dot = new THREE.Mesh(dotG, new THREE.MeshBasicMaterial(opt)); dot.position.copy(at);
    const sm = new THREE.MeshBasicMaterial(opt); fade.push(sm);
    const st = new THREE.Mesh(stemG, sm); st.position.copy(at); st.quaternion.copy(qn);
    for (const o of [dot, st]) { o.renderOrder = ghost ? 10 : 3; g.add(o); }
  }
  g.add(plateSprites(THREE, { tex, w, h, pos: tip, center: [0.5, 0], ink, stem: n, fade, letter, keep: [at] }));
  return g;
}

// Called once by view.html. Listens (passively — it never takes a gesture) for
// where the zoom is aimed and where the pointer is, and hooks the per-frame pass.
export function installPlates(THREE, viewer, el) {
  S.THREE = THREE; S.viewer = viewer;
  const canvas = viewer.renderer.domElement;
  const local = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  // Zooming at the cursor puts the cursor ON the plate that is in the way, so
  // "the pointer is over it" cannot mean "I am looking for it": a plate is held
  // only when the pointer ENTERS it where it now stands, and a wheel lets go.
  el.addEventListener('wheel', e => {
    S.focus = local(e);
    for (const p of plates) p.hover = false;
  }, { capture: true, passive: true });
  el.addEventListener('pointerdown', e => {
    if (e.pointerType === 'touch') {
      S.touches.set(e.pointerId, local(e));
      if (S.touches.size === 2) { const [a, b] = [...S.touches.values()]; S.focus = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }
    } else if (e.button === 0 || e.button === 2) S.focus = null;   // an orbit/pan: the zoom point is the middle again
    // a TAP (one finger, still, short) on a plate that stepped aside brings it
    // back; the next tap anywhere else lets go. Decided on lift, not on press:
    // the first finger of a pinch lands on the plate too.
    S.tap = e.pointerType === 'touch' && S.touches.size === 1 ? { id: e.pointerId, xy: local(e), t: performance.now() } : null;
  }, { capture: true, passive: true });
  el.addEventListener('pointermove', e => {
    if (e.pointerType === 'touch') {
      if (S.touches.has(e.pointerId)) S.touches.set(e.pointerId, local(e));
      if (S.touches.size > 1) S.tap = null;
      if (S.touches.size === 2) { const [a, b] = [...S.touches.values()]; S.focus = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }
      return;
    }
    const was = S.ptr; S.ptr = local(e);
    let changed = false;
    for (const p of plates) {
      if (!p.on) { p.hover = false; continue; }
      const now = inRect(p.shown, S.ptr[0], S.ptr[1]), before = !!was && inRect(p.shown, was[0], was[1]);
      if (now && !before && !p.hover) { p.hover = true; changed = true; }
      else if (!now && p.hover) { p.hover = false; changed = true; }
    }
    if (changed) viewer.invalidate();
  }, { capture: true, passive: true });
  const up = e => {
    S.touches.delete(e.pointerId);
    const tp = S.tap; S.tap = null;
    if (e.type !== 'pointerup' || !tp || tp.id !== e.pointerId) return;
    const [x, y] = local(e);
    if (Math.hypot(x - tp.xy[0], y - tp.xy[1]) > 12 || performance.now() - tp.t > 500) return;
    let changed = false;
    for (const p of plates) {
      const hit = p.on && inRect(p.shown, x, y);
      if (p.tapHeld !== hit) { p.tapHeld = hit; changed = true; }
    }
    if (changed) viewer.invalidate();
  };
  el.addEventListener('pointerup', up, { capture: true, passive: true });
  el.addEventListener('pointercancel', up, { capture: true, passive: true });
  el.addEventListener('pointerleave', e => {
    if (e.pointerType === 'touch') return;
    S.ptr = null;
    if (plates.some(p => p.hover)) { for (const p of plates) p.hover = false; viewer.invalidate(); }
  });
  const prev = viewer.scene.onBeforeRender;
  viewer.scene.onBeforeRender = function (renderer, scene, camera, target) {
    if (prev) prev.apply(this, arguments);
    if (!target) update(camera);                    // the main frame only, not the bloom pass
  };
  window.__noodlePlates = { debug: () => S.debug, PLATE, plates: () => plates };
}

function update(cam) {
  const THREE = S.THREE, viewer = S.viewer;
  const now = performance.now(), dt = Math.min(S.last ? now - S.last : 16, 250);
  S.last = now;
  const canvas = viewer.renderer.domElement, rr = canvas.getBoundingClientRect();
  const vw = rr.width, vh = rr.height;
  if (!(vw > 0 && vh > 0)) return;
  cam.updateMatrixWorld();
  const P = cam.projectionMatrix.elements, V = cam.matrixWorldInverse;
  const f = S.focus || [vw / 2, vh / 2];
  const mv = new THREE.Vector3(), tip = new THREE.Vector3(), right = new THREE.Vector3(), upv = new THREE.Vector3();
  right.setFromMatrixColumn(cam.matrixWorld, 0); upv.setFromMatrixColumn(cam.matrixWorld, 1);
  let moving = false;
  S.debug = [];
  plates = [];
  viewer.scene.traverseVisible(o => { if (o.userData.plate) plates.push(o.userData.plate); });
  for (const p of plates) {
    const sp = p.sprites[0];
    if (p.place) { const c = p.place(cam); for (const s of p.sprites) s.center.set(c[0], c[1]); }
    const wp = p.base.clone().applyMatrix4(p.group.matrixWorld);
    mv.copy(wp).applyMatrix4(V);
    const r = spriteRect([mv.x, mv.y, mv.z], p.w, p.h, sp.center.x, sp.center.y, P, vw, vh);
    p.rect = r;
    const s = crowding(r, vw, vh, p.letter);
    p.on = hysteresis(p.on, s);
    let tdx = 0, tdy = 0, top = 1;
    if (p.on && r) {
      let hint = null;
      if (p.stem) {
        tip.copy(p.base).add(p.stem).applyMatrix4(p.group.matrixWorld).applyMatrix4(V);
        const t = spriteRect([tip.x, tip.y, tip.z], 0, 0, 0, 0, P, vw, vh), b = spriteRect([mv.x, mv.y, mv.z], 0, 0, 0, 0, P, vw, vh);
        if (t && b) hint = [t.x0 - b.x0, t.y0 - b.y0];
      }
      const keep = p.keep.map(k => { tip.copy(k).applyMatrix4(p.group.matrixWorld).applyMatrix4(V);
                                     const q = spriteRect([tip.x, tip.y, tip.z], 0, 0, 0, 0, P, vw, vh); return q && [q.x0, q.y0]; })
                         .filter(Boolean);
      const e = escape(r, f, vw, vh, hint, keep);
      p.mode = e.mode;
      if (e.mode === 'slide') { tdx = e.dx; tdy = e.dy; }
      // the further past readable, the less there is to read: 0.15 at the
      // threshold, down to 0.06 for a plate many times too big (g51 zoomed in:
      // letters the height of the screen still showed at 0.15)
      else if (e.mode === 'fade') top = PLATE.FADE * Math.min(1, Math.max(0.4, 2 / s));
    } else p.mode = 'stay';
    // hover / tap on a plate that stepped aside: full again, where it is now
    if (!p.on) p.hover = p.tapHeld = false;
    p.held = p.on && (p.tapHeld || p.hover);
    if (p.held) top = 1;
    p.tdx = tdx; p.tdy = tdy; p.top = top;
    p.dx = ease(p.dx, tdx, dt); p.dy = ease(p.dy, tdy, dt); p.op = ease(p.op, top, dt);
    if (Math.abs(p.dx - tdx) < 0.5) p.dx = tdx;
    if (Math.abs(p.dy - tdy) < 0.5) p.dy = tdy;
    if (Math.abs(p.op - top) < 0.01) p.op = top;
    if (p.dx !== tdx || p.dy !== tdy || p.op !== top) moving = true;
    // apply: a view-space offset at the plate's own depth, so it keeps its size
    const [ox, oy] = pxToView(p.dx, p.dy, mv.z, P, vw, vh);
    const off = right.clone().multiplyScalar(ox).addScaledVector(upv, oy);
    // the offset is in WORLD units; the sprites live in the group's frame
    const inv = new THREE.Matrix4().copy(p.group.matrixWorld).invert();
    const lp = wp.clone().add(off).applyMatrix4(inv);
    for (const s of p.sprites) {
      s.position.copy(lp);
      s.material.opacity = (s.userData.ghost ? GHOST : 1) * p.op;
    }
    for (const { m, op } of p.fade) m.opacity = op * p.op;
    const far = Math.hypot(p.dx, p.dy) > 3;
    p.line.visible = far;
    if (far) {
      const a = p.line.geometry.attributes.position;
      a.setXYZ(0, p.base.x, p.base.y, p.base.z); a.setXYZ(1, lp.x, lp.y, lp.z); a.needsUpdate = true;
      p.line.geometry.computeBoundingSphere();
      p.line.material.opacity = 0.7 * p.op;
    }
    p.shown = r && shift(r, p.dx, p.dy);
    S.debug.push({ s: +s.toFixed(3), on: p.on, mode: p.mode, held: p.held, dx: Math.round(p.dx), dy: Math.round(p.dy),
                   op: +p.op.toFixed(2), w: r ? Math.round(r.x1 - r.x0) : null, h: r ? Math.round(r.y1 - r.y0) : null });
  }
  // this runs INSIDE the viewer's frame: the viewer clears its dirty flag
  // before drawing, so asking for the next frame from here is kept (it used to
  // be eaten and the plate froze half way — seen: stuck at 0.69 on g51)
  if (moving) viewer.invalidate();
}
