// webui/measure.js — the 📏 Metro's features, rebuilt from triangles. Pure
// module, so it runs here on hand-made meshes tessellated the way build123d
// does it: every face on its own, vertices duplicated along the edges.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'measure.js')).href);
const near = (a, b, tol = 1e-6, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg || ''} ${a} ≉ ${b} (±${tol})`);

// a mesh builder: faces as quads/polygons, each with its OWN vertices
function soup() {
  const pos = [], idx = [];
  return {
    pos, idx,
    quad(a, b, c, d) { const o = pos.length / 3; pos.push(...a, ...b, ...c, ...d); idx.push(o, o + 1, o + 2, o, o + 2, o + 3); },
    tri(a, b, c) { const o = pos.length / 3; pos.push(...a, ...b, ...c); idx.push(o, o + 1, o + 2); },
    done() { return [Float32Array.from(pos), Uint32Array.from(idx)]; },
  };
}
function box(x0, y0, z0, x1, y1, z1) {
  const s = soup(), P = (x, y, z) => [x ? x1 : x0, y ? y1 : y0, z ? z1 : z0];
  s.quad(P(0, 0, 0), P(0, 1, 0), P(1, 1, 0), P(1, 0, 0));   // bottom (−z)
  s.quad(P(0, 0, 1), P(1, 0, 1), P(1, 1, 1), P(0, 1, 1));   // top (+z)
  s.quad(P(0, 0, 0), P(1, 0, 0), P(1, 0, 1), P(0, 0, 1));   // front (−y)
  s.quad(P(0, 1, 0), P(0, 1, 1), P(1, 1, 1), P(1, 1, 0));   // back (+y)
  s.quad(P(0, 0, 0), P(0, 0, 1), P(0, 1, 1), P(0, 1, 0));   // left (−x)
  s.quad(P(1, 0, 0), P(1, 1, 0), P(1, 1, 1), P(1, 0, 1));   // right (+x)
  return s.done();
}
// a tube: outer radius R, hole radius r, height h, n segments; the walls are
// smooth (their facets meet at 360/n degrees), the rims are sharp
function tube(R, r, h, n = 48) {
  const s = soup(), at = (rad, i, z) => [rad * Math.cos(2 * Math.PI * i / n), rad * Math.sin(2 * Math.PI * i / n), z];
  for (let i = 0; i < n; i++) {
    const j = i + 1;
    s.quad(at(R, i, 0), at(R, j, 0), at(R, j, h), at(R, i, h));     // outer wall, normals out
    s.quad(at(r, i, 0), at(r, i, h), at(r, j, h), at(r, j, 0));     // hole wall, normals in
    s.quad(at(r, i, h), at(R, i, h), at(R, j, h), at(r, j, h));     // top ring
    s.quad(at(r, i, 0), at(r, j, 0), at(R, j, 0), at(R, i, 0));     // bottom ring
  }
  // one pool of vertices per face, as a tessellator would: weld happens in buildTopology
  return s.done();
}

test('a box: 12 straight edges, 8 corners, all edges sharp', async () => {
  const M = await load();
  const [pos, idx] = box(0, 0, 0, 20, 10, 6);
  const F = M.buildFeatures(pos, idx);
  assert.equal(F.topo.nV, 8, 'welded to the 8 corners');
  assert.equal(F.nSharp, 12);
  assert.equal(F.chains.length, 12);
  assert.ok(F.chains.every(c => c.type === 'line'));
  assert.deepEqual(F.chains.map(c => +c.length.toFixed(6)).sort((a, b) => a - b), [6, 6, 6, 6, 10, 10, 10, 10, 20, 20, 20, 20]);
  assert.equal(F.corners.length, 8);
});

test('the diagonal of a quad is NOT an edge', async () => {
  const M = await load();
  const [pos, idx] = box(0, 0, 0, 20, 10, 6);
  const F = M.buildFeatures(pos, idx);
  assert.equal(F.topo.eA.length, 18, '12 edges + 6 face diagonals');
});

test('Auto snaps a vertex before an edge before a face', async () => {
  const M = await load();
  const F = M.buildFeatures(...box(0, 0, 0, 20, 10, 6));
  const tri = t => t;                          // top face is triangles 2,3
  const v = M.pickFeature(F, [19.6, 9.7, 6], 3, { rV: 1, rE: 0.8 });
  assert.equal(v.snap, 'vertex'); assert.deepEqual(v.point, [20, 10, 6]);
  const e = M.pickFeature(F, [10, 9.6, 6], 3, { rV: 1, rE: 0.8 });
  assert.equal(e.snap, 'edge'); assert.deepEqual(e.point.map(x => +x.toFixed(9)), [10, 10, 6]);
  assert.equal(e.chain.type, 'line'); near(e.chain.length, 20);
  const f = M.pickFeature(F, [10, 5, 6], tri(3), { rV: 1, rE: 0.8 });
  assert.equal(f.snap, 'face'); assert.deepEqual(f.plane.normal.map(Math.round), [0, 0, 1]);
  assert.equal(f.tris.length, 2, 'the whole top face, not one triangle');
  const p = M.pickFeature(F, [10, 5, 6], 3, { rV: 1, rE: 0.8, mode: 'p2p' });
  assert.equal(p.snap, 'free');
});

test('two parallel faces give the thickness, perpendicular', async () => {
  const M = await load();
  const F = M.buildFeatures(...box(0, 0, 0, 20, 10, 6));
  const top = M.pickFeature(F, [3, 4, 6], 3, { rV: 0.1, rE: 0.1 });
  const bottom = M.pickFeature(F, [15, 7, 0], 0, { rV: 0.1, rE: 0.1 });
  const m = M.measureTwo(top, bottom);
  assert.equal(m.kind, 'face_gap'); near(m.value, 6);
  assert.deepEqual(m.b.map(x => +x.toFixed(9)), [3, 4, 0], 'measured straight down from the first point');
  // a vertex and a face: the distance to the plane
  const v = M.pickFeature(F, [20, 10, 6], 3, { rV: 1, rE: 1 });
  const d = M.measureTwo(v, bottom);
  assert.equal(d.kind, 'distance'); assert.ok(d.toPlane); near(d.value, 6);
});

test('a tube: rims are circles, the hole wall is a cylinder', async () => {
  const M = await load();
  const F = M.buildFeatures(...tube(8, 4, 10, 48));
  const circles = F.chains.filter(c => c.type === 'circle');
  assert.equal(circles.length, 4, 'two rims at each end');
  const rs = circles.map(c => c.circle.r).sort((a, b) => a - b);
  // a 48-gon inscribed in r: the fit lands between the apothem and r
  near(rs[0], 4, 4 * 0.003, 'hole rim'); near(rs[3], 8, 8 * 0.003, 'outer rim');
  for (const c of circles) near(Math.abs(c.circle.axis[2]), 1, 1e-6, 'axis along z');
  // tap near the hole rim: the centre, approx
  const pick = M.pickFeature(F, [4.05, 0.2, 10], null, { rV: 0.1, rE: 0.3 });
  assert.equal(pick.snap, 'circle_center'); assert.ok(pick.approx);
  near(pick.point[0], 0, 1e-6); near(pick.point[1], 0, 1e-6); near(pick.point[2], 10, 1e-6);
  const one = M.measureOne(pick);
  assert.equal(one.kind, 'diameter'); near(one.value, 8, 8 * 0.003);
});

test('Foro: tapping INSIDE the hole fits the cylinder', async () => {
  const M = await load();
  const [pos, idx] = tube(8, 4, 10, 48);
  const F = M.buildFeatures(pos, idx);
  // triangle 2 is the first hole-wall triangle (4 quads per segment, 2 tris each)
  const pick = M.pickFeature(F, [4, 0.2, 5], 2, { rV: 0.1, rE: 0.1, mode: 'hole' });
  assert.ok(pick && pick.cylinder, 'a cylinder');
  assert.ok(pick.cylinder.hole, 'normals point at the axis: a hole');
  near(pick.cylinder.r, 4, 4 * 0.003); near(Math.abs(pick.cylinder.axis[2]), 1, 1e-6);
  near(pick.cylinder.length, 10, 1e-6);
  const outer = M.pickFeature(F, [8, 0.2, 5], 0, { rV: 0.1, rE: 0.1, mode: 'hole' });
  assert.ok(outer.cylinder && !outer.cylinder.hole, 'the outside is a boss, not a hole');
});

test('an arc is a radius, not a diameter', async () => {
  const M = await load();
  const pts = [];
  for (let i = 0; i <= 12; i++) { const a = (Math.PI / 2) * i / 12; pts.push([5 + 3 * Math.cos(a), 2 + 3 * Math.sin(a), 1]); }
  const f = M.fitCircle3D(pts);
  near(f.r, 3, 1e-9); near(f.center[0], 5, 1e-9); near(f.center[1], 2, 1e-9); near(f.sweep, 90, 1e-6);
  const one = M.measureOne({ circle: f, chain: { type: 'arc' } });
  assert.equal(one.kind, 'radius'); near(one.value, 3, 1e-9);
});

test('a quarter arc with a little noise still gives its radius', async () => {
  const M = await load();
  const xy = [];
  for (let i = 0; i <= 20; i++) { const a = (Math.PI / 2) * i / 20, e = (i % 2 ? 1 : -1) * 0.002; xy.push([(10 + e) * Math.cos(a), (10 + e) * Math.sin(a)]); }
  const f = M.fitCircle2D(xy);
  near(f.r, 10, 0.01); near(f.cx, 0, 0.02); near(f.cy, 0, 0.02);
});

test('a dense piece (a thread) turns edge snapping off, faces still work', async () => {
  const M = await load();
  const [pos, idx] = box(0, 0, 0, 20, 10, 6);
  const F = M.buildFeatures(pos, idx, { maxSharp: 5 });
  assert.ok(F.dense);
  assert.equal(M.pickFeature(F, [20, 10, 6], 3, { rV: 1, rE: 1 }).snap, 'face');
});

test('picking the same circle twice means its diameter', async () => {
  const M = await load();
  const F = M.buildFeatures(...tube(8, 4, 10, 48));
  const a = M.pickFeature(F, [4.05, 0.2, 10], null, { rV: 0.1, rE: 0.3 });
  const b = M.pickFeature(F, [-4.02, -0.3, 10], null, { rV: 0.1, rE: 0.3 });
  assert.ok(M.sameFeature(a, b));
  const c = M.pickFeature(F, [8.02, 0.1, 10], null, { rV: 0.1, rE: 0.3 });
  assert.ok(!M.sameFeature(a, c), 'the outer rim is another circle');
  const m = M.measureTwo(a, c);                // concentric: interaxis 0
  near(m.value, 0, 1e-6);
});

test('a ridged spot (a thread) snaps no vertex or edge, a circle still', async () => {
  const M = await load();
  // a comb: 30 sharp ridges 0.2 apart on a strip, beside a tube rim
  const s = soup();
  for (let i = 0; i < 30; i++) {
    const x = i * 0.2;
    s.quad([x, 0, 0], [x + 0.1, 0, 0.1], [x + 0.1, 2, 0.1], [x, 2, 0]);
    s.quad([x + 0.1, 0, 0.1], [x + 0.2, 0, 0], [x + 0.2, 2, 0], [x + 0.1, 2, 0.1]);
  }
  const [pos, idx] = s.done();
  const F = M.buildFeatures(pos, idx);
  assert.ok(!F.dense, 'not dense as a whole');
  const p = [3.05, 1, 0.05];
  assert.ok(M.edgesNear(F, p, 1) > M.RIDGED_EDGES);
  const f = M.pickFeature(F, p, null, { rV: 0.3, rE: 0.3 });
  assert.notEqual(f.snap, 'vertex'); assert.notEqual(f.snap, 'edge');
  // on a plain box edge the same radii do snap
  const B = M.buildFeatures(...box(0, 0, 0, 20, 10, 6));
  assert.equal(M.pickFeature(B, [10, 9.8, 6], 3, { rV: 0.3, rE: 0.3 }).snap, 'edge');
});
