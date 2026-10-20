// 🖥 Schermo and ∿ Curva — drawing on a pane of glass in front of the part.
//
// quill: «un bottone schermo che è come piano ma proietta il piano dove guardi
// e blocca la vista, disegna sullo schermo davanti al pezzo, e una curva che fa
// la stessa cosa ma con una curva spline a punti spostabili».
//
// ⊞ Piano draws in the VOID and still on the part where the ray meets it. Here
// the part is never drawn on: taking the tool puts a plane square to the view,
// just in front of the nearest visible point of the part, and LOCKS the view —
// the screen becomes a sheet of glass you draw on. Put the tool down (✋ Muovi,
// another tool, ✕) and the view is free again; take it back and the glass is
// re-laid where you are looking now. 🔒 next to the tool frees the view without
// leaving it: then every new stroke / curve lays its own glass where you look.
//
// ∿ Curva: tap to add a point, drag a point to move it, drag on the curve to
// insert one, double tap a point to remove it; Enter or ✓ ends the curve (so
// does putting the tool down), Esc drops it, ↶ / Ctrl+Z takes the last point
// back. ◯ closes it into a loop. The curve is a centripetal Catmull-Rom through
// the points (no loops, no cusps between close points).
//
// Data: both make ordinary pen strokes with `plane: {origin, normal}`, exactly
// like ⊞ Piano's strokes in the void — the server already reports them as marks
// of kind "plane" with the nearest piece, the eraser already finds them.

import { registerTool, ctx as C } from '/static/view-tools.js';

const INK_COLOR = '#38bdf8';
const r4 = x => Math.round(x * 1e4) / 1e4;
const st = {
  locked: false,                               // the view is held (controls off)
  free: false,                                 // 🔒 off: the view moves, each stroke lays its own glass
  plane: null,                                 // {origin, normal} V3 — normal towards the camera
  camSig: '',
  closed: false,
  cur: null,                                   // the curve being edited {plane, data, ctrl:[V3], s, g}
  drag: null,                                  // {i, id}
  lastTap: { i: -1, t: 0 },
  svg: null, raf: 0, el: {},
};
const isScreenTool = d => !!d && (d.id === 'screen' || d.id === 'curve');
export const on = def => isScreenTool(def);
export const editing = () => !!st.cur;

// ── the glass ──
function camSig() {
  const c = C.viewer.camera;
  return [...c.position.toArray(), ...c.quaternion.toArray(), c.zoom].map(r4).join();
}
// the nearest visible vertex along the view direction (the bbox's nearest
// corner can stick out far in front of the part in an iso view)
function frontDepth(f, eye) {
  const T = C.THREE, root = C.viewer.previewGroup, v = new T.Vector3();
  root.updateMatrixWorld(true);
  const shown = o => { for (let p = o; p && p !== root; p = p.parent) if (!p.visible) return false; return true; };
  let best = Infinity;
  root.traverse(o => {
    if (!o.geometry || !o.geometry.attributes.position || !shown(o) || o.isInstancedMesh) return;
    if (o.userData.lookProxy != null) return;
    const a = o.geometry.attributes.position, M = o.matrixWorld;
    for (let i = 0; i < a.count; i++) {
      v.fromBufferAttribute(a, i).applyMatrix4(M);
      const d = v.sub(eye).dot(f);
      if (d < best) best = d;
    }
  });
  return best;
}
function layGlass() {
  const T = C.THREE, cam = C.viewer.camera;
  cam.updateMatrixWorld(true);
  const f = cam.getWorldDirection(new T.Vector3()), eye = cam.position.clone();
  let d = frontDepth(f, eye);
  if (!isFinite(d)) d = C.viewer.controls.target.clone().sub(eye).dot(f);
  if (cam.isPerspectiveCamera) d = Math.max(d, cam.near * 4);
  const p = eye.clone().addScaledVector(f, d);
  d -= 2 * C.mmPerPx(p);                       // a hair in front: ink never sinks into the face it covers
  st.plane = { origin: eye.addScaledVector(f, d), normal: f.negate() };
  st.camSig = camSig();
  return st.plane;
}
const planeData = pl => ({ origin: pl.origin.toArray().map(r4), normal: pl.normal.toArray().map(r4) });
// the glass for a new stroke: re-laid whenever the view has moved since
// (🔓 free, F frame, the axes gizmo, a resize…) — strokes keep their own copy
function glass() {
  if (!st.plane || camSig() !== st.camSig) layGlass();
  return st.plane;
}
function ndc(x, y) {
  const r = C.$('c').getBoundingClientRect();
  return new C.THREE.Vector2(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1);
}
function hitOn(pl, x, y) {
  const T = C.THREE, ray = new T.Raycaster();
  ray.setFromCamera(ndc(x, y), C.viewer.camera);
  const p = ray.ray.intersectPlane(new T.Plane().setFromNormalAndCoplanarPoint(pl.normal, pl.origin), new T.Vector3());
  return p ? { p, n: pl.normal.clone(), key: null, mesh: null, plane: planeData(pl) } : null;
}
// 🖥 where the pen goes: always on the glass, never on the part
export function hit(x, y) { return hitOn(glass(), x, y); }
// the start of a 🖥 stroke: with the view free, lay the glass where you look
export function strokeStart() { if (st.free) layGlass(); }
function toScreen(p) {
  const r = C.$('c').getBoundingClientRect(), q = p.clone().project(C.viewer.camera);
  return [(q.x + 1) / 2 * r.width, (1 - q.y) / 2 * r.height];
}

// ── the lock ──
function lock(want) {
  const ctl = C.viewer.controls;
  if (want && !st.locked) { st.saved = ctl.enabled; ctl.enabled = false; st.locked = true; }
  else if (!want && st.locked) { ctl.enabled = st.saved !== false; st.locked = false; }
  C.vp.classList.toggle('screen-lock', st.locked);
}
// after anything that may change it (view.html's syncDraw calls it)
let busy = false;
export function refresh() {
  if (busy || !C.viewer) return;
  busy = true;
  try {
    const def = C.toolDef(), active = C.isDrawing() && on(def);
    if (st.cur && st.cur.s && !C.inDraft(st.cur.s)) { st.cur = null; st.drag = null; }   // 🗑 / Fatto took it
    if (!active || (def && def.id !== 'curve')) commit();
    if (active && !st.active) layGlass();       // taking the tool: the glass goes where you look
    st.active = active;
    lock(active && !st.free);
    const { locks = [], closeBtn } = st.el;
    for (const b of locks) {                    // one lock, a button in each tool's row
      b.textContent = st.free ? '🔓' : '🔒'; b.classList.toggle('on', !st.free);
      b.title = st.free ? 'Vista libera: ogni tratto si posa dove guardi — tocca per bloccarla'
        : 'Vista bloccata: tocca per liberarla (la rotella, il destro e due dita tornano a muoverla)';
    }
    if (closeBtn) closeBtn.classList.toggle('on', st.closed);
    drawHandles();
  } finally { busy = false; }
}

// ── ∿ the curve ──
function samples(cur) {
  const T = C.THREE, n = cur.ctrl.length;
  if (n < 2) return cur.ctrl.map(p => p.clone());
  const closed = st.closed && n >= 3;
  const crv = new T.CatmullRomCurve3(cur.ctrl, closed, 'centripetal');
  const px = crv.getLength() / Math.max(C.mmPerPx(cur.plane.origin), 1e-6);
  const k = Math.min(800, Math.max(12, Math.ceil(px / 3)));
  const pts = crv.getSpacedPoints(k);
  if (closed) pts[pts.length - 1] = pts[0].clone();
  return pts;
}
function rebuild() {
  const cur = st.cur;
  if (!cur) return;
  if (cur.s && !C.inDraft(cur.s)) cur.s = null;   // 🗑 / a resume took it away
  if (!cur.ctrl.length) { if (cur.s) C.dropStroke(cur.s); st.cur = null; drawHandles(); C.syncDraw(); return; }
  const pts = samples(cur), n = cur.plane.normal;
  if (!cur.s) cur.s = C.newStroke({ p: pts[0], n: n.clone(), key: null, plane: cur.data }, cur.g);
  cur.s.pts = pts; cur.s.nrm = pts.map(() => n.clone()); cur.s.lift = pts.map(() => 0);
  delete cur.s._p3;
  C.redrawStroke(cur.s);
  drawHandles();
  C.syncDraw();
}
export function commit() {
  const cur = st.cur;
  st.cur = null; st.drag = null;
  if (!cur || !cur.s || !C.inDraft(cur.s)) { drawHandles(); return false; }
  C.pushAction({ type: 'pen', g: cur.g });
  C.takeView([cur.s]);
  drawHandles();
  queueMicrotask(() => C.syncDraw());           // ↶ lights up
  return true;
}
function dropCurve() {
  const cur = st.cur;
  st.cur = null; st.drag = null;
  if (cur && cur.s && C.inDraft(cur.s)) C.dropStroke(cur.s);
  drawHandles(); C.syncDraw();
}
// ↶ while a curve is open: its last point, not the action before it
export function undoPoint() {
  if (!st.cur) return false;
  st.cur.ctrl.pop(); rebuild();
  return true;
}
const near = e => e.pointerType === 'mouse' ? 9 : 18;
function handleAt(x, y, R) {
  const r = C.$('c').getBoundingClientRect();
  let best = -1, bd = R;
  st.cur.ctrl.forEach((p, i) => {
    const [a, b] = toScreen(p), d = Math.hypot(a + r.left - x, b + r.top - y);
    if (d <= bd) { bd = d; best = i; }
  });
  return best;
}
// on the curve between two points? → the index to insert at
function segmentAt(x, y, R) {
  const cur = st.cur, n = cur.ctrl.length;
  if (n < 2 || !cur.s) return -1;
  const r = C.$('c').getBoundingClientRect(), pts = cur.s.pts;
  let bi = -1, bd = R;
  for (let i = 1; i < pts.length; i++) {
    const [ax, ay] = toScreen(pts[i - 1]), [bx, by] = toScreen(pts[i]);
    const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
    const t = l2 ? Math.max(0, Math.min(1, ((x - r.left - ax) * dx + (y - r.top - ay) * dy) / l2)) : 0;
    const d = Math.hypot(ax + t * dx - (x - r.left), ay + t * dy - (y - r.top));
    if (d <= bd) { bd = d; bi = i; }
  }
  if (bi < 0) return -1;
  // the samples are evenly spaced along the curve: which span of points is it in?
  const segs = st.closed && n >= 3 ? n : n - 1;
  return Math.min(n, Math.floor((bi / (pts.length - 1)) * segs) + 1);
}
function curveDown(e) {
  if (e.button !== 0 || !e.isPrimary || e.target !== C.$('c')) return;
  e.stopPropagation(); e.preventDefault();
  try { C.$('c').setPointerCapture(e.pointerId); } catch {}
  if (!st.cur) {
    if (st.free) layGlass();
    const pl = glass();
    st.cur = { plane: { origin: pl.origin.clone(), normal: pl.normal.clone() }, data: planeData(pl), ctrl: [], s: null, g: C.nextGesture() };
  }
  const cur = st.cur, R = near(e);
  let i = handleAt(e.clientX, e.clientY, R);
  const now = performance.now();
  if (i >= 0 && st.lastTap.i === i && now - st.lastTap.t < 350) {   // double tap: the point goes
    cur.ctrl.splice(i, 1); st.lastTap = { i: -1, t: 0 }; rebuild(); return;
  }
  if (i < 0) {
    const h = hitOn(cur.plane, e.clientX, e.clientY);
    if (!h) return;
    const at = segmentAt(e.clientX, e.clientY, R);
    i = at >= 0 ? at : cur.ctrl.length;
    cur.ctrl.splice(i, 0, h.p);
    rebuild();
  }
  st.lastTap = { i, t: now };
  st.drag = { i, id: e.pointerId, x: e.clientX, y: e.clientY };
}
function curveMove(e) {
  const d = st.drag;
  if (!d || e.pointerId !== d.id || !st.cur) return;
  e.stopPropagation();
  if (Math.hypot(e.clientX - d.x, e.clientY - d.y) > 4) st.lastTap = { i: -1, t: 0 };   // a drag is no tap
  const h = hitOn(st.cur.plane, e.clientX, e.clientY);
  if (!h) return;
  st.cur.ctrl[d.i] = h.p; rebuild();
}
function curveUp(e) {
  if (!st.drag || e.pointerId !== st.drag.id) return;
  e.stopPropagation();
  st.drag = null;
}

// ── the handles: an SVG over the canvas (2D — they are screen things) ──
function drawHandles() {
  const svg = st.svg;
  if (!svg) return;
  const cur = st.cur, show = !!cur && C.isDrawing() && C.toolDef() && C.toolDef().id === 'curve';
  svg.style.display = show ? '' : 'none';
  cancelAnimationFrame(st.raf);
  if (!show) { svg.innerHTML = ''; return; }
  const col = (C.pen && C.pen.color) || INK_COLOR;
  const P = cur.ctrl.map(toScreen);
  const poly = P.map(p => p.map(v => v.toFixed(1)).join(',')).join(' ');
  svg.innerHTML = (P.length > 1 ? `<polyline points="${poly}${st.closed && P.length > 2 ? ' ' + P[0].join(',') : ''}" fill="none" stroke="#fff" stroke-opacity=".45" stroke-dasharray="4 4" stroke-width="1"/>` : '')
    + P.map(([x, y], i) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${i === P.length - 1 ? 7 : 6}" fill="${col}" fill-opacity=".35" stroke="#fff" stroke-width="2"/>`).join('');
  // the view can still move (🔓, a resize): follow it
  const sig = camSig() + C.$('c').clientWidth + 'x' + C.$('c').clientHeight;
  const follow = () => {
    const s2 = camSig() + C.$('c').clientWidth + 'x' + C.$('c').clientHeight;
    if (s2 !== sig) drawHandles(); else st.raf = requestAnimationFrame(follow);
  };
  st.raf = requestAnimationFrame(follow);
}

function onKey(e) {
  if (!C.isDrawing() || !st.cur || e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const k = e.key.toLowerCase();
  if (e.key === 'Enter') { e.preventDefault(); e.stopImmediatePropagation(); commit(); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopImmediatePropagation(); dropCurve(); }
  else if (e.key === 'Backspace' || e.key === 'Delete' || ((e.ctrlKey || e.metaKey) && k === 'z' && !e.shiftKey)) {
    e.preventDefault(); e.stopImmediatePropagation(); undoPoint();
  }
}

const HINT_LOCK = () => st.free ? 'vista libera (🔓): ogni tratto si posa dove guardi'
  : 'vista bloccata — ✋ Muovi o 🔒 per girarla, poi torna qui e il vetro si rimette dove guardi';
export function install(penGesture) {
  const style = document.createElement('style');
  style.textContent = `
  #vp.screen-lock::after{content:'';position:absolute;inset:0;pointer-events:none;z-index:3;
    box-shadow:inset 0 0 0 2px ${INK_COLOR}88, inset 0 0 40px ${INK_COLOR}22;}
  #d-curvesvg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:4;overflow:visible;}
  #dbar .tool.mini{min-width:0;padding-left:7px;padding-right:7px;}`;
  document.head.appendChild(style);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'd-curvesvg'; svg.style.display = 'none';
  document.getElementById('vp').appendChild(svg); st.svg = svg;   // ctx is filled after install
  const btn = (txt, title, fn) => { const b = document.createElement('button'); b.className = 'btn tool mini';
    b.textContent = txt; b.title = title; b.onclick = e => { e.stopPropagation(); fn(); }; return b; };
  const toggleLock = () => { st.free = !st.free; if (!st.free) layGlass(); C.syncDraw(); };
  const lockS = btn('🔒', '', toggleLock), lockC = btn('🔒', '', toggleLock);
  const closeBtn = btn('◯', 'Chiusa: la curva torna al primo punto', () => { st.closed = !st.closed; rebuild(); C.syncDraw(); });
  const done = btn('✓', 'Fine curva (Invio) — la prossima pressione ne inizia una nuova', () => commit());
  st.el = { locks: [lockS, lockC], closeBtn };
  addEventListener('keydown', onKey, true);
  registerTool({ id: 'screen', tab: 'pencil', mode: 'pen', screen: true, ownsTaps: true, icon: '🖥', label: 'Schermo', cursor: 'crosshair',
    title: 'Schermo: blocca la vista e disegna su un vetro davanti al pezzo, dove guardi — mai sul pezzo',
    hint: () => 'Schermo: disegna sul vetro davanti al pezzo · ' + HINT_LOCK(),
    options: () => [lockS], ...penGesture });
  registerTool({ id: 'curve', tab: 'pencil', mode: 'curve', screen: true, icon: '∿', label: 'Curva', cursor: 'crosshair', ownsTaps: true,
    title: 'Curva: come Schermo, ma una spline a punti spostabili — tocca per aggiungere, trascina per spostare, doppio tocco toglie, Invio finisce',
    hint: () => 'Curva: tocca per aggiungere un punto, trascina un punto per spostarlo, trascina sulla curva per inserirne uno, doppio tocco lo toglie · Invio/✓ finisce, Esc annulla, ↶ toglie l\'ultimo · ' + HINT_LOCK(),
    options: () => [closeBtn, done, lockC],
    down: curveDown, move: curveMove, up: curveUp, cancel: curveUp });
}
