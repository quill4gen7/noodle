// ✂ Sezione — the PURE half (no three, no DOM): the plane, the triangle/plane
// slice that draws the dark contour, the closed-mesh test that decides whether a
// piece gets a cap at all, the stencil-count model the cap relies on, and the
// hash keys. Pure so tests/ui/section.test.cjs runs it in node; webui/section.js
// does the rendering on top of it.

export const AXES = ['x', 'y', 'z'];

// The plane keeps n·p + c ≥ 0 — three's clipping convention (a point at a
// negative distance is clipped). cut=z:12.5 keeps z ≤ 12.5 (the top comes off,
// the usual way to look into a part on a table); flip keeps z ≥ 12.5.
export function planeOf({ axis = 'z', pos = 0, flip = false } = {}) {
  const i = Math.max(0, AXES.indexOf(axis));
  const n = [0, 0, 0];
  n[i] = flip ? 1 : -1;
  return { n, c: flip ? -pos : pos, axis: i };
}
export const signedDist = (pl, x, y, z) => pl.n[0] * x + pl.n[1] * y + pl.n[2] * z + pl.c;

// Where the triangles of [start, start+count) (index positions, as a geometry
// group counts them) cross the plane: a flat [x0,y0,z0,x1,y1,z1, …] of segments
// in WORLD coordinates. `m` is the object's matrixWorld (column-major 16, as
// three keeps it) or null. A vertex exactly on the plane counts as the kept
// side, so a triangle touching it with one corner gives no zero-length segment
// and a face lying in it gives none at all (the cap shows it, not the outline).
export function sliceTriangles(pos, index, start, count, m, pl) {
  const out = [];
  const nv = pos.length / 3;
  const W = new Float64Array(nv * 3), D = new Float64Array(nv);
  const done = new Uint8Array(nv);
  const vert = k => {
    if (done[k]) return;
    done[k] = 1;
    let x = pos[3 * k], y = pos[3 * k + 1], z = pos[3 * k + 2];
    if (m) {
      const X = m[0] * x + m[4] * y + m[8] * z + m[12];
      const Y = m[1] * x + m[5] * y + m[9] * z + m[13];
      const Z = m[2] * x + m[6] * y + m[10] * z + m[14];
      x = X; y = Y; z = Z;
    }
    W[3 * k] = x; W[3 * k + 1] = y; W[3 * k + 2] = z;
    D[k] = signedDist(pl, x, y, z);
  };
  const end = index ? Math.min(start + count, index.length) : Math.min(start + count, nv);
  const tri = [0, 0, 0], pts = [];
  for (let t = start; t + 2 < end; t += 3) {
    for (let j = 0; j < 3; j++) { tri[j] = index ? index[t + j] : t + j; vert(tri[j]); }
    const a = D[tri[0]] >= 0, b = D[tri[1]] >= 0, c = D[tri[2]] >= 0;
    if (a === b && b === c) continue;
    pts.length = 0;
    for (let j = 0; j < 3; j++) {
      const p = tri[j], q = tri[(j + 1) % 3];
      const dp = D[p], dq = D[q];
      if ((dp >= 0) === (dq >= 0)) continue;
      const f = dp / (dp - dq);
      pts.push(W[3 * p] + f * (W[3 * q] - W[3 * p]),
               W[3 * p + 1] + f * (W[3 * q + 1] - W[3 * p + 1]),
               W[3 * p + 2] + f * (W[3 * q + 2] - W[3 * p + 2]));
    }
    if (pts.length === 6) out.push(...pts);
  }
  return out;
}

// Is the surface of [start, start+count) CLOSED? Every edge, with the vertices
// welded by position (a B-Rep tessellation repeats each face's vertices, and a
// mesh-lane body shares them), must be used an even number of times. An open
// shell (a Join of five faces, §5f), a single face, a surface: boundary edges,
// and the stencil parity would then paint a cap that "runs" outside the part —
// so they are cut and NOT capped. `tol` is the weld size in model units.
export function closedRange(pos, index, start, count, tol = 1e-4) {
  const nv = pos.length / 3;
  const id = new Int32Array(nv).fill(-1);
  const weld = new Map();
  const q = v => Math.round(v / tol);
  const W = k => {
    if (id[k] >= 0) return id[k];
    const key = q(pos[3 * k]) + ',' + q(pos[3 * k + 1]) + ',' + q(pos[3 * k + 2]);
    let w = weld.get(key);
    if (w === undefined) { w = weld.size; weld.set(key, w); }
    return (id[k] = w);
  };
  const end = index ? Math.min(start + count, index.length) : Math.min(start + count, nv);
  const edges = new Map();
  let tris = 0;
  for (let t = start; t + 2 < end; t += 3) {
    const v = [0, 1, 2].map(j => W(index ? index[t + j] : t + j));
    if (v[0] === v[1] || v[1] === v[2] || v[0] === v[2]) continue;   // degenerate sliver
    tris++;
    for (let j = 0; j < 3; j++) {
      const a = v[j], b = v[(j + 1) % 3];
      const k = a < b ? a * 4294967296 + b : b * 4294967296 + a;
      edges.set(k, (edges.get(k) || 0) + 1);
    }
  }
  let boundary = 0;
  for (const n of edges.values()) if (n & 1) boundary++;
  return { closed: tris > 0 && boundary === 0, boundary, edges: edges.size, tris };
}

// What the stencil holds at one pixel, modelled on the CPU: walk the ray
// through every triangle that survives the cut (kept side), +1 for a back face
// (facing away from the eye), −1 for a front face — the two colourless passes
// of webui/section.js, IncrementWrap / DecrementWrap, with no depth test. The
// cap is drawn where the count is NOT zero. `tris` = [[a,b,c], …] of [x,y,z],
// counter-clockwise seen from outside.
export function stencilCount(tris, eye, dir, pl) {
  let s = 0;
  for (const [a, b, c] of tris) {
    const e1 = sub(b, a), e2 = sub(c, a);
    const nrm = cross(e1, e2);
    const h = cross(dir, e2), det = dot(e1, h);
    if (Math.abs(det) < 1e-12) continue;
    const f = 1 / det, sv = sub(eye, a), u = f * dot(sv, h);
    if (u < 0 || u > 1) continue;
    const qv = cross(sv, e1), v = f * dot(dir, qv);
    if (v < 0 || u + v > 1) continue;
    const t = f * dot(e2, qv);
    if (t <= 0) continue;
    const p = [eye[0] + t * dir[0], eye[1] + t * dir[1], eye[2] + t * dir[2]];
    if (pl && signedDist(pl, p[0], p[1], p[2]) < 0) continue;     // clipped away
    s += dot(nrm, dir) > 0 ? 1 : -1;
  }
  return s;
}
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];

// 1 / 2 / 5 × 10ⁿ — the hatch pitch and the slider step snap to it, so the
// lines do not crawl at every wheel notch (the pen width does the same).
export function niceStep(x) {
  if (!(x > 0)) return 1;
  const b = Math.pow(10, Math.floor(Math.log10(x))), f = x / b;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * b;
}

// ── the hash: cut=z:12.5 · cutflip=1 · nocut=n3,n7.2 ──
export function parseCut(s) {
  const m = /^([xyz]):(-?\d+(?:\.\d+)?(?:e-?\d+)?)$/i.exec(String(s || '').trim());
  if (!m) return null;
  const pos = parseFloat(m[2]);
  return Number.isFinite(pos) ? { axis: m[1].toLowerCase(), pos } : null;
}
export const formatCut = ({ axis, pos }) => `${axis}:${+(+pos).toFixed(3)}`;

// A set of leaf keys as `hide=` writes it: a node whose leaves are ALL in the
// set (and has more than one) goes by its bare id. nodes = [{id, leaves:[key]}].
export function encodeKeys(set, nodes) {
  const out = [];
  for (const n of nodes) {
    const h = n.leaves.filter(k => set.has(k));
    if (!h.length) continue;
    if (h.length === n.leaves.length && n.leaves.length > 1) out.push(n.id);
    else out.push(...h);
  }
  return out.join(',');
}
export function decodeKeys(s, nodes) {
  const out = new Set();
  const all = new Set(nodes.flatMap(n => n.leaves));
  for (const k of String(s || '').split(',').filter(Boolean)) {
    const n = nodes.find(x => x.id === k);
    if (n) n.leaves.forEach(l => out.add(l));
    else if (all.has(k)) out.add(k);
  }
  return out;
}
