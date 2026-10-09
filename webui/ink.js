// ✎ / ✎³ — how a stroke LOOKS. Pure geometry (no three, no DOM), so the
// builders run under node: tests/ui/ink.test.cjs. The materials that read these
// arrays live in webui/view-ink.js.
//
// The two pens draw the same data (surface points + normals + lifts) two ways:
//
//  - ✎ SPRAY: a flat band ON the surface, one quad per segment, each quad a
//    CAPSULE in its own coordinates — `ink` = (x along in radii from the
//    segment's start, y across in radii, L = the segment's length in radii,
//    soft). The fragment shader takes the distance to the segment (x clamped to
//    0…L) and fades it out, so every segment ends in a round soft cap and the
//    joints are capsules overlapping. That overlap is what would show as a
//    darker seam at every sample, and the reason it does not is in view-ink.js
//    (one fragment per pixel per colour run wins, by depth).
//  - ✎³ FILAMENT: a real tube, one ring per sample framed by the SURFACE normal
//    (so it never twists), hemispherical ends, smooth normals, and a per-vertex
//    shade: the underside darkened where it rests on the part or on the layer
//    below (a contact shadow), and alternate layers a shade apart, so a heap
//    reads as stacked filament and not as one blob.

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mul = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const len = a => Math.hypot(a[0], a[1], a[2]);
const norm = (a, fb = [0, 0, 1]) => { const l = len(a); return l > 1e-12 ? mul(a, 1 / l) : fb.slice(); };
// any unit vector perpendicular to n
const perp = n => norm(Math.abs(n[0]) < 0.9 ? cross(n, [1, 0, 0]) : cross(n, [0, 1, 0]));

export const SPRAY = {
  lift: 0.06,       // × r above the surface (plus the depth pull, view-ink.js)
  spread: 1.3,      // the mist reaches 1.3 r: solid core ~0.5 r … faded out at 1.3 r
  soft: 0.62,       // the outer 62% of the band fades, like a spray can's mist
  softThin: 0.3,    // painted letters (T vernice): crisper, or the words go to mush
  grain: 1,         // speckle in the fading band (0 = none)
  grainThin: 0.35,
};
export const FILAMENT = {
  radial: 16,       // ring segments: round at any zoom a person draws at
  capRings: 5,      // rings in each hemispherical end
  sit: 0.82,        // the tube's centre sits 0.82 r above the part (a filament squashes a little)
  contact: 0.5,     // how dark the underside gets where it rests on something
  sub: 3,           // centre-line pieces per sample step (Catmull-Rom): a heap of small
                    // circles is drawn with few samples per loop, and a filament has no corners
  layer: 0.7,       // one layer of the 3D pen = 0.7 × width (webui/pen3d.js PEN3D.layer)
  layerTone: 0.16,  // alternate layers ±8%
};

// s = { pts: [[x,y,z]…], nrm: [[x,y,z]…], width, label? } → arrays for one
// BufferGeometry: position (3), normal (3: the SURFACE normal, for shading),
// ink (4: x, y, L, soft), grain (1), index.
export function sprayArrays(s, o = SPRAY) {
  const r0 = s.width / 2, thin = s.label != null;
  const r = r0 * (thin ? 1.1 : o.spread);    // the band's half width (ink units are radii of THIS)
  const soft = thin ? o.softThin : o.soft, grain = thin ? o.grainThin : o.grain;
  const P = s.pts.map((p, i) => add(p, mul(norm(s.nrm[i] || [0, 0, 1]), r0 * o.lift)));
  const N = s.pts.map((_, i) => norm(s.nrm[i] || [0, 0, 1]));
  const pos = [], nrm = [], ink = [], gr = [], idx = [];
  const quad = (a, b, na, nb) => {
    let t = sub(b, a); const L = len(t);
    const nm = norm(add(na, nb), na);
    t = L > 1e-9 ? mul(t, 1 / L) : perp(nm);
    let side = cross(t, nm);
    side = len(side) > 1e-9 ? norm(side) : perp(t);
    const Lr = L / r, base = pos.length / 3;
    // corners: start − r·t ± r·side, end + r·t ± r·side
    const c = [[a, na, -1, -1], [a, na, -1, 1], [b, nb, Lr + 1, -1], [b, nb, Lr + 1, 1]];
    for (const [p, n, x, y] of c) {
      const along = x < 0 ? -r : (x > Lr ? r : 0);
      const q = add(add(p, mul(t, along)), mul(side, y * r));
      pos.push(q[0], q[1], q[2]); nrm.push(n[0], n[1], n[2]);
      ink.push(x, y, Lr, soft); gr.push(grain);
    }
    idx.push(base, base + 2, base + 1, base + 1, base + 2, base + 3);
  };
  if (P.length === 1) quad(P[0], P[0], N[0], N[0]);
  for (let i = 1; i < P.length; i++) quad(P[i - 1], P[i], N[i - 1], N[i]);
  return { position: new Float32Array(pos), normal: new Float32Array(nrm), ink: new Float32Array(ink),
           grain: new Float32Array(gr), index: idx, r: r0 };
}

// s = { pts, nrm, width, lift: [mm…] } → { position, normal, shade, index }:
// a closed tube (rings + hemispherical caps), `shade` = per-vertex brightness.
export function filamentArrays(s, o = FILAMENT) {
  const r = s.width / 2, R = o.radial, lift = s.lift || [];
  const N0 = s.pts.map((_, i) => norm(s.nrm[i] || [0, 0, 1]));
  const C0 = s.pts.map((p, i) => add(p, mul(N0[i], r * o.sit + (lift[i] || 0))));
  // smooth centre line: Catmull-Rom through the samples, normals and lifts lerped
  const C = [], N = [], LI = [];
  const K0 = C0.length > 2 ? Math.max(1, o.sub | 0) : 1;
  for (let i = 0; i < C0.length; i++) {
    if (i === C0.length - 1 || K0 === 1) { C.push(C0[i]); N.push(N0[i]); LI.push(lift[i] || 0); if (i === C0.length - 1) break; if (K0 === 1) continue; }
    const p0 = C0[Math.max(0, i - 1)], p1 = C0[i], p2 = C0[i + 1], p3 = C0[Math.min(C0.length - 1, i + 2)];
    for (let q = 0; q < K0; q++) {
      const t = q / K0, t2 = t * t, t3 = t2 * t;
      const c = [0, 1, 2].map(k => 0.5 * (2 * p1[k] + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
        + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3));
      C.push(c); N.push(norm(add(mul(N0[i], 1 - t), mul(N0[i + 1], t)), N0[i]));
      LI.push((lift[i] || 0) * (1 - t) + (lift[i + 1] || 0) * t);
    }
  }
  const n = C.length;
  // tangent per ring (central difference), and the frame from the surface normal
  const T = C.map((_, i) => {
    const a = C[Math.max(0, i - 1)], b = C[Math.min(n - 1, i + 1)];
    return norm(sub(b, a), perp(N[i]));
  });
  if (n > 1) for (let i = 0; i < n; i++) if (len(sub(C[Math.min(n - 1, i + 1)], C[Math.max(0, i - 1)])) < 1e-9)
    T[i] = T[i > 0 ? i - 1 : Math.min(i + 1, n - 1)];
  const pos = [], nrm = [], shade = [], idx = [];
  const tone = i => {
    const L = Math.round(LI[i] / (o.layer * s.width));
    return 1 + (L % 2 ? -o.layerTone / 2 : o.layerTone / 2) * (L > 0 ? 1 : 0);
  };
  // one ring: centre c, frame (u up = surface normal ⟂ t, v = t × u), radius k·r,
  // pushed `dt` along t; the ring's normals blend toward ±t on the caps
  const ring = (c, t, nn, k, dt, tw, tn) => {
    let u = sub(nn, mul(t, dot(nn, t))); u = norm(u, perp(t));
    const v = cross(t, u);
    for (let j = 0; j < R; j++) {
      const a = (j / R) * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a);
      const d = add(mul(u, ca), mul(v, sa));
      const p = add(add(c, mul(t, dt)), mul(d, r * k));
      const nv = norm(add(mul(d, k), mul(t, tw)));
      pos.push(p[0], p[1], p[2]); nrm.push(nv[0], nv[1], nv[2]);
      // a baked occlusion round the section: full tone on top (d·u = 1), the
      // flanks a step darker, the underside — where it rests on the part or on
      // the layer below — in the contact shadow. The scene's lights then add
      // the real shading and the highlight on top of it.
      shade.push(tn * (1 - o.contact * Math.pow((1 - ca) / 2, 1.2)));
    }
  };
  const rings = [];                              // [first vertex of ring]
  const K = o.capRings;
  const capStart = () => {                       // pole … equator, before ring 0
    for (let q = 0; q < K; q++) {
      const phi = (Math.PI / 2) * (1 - q / K);   // 90° (pole) → just before the equator
      rings.push(pos.length / 3);
      ring(C[0], T[0], N[0], Math.max(Math.cos(phi), 0.02), -r * Math.sin(phi), -Math.sin(phi), tone(0));
    }
  };
  const capEnd = () => {
    for (let q = 1; q <= K; q++) {
      const phi = (Math.PI / 2) * (q / K);
      rings.push(pos.length / 3);
      ring(C[n - 1], T[n - 1], N[n - 1], Math.max(Math.cos(phi), 0.02), r * Math.sin(phi), Math.sin(phi), tone(n - 1));
    }
  };
  capStart();
  for (let i = 0; i < n; i++) { rings.push(pos.length / 3); ring(C[i], T[i], N[i], 1, 0, 0, tone(i)); }
  capEnd();
  for (let q = 1; q < rings.length; q++) {
    const a = rings[q - 1], b = rings[q];
    for (let j = 0; j < R; j++) {
      const j1 = (j + 1) % R;
      idx.push(a + j, a + j1, b + j, a + j1, b + j1, b + j);   // outward (CCW seen from outside)
    }
  }
  return { position: new Float32Array(pos), normal: new Float32Array(nrm), shade: new Float32Array(shade), index: idx, r };
}

// Colour runs: consecutive spray strokes of one colour are ONE coat (they merge
// into a single even layer); a stroke of another colour starts the next run,
// which is drawn over everything before it. `prev` is the last spray stroke
// before s (or null).
export function nextRun(prev, color) {
  if (!prev) return 0;
  return prev.color === color ? prev.run : prev.run + 1;
}
