// 📏 Metro — the features a dimension snaps to, rebuilt from the TRIANGLES.
//
// The /view page has no B-Rep: view.json carries tessellations only. A CAD's
// measure tool snaps to vertices, edges, circles and faces, so those are
// recovered here from the mesh, in the browser:
//
//   sharp edges  — a mesh edge with one triangle (a border) or whose two
//                  triangles meet at more than ~28° (build123d tessellates a
//                  face on its own, so a real edge of the part is a crease);
//   chains       — sharp edges strung together between corners: a LINE (an
//                  edge of a box), a CIRCLE or ARC (the rim of a hole), or a
//                  free curve;
//   corners      — chain ends: where ≥ 3 sharp edges meet, or the direction
//                  turns by more than ~20°;
//   planar faces — a flood fill over neighbours with the same normal and on
//                  the same plane;
//   cylinders    — a smooth patch (flood fill that never crosses a sharp
//                  edge) whose normals all lie in one plane: the axis is the
//                  direction they never point along, the radius a circle fit.
//
// What is EXACT and what is not: build123d puts tessellation vertices on the
// true geometry, so vertices and planar faces are exact; a tessellated circle
// is an inscribed polygon, so a radius fitted on it is an approximation (good
// to about a hundredth on normal tessellations) and is flagged `approx`.
//
// Pure: no three.js, no DOM. Positions in, plain arrays out — so it is tested
// in node (tests/ui/measure.test.cjs), like anticipate.js.

export const SHARP_DEG = 28;      // dihedral angle above which an edge is a real edge
export const CORNER_DEG = 20;     // turn along a chain above which a vertex is a corner
export const FACE_DEG = 1;        // normals within this are one planar face
export const MAX_SHARP = 20000;   // above: a ridged/dense piece (a thread) — no edge snap

// ── small vector helpers on [x, y, z] ──
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = a => Math.hypot(a[0], a[1], a[2]);
const norm = a => { const l = len(a) || 1; return [a[0] / l, a[1] / l, a[2] / l]; };
const dist = (a, b) => len(sub(a, b));
export const vec = { sub, add, mul, dot, cross, len, norm, dist };

// ── 3×3 symmetric eigen-decomposition (Jacobi) ──
// Returns eigenvalues ascending with their unit eigenvectors.
export function eigSym3(m) {
  const a = [[m[0][0], m[0][1], m[0][2]], [m[1][0], m[1][1], m[1][2]], [m[2][0], m[2][1], m[2][2]]];
  const v = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  for (let sweep = 0; sweep < 50; sweep++) {
    const off = Math.abs(a[0][1]) + Math.abs(a[0][2]) + Math.abs(a[1][2]);
    if (off < 1e-15 * (Math.abs(a[0][0]) + Math.abs(a[1][1]) + Math.abs(a[2][2]) + 1e-300)) break;
    for (const [p, q] of [[0, 1], [0, 2], [1, 2]]) {
      if (Math.abs(a[p][q]) < 1e-300) continue;
      const th = (a[q][q] - a[p][p]) / (2 * a[p][q]);
      const t = Math.sign(th || 1) / (Math.abs(th) + Math.sqrt(th * th + 1));
      const c = 1 / Math.sqrt(t * t + 1), s = t * c;
      for (let k = 0; k < 3; k++) {          // A ← Jᵀ A J
        const akp = a[k][p], akq = a[k][q];
        a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq;
      }
      for (let k = 0; k < 3; k++) {
        const apk = a[p][k], aqk = a[q][k];
        a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk;
      }
      for (let k = 0; k < 3; k++) {
        const vkp = v[k][p], vkq = v[k][q];
        v[k][p] = c * vkp - s * vkq; v[k][q] = s * vkp + c * vkq;
      }
    }
  }
  return [0, 1, 2].map(i => ({ value: a[i][i], vector: norm([v[0][i], v[1][i], v[2][i]]) }))
    .sort((x, y) => x.value - y.value);
}

// a basis (u, v) of the plane perpendicular to n
function planeBasis(n) {
  const ref = Math.abs(n[0]) < 0.9 ? [1, 0, 0] : [0, 1, 0];
  const u = norm(sub(ref, mul(n, dot(ref, n))));
  return [u, cross(n, u)];
}

// ── circle fits ──
// 2D: Kåsa (algebraic, linear) for a start, then Gauss–Newton on the true
// geometric distance — Kåsa alone is biased short on an arc.
export function fitCircle2D(xy) {
  const n = xy.length;
  if (n < 3) return null;
  let mx = 0, my = 0;
  for (const [x, y] of xy) { mx += x; my += y; }
  mx /= n; my /= n;
  let suu = 0, suv = 0, svv = 0, suuu = 0, svvv = 0, suvv = 0, svuu = 0;
  for (const [x, y] of xy) {
    const u = x - mx, v = y - my;
    suu += u * u; suv += u * v; svv += v * v;
    suuu += u * u * u; svvv += v * v * v; suvv += u * v * v; svuu += v * u * u;
  }
  const det = suu * svv - suv * suv;
  if (Math.abs(det) < 1e-18) return null;      // collinear
  const b1 = 0.5 * (suuu + suvv), b2 = 0.5 * (svvv + svuu);
  let cx = (b1 * svv - b2 * suv) / det + mx, cy = (b2 * suu - b1 * suv) / det + my;
  let r = 0;
  for (const [x, y] of xy) r += Math.hypot(x - cx, y - cy);
  r /= n;
  for (let it = 0; it < 8; it++) {             // Gauss–Newton on (cx, cy, r)
    let JtJ = [[0, 0, 0], [0, 0, 0], [0, 0, 0]], Jtr = [0, 0, 0];
    for (const [x, y] of xy) {
      const dx = x - cx, dy = y - cy, d = Math.hypot(dx, dy) || 1e-12;
      const J = [-dx / d, -dy / d, -1], res = d - r;
      for (let i = 0; i < 3; i++) { Jtr[i] += J[i] * res; for (let j = 0; j < 3; j++) JtJ[i][j] += J[i] * J[j]; }
    }
    const s = solve3(JtJ, Jtr.map(x => -x));
    if (!s) break;
    cx += s[0]; cy += s[1]; r += s[2];
    if (Math.hypot(s[0], s[1], s[2]) < 1e-12 * (r + 1)) break;
  }
  let ss = 0;
  for (const [x, y] of xy) ss += (Math.hypot(x - cx, y - cy) - r) ** 2;
  return { cx, cy, r: Math.abs(r), rms: Math.sqrt(ss / n) };
}
function solve3(A, b) {
  const m = A.map((row, i) => [...row, b[i]]);
  for (let c = 0; c < 3; c++) {
    let p = c;
    for (let r = c + 1; r < 3; r++) if (Math.abs(m[r][c]) > Math.abs(m[p][c])) p = r;
    if (Math.abs(m[p][c]) < 1e-300) return null;
    [m[c], m[p]] = [m[p], m[c]];
    for (let r = 0; r < 3; r++) {
      if (r === c) continue;
      const f = m[r][c] / m[c][c];
      for (let k = c; k < 4; k++) m[r][k] -= f * m[c][k];
    }
  }
  return [m[0][3] / m[0][0], m[1][3] / m[1][1], m[2][3] / m[2][2]];
}
// 3D: the plane by PCA (normal = least-variance direction), then the 2D fit in
// it. `planeRms` says how planar the points are; `sweep` how much of the turn
// they cover (360 for a closed rim, ~90 for a quarter round).
export function fitCircle3D(pts) {
  if (pts.length < 3) return null;
  const c = [0, 0, 0];
  for (const p of pts) { c[0] += p[0]; c[1] += p[1]; c[2] += p[2]; }
  const m = mul(c, 1 / pts.length);
  const C = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  for (const p of pts) { const d = sub(p, m); for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) C[i][j] += d[i] * d[j]; }
  const eg = eigSym3(C), axis = eg[0].vector;
  const planeRms = Math.sqrt(Math.max(eg[0].value, 0) / pts.length);
  const [u, v] = planeBasis(axis);
  const xy = pts.map(p => { const d = sub(p, m); return [dot(d, u), dot(d, v)]; });
  const f = fitCircle2D(xy);
  if (!f) return null;
  const center = add(m, add(mul(u, f.cx), mul(v, f.cy)));
  const ang = xy.map(([x, y]) => Math.atan2(y - f.cy, x - f.cx)).sort((a, b) => a - b);
  let gap = ang[0] + 2 * Math.PI - ang[ang.length - 1];
  for (let i = 1; i < ang.length; i++) gap = Math.max(gap, ang[i] - ang[i - 1]);
  return { center, axis, r: f.r, rms: f.rms, planeRms, sweep: (2 * Math.PI - gap) * 180 / Math.PI };
}

// ── topology: weld, triangles, edges ──
export function buildTopology(pos, index = null, { weldTol } = {}) {
  const nV = pos.length / 3, nT = index ? index.length / 3 : nV / 3;
  let lo = [Infinity, Infinity, Infinity], hi = [-Infinity, -Infinity, -Infinity];
  for (let i = 0; i < nV; i++) for (let k = 0; k < 3; k++) {
    const x = pos[3 * i + k]; if (x < lo[k]) lo[k] = x; if (x > hi[k]) hi[k] = x;
  }
  const diag = nV ? Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) : 0;
  const q = weldTol || Math.max(diag * 1e-6, 1e-9);
  const keyOf = new Map(), remap = new Int32Array(nV), verts = [];
  for (let i = 0; i < nV; i++) {
    const x = pos[3 * i], y = pos[3 * i + 1], z = pos[3 * i + 2];
    const k = `${Math.round(x / q)},${Math.round(y / q)},${Math.round(z / q)}`;
    let id = keyOf.get(k);
    if (id === undefined) { id = verts.length / 3; keyOf.set(k, id); verts.push(x, y, z); }
    remap[i] = id;
  }
  const V = Float64Array.from(verts), nW = V.length / 3;
  const tris = new Int32Array(nT * 3), N = new Float64Array(nT * 3), area = new Float64Array(nT);
  const P = i => [V[3 * i], V[3 * i + 1], V[3 * i + 2]];
  for (let t = 0; t < nT; t++) {
    for (let k = 0; k < 3; k++) tris[3 * t + k] = remap[index ? index[3 * t + k] : 3 * t + k];
    const a = P(tris[3 * t]), b = P(tris[3 * t + 1]), c = P(tris[3 * t + 2]);
    const n = cross(sub(b, a), sub(c, a)), l = len(n);
    area[t] = l / 2;
    if (l > 0) { N[3 * t] = n[0] / l; N[3 * t + 1] = n[1] / l; N[3 * t + 2] = n[2] / l; }
  }
  // edges, keyed by the welded vertex pair
  const ek = new Map(), eA = [], eB = [], eT = [];      // eT: list of triangles per edge
  const triEdges = new Int32Array(nT * 3);
  for (let t = 0; t < nT; t++) {
    if (!(area[t] > 0)) { triEdges[3 * t] = triEdges[3 * t + 1] = triEdges[3 * t + 2] = -1; continue; }
    for (let k = 0; k < 3; k++) {
      let a = tris[3 * t + k], b = tris[3 * t + (k + 1) % 3];
      if (a > b) [a, b] = [b, a];
      const key = a * nW + b;
      let e = ek.get(key);
      if (e === undefined) { e = eA.length; ek.set(key, e); eA.push(a); eB.push(b); eT.push([]); }
      eT[e].push(t);
      triEdges[3 * t + k] = e;
    }
  }
  return { V, nV: nW, tris, N, area, nT, eA: Int32Array.from(eA), eB: Int32Array.from(eB), eT, triEdges, diag,
           point: P, normal: t => [N[3 * t], N[3 * t + 1], N[3 * t + 2]] };
}

export function sharpEdges(topo, deg = SHARP_DEG) {
  const cos = Math.cos(deg * Math.PI / 180), n = topo.eA.length, sharp = new Uint8Array(n);
  for (let e = 0; e < n; e++) {
    const ts = topo.eT[e];
    if (ts.length !== 2) { sharp[e] = 1; continue; }         // a border, or non-manifold
    if (dot(topo.normal(ts[0]), topo.normal(ts[1])) < cos) sharp[e] = 1;
  }
  return sharp;
}

// ── chains of sharp edges ──
export function chainsOf(topo, sharp, { cornerDeg = CORNER_DEG } = {}) {
  const adj = new Map();                       // vertex → [edge]
  for (let e = 0; e < sharp.length; e++) if (sharp[e]) {
    for (const v of [topo.eA[e], topo.eB[e]]) { if (!adj.has(v)) adj.set(v, []); adj.get(v).push(e); }
  }
  const other = (e, v) => (topo.eA[e] === v ? topo.eB[e] : topo.eA[e]);
  const cosC = Math.cos(cornerDeg * Math.PI / 180);
  const corner = new Set();
  for (const [v, es] of adj) {
    if (es.length !== 2) { corner.add(v); continue; }
    const p = topo.point(v), d1 = norm(sub(p, topo.point(other(es[0], v)))), d2 = norm(sub(topo.point(other(es[1], v)), p));
    if (dot(d1, d2) < cosC) corner.add(v);
  }
  const used = new Uint8Array(sharp.length), chains = [], edgeChain = new Int32Array(sharp.length).fill(-1);
  const walk = (v0, e0) => {
    const vs = [v0], es = [];
    let v = v0, e = e0;
    while (e !== undefined && !used[e]) {
      used[e] = 1; es.push(e);
      v = other(e, v); vs.push(v);
      if (corner.has(v) || v === v0) break;
      e = adj.get(v).find(x => !used[x]);
    }
    return { verts: vs, edges: es, closed: vs.length > 2 && vs[0] === vs[vs.length - 1] };
  };
  for (const v of corner) for (const e of adj.get(v)) if (!used[e]) chains.push(walk(v, e));
  for (const [v, es] of adj) for (const e of es) if (!used[e]) chains.push(walk(v, e));   // loops with no corner
  chains.forEach((c, i) => { for (const e of c.edges) edgeChain[e] = i; classify(topo, c); });
  return { chains, edgeChain, corners: [...corner] };
}
// line / circle / arc / curve, with what a dimension needs of each
function classify(topo, c) {
  const pts = c.verts.map(v => topo.point(v));
  if (c.closed) pts.pop();
  let L = 0;
  for (let i = 1; i < c.verts.length; i++) L += dist(topo.point(c.verts[i - 1]), topo.point(c.verts[i]));
  c.length = L;
  const a = pts[0], b = pts[pts.length - 1], ab = sub(b, a), lab = len(ab);
  if (!c.closed && lab > 0) {
    const u = mul(ab, 1 / lab);
    let dev = 0;
    for (const p of pts) { const d = sub(p, a); dev = Math.max(dev, len(sub(d, mul(u, dot(d, u))))); }
    if (dev <= Math.max(1e-6 * (topo.diag || 1), 2e-3 * lab)) {
      Object.assign(c, { type: 'line', a, b, length: lab });
      return;
    }
  }
  if (pts.length >= (c.closed ? 6 : 5)) {
    const f = fitCircle3D(pts);
    if (f && f.r > 0 && f.planeRms < 0.01 * f.r + 1e-9 && f.rms < 0.02 * f.r && (c.closed || f.sweep >= 45)) {
      Object.assign(c, { type: c.closed || f.sweep > 330 ? 'circle' : 'arc', circle: f });
      return;
    }
  }
  c.type = 'curve';
}

// ── faces ──
// The planar face a triangle belongs to: neighbours with the same normal AND on
// the same plane (two parallel faces joined by a fillet must not merge).
export function planarPatch(topo, seed, { deg = FACE_DEG, cap = 200000 } = {}) {
  const n0 = topo.normal(seed), cos = Math.cos(deg * Math.PI / 180);
  const p0 = topo.point(topo.tris[3 * seed]), tol = Math.max(1e-6 * (topo.diag || 1), 1e-9) * 50;
  const seen = new Set([seed]), out = [seed], stack = [seed];
  let A = 0, cen = [0, 0, 0];
  while (stack.length && out.length < cap) {
    const t = stack.pop();
    const ct = mul(add(add(topo.point(topo.tris[3 * t]), topo.point(topo.tris[3 * t + 1])), topo.point(topo.tris[3 * t + 2])), 1 / 3);
    A += topo.area[t]; cen = add(cen, mul(ct, topo.area[t]));
    for (let k = 0; k < 3; k++) {
      const e = topo.triEdges[3 * t + k]; if (e < 0) continue;
      for (const u of topo.eT[e]) {
        if (seen.has(u) || !(topo.area[u] > 0)) continue;
        if (dot(topo.normal(u), n0) < cos) continue;
        const q = topo.point(topo.tris[3 * u]);
        if (Math.abs(dot(sub(q, p0), n0)) > tol + 1e-4 * Math.max(dist(q, p0), 1)) continue;
        seen.add(u); out.push(u); stack.push(u);
      }
    }
  }
  return { tris: out, normal: n0, point: A > 0 ? mul(cen, 1 / A) : p0, area: A };
}
// A smooth patch: everything reachable from `seed` without crossing a sharp edge.
export function smoothPatch(topo, seed, sharp, cap = 60000) {
  const seen = new Set([seed]), out = [seed], stack = [seed];
  while (stack.length && out.length < cap) {
    const t = stack.pop();
    for (let k = 0; k < 3; k++) {
      const e = topo.triEdges[3 * t + k]; if (e < 0 || sharp[e]) continue;
      for (const u of topo.eT[e]) if (!seen.has(u) && topo.area[u] > 0) { seen.add(u); out.push(u); stack.push(u); }
    }
  }
  return out;
}
// A cylinder through a smooth patch: the normals all lie in the plane ⟂ axis,
// so the axis is the direction of LEAST normal variance; the radius is a circle
// fit of the points projected along it. `hole` = the normals point at the axis.
export function fitCylinder(topo, tris) {
  if (tris.length < 4) return null;
  const C = [[0, 0, 0], [0, 0, 0], [0, 0, 0]];
  let W = 0;
  for (const t of tris) {
    const n = topo.normal(t), w = topo.area[t];
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) C[i][j] += w * n[i] * n[j];
    W += w;
  }
  if (!(W > 0)) return null;
  const eg = eigSym3(C);
  // along the axis: no normal at all; round it: the normals sweep a real angle
  if (eg[0].value > 0.02 * W || eg[1].value < 0.05 * W) return null;
  const axis = eg[0].vector, [u, v] = planeBasis(axis);
  const vs = new Set();
  for (const t of tris) for (let k = 0; k < 3; k++) vs.add(topo.tris[3 * t + k]);
  const pts = [...vs].map(i => topo.point(i));
  const xy = pts.map(p => [dot(p, u), dot(p, v)]);
  const f = fitCircle2D(xy);
  if (!f || f.rms > 0.02 * f.r) return null;
  let h0 = Infinity, h1 = -Infinity;
  for (const p of pts) { const h = dot(p, axis); h0 = Math.min(h0, h); h1 = Math.max(h1, h); }
  const base = add(mul(u, f.cx), mul(v, f.cy));
  const center = add(base, mul(axis, (h0 + h1) / 2));
  let inward = 0;
  for (const t of tris) {
    const ct = topo.point(topo.tris[3 * t]), r = sub(ct, add(base, mul(axis, dot(ct, axis))));
    inward += topo.area[t] * Math.sign(-dot(topo.normal(t), r));
  }
  return { center, axis, r: f.r, rms: f.rms, length: h1 - h0, hole: inward > 0 };
}

// ── spatial grids, so a hover never loops over every edge ──
function gridOf(cell) { return { cell, m: new Map() }; }
const cellKey = (i, j, k) => `${i},${j},${k}`;
function gridAddBox(g, lo, hi, id) {
  const c = g.cell;
  for (let i = Math.floor(lo[0] / c); i <= Math.floor(hi[0] / c); i++)
    for (let j = Math.floor(lo[1] / c); j <= Math.floor(hi[1] / c); j++)
      for (let k = Math.floor(lo[2] / c); k <= Math.floor(hi[2] / c); k++) {
        const key = cellKey(i, j, k); let l = g.m.get(key);
        if (!l) { l = []; g.m.set(key, l); } l.push(id);
      }
}
function gridQuery(g, p, r) {
  const c = g.cell, out = new Set();
  for (let i = Math.floor((p[0] - r) / c); i <= Math.floor((p[0] + r) / c); i++)
    for (let j = Math.floor((p[1] - r) / c); j <= Math.floor((p[1] + r) / c); j++)
      for (let k = Math.floor((p[2] - r) / c); k <= Math.floor((p[2] + r) / c); k++) {
        const l = g.m.get(cellKey(i, j, k)); if (l) for (const x of l) out.add(x);
      }
  return out;
}
export function closestOnSegment(p, a, b) {
  const ab = sub(b, a), l2 = dot(ab, ab);
  const t = l2 > 0 ? Math.min(1, Math.max(0, dot(sub(p, a), ab) / l2)) : 0;
  return add(a, mul(ab, t));
}

// Everything a piece offers to snap to, built once (lazily, on first hover).
export function buildFeatures(pos, index = null, { maxSharp = MAX_SHARP } = {}) {
  const topo = buildTopology(pos, index);
  const sharp = sharpEdges(topo);
  let nSharp = 0; for (const s of sharp) nSharp += s;
  const F = { topo, sharp, nSharp, dense: nSharp > maxSharp, chains: [], edgeChain: null, corners: [] };
  if (F.dense) return F;                       // a thread: faces and free points only
  Object.assign(F, chainsOf(topo, sharp));
  // grid cell ≈ the mean sharp edge length, kept within sane bounds
  let L = 0;
  for (let e = 0; e < sharp.length; e++) if (sharp[e]) L += dist(topo.point(topo.eA[e]), topo.point(topo.eB[e]));
  const cell = Math.max(nSharp ? L / nSharp : topo.diag / 50, topo.diag / 400, 1e-6);
  F.egrid = gridOf(cell); F.cgrid = gridOf(cell);
  for (let e = 0; e < sharp.length; e++) if (sharp[e]) {
    const a = topo.point(topo.eA[e]), b = topo.point(topo.eB[e]);
    gridAddBox(F.egrid, [0, 1, 2].map(k => Math.min(a[k], b[k])), [0, 1, 2].map(k => Math.max(a[k], b[k])), e);
  }
  for (const v of F.corners) { const p = topo.point(v); gridAddBox(F.cgrid, p, p, v); }
  return F;
}
export function nearestCorner(F, p, r) {
  if (!F.cgrid) return null;
  let best = null;
  for (const v of gridQuery(F.cgrid, p, r)) {
    const q = F.topo.point(v), d = dist(p, q);
    if (d <= r && (!best || d < best.d)) best = { v, point: q, d };
  }
  return best;
}
export function nearestEdge(F, p, r, filter = null) {
  if (!F.egrid) return null;
  let best = null;
  for (const e of gridQuery(F.egrid, p, r)) {
    if (filter && !filter(F.chains[F.edgeChain[e]])) continue;
    const q = closestOnSegment(p, F.topo.point(F.topo.eA[e]), F.topo.point(F.topo.eB[e])), d = dist(p, q);
    if (d <= r && (!best || d < best.d)) best = { e, point: q, d, chain: F.chains[F.edgeChain[e]] };
  }
  return best;
}

// How many sharp edges pass within r of p — a thread or a knurl packs dozens
// into a fingertip, a real edge or corner of a part a handful.
export function edgesNear(F, p, r) {
  if (!F.egrid) return 0;
  let n = 0;
  for (const e of gridQuery(F.egrid, p, r)) {
    const q = closestOnSegment(p, F.topo.point(F.topo.eA[e]), F.topo.point(F.topo.eB[e]));
    if (dist(p, q) <= r) n++;
  }
  return n;
}
export const RIDGED_EDGES = 10;   // sharp edges within 2·rE above which a spot is ridged
const isRound = c => c && (c.type === 'circle' || c.type === 'arc');

// What is under the pointer, most specific first: a vertex, the centre of a
// circle whose rim is near, an edge, a planar face, a cylinder (Ø of a hole
// tapped from inside), a free point. `rV` / `rE` are the vertex and edge snap
// radii in the piece's own units (px × mm-per-px, worked out by the caller).
// `mode` narrows it: p2p = free points only; edge / hole / faces = only that.
export function pickFeature(F, p, tri, { rV, rE, mode = 'auto' }) {
  const free = { snap: 'free', point: p };
  if (mode === 'p2p') return free;
  const topo = F.topo;
  const face = () => {
    if (tri == null || tri < 0 || tri >= topo.nT) return null;
    const pp = planarPatch(topo, tri);
    // a curved surface floods to a lone sliver; a real planar face has room
    const planar = pp.tris.length >= 2 || topo.triEdges.slice(3 * tri, 3 * tri + 3).every(e => e >= 0 && F.sharp[e]);
    if (!planar) return null;
    const n = pp.normal, foot = sub(p, mul(n, dot(sub(p, pp.point), n)));
    return { snap: 'face', point: foot, plane: { point: pp.point, normal: n }, tris: pp.tris };
  };
  const cyl = () => {
    if (tri == null || tri < 0 || tri >= topo.nT) return null;
    const ts = smoothPatch(topo, tri, F.sharp);
    const c = fitCylinder(topo, ts);
    return c ? { snap: 'circle_center', point: c.center, circle: { center: c.center, axis: c.axis, r: c.r },
                 cylinder: c, tris: ts, approx: true } : null;
  };
  if (mode === 'faces') return face();
  if (!F.dense) {
    // on a ridged spot (a thread) every crest is a "vertex" and an "edge":
    // snapping there would jump between them at random, so only circles count
    const ridged = edgesNear(F, p, 2 * rE) > RIDGED_EDGES;
    const ce = (mode === 'auto' && !ridged) ? nearestCorner(F, p, rV) : null;
    if (ce) return { snap: 'vertex', point: ce.point, vertex: ce.v, ridged };
    const rc = mode !== 'edge' ? nearestEdge(F, p, mode === 'hole' ? rE * 2 : rE * 1.5, isRound) : null;
    if (rc) {
      const c = rc.chain.circle;
      return { snap: 'circle_center', point: c.center, circle: { center: c.center, axis: c.axis, r: c.r },
               chain: rc.chain, approx: true };
    }
    const ed = (mode !== 'hole' && !ridged) ? nearestEdge(F, p, rE) : null;
    if (ed) return { snap: 'edge', point: ed.point, chain: ed.chain, approx: ed.chain && ed.chain.type !== 'line' };
  }
  if (mode === 'edge') return null;
  if (mode === 'hole') return cyl();
  return face() || free;
}

// The same feature twice → it is measured on its own (an edge's length, a
// circle's Ø) rather than as the zero distance to itself.
export function sameFeature(a, b) {
  if (!a || !b) return false;
  if (a.chain && a.chain === b.chain) return true;
  if (a.cylinder && b.cylinder) return dist(a.cylinder.center, b.cylinder.center) < 1e-6 && Math.abs(a.cylinder.r - b.cylinder.r) < 1e-6;
  return false;
}

// One feature alone: a circle → Ø (an arc that is not most of a turn → R),
// a straight edge → its length, any other chain → its length along the curve.
export function measureOne(f) {
  if (!f) return null;
  if (f.circle) {
    const arc = f.chain && f.chain.type === 'arc';
    const c = f.circle;
    return { kind: arc ? 'radius' : 'diameter', value: arc ? c.r : 2 * c.r, approx: true,
             circle: { center: c.center, axis: c.axis, r: c.r } };
  }
  if (f.chain && f.chain.type === 'line')
    return { kind: 'edge', a: f.chain.a, b: f.chain.b, value: f.chain.length, approx: false };
  if (f.chain) {
    const vs = f.chain.verts;
    return { kind: 'edge', a: null, b: null, value: f.chain.length, approx: true, chain: f.chain,
             ends: [vs[0], vs[vs.length - 1]] };
  }
  return null;
}

// Two picks → the dimension Auto means. Parallel planar faces: the thickness,
// perpendicular to them. A face and anything else: the distance from that
// point to the plane. Otherwise the straight distance between the snapped
// points (vertex–vertex, centre–centre = the interaxis…).
export function measureTwo(a, b, { parallelDeg = FACE_DEG } = {}) {
  const approx = !!(a.approx || b.approx);
  if (a.plane && b.plane) {
    const c = Math.abs(dot(a.plane.normal, b.plane.normal));
    if (c > Math.cos(parallelDeg * Math.PI / 180)) {
      const n = b.plane.normal, A = a.point;
      const B = sub(A, mul(n, dot(sub(A, b.plane.point), n)));
      return { kind: 'face_gap', a: A, b: B, value: dist(A, B), approx: false, n: a.plane.normal };
    }
    return { kind: 'distance', a: a.point, b: b.point, value: dist(a.point, b.point), approx, nonParallel: true };
  }
  const f = a.plane ? a : b.plane ? b : null;
  if (f) {
    const P = (f === a ? b : a).point, n = f.plane.normal;
    const foot = sub(P, mul(n, dot(sub(P, f.plane.point), n)));
    const [A, B] = f === a ? [foot, P] : [P, foot];
    return { kind: 'distance', a: A, b: B, value: dist(A, B), approx, toPlane: true };
  }
  return { kind: 'distance', a: a.point, b: b.point, value: dist(a.point, b.point), approx };
}
