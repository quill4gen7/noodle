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
// ALPHAS — the brush's tip, as in ZBrush: the same stroke stamped three ways,
// for both pens. `soft` (sfumato, the default — the spray that evens out),
// `normal` (a marker: solid core, a crisp antialiased edge), `star` (a row of
// little stars along the stroke on ✎; on ✎³ the tube is pushed through a star
// nozzle, like icing). Stored per stroke as `alpha`; absent (a note saved
// before alphas) = what that pen drew then: soft spray, round filament.
// `img` = a picture of the user's (webui/brush-img.js): stamped like the stars
// on ✎, a RELIEF on the tube of ✎³ (s.relief = its grey N×N).
export const ALPHAS = ['soft', 'normal', 'star'];
const KNOWN = [...ALPHAS, 'img'];
export const alphaOf = (s, dflt = 'soft') => (KNOWN.includes(s && s.alpha) ? s.alpha : dflt);
const SPRAY_ALPHA = {
  normal: { spread: 1.0, soft: 0.12, grain: 0 },
  star: { spread: 1.35, soft: 0.1, grain: 0 },   // a star's points reach the band's edge
  img: { spread: 1.0, soft: 0.1, grain: 0 },      // the stamp = the stroke's width
};
export const IMG = {
  spacing: 1.6,     // ✎ picture stamps, centre to centre in radii: just overlapping
  relief: 0.45,     // ✎³ picture relief: ±22% of the radius
  ring: 6,          // ✎³ with a relief: a ring at least every width/6
};
// a grey N×N (Uint8, 255 = high) sampled bilinear with mirrored repeat — the
// relief's u, v run past 1 along a stroke and must not show a seam
export function sampleMirror(g, N, u, v) {
  const m = t => { t = Math.abs(t) % 2; return t > 1 ? 2 - t : t; };
  const x = m(u) * (N - 1), y = m(v) * (N - 1);
  const x0 = Math.floor(x), y0 = Math.floor(y), x1 = Math.min(N - 1, x0 + 1), y1 = Math.min(N - 1, y0 + 1);
  const fx = x - x0, fy = y - y0, G = (i, j) => g[j * N + i] / 255;
  return (G(x0, y0) * (1 - fx) + G(x1, y0) * fx) * (1 - fy) + (G(x0, y1) * (1 - fx) + G(x1, y1) * fx) * fy;
}
export const STAR = {
  spacing: 2.3,     // centre to centre, in band radii (a star is ~2 radii across)
  points: 5,
  sharp: 2.6,       // ✎: iq's star `m` — 2 = needle points, 5 = a pentagon
  nozzle: 0.3,      // ✎³: how deep the star nozzle's grooves are (× r)
  taper: 2.5,       // ✎³ sfumato: each end tapers over 2.5 widths
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

// s = { pts: [[x,y,z]…], nrm: [[x,y,z]…], width, label?, alpha? } → arrays for
// one BufferGeometry: position (3), normal (3: the SURFACE normal, for shading),
// ink (4: x, y, L, soft), grain (1), stamp (2: the segment's start along the
// whole stroke in radii, 1 = stars), index.
export function sprayArrays(s, o = SPRAY) {
  const r0 = s.width / 2, thin = s.label != null, al = thin ? 'soft' : alphaOf(s);
  const A = SPRAY_ALPHA[al] || o;
  const r = r0 * (thin ? 1.1 : A.spread);    // the band's half width (ink units are radii of THIS)
  const soft = thin ? o.softThin : A.soft, grain = thin ? o.grainThin : A.grain;
  const star = al === 'star' ? 1 : al === 'img' ? 2 : 0, stamp = [];
  let s0 = 0;                                // arc length so far, in radii: the stars keep their pace across segments
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
      ink.push(x, y, Lr, soft); gr.push(grain); stamp.push(s0, star);
    }
    idx.push(base, base + 2, base + 1, base + 1, base + 2, base + 3);
    s0 += Lr;
  };
  if (P.length === 1) quad(P[0], P[0], N[0], N[0]);
  for (let i = 1; i < P.length; i++) quad(P[i - 1], P[i], N[i - 1], N[i]);
  return { position: new Float32Array(pos), normal: new Float32Array(nrm), ink: new Float32Array(ink),
           grain: new Float32Array(gr), stamp: new Float32Array(stamp), index: idx, r: r0 };
}

// s = { pts, nrm, width, lift: [mm…] } → { position, normal, shade, index }:
// a closed tube (rings + hemispherical caps), `shade` = per-vertex brightness.
// The tube's cross-section by alpha: `normal` round; `star` the star nozzle
// (STAR.points ridges); `soft` round too, but the stroke TAPERS to a point at
// both ends and the profile is a melted bead (wider than tall) — a soft stroke.
function profile(al, o) {
  if (al === 'star') {
    const P = STAR.points, g = STAR.nozzle;
    // ρ(a): 1 on a ridge, 1 − g between two — smooth enough for the light to roll
    return { R: P * 8, rho: a => 1 - g * Math.pow(Math.abs(Math.sin(a * P / 2)), 1.5), sx: 1, sy: 1 };
  }
  if (al === 'soft') return { R: o.radial, rho: () => 1, sx: 0.8, sy: 1.15 };   // x = up the normal, y = across
  if (al === 'img') return { R: 32, rho: () => 1, sx: 1, sy: 1 };              // round, the relief does the rest
  return { R: o.radial, rho: () => 1, sx: 1, sy: 1 };
}
export function filamentArrays(s, o = FILAMENT) {
  const al = alphaOf(s, 'normal'), pr = profile(al, o);
  const r = s.width / 2, R = pr.R, lift = s.lift || [];
  const N0 = s.pts.map((_, i) => norm(s.nrm[i] || [0, 0, 1]));
  const C0 = s.pts.map((p, i) => add(p, mul(N0[i], r * o.sit + (lift[i] || 0))));
  // smooth centre line: Catmull-Rom through the samples, normals and lifts lerped
  const C = [], N = [], LI = [];
  const relief = al === 'img' && s.relief ? s.relief : null;
  const kOf = i => {
    let k = C0.length > 2 ? Math.max(1, o.sub | 0) : 1;
    if (relief && i < C0.length - 1)            // a relief needs rings close enough to show it
      k = Math.max(k, Math.min(24, Math.ceil(len(sub(C0[i + 1], C0[i])) / (s.width / IMG.ring))));
    return k;
  };
  for (let i = 0; i < C0.length; i++) {
    const K0 = kOf(i);
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
  // ✎³ sfumato: the radius ramps up from each end (smoothstep over STAR.taper widths)
  const arc = [0];
  for (let i = 1; i < n; i++) arc.push(arc[i - 1] + len(sub(C[i], C[i - 1])));
  const tot = arc[n - 1], ramp = STAR.taper * s.width, tile = Math.PI * s.width;
  const taper = i => {
    if (al !== 'soft' || tot <= 0) return 1;
    const e = Math.min(arc[i], tot - arc[i]) / Math.min(ramp, tot / 2), t = Math.max(0, Math.min(1, e));
    return 0.25 + 0.75 * t * t * (3 - 2 * t);
  };
  // a thinner ring sits lower, so a tapered end still rests on the part
  const sink = i => sub(C[i], mul(N[i], r * o.sit * (1 - taper(i))));
  // tangent per ring (central difference), and the frame from the surface normal
  const T = C.map((_, i) => {
    const a = C[Math.max(0, i - 1)], b = C[Math.min(n - 1, i + 1)];
    return norm(sub(b, a), perp(N[i]));
  });
  if (n > 1) for (let i = 0; i < n; i++) if (len(sub(C[Math.min(n - 1, i + 1)], C[Math.max(0, i - 1)])) < 1e-9)
    T[i] = T[i > 0 ? i - 1 : Math.min(i + 1, n - 1)];
  const pos = [], nrm = [], shade = [], uv = [], idx = [];
  const tone = i => {
    const L = Math.round(LI[i] / (o.layer * s.width));
    return 1 + (L % 2 ? -o.layerTone / 2 : o.layerTone / 2) * (L > 0 ? 1 : 0);
  };
  // one ring: centre c, frame (u up = surface normal ⟂ t, v = t × u), radius k·r,
  // pushed `dt` along t; the ring's normals blend toward ±t on the caps.
  // R + 1 vertices: the last repeats the first with v = 1, so a texture wraps
  // round the tube without a seam running backwards across it. `ua` = the
  // ring's place along the stroke in TEXTURE units (one tile per circumference).
  const ring = (c, t, nn, k, dt, tw, tn, ua) => {
    let u = sub(nn, mul(t, dot(nn, t))); u = norm(u, perp(t));
    const v = cross(t, u);
    for (let j = 0; j <= R; j++) {
      const a = (j / R) * Math.PI * 2, ca = Math.cos(a), sa = Math.sin(a);
      // the section's outline: ρ(a)·(sx cos, sy sin) in the (u, v) frame, and
      // its 2D normal from the outline's tangent (rotated −90°)
      const h = 1e-3, q = b => [pr.rho(b) * pr.sx * Math.cos(b), pr.rho(b) * pr.sy * Math.sin(b)];
      const q0 = q(a), qa = q(a - h), qb = q(a + h);
      const tq = [qb[0] - qa[0], qb[1] - qa[1]], tl = Math.hypot(tq[0], tq[1]) || 1;
      const n2 = [tq[1] / tl, -tq[0] / tl];
      const d = add(mul(u, q0[0]), mul(v, q0[1]));
      const dn = add(mul(u, n2[0]), mul(v, n2[1]));
      // ✎³ picture alpha: the grey pushes the skin out (light) or in (dark)
      const rel = relief ? 1 + IMG.relief * (sampleMirror(relief.g, relief.n, ua, j / R) - 0.5) : 1;
      const p = add(add(c, mul(t, dt)), mul(d, r * k * rel));
      const nv = norm(add(mul(dn, k), mul(t, tw)));
      pos.push(p[0], p[1], p[2]); nrm.push(nv[0], nv[1], nv[2]);
      // a baked occlusion round the section: full tone on top (d·u = 1), the
      // flanks a step darker, the underside — where it rests on the part or on
      // the layer below — in the contact shadow. The scene's lights then add
      // the real shading and the highlight on top of it.
      shade.push(tn * (1 - o.contact * Math.pow((1 - ca) / 2, 1.2)));
      uv.push(ua, j / R);
    }
  };
  const rings = [];                              // [first vertex of ring]
  const K = o.capRings;
  const capStart = () => {                       // pole … equator, before ring 0
    for (let q = 0; q < K; q++) {
      const phi = (Math.PI / 2) * (1 - q / K);   // 90° (pole) → just before the equator
      rings.push(pos.length / 3);
      ring(sink(0), T[0], N[0], Math.max(Math.cos(phi), 0.02) * taper(0), -r * taper(0) * Math.sin(phi), -Math.sin(phi), tone(0),
        -r * Math.sin(phi) / tile);
    }
  };
  const capEnd = () => {
    for (let q = 1; q <= K; q++) {
      const phi = (Math.PI / 2) * (q / K);
      rings.push(pos.length / 3);
      ring(sink(n - 1), T[n - 1], N[n - 1], Math.max(Math.cos(phi), 0.02) * taper(n - 1), r * taper(n - 1) * Math.sin(phi), Math.sin(phi), tone(n - 1),
        (tot + r * Math.sin(phi)) / tile);
    }
  };
  capStart();
  for (let i = 0; i < n; i++) { rings.push(pos.length / 3); ring(sink(i), T[i], N[i], taper(i), 0, 0, tone(i), arc[i] / tile); }
  capEnd();
  for (let q = 1; q < rings.length; q++) {
    const a = rings[q - 1], b = rings[q];
    for (let j = 0; j < R; j++) {
      const j1 = j + 1;
      idx.push(a + j, a + j1, b + j, a + j1, b + j1, b + j);   // outward (CCW seen from outside)
    }
  }
  return { position: new Float32Array(pos), normal: new Float32Array(nrm), shade: new Float32Array(shade),
           uv: new Float32Array(uv), index: idx, r, stride: R + 1, relief: !!relief };
}

// Colour runs: consecutive spray strokes of one colour are ONE coat (they merge
// into a single even layer); a stroke of another colour starts the next run,
// which is drawn over everything before it. `prev` is the last spray stroke
// before s (or null).
export function nextRun(prev, color) {
  if (!prev) return 0;
  return prev.color === color ? prev.run : prev.run + 1;
}
