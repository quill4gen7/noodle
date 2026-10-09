// ════════════════════════════════════════════════════════════════════
// CadViewer — the shared Three.js viewport for BOTH webui pages
// (the /ui code view and the /nodes editor).
//
// It owns the common, page-agnostic parts: a Z-up CAD scene (grid = XY
// construction plane, origin marker, lights), an orbit camera, the navigation
// ViewHelper, the animate loop, framing/resize, and — crucially — the
// multi-mesh preview renderer that turns an execution `view.previews` dict into
// coloured meshes / polylines / point spheres. This is the "good" render path;
// previously it lived only in nodes.html, so /ui got a worse single-STL view.
//
// Page-specific behaviour stays OUT of here and hooks onto the exposed objects:
//   - the node editor attaches its TransformControls gizmo to viewer.scene/camera
//     and its click-to-select picking via viewer.pick();
//   - both pages supply colorOf/wireOf/onEmpty callbacks to renderPreviews.
// ════════════════════════════════════════════════════════════════════
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { toCreasedNormals } from 'three/addons/utils/BufferGeometryUtils.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { ViewHelper } from 'three/addons/helpers/ViewHelper.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';

// distinct, legible-on-dark palette; terminals cycle through it by default
export const PALETTE = ['#2dd4a0', '#3b82f6', '#f59e0b', '#e94560', '#a855f7',
  '#fb923c', '#22d3ee', '#84cc16', '#ec4899', '#eab308', '#14b8a6', '#8b5cf6',
  '#f43f5e', '#38bdf8'];

export function nodeNum(id) { const m = /(\d+)/.exec(id || ''); return m ? parseInt(m[1]) : 0; }

// nearest "nice" 1/2/5×10ⁿ step — used to pick grid spacing that reads cleanly
function niceStep(x) {
  if (!(x > 0)) return 1;
  const base = Math.pow(10, Math.floor(Math.log10(x))), f = x / base;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * base;
}
// tick label: round off float dust, no trailing zeros ("40", "2.5", "-10")
function fmtTick(v) { return Math.abs(v) < 1e-6 ? '0' : String(Math.round(v * 1000) / 1000); }

// ── pure data -> THREE object builders (no app/page state) ──
function geomFromData(m) {
  const g = new THREE.BufferGeometry();
  const verts = m.vertices, tris = m.triangles;
  const pos = new Float32Array(verts.length * 3);
  for (let i = 0; i < verts.length; i++) { pos[3 * i] = verts[i][0]; pos[3 * i + 1] = verts[i][1]; pos[3 * i + 2] = verts[i][2]; }
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const idx = new Uint32Array(tris.length * 3);
  for (let i = 0; i < tris.length; i++) { idx[3 * i] = tris[i][0]; idx[3 * i + 1] = tris[i][1]; idx[3 * i + 2] = tris[i][2]; }
  g.setIndex(new THREE.BufferAttribute(idx, 1));
  g.computeVertexNormals();
  return g;
}
// A hue per index, walked by the golden angle so neighbours never collide. It is
// deterministic, not random: "random colours" that reshuffle on every re-render
// would make a part you are looking at change identity while you turn it.
// Finish decides what a preview is MADE of. `glass` is real transmission, not
// opacity: light refracts through it and the pieces behind stay in the right
// order, which flat alpha blending cannot do at any price. It costs a render
// target per frame — Settings can turn it off (HQ = false) and everything falls
// back to cheap alpha, but in a CAD viewport being able to trust what you see
// through a wall is worth more than the frames.
let HQ = true;
export function setQuality(on) { HQ = !!on; }
// The layer the glow pass renders alone. An emitter is on 0 AND here, so it
// still draws normally in the main pass; everything else stays on 0 only.
export const GLOW_LAYER = 1;
export function markGlow(obj) {
  obj.traverse(o => {
    const m = o.material;
    if (!m) return;
    const emits = (Array.isArray(m) ? m : [m]).some(
      x => x && x.emissive && x.emissiveIntensity > 0 && x.emissive.getHex() !== 0);
    if (emits) o.layers.enable(GLOW_LAYER);
    else o.layers.disable(GLOW_LAYER);
  });
}
export function makeMaterial(color, finish) {
  const base = { color, side: THREE.DoubleSide };
  if (finish === 'glass') {
    return HQ
      ? new THREE.MeshPhysicalMaterial({ ...base, roughness: .08, metalness: 0,
          transmission: 1, thickness: 6, ior: 1.45, transparent: true })
      : new THREE.MeshStandardMaterial({ ...base, roughness: .2, metalness: 0,
          transparent: true, opacity: .35, depthWrite: false });
  }
  if (finish === 'emissive') {
    // Below 1 on purpose. Pushed past it, ACES desaturates the highlight and the
    // body goes flat WHITE — you lose the colour of the thing that is glowing.
    // The halo is what says "source"; the core only has to stay saturated.
    return new THREE.MeshStandardMaterial({ ...base, emissive: color,
      emissiveIntensity: .9, roughness: .5, metalness: 0 });
  }
  if (finish === 'metal')
    return new THREE.MeshStandardMaterial({ ...base, roughness: .22, metalness: 1 });
  if (finish === 'ghost') {
    // The CAD «X-ray»: a faint skin that writes no depth, so whatever is inside
    // stays drawn — another ghost included, which is what glass cannot do (three
    // refracts only the OPAQUE list; a glass inside glass comes out as a ring,
    // and on Android not at all). Plain alpha, no transmission target: it costs
    // nothing and looks the same on a phone. The draw order of alpha is not
    // exact, at 0.15 nobody can tell. Its sharp edges come from ghostEdges().
    const m = new THREE.MeshStandardMaterial({ ...base, roughness: .4, metalness: 0,
      transparent: true, opacity: GHOST_OPACITY, depthWrite: false });
    m.userData.ghost = true;
    return m;
  }
  return new THREE.MeshStandardMaterial({ ...base, roughness: .4, metalness: .12 });
}
export const GHOST_OPACITY = 0.15;
// The sharp edges (dihedral > 30°) of a ghost, drawn OPAQUE — the faces alone
// at 0.15 read as fog; the edges are what say «a box, a hole, a boss». A child
// of the mesh, so it follows every pose. `range` = one geometry group (a piece
// of a fanned-out buffer). It never takes a pick: a click goes to the faces.
export function ghostEdges(geo, color, range = null) {
  let src = geo;
  if (range && geo.index) {
    src = new THREE.BufferGeometry();
    src.setAttribute('position', geo.attributes.position);
    src.setIndex(new THREE.BufferAttribute(geo.index.array.slice(range.start, range.start + range.count), 1));
  }
  const eg = new THREE.EdgesGeometry(src, 30);
  const c = new THREE.Color(color && color.isColor ? color : (color === 'rainbow' || color == null ? 0xc5cdd8 : color));
  const lines = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({ color: c.lerp(new THREE.Color(0xffffff), .25) }));
  lines.raycast = () => {};
  lines.userData.ghostEdges = true;
  return lines;
}
// Keep a mesh's edge child in step with its material(s): edges when every drawn
// material is a ghost, none otherwise. The editor path (one finish per node).
export function syncGhostEdges(mesh, color) {
  if (!mesh || !mesh.isMesh || mesh.isInstancedMesh) return;
  for (const ch of [...mesh.children]) if (ch.userData.ghostEdges) {
    ch.removeFromParent(); ch.geometry.dispose(); ch.material.dispose();
  }
  const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
  if (mats.length && mats.every(m => m && m.userData && m.userData.ghost))
    mesh.add(ghostEdges(mesh.geometry, color));
}

export function rainbowHue(i) {
  const c = new THREE.Color();
  c.setHSL(((i * 137.508) % 360) / 360, 0.62, 0.56);
  return c;
}
// A B-Rep tessellation arrives with every face's vertices of its own, so plain
// vertex normals stay sharp at the edges. A mesh-lane body (manifold3d, trimesh)
// arrives WELDED: one vertex shared by the three faces of a cube corner, and
// averaging across it shades a cube like a pillow. Crease those at 30°, which
// leaves the facets of a curved surface smooth.
const CREASE = Math.PI / 6;
export function meshFromData(m, color, parts, finish, kind) {
  let geo = geomFromData(m);
  if (kind === 'Mesh') { const c = toCreasedNormals(geo, CREASE); geo.dispose(); geo = c; }
  const std = c => makeMaterial(c, finish);
  // Rainbow over a fanned list: one geometry group per piece (`parts` counts
  // triangles, so the offsets are 3x that) and a material per group. Without it
  // the whole buffer keeps ONE material and one draw call, exactly as before.
  if (color === 'rainbow' && parts && parts.length > 1) {
    const mats = []; let start = 0;
    parts.forEach((n, i) => {
      geo.addGroup(start * 3, n * 3, i);
      mats.push(std(rainbowHue(i)));
      start += n;
    });
    const mesh = new THREE.Mesh(geo, mats);
    if (finish === 'ghost') syncGhostEdges(mesh, null);
    return mesh;
  }
  const mesh = new THREE.Mesh(geo, std(color === 'rainbow' ? rainbowHue(0) : color));
  if (finish === 'ghost') syncGhostEdges(mesh, color);
  return mesh;
}
function lineFromPolylines(polys, color) {
  if (color === 'rainbow') color = rainbowHue(0);
  const segs = [];
  for (const poly of polys)
    for (let i = 0; i + 1 < poly.length; i++) { segs.push(poly[i], poly[i + 1]); }
  const g = new THREE.BufferGeometry();
  const pos = new Float32Array(segs.length * 3);
  for (let i = 0; i < segs.length; i++) { pos[3 * i] = segs[i][0]; pos[3 * i + 1] = segs[i][1]; pos[3 * i + 2] = segs[i][2]; }
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  return new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, linewidth: 2 }));
}
function spheresFromData(pts, color, r) {
  if (color === 'rainbow') color = rainbowHue(0);
  const geo = new THREE.SphereGeometry(r, 12, 12);
  const mat = new THREE.MeshStandardMaterial({ color, roughness: .45, metalness: .1 });
  const im = new THREE.InstancedMesh(geo, mat, pts.length);
  const m = new THREE.Matrix4();
  for (let i = 0; i < pts.length; i++) { m.setPosition(pts[i][0], pts[i][1], pts[i][2]); im.setMatrixAt(i, m); }
  im.instanceMatrix.needsUpdate = true;
  im.userData.points = pts;           // keep coords for picking / edit mode
  return im;
}
function previewsExtent(previews, ids) {
  const box = new THREE.Box3(); let any = false;
  for (const id of ids) {
    const e = previews[id]; if (!e || !e.bbox) continue;
    box.expandByPoint(new THREE.Vector3(...e.bbox.min));
    box.expandByPoint(new THREE.Vector3(...e.bbox.max)); any = true;
  }
  if (!any) return 50;
  const s = new THREE.Vector3(); box.getSize(s);
  return Math.max(s.length(), 1);
}
function objFromPreview(p, color, opts, scale) {
  if (p.bodies) {                              // a collide Scene: one child per body,
    const grp = new THREE.Group();             // each independently posable at scrub time
    p.bodies.forEach((b, i) => {
      // Every body of a collide scene is already its own mesh with its own
      // material, so a colour per body is free — no extra draw calls at all.
      // A body that names its OWNER node (today: a Drop container) resolves its
      // own colour and finish from that node, instead of inheriting the Drop's:
      // that is what lets a glass jar pour steel bolts. Bodies without an owner
      // are the Drop's own output and keep the node-level look.
      const own = b.owner || null;
      let bcolor = color === 'rainbow' ? rainbowHue(i) : color;
      let bfinish = opts && opts.finish;
      if (own && opts) {
        // colorOf takes (id, order) — omitting `order` used to reach
        // `order.indexOf` on undefined and kill the whole render.
        // Only an EXPLICIT value on the owner overrides: every falling body now
        // names its own source node, and treating "unset" as an override would
        // silently stop the Drop's own finish from reaching the parts it pours —
        // which is what every existing graph relies on.
        if (opts.colorOf) { const c = opts.colorOf(own, opts.order); if (c) bcolor = c; }
        if (opts.finishOf) { const f = opts.finishOf(own); if (f) bfinish = f; }
      }
      const child = objFromPreview(b, bcolor,
                                   Object.assign({}, opts, {finish: bfinish}), scale);
      if (!child) return;
      child.userData.bodyIndex = i;
      child.userData.anim = b.anim || null;
      grp.add(child);
    });
    grp.userData.isScene = true;
    return grp;
  }
  if (p.mesh) {
    const obj = meshFromData(p.mesh, color, p.parts, opts && opts.finish, p.kind);
    if (opts && opts.wireframe) {
      for (const mt of (Array.isArray(obj.material) ? obj.material : [obj.material])) {
        mt.wireframe = true; mt.metalness = 0;
      }
    }
    return obj;
  }
  if (p.polylines) return lineFromPolylines(p.polylines, color);
  if (p.points) {
    const r = Math.min(Math.max(scale * 0.012, 0.4), scale * 0.05);
    return spheresFromData(p.points, color, r);
  }
  return null;
}

// ── Drop live replay ────────────────────────────────────────────────────────
// The engine ships the WHOLE fall with the preview (previews[id].anim: bounce
// segments + topple steps, world coordinates, and the t it was baked at), so
// the browser can play any t of the timeline as pure matrix math — the same
// physics, anticipated at 60fps while the slider drags; the exact re-bake
// lands when the drag settles.
export function dropMatrixAt(anim, t){
  const M = new THREE.Matrix4();
  const n = new THREE.Vector3(anim.n[0], anim.n[1], anim.n[2]);
  const tau = Math.min(Math.max(+t || 0, 0), 1) * anim.T;
  const h0 = anim.h0;
  if (tau < anim.Tb || !(anim.steps||[]).length){      // still in the air
    const f = anim.Tb > 0 ? tau / anim.Tb : 1;
    let h = 0;
    if (h0 <= 0) h = h0 * (1 - f);
    else {
      let tn = f * anim.tot_n;
      for (const [d, up] of anim.segs){
        if (tn <= d){ h = h0 * Math.max((up == null ? 1 - tn*tn : up*tn - tn*tn), 0); break; }
        tn -= d;
      }
    }
    return M.makeTranslation(n.x*(h-h0), n.y*(h-h0), n.z*(h-h0));
  }
  M.makeTranslation(-h0*n.x, -h0*n.y, -h0*n.z);        // landed…
  let left = tau - anim.Tb;
  for (const s of anim.steps){                         // …then the topples, in order
    let deg = s.deg, partial = false;
    if (left >= s.du - 1e-12){ left -= s.du; }
    else { const fr = Math.max(0, left / s.du); deg = s.deg * fr * fr; partial = true; }
    const ax = new THREE.Vector3(s.ax[0], s.ax[1], s.ax[2]).normalize();
    M.premultiply(new THREE.Matrix4().makeTranslation(-s.p[0], -s.p[1], -s.p[2]))
     .premultiply(new THREE.Matrix4().makeRotationAxis(ax, deg * Math.PI/180))
     .premultiply(new THREE.Matrix4().makeTranslation(s.p[0], s.p[1], s.p[2]));
    if (partial) break;
  }
  return M;
}

// A collide body ships a KEYFRAME plan instead (kind "keys"): pybullet ran the
// whole scene once, and here we interpolate (lerp + slerp) the pose at any t.
export function keyInterp(anim, tau){
  const T = anim.times; if (!T || !T.length) return null;
  const q = (i)=> new THREE.Quaternion(anim.quat[i][0], anim.quat[i][1], anim.quat[i][2], anim.quat[i][3]);
  if (tau <= T[0])            return { p: anim.pos[0], q: q(0) };
  if (tau >= T[T.length-1])   return { p: anim.pos[T.length-1], q: q(T.length-1) };
  let lo = 0, hi = T.length - 1;
  while (hi - lo > 1){ const mid = (lo+hi)>>1; if (T[mid] <= tau) lo = mid; else hi = mid; }
  const f = (tau - T[lo]) / ((T[hi] - T[lo]) || 1);
  const p = [0,1,2].map(i => anim.pos[lo][i]*(1-f) + anim.pos[hi][i]*f);
  return { p, q: q(lo).slerp(q(hi), f) };
}

// Move one Scene body (baked at anim.t) to tNew. Its world vertices sit at pose
// (pB,qB); target is (pT,qT). The rigid delta is Δq = qT·qB⁻¹, and the child's
// offset = pT − Δq·pB, so vertex y → Δq·(y−pB) + pT.
export function sceneBodyPose(anim, tNew, child){
  const T = anim.T || 1;
  const at = keyInterp(anim, Math.min(Math.max(tNew,0),1) * T);
  const bk = keyInterp(anim, Math.min(Math.max(anim.t,0),1) * T);
  if (!at || !bk) return;
  const dq = at.q.clone().multiply(bk.q.clone().invert());
  const pB = new THREE.Vector3(bk.p[0], bk.p[1], bk.p[2]);
  const pT = new THREE.Vector3(at.p[0], at.p[1], at.p[2]);
  child.quaternion.copy(dq);
  child.position.copy(pT).sub(pB.clone().applyQuaternion(dq));
}

// Seat one drawn preview object at timeline position tNew (0..1), relative to
// the t its mesh was baked at. `anim` is the node-level plan (previews[id].anim);
// a collide Scene group carries one plan per body on child.userData.anim, and a
// single part falling into a container is a plain mesh with a "keys" plan.
// Returns false when there is nothing to replay. Shared by the editor's live
// scrub and the /view page's timeline, so the two cannot drift.
export function poseAnim(obj, anim, tNew) {
  if (!obj) return false;
  if (obj.userData && obj.userData.isScene) {
    let any = false;
    for (const child of obj.children) {
      const a = child.userData && child.userData.anim;
      if (a && a.kind === 'keys') { sceneBodyPose(a, tNew, child); any = true; }
    }
    return any;
  }
  if (!anim) return false;
  if (anim.kind === 'keys') { sceneBodyPose(anim, tNew, obj); return true; }
  const rel = dropMatrixAt(anim, tNew).multiply(dropMatrixAt(anim, anim.t).invert());
  rel.decompose(obj.position, obj.quaternion, obj.scale);
  return true;
}

function disposeObject(obj) {
  obj.removeFromParent();
  obj.traverse(o=>{
    if (o.geometry) o.geometry.dispose();
    for (const m of (Array.isArray(o.material) ? o.material : [o.material])) if (m){
      if (m.map) m.map.dispose(); m.dispose();
    }
  });
}

// Appearance is independent of tessellation. Keep vertex/index buffers intact.
function restylePreview(obj, p, color, opts) {
  if (p.bodies){
    for (const child of obj.children){
      const i = child.userData.bodyIndex, b = p.bodies[i];
      let c = color === 'rainbow' ? rainbowHue(i) : color, finish = opts.finish;
      if (b.owner){
        c = (opts.colorOf && opts.colorOf(b.owner, opts.order)) || c;
        finish = (opts.finishOf && opts.finishOf(b.owner)) || finish;
      }
      restylePreview(child, b, c, {...opts, finish});
    }
    return;
  }
  const look = JSON.stringify([color.isColor ? color.getHex() : color, opts.finish, opts.wireframe, HQ]);
  if (obj.userData.look === look) return;
  obj.userData.look = look;
  if (p.mesh){
    for (const m of (Array.isArray(obj.material) ? obj.material : [obj.material])) m.dispose();
    obj.geometry.clearGroups();
    if (color === 'rainbow' && p.parts && p.parts.length > 1){
      let start = 0;
      obj.material = p.parts.map((n,i)=>{
        obj.geometry.addGroup(start * 3, n * 3, i); start += n;
        return makeMaterial(rainbowHue(i), opts.finish);
      });
    } else obj.material = makeMaterial(color === 'rainbow' ? rainbowHue(0) : color, opts.finish);
    for (const m of (Array.isArray(obj.material) ? obj.material : [obj.material])){
      m.wireframe = !!opts.wireframe;
      if (m.wireframe) m.metalness = 0;
    }
    syncGhostEdges(obj, color);
  } else if (obj.material && obj.material.color){
    obj.material.color.set(color === 'rainbow' ? rainbowHue(0) : color);
  }
}

export class CadViewer {
  constructor(canvas, { background = 0x0b0e14 } = {}) {
    this.canvas = canvas;
    this.currentMesh = null;
    this._framed = false;
    this._previewCache = new Map();
    this._loadGeneration = 0;
    this._raf = 0; this._dirty = true; this._disposed = false;
    const c = canvas.parentElement;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(background);
    // Z-up (build123d/CAD convention): the grid IS the XY construction plane,
    // fixed at the world origin — it never follows the geometry.
    const grid = new THREE.GridHelper(200, 40, 0x2a3346, 0x161b25);
    grid.rotation.x = Math.PI / 2; scene.add(grid);
    // origin marker so (0,0,0) is always visible even when shapes move off it
    scene.add(new THREE.Mesh(new THREE.SphereGeometry(0.8, 16, 16),
      new THREE.MeshBasicMaterial({ color: 0xe94560 })));
    const previewGroup = new THREE.Group(); scene.add(previewGroup);
    const ax = new THREE.AxesHelper(20); ax.material.transparent = true; ax.material.opacity = .6; scene.add(ax);
    this.axes = ax;                      // scene furniture: hidden in thumbnails
    scene.add(new THREE.AmbientLight(0x404060, 1.1));
    const key = new THREE.DirectionalLight(0xffffff, 2); key.position.set(40, 60, 80); scene.add(key);
    const fill = new THREE.DirectionalLight(0x8888cc, .8); fill.position.set(-40, -20, 40); scene.add(fill);

    this._fov = 40;
    const camera = new THREE.PerspectiveCamera(this._fov, c.clientWidth / c.clientHeight, .1, 5000);
    camera.up.set(0, 0, 1);                 // Z is up
    camera.position.set(60, -60, 45);

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  // Filmic tone mapping + an image-based environment. This is the whole
  // difference between "shaded triangles" and "a render": specular highlights
  // that wrap, and a sky/floor gradient reflected in every curved face.
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  {
    const pmrem = new THREE.PMREMGenerator(renderer);
    scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();                       // the cubemap is kept, the generator is not
  }
    renderer.setSize(c.clientWidth, c.clientHeight);
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.autoClear = false;             // ViewHelper overlays after the main render
    // ── SELECTIVE bloom ───────────────────────────────────────────────────
    // Blooming the finished image cannot work here, and the reason is worth
    // writing down: `transmission` is not alpha blending — three renders the
    // scene into a separate target and samples it refracted through the glass,
    // so an emitter seen THROUGH a wall arrives already attenuated, lands under
    // the luminance threshold, and the bloom pass never sees it. The glow
    // stopped at the glass.
    // So the emitters get their own layer, are rendered ALONE on black, blurred,
    // and composited ADDITIVELY over the finished frame. Nothing thresholds them
    // (the buffer is black except for them, so threshold 0 and a wide radius give
    // the soft spread the old pass could not), and being additive on top they
    // spill across the glass — which is exactly what the eye reads as light
    // coming through. It is a 2D lie: no refraction of the glow, no caustics.
    // Those need rays. At screen size, in a CAD viewport, you cannot tell.
    this._glowScene = null;                 // built lazily, below
    this._glowComposer = new EffectComposer(renderer);
    this._glowComposer.renderToScreen = false;
    this._glowPass = new RenderPass(scene, camera);
    this._glowComposer.addPass(this._glowPass);
    // HALF resolution on purpose: bloom is a blur, and a blur does not need the
    // pixels. Full-res cost the bowl scene ~1fps against ~29 — the mip chain is
    // rebuilt per frame and a transmission material already re-renders the scene
    // once. Half res is visually identical here and ~4x cheaper.
    this._bloom = new UnrealBloomPass(
      new THREE.Vector2((c.clientWidth || 2) / 2, (c.clientHeight || 2) / 2),
      1.6, 0.8, 0.0);                       // strength, radius, threshold
    this._glowComposer.addPass(this._bloom);
    this._glowComposer.setSize((c.clientWidth || 2) / 2, (c.clientHeight || 2) / 2);
    this._bloomOn = false;

    // the full-screen quad that adds the blurred emitters back over the frame.
    // toneMapped:false — the frame underneath is already tone mapped; mapping the
    // glow a second time would grey it down to nothing.
    this._glowQuadCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this._glowQuadScene = new THREE.Scene();
    // Scaled down (`color`) because the target holds LINEAR, un-tone-mapped values —
    // three only tone maps when it renders to the canvas — and because the pass
    // hands back emitters+blur, so the core would otherwise be added twice.
    this._glowQuadMat = new THREE.MeshBasicMaterial({
      color: 0x595959,
      map: this._glowComposer.renderTarget2.texture,
      blending: THREE.AdditiveBlending, transparent: true,
      depthTest: false, depthWrite: false, toneMapped: false });
    this._glowQuadScene.add(new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this._glowQuadMat));

    const controls = new OrbitControls(camera, canvas);
    controls.enableDamping = true; controls.dampingFactor = .08;

    const viewHelper = new ViewHelper(camera, canvas);
    const clock = new THREE.Clock();

    Object.assign(this, { scene, camera, renderer, controls, previewGroup, grid, viewHelper, _clock: clock });
    this._persp = camera;                   // the two projection cameras; `camera` = active
    this._ortho = null;
    this._projection = 'persp';
    this.onProjectionChange = null;         // pages re-bind their gizmo camera here
    this.inspectGrid = null;                // ruled measurement grid (inspect mode)
    this._pendingInspect = false;           // arm inspect once a nav-gizmo snap lands
    this._wasAnimating = false;
    this._stl = new STLLoader();
    this._ray = new THREE.Raycaster();
    this._mouse = new THREE.Vector2();

    // A user-driven orbit exits the inspect grid (but keeps the projection).
    controls.addEventListener('start', () => { this._pendingInspect = false; this._hideInspectGrid(); });

    canvas.addEventListener('dblclick', () => this.frame());
    this._loop();
  }

  _loop() {
    this._tick = () => {
      this._raf = 0;
      if (this._disposed || document.hidden) return;
      const dt = Math.min(this._clock.getDelta(), 0.05);
      const anim = this.viewHelper.animating;
      if (anim) this.viewHelper.update(dt);
      if (this._wasAnimating && !anim && this._pendingInspect) {
        this._pendingInspect = false;
        this._enterInspectFromView();
      }
      this._wasAnimating = anim;
      const moving = this.controls.update();
      if (this._dirty || anim || moving) this._renderFrame(true);
      this._dirty = false;
      if (anim || moving) this.invalidate();
    };
    this._onChange = () => this.invalidate();
    this.controls.addEventListener('change', this._onChange);
    this._onVisibility = () => {
      if (document.hidden){ cancelAnimationFrame(this._raf); this._raf = 0; }
      else this.invalidate();
    };
    document.addEventListener('visibilitychange', this._onVisibility);
    this.invalidate();
  }

  invalidate() {
    this._dirty = true;
    if (!this._disposed && !document.hidden && !this._raf)
      this._raf = requestAnimationFrame(this._tick);
  }

  // One frame, drawn exactly as the animate loop draws it. Extracted so that
  // snapshot() can render off-clock and read the buffer back without
  // duplicating the bloom sequence — a second copy of it would drift.
  _renderFrame(withHelper = true) {
    // Bloom is what makes an emissive surface look like a SOURCE rather than a
    // brightly painted one — the glow has to spill onto its surroundings. The
    // extra passes run only while something in the scene actually declares
    // itself emissive, and never when HQ is off.
    const glow = this._bloomOn && HQ && this._glowComposer;
    if (glow) {
      // pass 1 — the emitters ALONE, on black, blurred into the glow target.
      const mask = this.camera.layers.mask, bg = this.scene.background;
      this.camera.layers.set(GLOW_LAYER);
      this.scene.background = null;              // or the clear colour blooms too
      this._glowPass.camera = this.camera;       // the viewport swaps persp/ortho
      this._glowComposer.render();
      this.camera.layers.mask = mask;
      this.scene.background = bg;
    }
    this.renderer.clear();
    this.renderer.render(this.scene, this.camera);
    // pass 2 — add the blur back over the finished frame, glass included.
    if (glow) this.renderer.render(this._glowQuadScene, this._glowQuadCam);
    if (withHelper) this.viewHelper.render(this.renderer);
  }

  // ── thumbnail: read the viewport back as a JPEG data URL ────────────────
  // The picture is the one already on screen, so it costs one extra render of a
  // scene that is drawn 60 times a second anyway — no re-execution, no second
  // browser (that is the agent's screenshot API, cad_nodes/screenshot.py, and
  // it drives a headless page of its own). Returns null when there is nothing
  // to show, so a caller never stores a black frame that reads as a bug.
  //
  // The read-back has to happen in the SAME task as the render: without
  // `preserveDrawingBuffer` the WebGL buffer is cleared once the browser
  // composites, and an await in between comes back blank.
  snapshot({ maxSize = 480, quality = 0.85, frame = true } = {}) {
    if (!this.previewGroup.children.length && !this.currentMesh) return null;
    const cam = this.camera;
    const saved = {
      pos: cam.position.clone(), quat: cam.quaternion.clone(), zoom: cam.zoom,
      tgt: this.controls.target.clone(), grid: this.grid.visible,
      axes: this.axes.visible,
      frustum: this.isOrtho
        ? { l: cam.left, r: cam.right, t: cam.top, b: cam.bottom } : null,
    };
    try {
      if (frame) this.frame();       // whole part in shot, at the user's angle
      this.grid.visible = false;     // a 200px card wants the part, not the floor
      this.axes.visible = false;     // nor three stray lines crossing it
      this._renderFrame(false);      // and not the nav gizmo either
      const src = this.canvas;
      const s = Math.min(1, maxSize / Math.max(src.width, src.height, 1));
      const out = document.createElement('canvas');
      out.width = Math.max(1, Math.round(src.width * s));
      out.height = Math.max(1, Math.round(src.height * s));
      out.getContext('2d').drawImage(src, 0, 0, out.width, out.height);
      return out.toDataURL('image/jpeg', quality);
    } catch (e) {
      return null;                   // a thumbnail is never worth an exception
    } finally {
      cam.position.copy(saved.pos); cam.quaternion.copy(saved.quat);
      cam.zoom = saved.zoom;
      if (saved.frustum) {
        cam.left = saved.frustum.l; cam.right = saved.frustum.r;
        cam.top = saved.frustum.t; cam.bottom = saved.frustum.b;
      }
      cam.updateProjectionMatrix();
      this.controls.target.copy(saved.tgt); this.controls.update();
      this.grid.visible = saved.grid;
      this.axes.visible = saved.axes;
      this.invalidate();
    }
  }

  resize() {
    this.invalidate();
    const c = this.canvas.parentElement;
    const aspect = c.clientWidth / c.clientHeight;
    this._persp.aspect = aspect;
    this._persp.updateProjectionMatrix();
    if (this._ortho) {                      // keep ortho frustum matched to the aspect
      const h = (this._ortho.top - this._ortho.bottom) / 2;
      this._ortho.left = -h * aspect; this._ortho.right = h * aspect;
      this._ortho.updateProjectionMatrix();
    }
    this.renderer.setSize(c.clientWidth, c.clientHeight);
    if (this._glowComposer) {
      this._glowComposer.setSize(c.clientWidth / 2, c.clientHeight / 2);
      // setSize rebuilds the targets, so the quad must be re-pointed at the new one
      this._glowQuadMat.map = this._glowComposer.renderTarget2.texture;
    }
    if (this._bloom) this._bloom.resolution.set(c.clientWidth / 2, c.clientHeight / 2);
  }

  setGrid(on) { this.grid.visible = on; this.invalidate(); }

  // ── projection: perspective ⇄ orthographic ─────────────────────────────
  // Ortho is the CAD measuring projection (parallel — no perspective foreshorten).
  // The swap preserves pose AND apparent scale at the orbit target, so it never
  // jumps. OrbitControls, the ViewHelper and (via onProjectionChange) each page's
  // TransformControls are all re-pointed at the newly-active camera.
  get isOrtho() { return this._projection === 'ortho'; }

  setProjection(mode) {
    if (mode === this._projection) return;
    const c = this.canvas.parentElement;
    const aspect = c.clientWidth / c.clientHeight;
    const tgt = this.controls.target;
    const from = this.camera;
    let to;
    if (mode === 'ortho') {
      const dist = from.position.distanceTo(tgt);
      const h = dist * Math.tan(THREE.MathUtils.degToRad(this._fov) / 2);  // half-height at target
      to = this._ortho || new THREE.OrthographicCamera(-h * aspect, h * aspect, h, -h, -10000, 10000);
      to.left = -h * aspect; to.right = h * aspect; to.top = h; to.bottom = -h; to.zoom = 1;
      to.up.copy(from.up); to.position.copy(from.position); to.quaternion.copy(from.quaternion);
      this._ortho = to;
    } else {
      to = this._persp;
      // carry the ortho zoom back into a perspective distance so scale matches
      const visH = ((from.top - from.bottom) / 2) / from.zoom;
      const dist = visH / Math.tan(THREE.MathUtils.degToRad(this._fov) / 2);
      const dir = new THREE.Vector3().subVectors(from.position, tgt);
      if (dir.lengthSq() < 1e-9) dir.set(1, -1, 0.8);
      dir.normalize().multiplyScalar(dist);
      to.up.copy(from.up); to.quaternion.copy(from.quaternion);
      to.position.copy(tgt).add(dir);
      to.aspect = aspect;
    }
    to.updateProjectionMatrix();
    this.camera = to;
    this._projection = mode;
    this.controls.object = to; this.controls.update();
    // re-point the nav gizmo at the active camera (ViewHelper binds one camera)
    if (this.viewHelper.dispose) this.viewHelper.dispose();
    this.viewHelper = new ViewHelper(to, this.canvas);
    this.viewHelper.center.copy(tgt);
    if (this.onProjectionChange) this.onProjectionChange(this._projection);
    this.invalidate();
  }

  toggleProjection() { this.setProjection(this.isOrtho ? 'persp' : 'ortho'); return this._projection; }

  // Re-target + re-distance the camera to fit the shown geometry WITHOUT moving
  // it: keep the current view direction, just frame the real bounds. `fit` (a
  // Box3) frames that box instead — /view passes the pieces left visible.
  frame(fit = null) {
    this.invalidate();
    const box = new THREE.Box3();
    if (fit) box.copy(fit);
    else if (this.previewGroup.children.length) box.setFromObject(this.previewGroup);
    else if (this.currentMesh) box.setFromObject(this.currentMesh);
    if (box.isEmpty()) return;
    const ctr = new THREE.Vector3(); box.getCenter(ctr);
    const size = new THREE.Vector3(); box.getSize(size);
    const md = Math.max(size.x, size.y, size.z) || 10, d = md * 2.0;
    let dir = new THREE.Vector3().subVectors(this.camera.position, this.controls.target);
    if (dir.lengthSq() < 1e-6) dir.set(1, -1, 0.8);
    dir.normalize().multiplyScalar(d);
    this.controls.target.copy(ctr);
    this.camera.position.copy(ctr).add(dir);
    if (this.isOrtho) {                     // size the ortho frustum to fit the bounds
      const c = this.canvas.parentElement;
      const aspect = c.clientWidth / c.clientHeight;
      const h = md * 0.75;
      this.camera.top = h; this.camera.bottom = -h;
      this.camera.left = -h * aspect; this.camera.right = h * aspect;
      this.camera.zoom = 1; this.camera.updateProjectionMatrix();
    }
    this.controls.update();
  }

  // ── inspect mode: a millimetre-ruled grid on the plane you snapped to ───
  // Armed by the page right after a nav-gizmo click consumes a pointer-up; the
  // animate loop fires _enterInspectFromView once the snap animation settles.
  requestInspectOnSnap() { this._pendingInspect = true; this.invalidate(); }

  _modelBox() {
    const box = new THREE.Box3();
    if (this.previewGroup.children.length) box.setFromObject(this.previewGroup);
    else if (this.currentMesh) box.setFromObject(this.currentMesh);
    return box.isEmpty() ? null : box;
  }

  _enterInspectFromView() {
    this.setProjection('ortho');
    const box = this._modelBox();
    const ctr = new THREE.Vector3(0, 0, 0), size = new THREE.Vector3(20, 20, 20);
    if (box) { box.getCenter(ctr); box.getSize(size); }
    // grid normal = the world axis the camera looks most along
    const dir = new THREE.Vector3().subVectors(this.controls.target, this.camera.position).normalize();
    const a = [Math.abs(dir.x), Math.abs(dir.y), Math.abs(dir.z)];
    const normal = a[0] >= a[1] && a[0] >= a[2] ? 0 : (a[1] >= a[2] ? 1 : 2);
    this._buildInspectGrid(normal, ctr, size);
  }

  _hideInspectGrid() {
    if (!this.inspectGrid) return;
    this.scene.remove(this.inspectGrid);
    this.inspectGrid.traverse(o => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
    });
    this.inspectGrid = null;
    this.invalidate();
  }

  _buildInspectGrid(normalAxis, center, size) {
    this._hideInspectGrid();
    const AX = ['x', 'y', 'z'];
    const inPlane = [0, 1, 2].filter(i => i !== normalAxis);   // the two ruled axes
    const uA = inPlane[0], vA = inPlane[1];
    const cu = center[AX[uA]], cv = center[AX[vA]], cn = center[AX[normalAxis]];
    const span = Math.max(size[AX[uA]], size[AX[vA]], 10);
    const H = niceStep(span * 0.75);        // half-extent, rounded to a nice number (margin around the part)
    const major = niceStep(span / 5);
    let minor = major / 10;
    if ((2 * H) / minor > 600) minor = major;                  // never explode the line count

    const grp = new THREE.Group();
    const put = (arr, u, v) => {                               // world point on the grid plane
      const p = new THREE.Vector3(); p.setComponent(uA, u); p.setComponent(vA, v);
      p.setComponent(normalAxis, cn); arr.push(p.x, p.y, p.z);
    };
    const line = (pts, color, opacity) => {
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3));
      const m = new THREE.LineBasicMaterial({ color, transparent: true, opacity });
      return new THREE.LineSegments(g, m);
    };
    const minorPts = [], majorPts = [];
    const lo = k => Math.ceil((k - H) / minor) * minor;
    for (let u = lo(cu); u <= cu + H + 1e-6; u += minor) {
      const isMajor = Math.abs((u / major) - Math.round(u / major)) < 1e-6;
      const dst = isMajor ? majorPts : minorPts;
      put(dst, u, cv - H); put(dst, u, cv + H);
    }
    for (let v = lo(cv); v <= cv + H + 1e-6; v += minor) {
      const isMajor = Math.abs((v / major) - Math.round(v / major)) < 1e-6;
      const dst = isMajor ? majorPts : minorPts;
      put(dst, cu - H, v); put(dst, cu + H, v);
    }
    if (minorPts.length) grp.add(line(minorPts, 0x2a3346, 0.55));
    grp.add(line(majorPts, 0x4a6a9a, 0.9));

    // tick labels (world coords) along the two negative edges — major ticks only
    const lblScale = major * 0.55;
    for (let u = Math.ceil((cu - H) / major) * major; u <= cu + H + 1e-6; u += major) {
      const s = this._labelSprite(fmtTick(u), lblScale);
      const p = new THREE.Vector3(); p.setComponent(uA, u); p.setComponent(vA, cv - H); p.setComponent(normalAxis, cn);
      s.position.copy(p); grp.add(s);
    }
    for (let v = Math.ceil((cv - H) / major) * major; v <= cv + H + 1e-6; v += major) {
      const s = this._labelSprite(fmtTick(v), lblScale);
      const p = new THREE.Vector3(); p.setComponent(uA, cu - H); p.setComponent(vA, v); p.setComponent(normalAxis, cn);
      s.position.copy(p); grp.add(s);
    }
    this.inspectGrid = grp;
    this.scene.add(grp);
    this.invalidate();
  }

  _labelSprite(text, worldSize) {
    const cv = document.createElement('canvas'); cv.width = 128; cv.height = 64;
    const ctx = cv.getContext('2d');
    ctx.font = 'bold 34px -apple-system,Segoe UI,Roboto,sans-serif';
    ctx.fillStyle = '#8ba3c7'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, 64, 32);
    const tex = new THREE.CanvasTexture(cv); tex.anisotropy = 4;
    const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
    spr.scale.set(worldSize * 2, worldSize, 1);
    return spr;
  }

  _clearPreviewGroup() {
    for (let i = this.previewGroup.children.length - 1; i >= 0; i--) {
      const c = this.previewGroup.children[i]; this.previewGroup.remove(c);
      // A collide Scene is a GROUP of bodies, not a Mesh: it carries no geometry
      // of its own, and reaching for one threw — aborting the clear mid-loop, so
      // the stale meshes stayed and every re-render after the first was broken
      // (that is what "Live doesn't work" looked like). Dispose the whole
      // subtree, and only what actually has something to dispose.
      c.traverse(o => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
          for (const m of (Array.isArray(o.material) ? o.material : [o.material])) {
            if (m.map) m.map.dispose();
            m.dispose();
          }
        }
      });
    }
    this.previewGroup.position.set(0, 0, 0);
    this._previewCache.clear();
    this._bloomOn = false;
  }

  _dropMesh() {
    if (this.currentMesh) {
      this.scene.remove(this.currentMesh);
      this.currentMesh.geometry.dispose(); this.currentMesh.material.dispose();
      this.currentMesh = null;
    }
  }

  // Draw one mesh/polyline/point object per drawable preview entry. Returns
  // {order, colors, meshes, size}. The caller supplies:
  //   colorOf(id, order) -> hex   (default: palette by order index)
  //   wireOf(id) -> bool          (default: false)
  //   onEmpty()                   (called when nothing is drawable; e.g. STL fallback)
  renderPreviews(previews, { colorOf, wireOf, finishOf, onEmpty } = {}) {
    let glowing = false;   // does anything in this view declare itself a source?
    previews = previews || {};
    this._loadGeneration++;
    const previous = this._previewCache, next = new Map();
    const drawable = e => e && (e.mesh || e.polylines || e.points || e.bodies);
    const order = Object.keys(previews).filter(k => drawable(previews[k]))
      .sort((a, b) => nodeNum(a) - nodeNum(b));   // stable order for colour slots
    if (!order.length) {
      this._clearPreviewGroup(); this._dropMesh(); this.invalidate();
      if (onEmpty) onEmpty();
      return { order: [], colors: {}, meshes: {}, size: null };
    }
    this._dropMesh();
    const scale = previewsExtent(previews, order);
    const colors = {}, meshes = {};
    for (const id of order) {
      const color = colorOf ? colorOf(id, order) : PALETTE[order.indexOf(id) % PALETTE.length];
      const finish = finishOf ? finishOf(id) : null;
      if (finish === 'emissive') glowing = true;
      // A Scene body may name its own owner node (a Drop container), whose finish
      // is resolved separately — so the glow layer has to look there too, or an
      // emissive bowl would light nothing.
      for (const b of (previews[id].bodies || []))
        if (b.owner && finishOf && finishOf(b.owner) === 'emissive') glowing = true;
      const p = previews[id], old = previous.get(id);
      const opts = {wireframe: wireOf ? wireOf(id) : false, finish, colorOf, finishOf, order};
      const reusable = old && (old.data === p || (p.cache_key && old.key === p.cache_key)) &&
        (!p.points || old.scale === scale);
      let obj;
      if (reusable){
        obj = old.obj;
        if (old.data !== p) obj.traverse(o=>{
          o.position.set(0,0,0); o.quaternion.identity(); o.scale.set(1,1,1); o.updateMatrix();
        });
        restylePreview(obj, p, color, opts);
      } else {
        obj = objFromPreview(p, color, opts, scale);
        if (old) disposeObject(old.obj);
      }
      if (!obj) continue;
      next.set(id, {obj, data:p, key:p.cache_key, scale});
      obj.userData.nodeId = id;
      markGlow(obj);                      // put its emitters on the glow layer
      if (obj.parent !== this.previewGroup) this.previewGroup.add(obj);
      colors[id] = color; meshes[id] = obj;
    }
    for (const [id, old] of previous) if (!next.has(id)) disposeObject(old.obj);
    this._previewCache = next;
    this._bloomOn = glowing;
    this.invalidate();
    const box = new THREE.Box3().setFromObject(this.previewGroup);
    const size = new THREE.Vector3(); box.getSize(size);
    if (!this._framed) { this.frame(); this._framed = true; }
    return { order, colors, meshes, size };
  }

  // Fallback single-STL load (used when an execution produced no per-node mesh).
  loadSTL(url, { onSize } = {}) {
    const generation = ++this._loadGeneration;
    this._stl.load(url, (geo) => {
      if (generation !== this._loadGeneration || this._disposed){ geo.dispose(); return; }
      this._dropMesh();
      geo.computeVertexNormals();
      this.currentMesh = new THREE.Mesh(geo,
        new THREE.MeshStandardMaterial({ color: 0xe94560, roughness: .35, metalness: .15, side: THREE.DoubleSide }));
      this.scene.add(this.currentMesh);          // real coords — no recentering
      this.invalidate();
      if (!this._framed) { this.frame(); this._framed = true; }
      if (onSize) {
        geo.computeBoundingBox(); const s = new THREE.Vector3(); geo.boundingBox.getSize(s);
        onSize(s);
      }
    }, undefined, (err) => { if (generation === this._loadGeneration && onSize) onSize(null, err); });
  }

  clear() {
    this._loadGeneration++;
    this._clearPreviewGroup(); this._dropMesh(); this._hideInspectGrid(); this.invalidate();
  }
  dispose() {
    this.clear(); this._disposed = true;
    cancelAnimationFrame(this._raf); this._raf = 0;
    document.removeEventListener('visibilitychange', this._onVisibility);
    this.controls.removeEventListener('change', this._onChange);
    this.controls.dispose(); this.viewHelper.dispose();
    this._bloom.dispose(); this._glowComposer.dispose();
    this._glowQuadMat.dispose();
    this._glowQuadScene.traverse(o=>{ if (o.geometry) o.geometry.dispose(); });
    if (this.scene.environment) this.scene.environment.dispose();
    this.scene.traverse(o=>{
      if (o.geometry) o.geometry.dispose();
      for (const m of (Array.isArray(o.material) ? o.material : [o.material])) if (m) m.dispose();
    });
    this.renderer.dispose();
  }
  resetFraming() { this._framed = false; }
  // After a page restyles pieces in place (the /view 🔍 Aspetto): markGlow() the
  // objects it touched, then this — the glow passes run only while something on
  // the GLOW_LAYER is in the scene.
  syncGlow() {
    let on = false;
    this.previewGroup.traverse(o => { if (o.layers.isEnabled(GLOW_LAYER) && o.visible) on = true; });
    this._bloomOn = on;
    this.invalidate();
  }

  // Raycast the previews under a screen point; returns the owning graph node id
  // (obj.userData.nodeId) of the nearest hit, or null. For click-to-select.
  pick(clientX, clientY) {
    const r = this.canvas.getBoundingClientRect();
    this._mouse.x = ((clientX - r.left) / r.width) * 2 - 1;
    this._mouse.y = -((clientY - r.top) / r.height) * 2 + 1;
    this._ray.setFromCamera(this._mouse, this.camera);
    this._ray.params.Line.threshold = 0.5;
    this._ray.params.Points.threshold = 3;
    const hits = this._ray.intersectObjects(this.previewGroup.children, true)
      .filter(h => h.object.visible !== false);   // recurse into Scene groups
    for (const h of hits) {                        // walk up to the node-owning object
      let o = h.object;
      while (o && o.userData.nodeId == null && o !== this.previewGroup) o = o.parent;
      if (o && o.userData.nodeId != null) return o.userData.nodeId;
    }
    return null;
  }
}
