// ✂ Sezione — cut the pieces with ONE plane and fill each cut with the
// technical-drawing hatch. Rendering only; the pure geometry is section-core.js.
// Lives beside viewer.js (not in view.html) so the editor can take it later.
//
//  - THE CUT is `material.clippingPlanes`, per piece, never
//    `renderer.clippingPlanes`: that one would cut the grid, the notes and the
//    tags too, and could not leave a piece whole. It is set in each piece's
//    `onBeforeRender`, i.e. on whatever material the piece wears at draw time —
//    a restyle (colour, glass, ghost) swaps the material and the cut follows
//    with no call to remember.
//  - THE CAP is the stencil technique (three's webgl_clipping_stencil), PER
//    PIECE: two colourless passes count the piece's back faces up and its front
//    faces down, then a plane on the cut is drawn where the count is not zero,
//    and clears the stencil after itself. One stencil for the whole scene would
//    get the parity wrong wherever two pieces nest or overlap.
//  - THE HATCH is in the PLANE's own coordinates, in mm — not in screen space —
//    so it does not swim while you orbit; the pitch is ~9 px snapped to
//    1/2/5×10ⁿ mm, so it does not crawl at each wheel notch either. Neighbour
//    pieces (overlapping boxes) get 45° and 135°, as in an assembly drawing.
//  - THE CONTOUR (dark line round each cap) is the triangle/plane intersection
//    on the CPU, once per plane position; while the timeline plays it is
//    hidden (the stencil is free per frame, the CPU slice is not), and drawn
//    again when the pieces stop.
//  - A piece with no volume — lines, points, an open shell — is cut and NOT
//    capped (closedRange): the parity of an open surface has no inside.
import * as THREE from 'three';
import { planeOf, sliceTriangles, closedRange, niceStep, signedDist } from './section-core.js';

const NONE = [];
const ORDER0 = 10;                              // after every ordinary piece (renderOrder 0)

const HATCH_VS = `
varying vec3 vW;
void main() {
  vec4 w = modelMatrix * vec4(position, 1.0);
  vW = w.xyz;
  gl_Position = projectionMatrix * viewMatrix * w;
}`;
const HATCH_FS = `
uniform vec3 color;
uniform vec3 origin;
uniform vec3 uAx;
uniform vec3 vAx;
uniform float pitch;
uniform vec2 dir;
varying vec3 vW;
void main() {
  vec3 d = vW - origin;
  float s = (dot(d, uAx) * dir.x + dot(d, vAx) * dir.y) / pitch;
  float k = abs(fract(s + 0.5) - 0.5);           // 0 on a line, 0.5 between two
  float w = fwidth(s);
  float line = 1.0 - smoothstep(0.11 - w, 0.11 + w, k);
  vec3 c = mix(color * 0.82, color * 0.16, line);
  gl_FragColor = vec4(c, 1.0);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}`;

const stencilMat = side => new THREE.MeshBasicMaterial({
  side, colorWrite: false, depthWrite: false, depthTest: false,
  stencilWrite: true, stencilFunc: THREE.AlwaysStencilFunc,
  stencilFail: side === THREE.BackSide ? THREE.IncrementWrapStencilOp : THREE.DecrementWrapStencilOp,
  stencilZFail: side === THREE.BackSide ? THREE.IncrementWrapStencilOp : THREE.DecrementWrapStencilOp,
  stencilZPass: side === THREE.BackSide ? THREE.IncrementWrapStencilOp : THREE.DecrementWrapStencilOp,
});

// leaves: [{key, obj, part?}] — `obj` is what draws it (a Mesh, a scene body, a
// LineSegments, the dots' InstancedMesh); `part` = its geometry group when one
// buffer holds several pieces.
export class Section {
  constructor(viewer, { onChange } = {}) {
    this.viewer = viewer;
    this.onChange = onChange || null;
    this.state = { on: false, axis: 'z', pos: 0, flip: false };
    this.nocut = new Set();
    this.plane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);
    this._planes = [this.plane];
    this.moving = false;
    this.leaves = [];
    this.rigs = [];
    this.group = new THREE.Group();
    this.group.name = 'section';
    viewer.scene.add(this.group);
    viewer.renderer.localClippingEnabled = true;
    this._back = stencilMat(THREE.BackSide);
    this._front = stencilMat(THREE.FrontSide);
    this._back.clippingPlanes = this._front.clippingPlanes = this._planes;
    this._capGeo = new THREE.PlaneGeometry(1, 1);
    this._box = new THREE.Box3();
    this._contourTimer = 0;
    // per frame, before anything is drawn: rigs follow their piece (pose,
    // visibility, colour) and the hatch pitch follows the zoom
    const prev = viewer.scene.onBeforeRender;
    viewer.scene.onBeforeRender = (...a) => { this._frame(); prev.apply(viewer.scene, a); };
  }

  // ── what is cut ──
  bind(leaves) {
    this._unbind();
    this.leaves = leaves;
    const byObj = new Map();
    for (const L of leaves) {
      if (!byObj.has(L.obj)) byObj.set(L.obj, []);
      byObj.get(L.obj).push(L);
    }
    for (const [obj, Ls] of byObj) {
      const keyOf = Ls.length > 1 || Ls[0].part != null
        ? grp => { const L = Ls.find(x => x.part === (grp ? grp.materialIndex : 0)); return L ? L.key : null; }
        : () => Ls[0].key;
      obj.traverse(o => {
        if (!o.material) return;
        const prev = o.onBeforeRender;
        o.userData._secPrev = prev;
        o.onBeforeRender = (r, s, c, g, m, grp) => {
          m.clippingPlanes = this.cuts(keyOf(grp)) ? this._planes : NONE;
          prev.call(o, r, s, c, g, m, grp);
        };
      });
    }
    // a cap per closed triangle piece; neighbours (overlapping boxes) alternate
    // 45° / 135°, chosen greedily against the ones already placed
    const placed = [];
    leaves.forEach((L, i) => {
      const o = L.obj;
      if (!o.isMesh || o.isInstancedMesh || !o.geometry || !o.geometry.attributes.position) return;
      const g = o.geometry, pos = g.attributes.position.array, idx = g.index ? g.index.array : null;
      let start = 0, count = idx ? idx.length : pos.length / 3;
      if (L.part != null) {
        const gr = g.groups.find(x => x.materialIndex === L.part);
        if (!gr) return;
        start = gr.start; count = gr.count;
      }
      if (!g.boundingBox) g.computeBoundingBox();
      const size = g.boundingBox.getSize(new THREE.Vector3()).length() || 1;
      const shut = closedRange(pos, idx, start, count, Math.max(size * 1e-6, 1e-5));
      o.updateWorldMatrix(true, false);
      const box = new THREE.Box3();
      for (let t = start; t < start + count; t++) {
        const k = idx ? idx[t] : t;
        box.expandByPoint(new THREE.Vector3(pos[3 * k], pos[3 * k + 1], pos[3 * k + 2]));
      }
      box.applyMatrix4(o.matrixWorld);
      const rig = { L, i, start, count, closed: shut.closed, boundary: shut.boundary, box };
      this.rigs.push(rig);
      if (!shut.closed) return;
      let view = g;
      if (L.part != null) {                     // the group's triangles only, same buffers
        view = new THREE.BufferGeometry();
        for (const [k, a] of Object.entries(g.attributes)) view.setAttribute(k, a);
        view.setIndex(g.index);
        view.setDrawRange(start, count);
        rig.view = view;
      }
      const pad = box.clone().expandByScalar(box.getSize(new THREE.Vector3()).length() * 0.02);
      const near = placed.filter(p => p.box.intersectsBox(pad));
      const n45 = near.filter(p => p.ang === 45).length, n135 = near.length - n45;
      rig.ang = n45 > n135 ? 135 : 45;
      placed.push(rig);
      const order = ORDER0 + placed.length * 3;
      const mk = mat => {
        const m = new THREE.Mesh(view, mat);
        m.matrixAutoUpdate = false; m.matrixWorldAutoUpdate = false; m.frustumCulled = false;
        m.raycast = () => {};
        return m;
      };
      rig.back = mk(this._back); rig.back.renderOrder = order;
      rig.front = mk(this._front); rig.front.renderOrder = order + 1;
      const a = rig.ang * Math.PI / 180;
      rig.mat = new THREE.ShaderMaterial({
        vertexShader: HATCH_VS, fragmentShader: HATCH_FS, side: THREE.DoubleSide,
        uniforms: { color: { value: new THREE.Color() }, origin: { value: new THREE.Vector3() },
          uAx: { value: new THREE.Vector3(1, 0, 0) }, vAx: { value: new THREE.Vector3(0, 1, 0) },
          pitch: { value: 1 }, dir: { value: new THREE.Vector2(Math.cos(a), Math.sin(a)) } },
        stencilWrite: true, stencilRef: 0, stencilFunc: THREE.NotEqualStencilFunc,
        stencilFail: THREE.ReplaceStencilOp, stencilZFail: THREE.ReplaceStencilOp, stencilZPass: THREE.ReplaceStencilOp,
      });
      rig.cap = new THREE.Mesh(this._capGeo, rig.mat);
      rig.cap.renderOrder = order + 2;
      rig.cap.frustumCulled = false;
      rig.cap.raycast = () => {};
      rig.cap.onAfterRender = r => r.clearStencil();
      this.group.add(rig.back, rig.front, rig.cap);
    });
    this._place();
    this._contoursLater(0);
    this.viewer.invalidate();
  }
  _unbind() {
    for (const L of this.leaves) L.obj.traverse(o => {
      if (o.userData._secPrev) { o.onBeforeRender = o.userData._secPrev; delete o.userData._secPrev; }
    });
    for (const r of this.rigs) {
      if (r.mat) r.mat.dispose();
      if (r.line) { r.line.geometry.dispose(); r.line.material.dispose(); }
      // r.view shares the piece's buffers: dropping it is enough, disposing it
      // would free the piece's own GPU buffers
    }
    this.group.clear();
    this.rigs = []; this.leaves = [];
  }

  // ── the state ──
  get on() { return this.state.on; }
  cuts(key) { return this.state.on && key != null && !this.nocut.has(key); }
  // a point the cut took away from this piece: firstHit skips it
  hides(key, p) { return this.cuts(key) && this.plane.distanceToPoint(p) < -1e-6; }
  // Does the ray meet the CAP of a cut piece, and where? `hits` = every
  // raycast hit along it, removed side included, nearest first, as {h, key}.
  // The ray is inside a piece where it crosses the plane when it has crossed
  // that piece's surface an odd number of times before — the same parity the
  // stencil counts. Returns a hit shaped like the raycaster's ({h, key}, with
  // h.cap = true and no faceIndex), or null.
  capAt(ray, hits) {
    if (!this.state.on) return null;
    const t = ray.distanceToPlane(this.plane);
    if (t == null || t <= 0) return null;
    const odd = new Map();
    for (const { h, key } of hits) {
      if (h.distance >= t) break;
      if (h.face) odd.set(key, !odd.get(key));
    }
    let r = null;
    for (const [key, inside] of odd) {
      if (!inside) continue;
      r = this.rigs.find(x => x.cap && x.L.key === key && this.cuts(key) && shownLeaf(x.L, this.viewer.previewGroup));
      if (r) break;
    }
    if (!r) return null;
    const point = ray.at(t, new THREE.Vector3());
    const n = this.plane.normal.clone();
    if (this.plane.distanceToPoint(ray.origin) < 0) n.negate();       // the face you look at
    const o = r.L.obj;
    o.updateWorldMatrix(true, false);
    const local = n.transformDirection(new THREE.Matrix4().copy(o.matrixWorld).invert());
    return { key: r.L.key, h: { point, distance: t, object: o, cap: true, faceIndex: null,
      face: { normal: local, materialIndex: r.L.part || 0 } } };
  }
  set(patch) {
    const s = this.state, was = JSON.stringify([s, [...this.nocut]]);
    Object.assign(s, patch);
    if (!['x', 'y', 'z'].includes(s.axis)) s.axis = 'z';
    s.pos = +s.pos || 0; s.flip = !!s.flip; s.on = !!s.on;
    if (JSON.stringify([s, [...this.nocut]]) === was) return;
    this._place();
    this._contoursLater();
    this.viewer.invalidate();
    if (this.onChange) this.onChange(this.state);
  }
  setExcluded(keys, excluded) {
    for (const k of keys) excluded ? this.nocut.add(k) : this.nocut.delete(k);
    this._contoursLater(0);
    this.viewer.invalidate();
    if (this.onChange) this.onChange(this.state);
  }
  // while the timeline plays the pieces move under a still plane: the caps
  // follow for free (stencil), the CPU contour waits for them to stop
  setMoving(on) {
    if (this.moving === !!on) return;
    this.moving = !!on;
    if (!on) this._contoursLater(0);
    this.viewer.invalidate();
  }
  posed() { if (!this.moving) this._contoursLater(80); }

  // the range of the slider: the box of what is SHOWN, along the axis
  range(box) {
    const i = ['x', 'y', 'z'].indexOf(this.state.axis);
    if (!box || box.isEmpty()) return { min: -50, max: 50, step: 1 };
    const lo = box.min.getComponent(i), hi = box.max.getComponent(i);
    const step = niceStep(Math.max(hi - lo, 1e-3) / 400);
    return { min: Math.floor(lo / step) * step, max: Math.ceil(hi / step) * step, step };
  }

  _place() {
    const pl = planeOf(this.state);
    this.plane.normal.set(pl.n[0], pl.n[1], pl.n[2]);
    this.plane.constant = pl.c;
    // the caps: one big quad on the plane, facing the removed side
    const box = this._box.makeEmpty();
    for (const r of this.rigs) box.union(r.box);
    if (box.isEmpty()) return;
    const ctr = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3()).length() * 1.5 + 1;
    this.plane.projectPoint(ctr, ctr);
    const n = this.plane.normal.clone().negate();          // towards the viewer of the cut
    const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), n);
    // the plane's own axes, in world terms: the hatch is drawn in them
    const ax = pl.axis, u = new THREE.Vector3(), v = new THREE.Vector3();
    u.setComponent((ax + 1) % 3, 1); v.setComponent((ax + 2) % 3, 1);
    for (const r of this.rigs) if (r.cap) {
      r.cap.position.copy(ctr); r.cap.quaternion.copy(q); r.cap.scale.set(size, size, 1);
      r.cap.updateMatrix();
      r.mat.uniforms.origin.value.copy(ctr);
      r.mat.uniforms.uAx.value.copy(u); r.mat.uniforms.vAx.value.copy(v);
    }
  }

  _frame() {
    const on = this.state.on;
    this.group.visible = on;
    if (!on || !this.rigs.length) return;
    const cam = this.viewer.camera;
    const h = this.viewer.canvas.getBoundingClientRect().height || 1;
    const ctr = this._box.isEmpty() ? new THREE.Vector3() : this._box.getCenter(new THREE.Vector3());
    const mmpx = cam.isOrthographicCamera ? (cam.top - cam.bottom) / cam.zoom / h
      : 2 * cam.position.distanceTo(ctr) * Math.tan(THREE.MathUtils.degToRad(cam.fov) / 2) / h;
    const pitch = niceStep(9 * mmpx);
    for (const r of this.rigs) {
      const show = this.cuts(r.L.key) && shownLeaf(r.L, this.viewer.previewGroup);
      if (r.line) r.line.visible = show && !this.moving && !r.stale;
      if (!r.cap) continue;
      r.back.visible = r.front.visible = r.cap.visible = show;
      if (!show) continue;
      r.back.matrixWorld.copy(r.L.obj.matrixWorld);
      r.front.matrixWorld.copy(r.L.obj.matrixWorld);
      const m = materialOf(r.L);
      if (m && m.color) r.mat.uniforms.color.value.copy(m.color);
      r.mat.uniforms.pitch.value = pitch;
    }
  }

  // the dark outline, on the CPU: later (a slider drag fires many inputs) and
  // never while the pieces are moving
  _contoursLater(ms = 60) {
    for (const r of this.rigs) r.stale = true;
    clearTimeout(this._contourTimer);
    if (!this.state.on || this.moving) return;
    this._contourTimer = setTimeout(() => this._contours(), ms);
  }
  _contours() {
    const pl = planeOf(this.state);
    // a hair towards the removed side, so the line wins over its own cap
    const lift = this._box.isEmpty() ? 0 : this._box.getSize(new THREE.Vector3()).length() * 2e-4;
    for (const r of this.rigs) {
      if (r.line) { this.group.remove(r.line); r.line.geometry.dispose(); r.line.material.dispose(); r.line = null; }
      r.stale = false;
      if (!r.closed || !this.cuts(r.L.key)) continue;
      const o = r.L.obj, g = o.geometry;
      o.updateWorldMatrix(true, false);
      const seg = sliceTriangles(g.attributes.position.array, g.index ? g.index.array : null,
                                 r.start, r.count, o.matrixWorld.elements, pl);
      if (!seg.length) continue;
      const arr = new Float32Array(seg);
      for (let k = 0; k < arr.length; k += 3) {
        arr[k] -= pl.n[0] * lift; arr[k + 1] -= pl.n[1] * lift; arr[k + 2] -= pl.n[2] * lift;
      }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(arr, 3));
      const m = materialOf(r.L);
      const c = (m && m.color ? m.color.clone() : new THREE.Color(0x888888)).multiplyScalar(0.18);
      r.line = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: c }));
      r.line.renderOrder = ORDER0 + 9000;
      r.line.raycast = () => {};
      this.group.add(r.line);
    }
    this.viewer.invalidate();
  }
  // the note keeps the plane it was drawn on: without it the agent sees a hole
  // the model does not have
  toJSON() {
    return { axis: this.state.axis, pos: +this.state.pos.toFixed(4), flip: this.state.flip,
             ...(this.nocut.size ? { nocut: [...this.nocut] } : {}) };
  }
}

function materialOf(L) {
  const m = L.obj.material;
  return Array.isArray(m) ? m[L.part || 0] : m;
}
function shownLeaf(L, root) {
  for (let p = L.obj; p && p !== root; p = p.parent) if (!p.visible) return false;
  if (L.part != null) { const m = materialOf(L); if (m && m.visible === false) return false; }
  return true;
}
export { signedDist };
