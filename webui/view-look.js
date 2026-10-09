// 🔍 Aspetto in /view — a piece in glass, glowing, or a ghost, ONLY in this view.
//
// The generation is immutable: colour and finish come from the graph frozen with
// it. This module lays a per-LEAF override on top (a leaf is what the visibility
// works on: a node, one body of a collide scene, one piece of a fan-out), kept in
// the URL hash so a link carries the look:
//
//   #look=n3:glass,n7.2:emissive:#ffcc00,n5:ghost,n9:#ff0000
//
// (finish and/or colour; a bare node id = all its leaves.) Nothing is saved.
//
// What the finishes are FOR (PLAN_VIEW_SECTION §0, measured): three refracts
// only the OPAQUE list through a glass, so the outer piece goes glass and the
// inner one stays opaque or emissive — glass inside glass shows as a ring (and on
// Android, where WEBGL_multisampled_render_to_texture exists, not at all), and a
// ghost inside glass not at all. The ghost (viewer.js makeMaterial) is plain
// alpha with opaque sharp edges: ghost in ghost works, on a phone too. Hence
// «Guarda dentro» puts the whole envelope in glass OR in ghost, never a mix.
//
// How a leaf is restyled:
//   - a whole object (a node's mesh, a scene body, a point cloud): its material
//     is swapped, the original kept to put back;
//   - one piece of a fanned-out buffer (one geometry group of a shared mesh):
//     a PROXY child mesh drawing just that group with the new material, and the
//     group's own material hidden. Swapping the group's material in place would
//     work for colour, but the glow pass renders whole OBJECTS — one emissive
//     group would light up every piece of the buffer. The proxy is its own
//     object, so markGlow() puts only it on the glow layer.
import * as THREE from 'three';
import { makeMaterial, markGlow, ghostEdges, PALETTE } from '/static/viewer.js';

export const FINISHES = [
  ['solid', 'Normale', '●'],
  ['glass', 'Vetro', '🔎'],
  ['emissive', 'Emissivo', '💡'],
  ['ghost', 'Fantasma', '👻'],
];
const OK_FINISH = new Set(['solid', 'glass', 'emissive', 'ghost', 'metal']);
const HEX = /^#?([0-9a-f]{6})$/i;

let V = null;                       // { viewer, nodes, leafIndex, labelOf, onChange, toast }
const looks = new Map();            // leaf key → { finish?, color? }   (what the user asked)
const live = new Map();             // leaf key → { orig, mats, proxy, edges }   (what is drawn)
let inside = null;                  // the last «Guarda dentro»: { key, mode, keys, prev }

// ── reading the scene ────────────────────────────────────────────────────
const entry = k => V && V.leafIndex.get(k);
const isPart = leaf => leaf.part != null;
function kindOf(k) {                // 'mesh' (any finish) | 'line' (colour only) | null
  const e = entry(k); if (!e) return null;
  const o = e.leaf.obj;
  if (o && o.isMesh) return 'mesh';
  if (o && (o.isLineSegments || o.isLine)) return 'line';
  return null;
}
function origColor(k) {
  const e = entry(k); if (!e) return null;
  const s = live.get(k);
  if (s && s.orig) { const m = Array.isArray(s.orig) ? s.orig[0] : s.orig; return m && m.color; }
  const o = e.leaf.obj;
  if (isPart(e.leaf)) { const m = o.material[e.leaf.part]; return m && m.color; }
  const m = Array.isArray(o.material) ? o.material[0] : o.material;
  return m && m.color;
}
const hexOf = c => c == null ? null : '#' + new THREE.Color(c).getHexString();

// The leaf's box in world space, at the pose on screen.
function leafBox(k) {
  const e = entry(k); if (!e) return null;
  const o = e.leaf.obj; o.updateWorldMatrix(true, true);
  const box = new THREE.Box3();
  if (isPart(e.leaf)) {
    const g = o.geometry, gr = g.groups.find(x => x.materialIndex === e.leaf.part);
    if (!gr || !g.index) return null;
    const v = new THREE.Vector3(), pos = g.attributes.position;
    for (let i = gr.start; i < gr.start + gr.count; i++)
      box.expandByPoint(v.fromBufferAttribute(pos, g.index.getX(i)).applyMatrix4(o.matrixWorld));
  } else box.setFromObject(o);
  return box.isEmpty() ? null : box;
}
// Which pieces COVER this one, and how much: rays from points of its surface
// out in 26 directions; the first OTHER piece each ray meets is charged with it.
// Rays, not boxes — measured on creepyfinger-v4/g64: the electronics
// («ingombri») sit in a shell split in two halves, and neither half's box holds
// even 60% of theirs (0.40 and 0.55), yet together they close them in. A ray
// does not care how the envelope is cut. Returns [[key, share of rays]].
const DIRS = [];
for (const x of [-1, 0, 1]) for (const y of [-1, 0, 1]) for (const z of [-1, 0, 1])
  if (x || y || z) DIRS.push(new THREE.Vector3(x, y, z).normalize());
export function coverOf(key, { samples = 60 } = {}) {
  const e = entry(key); if (!e) return [];
  const B = leafBox(key); if (!B) return [];
  const reach = B.clone().expandByScalar(B.getSize(new THREE.Vector3()).length() * 0.25);
  const cands = [];
  for (const n of V.nodes) for (const l of n.leaves) {
    if (l.key === key || kindOf(l.key) !== 'mesh' || (V.hidden && V.hidden.has(l.key))) continue;
    const b = leafBox(l.key); if (b && b.intersectsBox(reach)) cands.push(l);
  }
  if (!cands.length) return [];
  // sample points: the target's vertices, strided (a part: its group's indices)
  const o = e.leaf.obj, g = o.geometry, pos = g.attributes.position, pts = [];
  let idx;
  if (isPart(e.leaf)) {
    const gr = g.groups.find(x => x.materialIndex === e.leaf.part);
    idx = i => g.index.getX(gr.start + i); idx.n = gr.count;
  } else if (g.index) { idx = i => g.index.getX(i); idx.n = g.index.count; }
  else { idx = i => i; idx.n = pos.count; }
  const step = Math.max(1, Math.floor(idx.n / samples));
  for (let i = 0; i < idx.n && pts.length < samples; i += step)
    pts.push(new THREE.Vector3().fromBufferAttribute(pos, idx(i)).applyMatrix4(o.matrixWorld));
  const rc = new THREE.Raycaster(), count = new Map(); let total = 0;
  const eps = B.getSize(new THREE.Vector3()).length() * 1e-3;
  for (const p of pts) for (const d of DIRS) {
    rc.set(p.clone().addScaledVector(d, eps), d);
    let best = null;
    for (const l of cands) {
      for (const h of rc.intersectObject(l.obj, false)) {
        if (isPart(l) && (!h.face || h.face.materialIndex !== l.part)) continue;
        if (!best || h.distance < best.d) best = { d: h.distance, k: l.key };
        break;
      }
    }
    total++;
    if (best) count.set(best.k, (count.get(best.k) || 0) + 1);
  }
  return [...count].map(([k, c]) => [k, c / total]).sort((a, b) => b[1] - a[1]);
}
export const COVER_MIN = 0.1;      // a piece met by fewer of the rays is beside it, not around it

// ── drawing a look ───────────────────────────────────────────────────────
function unpaint(k) {
  const s = live.get(k), e = entry(k); if (!s || !e) return;
  const o = e.leaf.obj;
  if (s.proxy) {
    s.proxy.removeFromParent();
    s.proxy.traverse(x => { if (x.geometry) x.geometry.dispose(); if (x.material) [].concat(x.material).forEach(m => m.dispose()); });
    o.material[e.leaf.part].visible = s.vis !== false;
  }
  if (s.edges) { s.edges.removeFromParent(); s.edges.geometry.dispose(); s.edges.material.dispose(); }
  if (s.mats) { [].concat(o.material).forEach(m => m.dispose()); o.material = s.orig; }
  if (s.lineColor) o.material.color.copy(s.lineColor);
  live.set(k, { vis: s.vis });
}
function paint(k) {
  const e = entry(k); if (!e) return;
  unpaint(k);
  const L = looks.get(k); if (!L) return;
  const s = live.get(k) || {}; live.set(k, s);
  const o = e.leaf.obj, kind = kindOf(k);
  if (kind === 'line') {                       // a curve takes a colour, not a material
    if (L.color) { s.lineColor = o.material.color.clone(); o.material.color.set(L.color); }
    return;
  }
  if (kind !== 'mesh') return;
  const finish = L.finish || 'solid';
  if (isPart(e.leaf)) {
    const i = e.leaf.part, g = o.geometry, gr = g.groups.find(x => x.materialIndex === i);
    if (!gr) return;
    const base = o.material[i];
    const pg = new THREE.BufferGeometry();
    for (const [n, a] of Object.entries(g.attributes)) pg.setAttribute(n, a);
    pg.setIndex(g.index);
    pg.addGroup(gr.start, gr.count, 0);       // a group, so /view's visibleBox measures just this piece
    const proxy = new THREE.Mesh(pg, [makeMaterial(L.color || base.color, finish)]);
    // a hit on the proxy names its piece the way a hit on the group would
    proxy.raycast = function (rc, out) {
      const n0 = out.length; THREE.Mesh.prototype.raycast.call(this, rc, out);
      for (let j = n0; j < out.length; j++) if (out[j].face) out[j].face.materialIndex = i;
    };
    proxy.userData.lookProxy = i;
    proxy.userData.ghost = finish === 'ghost';
    if (finish === 'ghost') proxy.add(s.edges = ghostEdges(g, L.color || base.color, gr));
    o.add(proxy);
    s.proxy = proxy;
    s.vis = base.visible;
    base.visible = false;
    proxy.visible = s.vis;
    return;
  }
  s.orig = o.material;
  const src = [].concat(o.material);
  const mats = src.map(m => makeMaterial(L.color || (m && m.color) || 0x888888, finish));
  o.material = Array.isArray(s.orig) ? mats : mats[0];
  s.mats = true;
  if (finish === 'ghost' && !o.isInstancedMesh) o.add(s.edges = ghostEdges(o.geometry, L.color || origColor(k)));
}

function refresh(keys) {
  for (const k of keys) paint(k);
  if (!V) return;
  for (const n of V.nodes) markGlow(n.obj);
  V.viewer.syncGlow();
}
function changed() { if (V && V.onChange) V.onChange(); }

// ── public API ───────────────────────────────────────────────────────────
export function init(opts) {
  V = opts;
  const css = document.createElement('style'); css.textContent = CSS; document.head.appendChild(css);
  // A piece of a fan-out is hidden by hiding its group's material: with a proxy
  // drawing it instead, the proxy is what has to follow.
  for (const n of V.nodes) for (const leaf of n.leaves) {
    if (!isPart(leaf)) continue;
    const a = leaf.apply;
    leaf.apply = v => {
      a(v);
      const s = live.get(leaf.key);
      if (s && s.proxy) { s.vis = v; leaf.obj.material[leaf.part].visible = false; s.proxy.visible = v; }
    };
  }
}
export const lookOf = k => looks.get(k) || null;
export function set(keys, { finish, color } = {}) {
  for (const k of keys) {
    if (!entry(k)) continue;
    const L = { ...(looks.get(k) || {}) };
    if (finish !== undefined) { if (finish) L.finish = finish; else delete L.finish; }
    if (color !== undefined) { if (color) L.color = hexOf(color); else delete L.color; }
    if (kindOf(k) === 'line') delete L.finish;
    if (L.finish || L.color) looks.set(k, L); else looks.delete(k);
  }
  if (inside && keys.some(k => inside.keys.includes(k))) inside = null;  // the user took it over
  refresh(keys); changed();
}
export function reset(keys) {
  for (const k of keys) looks.delete(k);
  if (inside && keys.some(k => inside.keys.includes(k) || k === inside.key)) inside = null;
  refresh(keys); changed();
}
// The swatch a list row shows: the override colour, else the piece's own.
export function colorOf(keys) {
  const cs = keys.map(k => (looks.get(k) && looks.get(k).color) || null);
  return cs.length && cs.every(c => c && c === cs[0]) ? cs[0] : null;
}
export const finishOf = keys => {
  const fs = keys.map(k => (looks.get(k) && looks.get(k).finish) || null);
  return fs.length && fs.every(f => f === fs[0]) ? fs[0] : null;
};
export const hasLook = keys => keys.some(k => looks.has(k));

// «Guarda dentro»: the selected piece stays as it is, every piece whose box
// encloses it goes to `mode` (glass | ghost) — all of them the same, never a
// mix. Again, same piece and same mode → back to how they were.
export function lookInside(key, mode) {
  if (!entry(key)) return 0;
  if (inside && inside.key === key && inside.mode === mode) { undoInside(); return -1; }
  if (inside) undoInside(false);
  const keys = coverOf(key).filter(([, f]) => f >= COVER_MIN).map(([k]) => k);
  if (!keys.length) {
    if (V.toast) V.toast(`Nessun pezzo racchiude <b>${V.labelOf ? V.labelOf(key) : key}</b>.`, 3500);
    return 0;
  }
  const prev = new Map(keys.map(k => [k, looks.has(k) ? { ...looks.get(k) } : null]));
  for (const k of keys) looks.set(k, { ...(looks.get(k) || {}), finish: mode });
  inside = { key, mode, keys, prev };
  refresh(keys); changed();
  return keys.length;
}
export function undoInside(notify = true) {
  if (!inside) return;
  const { keys, prev } = inside; inside = null;
  for (const k of keys) { const p = prev.get(k); if (p) looks.set(k, p); else looks.delete(k); }
  refresh(keys); if (notify) changed();
}
export const insideOf = () => inside && { key: inside.key, mode: inside.mode, n: inside.keys.length };

// ── the hash ─────────────────────────────────────────────────────────────
const spec = L => [L.finish, L.color].filter(Boolean).join(':');
export function hashValue() {
  if (!V) return '';
  const out = [];
  for (const n of V.nodes) {
    const ks = n.leaves.map(l => l.key).filter(k => looks.has(k));
    if (!ks.length) continue;
    const ss = ks.map(k => spec(looks.get(k)));
    if (n.leaves.length > 1 && ks.length === n.leaves.length && ss.every(x => x === ss[0])) out.push(`${n.id}:${ss[0]}`);
    else ks.forEach((k, i) => out.push(`${k}:${ss[i]}`));
  }
  return out.join(',');
}
export function parseLook(str) {        // pure: 'n3:glass,n7.2:emissive:#ffcc00' → [[key,{finish,color}]]
  const out = [];
  for (const item of String(str || '').split(',')) {
    const [k, ...rest] = item.split(':');
    if (!k) continue;
    const L = {};
    for (const t of rest) {
      const m = HEX.exec(t);
      if (m) L.color = '#' + m[1].toLowerCase();
      else if (OK_FINISH.has(t)) L.finish = t;
    }
    if (L.finish || L.color) out.push([k, L]);
  }
  return out;
}
export function fromHash(str) {
  if (!str || !V) return;
  const keys = [];
  for (const [k, L] of parseLook(str)) {
    const n = V.nodes.find(x => x.id === k);
    for (const kk of n ? n.leaves.map(l => l.key) : (entry(k) ? [k] : [])) {
      const LL = { ...L }; if (kindOf(kk) === 'line') delete LL.finish;
      if (LL.finish || LL.color) { looks.set(kk, LL); keys.push(kk); }
    }
  }
  refresh(keys);
}

// ── the menu ─────────────────────────────────────────────────────────────
const CSS = `
#lookpop{position:absolute;z-index:30;display:none;flex-direction:column;gap:8px;min-width:240px;max-width:min(330px,calc(100vw - 16px));
  background:var(--bg-2,#151a22);border:1px solid var(--border,#1e2738);border-radius:12px;padding:10px;
  box-shadow:0 10px 30px rgba(0,0,0,.55);font-size:12px;color:var(--text,#c5cdd8);}
#lookpop.open{display:flex;}
#lookpop .lp-h{display:flex;align-items:center;gap:6px;font-weight:600;}
#lookpop .lp-h .nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
#lookpop .lp-l{color:var(--text-dim,#5c6a7a);font-size:11px;}
#lookpop .lp-r{display:flex;flex-wrap:wrap;gap:4px;align-items:center;}
#lookpop .lp-r .btn{flex:1 1 auto;padding:4px 7px;min-height:var(--tap,34px);}
#lookpop .btn.on{border-color:var(--accent,#e94560);background:var(--bg-3,#1a2030);color:#fff;}
#lookpop .btn:disabled{opacity:.35;cursor:default;}
#lookpop .sw{width:24px;height:24px;border-radius:6px;border:2px solid transparent;cursor:pointer;flex:0 0 auto;}
#lookpop .sw.on{border-color:#fff;}
#lookpop input[type=color]{width:34px;height:28px;padding:0;border:1px solid var(--border,#1e2738);border-radius:6px;background:none;cursor:pointer;}
#lookpop .lp-in{border-top:1px solid var(--border,#1e2738);padding-top:8px;}
@media (pointer:coarse){ #lookpop .sw{width:32px;height:32px;} }
@media (max-width:760px){ #lookpop{left:8px !important;right:8px;top:auto !important;bottom:12px;max-width:none;} }
.row .lk{opacity:0;font-size:12px;}
.row:hover .lk,.row.sel .lk,.row .lk.has{opacity:1;}
.row .lk.has{color:var(--accent,#e94560);}
@media (pointer:coarse){ .row .lk{opacity:1;} }
/* phone: the selection bar gains 🎨 — icon only, and it rides ABOVE the ✎ Disegna
   button that shares its bottom row there (they overlapped as soon as a long name
   made the bar wide) */
@media (max-width:760px){
  #sel-look .t{display:none;}
  #selbar{bottom:64px;}
  #vp.has-anim #selbar{bottom:calc(var(--tlh,104px) + 64px);}
}`;
let pop = null, popKeys = null, popAnchor = null;
const SWATCHES = [...PALETTE.slice(0, 7), '#ffffff', '#ffcc00'];
function ensurePop() {
  if (pop) return pop;
  pop = document.createElement('div'); pop.id = 'lookpop'; pop.setAttribute('role', 'dialog');
  document.body.appendChild(pop);
  document.addEventListener('pointerdown', ev => {
    if (pop.classList.contains('open') && !pop.contains(ev.target) && ev.target !== popAnchor
        && !(ev.target.closest && ev.target.closest('.lk'))) closeMenu();
  }, true);
  addEventListener('keydown', ev => { if (ev.key === 'Escape' && pop.classList.contains('open')) { ev.stopImmediatePropagation(); closeMenu(); } }, true);
  return pop;
}
export function closeMenu() { if (pop) pop.classList.remove('open'); popKeys = null; }
export const menuOpen = () => !!(pop && pop.classList.contains('open'));
export function openMenu(keys, anchor) {
  keys = (keys || []).filter(k => entry(k));
  if (!keys.length) return;
  ensurePop();
  if (popKeys && popKeys.join() === keys.join() && pop.classList.contains('open')) { closeMenu(); return; }
  popKeys = keys; popAnchor = anchor || null;
  draw();
  pop.classList.add('open');
  const r = anchor ? anchor.getBoundingClientRect() : { left: innerWidth / 2, right: innerWidth / 2, top: innerHeight / 2, bottom: innerHeight / 2 };
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let x = Math.min(Math.max(8, r.right - w), innerWidth - w - 8);
  let y = r.bottom + 6; if (y + h > innerHeight - 8) y = Math.max(8, r.top - h - 6);
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
}
function draw() {
  const keys = popKeys, one = keys.length === 1 ? keys[0] : null;
  const meshy = keys.some(k => kindOf(k) === 'mesh');
  const f = finishOf(keys), c = colorOf(keys);
  const title = one ? (V.labelOf ? V.labelOf(one) : one)
    : `${V.leafIndex.get(keys[0]).node.title} · ${keys.length} pezzi`;
  const ins = inside && one && inside.key === one ? inside.mode : null;
  pop.innerHTML = `<div class="lp-h"><span class="nm" title="${keys.join(', ')}">🎨 ${esc(title)}</span>
      <button class="btn" data-x aria-label="Chiudi">✕</button></div>
    <div class="lp-l">Finitura — solo in questa vista, la generazione non cambia</div>
    <div class="lp-r">${FINISHES.map(([id, lb, ic]) =>
      `<button class="btn${f === id ? ' on' : ''}" data-f="${id}" ${meshy ? '' : 'disabled'}>${ic} ${lb}</button>`).join('')}</div>
    <div class="lp-l">Colore</div>
    <div class="lp-r">${SWATCHES.map(s => `<span class="sw${c === s.toLowerCase() ? ' on' : ''}" data-c="${s}" style="background:${s}" role="button" aria-label="${s}"></span>`).join('')}
      <input type="color" data-pick value="${c || hexOf(origColor(keys[0])) || '#888888'}" aria-label="Altro colore"></div>
    <div class="lp-r"><button class="btn" data-reset ${hasLook(keys) ? '' : 'disabled'}>↺ Com'era nella generazione</button></div>
    ${one && meshy ? `<div class="lp-in"><div class="lp-l">Guarda dentro: questo pezzo resta com'è, quelli che lo racchiudono diventano…</div>
      <div class="lp-r"><button class="btn${ins === 'glass' ? ' on' : ''}" data-in="glass">🔎 tutti vetro</button>
      <button class="btn${ins === 'ghost' ? ' on' : ''}" data-in="ghost">👻 tutti fantasma</button></div></div>` : ''}`;
  pop.querySelector('[data-x]').onclick = closeMenu;
  pop.querySelectorAll('[data-f]').forEach(b => b.onclick = () => { set(keys, { finish: f === b.dataset.f ? null : b.dataset.f }); draw(); });
  pop.querySelectorAll('[data-c]').forEach(b => b.onclick = () => { set(keys, { color: c === b.dataset.c.toLowerCase() ? null : b.dataset.c }); draw(); });
  const pick = pop.querySelector('[data-pick]');
  pick.oninput = () => { set(keys, { color: pick.value }); };
  pick.onchange = () => draw();
  pop.querySelector('[data-reset]').onclick = () => { reset(keys); draw(); };
  pop.querySelectorAll('[data-in]').forEach(b => b.onclick = () => {
    const n = lookInside(one, b.dataset.in);
    if (n > 0 && V.toast) V.toast(`${b.dataset.in === 'glass' ? '🔎 Vetro' : '👻 Fantasma'} su ${n} ${n === 1 ? 'pezzo' : 'pezzi'} attorno a <b>${esc(V.labelOf ? V.labelOf(one) : one)}</b> — di nuovo per tornare indietro.`, 3500);
    draw();
  });
}
const esc = s => String(s ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
