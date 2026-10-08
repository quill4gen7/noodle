// webui/ffd.js — ▣ Forme → Gabbia → Deforma: the trilinear cage (FFD) and its
// constraints. Pure module, so it runs here on hand-made points.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'ffd.js')).href);
const near = (a, b, tol = 1e-9, msg = '') => assert.ok(Math.abs(a - b) <= tol, `${msg} ${a} ≉ ${b}`);
const nearV = (a, b, tol = 1e-9, msg = '') => a.forEach((v, k) => near(v, b[k], tol, msg + `[${k}]`));

// a grid of points filling the unit cube [−0.5, 0.5]³
function grid(n = 6) {
  const out = [];
  for (let i = 0; i <= n; i++) for (let j = 0; j <= n; j++) for (let k = 0; k <= n; k++)
    out.push(i / n - 0.5, j / n - 0.5, k / n - 0.5);
  return Float32Array.from(out);
}

test('corner order is the cage order: sx, sy, sz nested', async () => {
  const F = await load();
  const want = [];
  for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) want.push([sx, sy, sz]);
  assert.deepEqual(F.SIGNS, want);
  assert.equal(F.CAGE_EDGES.length, 12);
  for (const [a, b] of F.CAGE_EDGES) {
    const d = F.SIGNS[a].filter((v, k) => v !== F.SIGNS[b][k]).length;
    assert.equal(d, 1, 'an edge joins corners that differ on one axis');
  }
});

test('a null or zero ffd leaves the geometry identical', async () => {
  const F = await load();
  const pos = grid();
  assert.deepEqual(Array.from(F.deformPositions(pos, null)), Array.from(pos));
  assert.deepEqual(Array.from(F.deformPositions(pos, F.zeroFfd())), Array.from(pos));
  assert.notEqual(F.deformPositions(pos, null), pos, 'always a COPY');
  assert.equal(F.isDeformed(null), false);
  assert.equal(F.isDeformed(F.zeroFfd()), false);
});

test('the weights are a partition of unity, 1 at their own corner', async () => {
  const F = await load();
  for (const p of [[0, 0, 0], [0.3, -0.1, 0.45], [-0.5, 0.5, 0.2]]) {
    near(F.weights(...p).reduce((a, b) => a + b, 0), 1, 1e-12);
  }
  for (let i = 0; i < 8; i++) {
    const w = F.weights(...F.SIGNS[i].map(s => 0.5 * s));
    w.forEach((v, j) => near(v, i === j ? 1 : 0, 1e-12));
  }
});

test('one moved corner: it moves by d, the others and the far faces stay put', async () => {
  const F = await load();
  const i = 7, d = [0.3, -0.1, 0.2];                 // corner (+,+,+)
  const ffd = F.zeroFfd(); ffd[i] = d;
  // the corner moves exactly by d; the seven others not at all
  for (let j = 0; j < 8; j++) {
    const rest = F.SIGNS[j].map(s => 0.5 * s);
    const at = F.deformPoint(ffd, rest);
    nearV(at, j === i ? rest.map((v, k) => v + d[k]) : rest, 1e-12, `corner ${j}`);
  }
  // every point on the three faces NOT touching the corner (x=−½, y=−½, z=−½) is fixed
  const pos = grid(), out = F.deformPositions(pos, ffd);
  let inOwn = 0, moved = 0;
  for (let k = 0; k < pos.length; k += 3) {
    const p = [pos[k], pos[k + 1], pos[k + 2]], q = [out[k], out[k + 1], out[k + 2]];
    const m = Math.hypot(q[0] - p[0], q[1] - p[1], q[2] - p[2]);
    if (p.some(v => Math.abs(v + 0.5) < 1e-6)) near(m, 0, 1e-6, 'far face');
    // the displacement is always parallel to d and never more than |d|
    assert.ok(m <= Math.hypot(...d) + 1e-6);
    if (m > 1e-9) moved++;
    // strongest in its own octant: more than half of |d| only there
    if (m > 0.5 * Math.hypot(...d)) { inOwn++; assert.ok(p.every(v => v >= -1e-6), `${p} is outside the octant`); }
  }
  assert.ok(inOwn > 0 && moved > 0);
  // the centre gets an eighth of it
  nearV(F.deformPoint(ffd, [0, 0, 0]), d.map(v => v / 8), 1e-12);
});

test('the deformation is the trilinear interpolation of the corners', async () => {
  const F = await load();
  const ffd = F.zeroFfd().map((_, i) => [0.05 * i, -0.03 * i, 0.02 * (i % 3)]);
  const C = F.corners(ffd);
  const lerp = (a, b, t) => a.map((v, k) => v + (b[k] - v) * t);
  const p = [0.1, -0.2, 0.35], t = p.map(v => v + 0.5);
  // nested lerp: z, then y, then x (corner index = 4·ix + 2·iy + iz)
  const z = [0, 1, 2, 3].map(j => lerp(C[2 * j], C[2 * j + 1], t[2]));
  const y = [0, 1].map(j => lerp(z[2 * j], z[2 * j + 1], t[1]));
  nearV(F.deformPoint(ffd, p), lerp(y[0], y[1], t[0]), 1e-12);
});

test('a corner stays in its own octant, at least half the minimum from the centre', async () => {
  const F = await load();
  const minN = [0.1, 0.2, 0.05];
  for (let i = 0; i < 8; i++) {
    const s = F.SIGNS[i];
    // dragged far PAST the centre, to the opposite corner and beyond
    const c = F.constrainCorner(i, s.map(v => -2 * v), minN);
    for (let a = 0; a < 3; a++) {
      assert.equal(Math.sign(c[a]), s[a], `corner ${i} crossed axis ${a}`);
      near(Math.abs(c[a]), 0.5 * minN[a], 1e-12);
    }
    // a free move inside its octant is kept as it is
    const w = s.map((v, a) => v * (0.3 + 0.1 * a));
    nearV(F.constrainCorner(i, w, minN), w, 1e-12);
    // and never beyond the cap the backend accepts
    const far = F.constrainCorner(i, s.map(v => 100 * v), minN);
    far.forEach(v => near(Math.abs(v), F.FFD_MAX, 1e-12));
  }
});

test('after any moves, two corners are never closer than the minimum', async () => {
  const F = await load();
  const minN = [0.08, 0.08, 0.08];
  let ffd = F.zeroFfd();
  // throw every corner toward the centre and past it, in turn
  let seed = 7;
  const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647) * 2 - 1;
  for (let round = 0; round < 50; round++) {
    const i = round % 8;
    ffd = F.moveCorner(ffd, i, [rnd(), rnd(), rnd()].map(v => v * 0.8), minN);
    const C = F.corners(ffd);
    for (let a = 0; a < 8; a++) for (let b = a + 1; b < 8; b++) {
      const sep = Math.max(...[0, 1, 2].map(k => Math.abs(C[a][k] - C[b][k])));
      assert.ok(sep >= 0.08 - 1e-12, `corners ${a} and ${b} at ${sep}`);
    }
    // d stays inside what the backend accepts
    for (const d of ffd) for (const v of d) assert.ok(Math.abs(v) < 10);
  }
});

test('moveCorner returns a new ffd and stores d = place − rest', async () => {
  const F = await load();
  const f0 = F.zeroFfd(), f1 = F.moveCorner(f0, 0, [-0.7, -0.5, -0.6], [0, 0, 0]);
  assert.deepEqual(f0, F.zeroFfd(), 'the input is not touched');
  nearV(f1[0], [-0.2, 0, -0.1], 1e-12);
  nearV(F.cornerAt(f1, 0), [-0.7, -0.5, -0.6], 1e-12);
  assert.equal(F.isDeformed(f1), true);
  const box = F.cornerBox(f1);
  nearV(box.min, [-0.7, -0.5, -0.6], 1e-12);
  nearV(box.max, [0.5, 0.5, 0.5], 1e-12);
  assert.deepEqual(F.copyFfd(null), null);
});

test('the corner box holds the whole deformed shape', async () => {
  const F = await load();
  const ffd = F.zeroFfd().map((_, i) => F.SIGNS[i].map((s, a) => 0.2 * s * ((i + a) % 3 - 1)));
  const box = F.cornerBox(ffd), out = F.deformPositions(grid(), ffd);
  for (let k = 0; k < out.length; k += 3) for (let a = 0; a < 3; a++) {
    assert.ok(out[k + a] >= box.min[a] - 1e-6 && out[k + a] <= box.max[a] + 1e-6);
  }
});
