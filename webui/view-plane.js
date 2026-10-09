// ⊞ Piano — the pencil's working plane (PLAN_VIEW_TOOLS §4).
//
// quill: «fai una modalità più appropriata per fare la z tipo ZBrush, disegni
// sui piani anche se non c'è un pezzo». The pen used to draw only where the ray
// met the part; a drag off it moved the view. With a plane chosen here — XY,
// XZ, YZ, or Vista (perpendicular to the camera, fixed when you pick it) — a
// drag in the void draws ON THE PLANE, and over the part it still draws on the
// part: one stroke can start on an arm and run on into the air («questo braccio
// va prolungato fin qui»). It serves ✎ penna, ✎³ penna 3D and T vernice, which
// all go through view.html's inkHit(): surfaceHit first, then planeHit here.
//
// Where the plane is: through the last point a stroke started ON the part
// (Alt+click on the part puts it there without drawing), else through the
// centre of the visible pieces. Its depth moves along the normal with
// Shift+wheel — steps of a round number of mm, sized to the screen like the
// brush (mmPerPx) — or, on a phone, the vertical slider at the side. It is SEEN:
// a translucent sheet ruled every 1 and 10 mm (the ▣ Forme graph paper), cut to
// a rectangle round the pieces, with the plane's position written on its corner.
//
// Data (view.html): a sample on the plane has normal = the plane's, no piece,
// and its stroke carries `plane: {origin, normal}`; the server makes the
// gesture a mark of kind "plane" with the nearest piece (api._marks).
//
// Not here, on purpose: the line where the plane cuts the part. It needs the
// CPU section contour of ✂ (task A, in parallel) — see SECTION_HOOK below.

import { registerTool, byKey, ctx as C } from '/static/view-tools.js';

export const MODES = ['surface', 'xy', 'xz', 'yz', 'view'];
const LABEL = { surface: 'Superficie', xy: 'XY', xz: 'XZ', yz: 'YZ', view: 'Vista' };
const AXIS = { xy: [0, 0, 1], xz: [0, 1, 0], yz: [1, 0, 0] };
const INK = new Set(['pen', 'pen3d', 'paint']);   // the tools that draw on it
const INK_COLOR = '#38bdf8';

const st = {
  mode: 'surface', last: 'xy',
  origin: null,                                 // V3: a point of the plane
  anchor: null,                                 // V3: where it was last put (Vista measures depth from it)
  normal: null, u: null, v: null,               // unit normal + in-plane basis (world axes for XY/XZ/YZ)
  obj: null, shown: false, sig: '',
  el: {},
};
const r4 = x => Math.round(x * 1e4) / 1e4;

// ── the plane ──
export const mode = () => st.mode;
// is the plane in use for this tool? (a Matita setting: the ink tools only)
export const on = def => st.mode !== 'surface' && !!def && INK.has(def.id);
// a copy of the plane now — a painted label keeps it, so re-lettering it later
// lays the words on the plane they were written on
export const current = () => st.mode === 'surface' ? null
  : { origin: st.origin.clone(), normal: st.normal.clone() };
const planeData = pl => ({ origin: [r4(pl.origin.x), r4(pl.origin.y), r4(pl.origin.z)],
  normal: [r4(pl.normal.x), r4(pl.normal.y), r4(pl.normal.z)] });

function basis(n) {
  const T = C.THREE;
  if (st.mode === 'xy') return [new T.Vector3(1, 0, 0), new T.Vector3(0, 1, 0)];
  if (st.mode === 'xz') return [new T.Vector3(1, 0, 0), new T.Vector3(0, 0, 1)];
  if (st.mode === 'yz') return [new T.Vector3(0, 1, 0), new T.Vector3(0, 0, 1)];
  // Vista: the camera's right at the moment it was chosen, and n × right
  const u = new T.Vector3(1, 0, 0).applyQuaternion(C.viewer.camera.quaternion);
  u.addScaledVector(n, -u.dot(n)).normalize();
  return [u, n.clone().cross(u).normalize()];
}
function centre() {
  const b = C.visibleBox ? C.visibleBox() : null;
  return b && !b.isEmpty() ? b.getCenter(new C.THREE.Vector3()) : new C.THREE.Vector3();
}
export function setMode(m) {
  if (!MODES.includes(m)) return;
  const T = C.THREE;
  st.mode = m;
  if (m !== 'surface') {
    st.last = m;
    if (m === 'view') {                         // fixed now: it does not turn with the camera
      const cam = C.viewer.camera, tgt = C.viewer.controls.target;
      st.normal = cam.position.clone().sub(tgt).normalize();
      if (!(st.normal.lengthSq() > 0.5)) st.normal = new T.Vector3(0, 0, 1).applyQuaternion(cam.quaternion);
    } else st.normal = new T.Vector3(...AXIS[m]);
    if (!st.origin) { st.origin = centre(); st.anchor = st.origin.clone(); }
    [st.u, st.v] = basis(st.normal);
    // a plane to draw on wants a tool that draws
    if (!on(C.toolDef && C.toolDef())) { const p = byKey('p'); if (p) C.setTool(p.id); }
  }
  st.sig = '';
  C.syncDraw();                                 // → refresh()
}
// the plane goes through p (a point on the part): Alt+click, or a stroke begun there
export function anchor(p) {
  if (st.mode === 'surface' || !p) return;
  st.origin = p.clone(); st.anchor = p.clone(); st.sig = '';
  refresh();
}
// where the ray under (x, y) meets the plane (or `pl`, a kept copy) — null when
// it runs parallel or the plane is behind the camera
export function planeHit(x, y, pl = null) {
  const P = pl || (st.mode === 'surface' ? null : st);
  if (!P) return null;
  const T = C.THREE, r = C.$('c').getBoundingClientRect();
  const ray = new T.Raycaster();
  ray.setFromCamera(new T.Vector2(((x - r.left) / r.width) * 2 - 1, -((y - r.top) / r.height) * 2 + 1), C.viewer.camera);
  const plane = new T.Plane().setFromNormalAndCoplanarPoint(P.normal, P.origin);
  const p = ray.ray.intersectPlane(plane, new T.Vector3());
  if (!p) return null;
  const n = P.normal.clone();
  if (n.dot(ray.ray.direction) > 0) n.negate();  // the side you look at, as surfaceHit
  return { p, n, key: null, mesh: null, plane: planeData(P) };
}

// ── depth: Shift+wheel, the phone slider ──
// a round step: ~8 px of screen at the plane, rounded to 1-2-5 × 10^k mm
function step() {
  const raw = 8 * C.mmPerPx(st.origin);
  const k = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-3))));
  return [1, 2, 5, 10].map(m => m * k).find(s => s >= raw * 0.75) || 10 * k;
}
// the plane's position along its normal (a world coordinate for XY/XZ/YZ)
const coord = () => st.origin.dot(st.normal);
function moveTo(c) { st.origin.addScaledVector(st.normal, c - coord()); st.sig = ''; refresh(); }
// one step along the normal, landing on a multiple of the step
export function nudge(dir) {
  if (st.mode === 'surface') return;
  const s = step();
  moveTo(Math.round(coord() / s) * s + Math.sign(dir) * s);
}
function onWheel(e) {
  if (!e.shiftKey || st.mode === 'surface' || !C.isDrawing() || !on(C.toolDef())) return;
  e.preventDefault(); e.stopPropagation();      // before OrbitControls zooms
  const d = e.deltaY || e.deltaX;               // Shift turns the wheel sideways in some browsers
  if (d) nudge(d < 0 ? 1 : -1);
}
const fmt = x => (Math.abs(x) < 5e-5 ? 0 : x).toFixed(Math.abs(x) >= 100 ? 0 : 1).replace('.', ',');
export function depthText() {
  if (st.mode === 'surface') return '';
  if (st.mode === 'view') {
    const d = st.origin.clone().sub(st.anchor).dot(st.normal);
    return `Vista ${d >= 0 ? '+' : ''}${fmt(d)} mm`;
  }
  return `${{ xy: 'Z', xz: 'Y', yz: 'X' }[st.mode]} = ${fmt(coord())} mm`;
}

// ── the sheet: graph paper, 1 and 10 mm, round the pieces ──
const VERT = `varying vec2 vUV; uniform vec3 uU; uniform vec3 uV;
void main() { vec4 w = modelMatrix * vec4(position, 1.0); vUV = vec2(dot(w.xyz, uU), dot(w.xyz, uV));
  gl_Position = projectionMatrix * viewMatrix * w; }`;
const FRAG = `varying vec2 vUV; uniform vec3 uInk; uniform vec2 uLo; uniform vec2 uHi;
float rule(vec2 uv, float s, float w) {
  vec2 q = uv / s, g = abs(fract(q - 0.5) - 0.5) / max(fwidth(q), vec2(1e-6));
  float line = 1.0 - min(min(g.x, g.y) / w, 1.0);
  float dense = max(fwidth(q).x, fwidth(q).y);            // squares per pixel
  return line * (1.0 - smoothstep(0.12, 0.33, dense));
}
void main() {
  float r = max(rule(vUV, 1.0, 1.0) * 0.4, rule(vUV, 10.0, 1.3));
  vec2 e = min(vUV - uLo, uHi - vUV) / max(fwidth(vUV), vec2(1e-6));
  float edge = 1.0 - min(min(e.x, e.y) / 1.5, 1.0);
  // a veil, not a wall: the part behind must stay readable through it
  gl_FragColor = vec4(uInk, max(0.06, max(r * 0.26, edge * 0.6)));
}`;
function sheet() {
  const T = C.THREE, n = st.normal, u = st.u, v = st.v, o = st.origin;
  // the rectangle: the visible pieces' box seen along the normal, plus a margin,
  // on whole centimetres so the bold rules meet its border
  const b = C.visibleBox ? C.visibleBox() : new T.Box3();
  let lo = [Infinity, Infinity], hi = [-Infinity, -Infinity];
  const add = p => { const a = p.dot(u), c = p.dot(v);
    lo = [Math.min(lo[0], a), Math.min(lo[1], c)]; hi = [Math.max(hi[0], a), Math.max(hi[1], c)]; };
  if (!b.isEmpty()) for (let i = 0; i < 8; i++)
    add(new T.Vector3(i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y, i & 4 ? b.max.z : b.min.z));
  add(o);
  for (const p of inkOnPlane()) add(p);          // the paper grows under what was drawn on it
  const span = Math.max(hi[0] - lo[0], hi[1] - lo[1], 10), m = 0.15 * span + 5;
  lo = [Math.floor((lo[0] - m) / 10) * 10, Math.floor((lo[1] - m) / 10) * 10];
  hi = [Math.ceil((hi[0] + m) / 10) * 10, Math.ceil((hi[1] + m) / 10) * 10];
  const W = hi[0] - lo[0], H = hi[1] - lo[1], d = o.dot(n);
  const mat = new T.ShaderMaterial({ vertexShader: VERT, fragmentShader: FRAG, transparent: true,
    depthWrite: false, side: T.DoubleSide, toneMapped: false,
    uniforms: { uU: { value: u.clone() }, uV: { value: v.clone() }, uInk: { value: new T.Color(INK_COLOR) },
                uLo: { value: new T.Vector2(...lo) }, uHi: { value: new T.Vector2(...hi) } } });
  const mesh = new T.Mesh(new T.PlaneGeometry(W, H), mat);
  const at = (a, c) => u.clone().multiplyScalar(a).addScaledVector(v, c).addScaledVector(n, d);
  mesh.position.copy(at((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2));
  mesh.quaternion.setFromRotationMatrix(new T.Matrix4().makeBasis(u, v, n));
  mesh.renderOrder = 1;
  const g = new T.Group(); g.name = 'work-plane'; g.add(mesh);
  // the point it goes through. Its position is written in the bar beside ⊞ and
  // on the phone's slider — not in 3D: a label there sat on the very stroke it
  // was meant to help with, and went into the photo of the view with it
  const dot = new T.Mesh(new T.SphereGeometry(1, 12, 8),
    new T.MeshBasicMaterial({ color: INK_COLOR, depthTest: false, transparent: true, opacity: 0.9, toneMapped: false }));
  dot.position.copy(o); dot.scale.setScalar(3 * C.mmPerPx(o)); dot.renderOrder = 6;
  g.add(dot);
  // SECTION_HOOK: the line where the plane cuts the part — reuse ✂'s CPU
  // contour (webui/section.js, task A) when it is in: g.add(sectionLine(o, n))
  return g;
}
// the samples of the draft that lie on THIS plane (a stroke off the paper looks lost)
function inkOnPlane() {
  const out = [], d = st.origin.dot(st.normal);
  for (const s of (C.planeInk ? C.planeInk() : []))
    for (const p of s.pts) if (Math.abs(p.dot(st.normal) - d) < 0.05) out.push(p);
  return out;
}
// show / hide / rebuild — after anything that may change it (syncDraw calls it)
export function refresh() {
  if (!C.viewer) return;
  const want = st.mode !== 'surface' && C.isDrawing() && on(C.toolDef());
  const sig = want ? [st.mode, ...st.origin.toArray(), ...st.normal.toArray()].map(r4).join()
    + '|' + (C.planeInk ? C.planeInk().length : 0) : '';
  if (sig !== st.sig || want !== st.shown) {
    if (st.obj) { C.disposeObj(st.obj); st.obj = null; }
    if (want) { st.obj = sheet(); C.viewer.scene.add(st.obj); }
    st.sig = sig; st.shown = want;
    C.viewer.invalidate();
  }
  syncUI(want);
}
function syncUI(want) {
  const { sel, depth, slider, sdepth } = st.el, btn = document.getElementById('dt-plane');
  if (sel) sel.value = st.mode;
  if (btn) btn.classList.toggle('armed', st.mode !== 'surface');
  if (depth) { depth.textContent = depthText(); depth.hidden = st.mode === 'surface'; }
  if (sdepth) sdepth.textContent = depthText().replace(' mm', '');
  if (slider) {
    slider.hidden = !want;
    if (want) {
      // ± the pieces' size round where the plane was put
      const b = C.visibleBox ? C.visibleBox() : null;
      const R = b && !b.isEmpty() ? b.getSize(new C.THREE.Vector3()).length() * 0.75 + 10 : 50;
      const base = st.anchor.dot(st.normal), inp = slider.firstChild;
      inp.min = String(base - R); inp.max = String(base + R); inp.step = 'any';
      inp.value = String(coord());
    }
  }
}
// the line under the bar while the plane is in use
export function hint(def) {
  if (!on(def)) return null;
  return `Piano ${LABEL[st.mode]}: sul pezzo disegni sul pezzo, fuori sul piano · Shift+rotella sposta il piano · `
    + 'Alt+clic sul pezzo: il piano passa lì · tasto destro / rotella / due dita / ✋ Muovi: muovi la vista';
}

// ── install: ⊞ + its menu in the ✎ Matita row, the phone slider, the wheel ──
export function install() {
  const vp = document.getElementById('vp');
  const style = document.createElement('style');
  style.textContent = `
  #dbar .tool.armed{border-color:${INK_COLOR};color:${INK_COLOR};}
  #d-plane{max-width:110px;}
  #d-pdepth{font:11px var(--mono, monospace);color:${INK_COLOR};white-space:nowrap;padding:0 2px;}
  #d-pdepth[hidden]{display:none;}
  #d-pslide{position:absolute;right:6px;top:22%;z-index:7;display:flex;align-items:center;justify-content:center;
    width:38px;height:40%;background:rgba(11,14,20,.72);border:1px solid var(--border);border-radius:19px;
    padding:12px 0;touch-action:none;box-sizing:border-box;}
  #d-pslide input{writing-mode:vertical-lr;direction:rtl;width:30px;height:100%;margin:0;accent-color:${INK_COLOR};}
  #d-pslide b{position:absolute;top:-22px;right:0;font:600 11px var(--mono, monospace);color:${INK_COLOR};
    white-space:nowrap;background:rgba(11,14,20,.72);border-radius:6px;padding:2px 5px;}
  #d-pslide[hidden]{display:none;}
  @media (hover:hover) and (pointer:fine) and (min-width:761px){ #d-pslide{display:none;} }`;
  document.head.appendChild(style);
  const sel = document.createElement('select');
  sel.id = 'd-plane'; sel.title = 'Su cosa disegna la matita fuori dal pezzo';
  for (const m of MODES) { const o = document.createElement('option'); o.value = m; o.textContent = LABEL[m]; sel.appendChild(o); }
  sel.addEventListener('change', () => setMode(sel.value));
  const depth = document.createElement('span'); depth.id = 'd-pdepth'; depth.hidden = true;
  // the phone's depth: a vertical slider at the side of the view
  const slider = document.createElement('div'); slider.id = 'd-pslide'; slider.hidden = true;
  slider.innerHTML = '<input type="range" aria-label="Profondità del piano" title="Sposta il piano lungo la sua normale"><b></b>';
  slider.firstChild.addEventListener('input', e => moveTo(+e.target.value));
  for (const t of ['pointerdown', 'pointermove', 'pointerup', 'wheel']) slider.addEventListener(t, e => e.stopPropagation());
  vp.appendChild(slider);
  st.el = { sel, depth, slider, sdepth: slider.lastChild };
  vp.addEventListener('wheel', onWheel, { capture: true, passive: false });
  return registerTool({ id: 'plane', tab: 'pencil', icon: '⊞', label: 'Piano',
    title: 'Piano: disegna anche nel vuoto, su un piano XY / XZ / YZ o perpendicolare alla vista — sopra il pezzo resta sul pezzo; Shift+rotella lo sposta, Alt+clic sul pezzo lo fa passare lì',
    // the button turns the plane on (the one used last) and off; the menu picks which
    click: () => setMode(st.mode === 'surface' ? st.last : 'surface'),
    options: () => [sel, depth] });
}
