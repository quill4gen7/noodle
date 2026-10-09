// webui/ink.js — how ✎ (spray) and ✎³ (filament) strokes are built. Pure
// geometry, so it runs here on hand-made strokes on a flat part (z = 0, normal +z).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'ink.js')).href);
const line = (n, step = 1) => Array.from({ length: n }, (_, i) => [i * step, 0, 0]);
const up = n => Array.from({ length: n }, () => [0, 0, 1]);

test('spray: one capsule quad per segment, round soft ends, flat on the part', async () => {
  const I = await load();
  const A = I.sprayArrays({ pts: line(3), nrm: up(3), width: 2 });
  assert.equal(A.position.length / 3, 8);       // 2 segments × 4 corners
  assert.equal(A.index.length, 12);
  assert.equal(A.r, 1);
  // capsule coordinates: x from −1 (before the start) to L + 1, y = ±1
  const xs = [], ys = [];
  for (let i = 0; i < 8; i++) { xs.push(A.ink[4 * i]); ys.push(A.ink[4 * i + 1]); }
  assert.equal(Math.min(...xs), -1);
  const L = A.ink[2];                            // segment length in band radii
  assert.ok(Math.abs(L - 1 / (1 * I.SPRAY.spread)) < 1e-6);
  assert.ok(Math.abs(Math.max(...xs) - (L + 1)) < 1e-6);
  assert.deepEqual([...new Set(ys)].sort(), [-1, 1]);
  // a FLAT band, just above the part (no tube standing up)
  for (let i = 0; i < 8; i++) {
    const z = A.position[3 * i + 2];
    assert.ok(z > 0 && z < 0.2, `z=${z}`);
  }
  // the band is wider than the nominal stroke: the mist
  const w = Math.max(...Array.from({ length: 8 }, (_, i) => Math.abs(A.position[3 * i + 1])));
  assert.ok(Math.abs(w - I.SPRAY.spread) < 1e-6);
});

test('spray: a single tap is one quad; painted letters are crisper', async () => {
  const I = await load();
  const dot = I.sprayArrays({ pts: [[0, 0, 0]], nrm: up(1), width: 1 });
  assert.equal(dot.position.length / 3, 4);
  const paint = I.sprayArrays({ pts: line(2), nrm: up(2), width: 1, label: {} });
  const spray = I.sprayArrays({ pts: line(2), nrm: up(2), width: 1 });
  assert.ok(paint.ink[3] < spray.ink[3]);        // softness
  assert.ok(paint.grain[0] < spray.grain[0]);
});

test('spray: colour runs — one coat per colour, a new colour goes on top', async () => {
  const I = await load();
  const a = { color: '#f00', run: I.nextRun(null, '#f00') };
  const b = { color: '#f00', run: I.nextRun(a, '#f00') };
  const c = { color: '#00f', run: I.nextRun(b, '#00f') };
  const d = { color: '#f00', run: I.nextRun(c, '#f00') };
  assert.deepEqual([a.run, b.run, c.run, d.run], [0, 0, 1, 2]);
});

test('filament: a closed round tube, normals outward, shaded underneath', async () => {
  const I = await load();
  const F = I.FILAMENT;
  const A = I.filamentArrays({ pts: line(2, 4), nrm: up(2), width: 2 });
  const nv = A.position.length / 3;
  assert.equal(nv, (2 + 2 * F.capRings) * (F.radial + 1));   // 2 samples (no smoothing under 3); +1 = the seam vertex
  assert.equal(A.shade.length, nv);
  const zc = 1 * F.sit;                          // centre height: r · sit
  let top = -1, bottom = 2;
  for (let i = 0; i < nv; i++) {
    const p = [A.position[3 * i], A.position[3 * i + 1], A.position[3 * i + 2]];
    const n = [A.normal[3 * i], A.normal[3 * i + 1], A.normal[3 * i + 2]];
    assert.ok(Math.abs(Math.hypot(...n) - 1) < 1e-5);
    // outward: from the axis (or from the cap's centre) toward the vertex
    const cx = Math.min(4, Math.max(0, p[0]));
    const out = [p[0] - cx, p[1], p[2] - zc];
    assert.ok(out[0] * n[0] + out[1] * n[1] + out[2] * n[2] > -1e-6, `normal ${i} points in`);
    if (p[2] > zc + 0.99) top = Math.max(top, A.shade[i]);
    if (p[2] < zc - 0.99) bottom = Math.min(bottom, A.shade[i]);
  }
  assert.ok(top > bottom + 0.3, `top ${top} bottom ${bottom}`);
  // triangles wind outward: the first one of the body, seen from outside, is CCW
  const t = A.index.slice(0, 3).map(k => [A.position[3 * k], A.position[3 * k + 1], A.position[3 * k + 2]]);
  const e1 = t[1].map((v, k) => v - t[0][k]), e2 = t[2].map((v, k) => v - t[0][k]);
  const cr = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]];
  const c0 = t[0].map((v, k) => v - [Math.min(4, Math.max(0, t[0][0])), 0, zc][k]);
  assert.ok(cr[0] * c0[0] + cr[1] * c0[1] + cr[2] * c0[2] > 0);
});

test('filament: a heap stands on its lifts, layers alternate in tone, the line is smoothed', async () => {
  const I = await load();
  const F = I.FILAMENT;
  const pts = line(5), lift = [0, 0, 1.4, 1.4, 0];     // 1.4 = one layer of a 2 mm pen
  const A = I.filamentArrays({ pts, nrm: up(5), width: 2, lift });
  const nv = A.position.length / 3;
  assert.equal(nv, ((5 - 1) * F.sub + 1 + 2 * F.capRings) * (F.radial + 1));
  let zmax = 0;
  for (let i = 0; i < nv; i++) zmax = Math.max(zmax, A.position[3 * i + 2]);
  assert.ok(Math.abs(zmax - (1.4 + F.sit + 1)) < 0.2, `zmax ${zmax}`);   // the spline may overshoot a little
  // the top vertex of the ring at sample 0 (layer 0) vs sample 2 (layer 1)
  const ring = i => (F.capRings + i * F.sub) * (F.radial + 1);     // first vertex = top (cos 0)
  assert.notEqual(A.shade[ring(0)], A.shade[ring(2)]);
});

test('alphas: soft / normal / star on both pens; absent = what the pen drew before', async () => {
  const I = await load();
  assert.deepEqual(I.ALPHAS, ['soft', 'normal', 'star']);
  const S = a => I.sprayArrays({ pts: line(6), nrm: up(6), width: 2, alpha: a });
  const soft = S(undefined), normal = S('normal'), star = S('star');
  assert.ok(normal.ink[3] < soft.ink[3]);                 // the marker's edge is crisper
  assert.equal(normal.grain[0], 0);
  assert.ok(star.stamp.every((v, i) => i % 2 === 0 || v === 1));   // stars flagged
  assert.ok(soft.stamp.every((v, i) => i % 2 === 0 || v === 0));
  // the stars keep their pace across segments: start of each segment = arc so far
  const L = star.ink[2];
  assert.ok(Math.abs(star.stamp[4 * 2 * 3] - 3 * L) < 1e-6);  // 4th segment's first corner
  // ✎³: the star nozzle's outline has ridges; sfumato tapers at the ends
  const F = a => I.filamentArrays({ pts: line(12, 1), nrm: up(12), width: 2, alpha: a });
  const fn = F('normal'), fs = F('star'), fo = F('soft');
  assert.ok(fs.position.length > fn.position.length);    // more segments round the star
  const ext = A => { let y = 0; for (let i = 1; i < A.position.length; i += 3) y = Math.max(y, Math.abs(A.position[i])); return y; };
  assert.ok(ext(fo) > ext(fn));                           // the melted bead is wider
  const endZ = A => A.position[3 * (A.position.length / 3 - 1) + 2];
  assert.ok(endZ(fo) < endZ(fn));                         // the tapered tip sits low
});

test('a picture as alpha: stamps on ✎, a relief on ✎³; uv wraps the tube with no seam', async () => {
  const I = await load();
  const sp = I.sprayArrays({ pts: line(4), nrm: up(4), width: 2, alpha: 'img' });
  assert.ok(sp.stamp.every((v, i) => i % 2 === 0 || v === 2));
  // a grey with a bright stripe: the tube bulges there
  const N = 8, g = new Uint8Array(N * N).map((_, k) => (k % N < 4 ? 255 : 0));
  const flat = I.filamentArrays({ pts: line(6, 3), nrm: up(6), width: 2, alpha: 'img' });
  const rel = I.filamentArrays({ pts: line(6, 3), nrm: up(6), width: 2, alpha: 'img', relief: { g, n: N } });
  assert.ok(rel.relief && !flat.relief);
  assert.ok(rel.position.length > flat.position.length);   // denser rings to show it
  const spread = A => { let lo = 9, hi = 0; for (let i = 0; i < A.position.length; i += 3) { const y = Math.abs(A.position[i + 1]); if (A.position[i] > 2 && A.position[i] < 12) { hi = Math.max(hi, y); } } return hi; };
  assert.ok(spread(rel) > spread(flat));
  // the seam: first and last vertex of a ring coincide, v = 0 and 1
  const S = rel.stride;
  assert.ok(Math.abs(rel.uv[1]) < 1e-9 && Math.abs(rel.uv[2 * (S - 1) + 1] - 1) < 1e-9);
  assert.ok(Math.hypot(rel.position[0] - rel.position[3 * (S - 1)], rel.position[1] - rel.position[3 * (S - 1) + 1],
    rel.position[2] - rel.position[3 * (S - 1) + 2]) < 1e-6);
  // mirrored sampling is continuous across tiles
  assert.ok(Math.abs(I.sampleMirror(g, N, 0.999, 0.5) - I.sampleMirror(g, N, 1.001, 0.5)) < 0.01);
});
