// webui/section-core.js — the pure half of ✂ Sezione: the triangle/plane slice
// that draws the contour, the closed test that decides whether a piece gets a
// cap, the stencil parity the cap relies on (nested pieces, an open shell),
// and the hash keys.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'section-core.js')).href);

// an axis-aligned box as a B-Rep tessellation would ship it: every face its
// own 4 vertices (24 in all), counter-clockwise seen from outside
function box(lo = [0, 0, 0], hi = [10, 10, 10], skip = -1) {
  const pos = [], idx = [];
  const faces = [
    [[0, 0, 0], [0, 1, 0], [1, 1, 0], [1, 0, 0]],  // -z
    [[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]],  // +z
    [[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]],  // -y
    [[0, 1, 0], [0, 1, 1], [1, 1, 1], [1, 1, 0]],  // +y
    [[0, 0, 0], [0, 0, 1], [0, 1, 1], [0, 1, 0]],  // -x
    [[1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 0, 1]],  // +x
  ];
  faces.forEach((f, i) => {
    if (i === skip) return;
    const b = pos.length / 3;
    for (const c of f) pos.push(...c.map((u, k) => lo[k] + u * (hi[k] - lo[k])));
    idx.push(b, b + 1, b + 2, b, b + 2, b + 3);
  });
  return { pos: new Float32Array(pos), idx: new Uint32Array(idx) };
}
const trisOf = ({ pos, idx }) => {
  const P = k => [pos[3 * k], pos[3 * k + 1], pos[3 * k + 2]];
  const out = [];
  for (let t = 0; t < idx.length; t += 3) out.push([P(idx[t]), P(idx[t + 1]), P(idx[t + 2])]);
  return out;
};

test('the plane keeps z <= pos, and flip keeps the other half', async () => {
  const { planeOf, signedDist } = await load();
  const p = planeOf({ axis: 'z', pos: 5 });
  assert.ok(signedDist(p, 0, 0, 4) > 0 && signedDist(p, 0, 0, 6) < 0);
  const f = planeOf({ axis: 'z', pos: 5, flip: true });
  assert.ok(signedDist(f, 0, 0, 6) > 0 && signedDist(f, 0, 0, 4) < 0);
  const x = planeOf({ axis: 'x', pos: -2 });
  assert.deepEqual(x.n, [-1, 0, 0]);
  assert.equal(x.axis, 0);
});

test('a box sliced through the middle gives its square outline', async () => {
  const { planeOf, sliceTriangles } = await load();
  const { pos, idx } = box();
  const seg = sliceTriangles(pos, idx, 0, idx.length, null, planeOf({ axis: 'z', pos: 4 }));
  assert.equal(seg.length % 6, 0);
  let len = 0;
  for (let i = 0; i < seg.length; i += 6) {
    assert.ok(Math.abs(seg[i + 2] - 4) < 1e-9 && Math.abs(seg[i + 5] - 4) < 1e-9, 'on the plane');
    len += Math.hypot(seg[i + 3] - seg[i], seg[i + 4] - seg[i + 1]);
  }
  assert.ok(Math.abs(len - 40) < 1e-6, `perimeter ${len}`);
  // a plane missing the part, or one lying on a face, draws nothing
  assert.equal(sliceTriangles(pos, idx, 0, idx.length, null, planeOf({ axis: 'z', pos: 20 })).length, 0);
  assert.equal(sliceTriangles(pos, idx, 0, idx.length, null, planeOf({ axis: 'z', pos: 10 })).length, 0);
});

test('the slice follows the matrixWorld and only the group asked for', async () => {
  const { planeOf, sliceTriangles } = await load();
  const { pos, idx } = box();
  const m = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 100, 0, 0, 1];          // moved +100 in x
  const seg = sliceTriangles(pos, idx, 0, idx.length, m, planeOf({ axis: 'x', pos: 105 }));
  assert.ok(seg.length > 0 && seg.every((v, i) => i % 3 !== 0 || Math.abs(v - 105) < 1e-9));
  // only the first two triangles (the -z face): it lies in z=0, the x=5 plane cuts it once per triangle
  const part = sliceTriangles(pos, idx, 0, 6, null, planeOf({ axis: 'x', pos: 5 }));
  assert.equal(part.length, 12);
});

test('closed: a box is, a box with a face missing is not', async () => {
  const { closedRange } = await load();
  const b = box();
  const c = closedRange(b.pos, b.idx, 0, b.idx.length);
  assert.ok(c.closed, JSON.stringify(c));
  assert.equal(c.boundary, 0);
  const o = box([0, 0, 0], [10, 10, 10], 1);                              // a Join of 5 faces
  const r = closedRange(o.pos, o.idx, 0, o.idx.length);
  assert.ok(!r.closed);
  assert.equal(r.boundary, 4);
  // two boxes in one buffer: each group is judged on its own
  const a = box(), d = box([20, 0, 0], [30, 10, 10], 3);
  const pos = new Float32Array([...a.pos, ...d.pos]);
  const idx = new Uint32Array([...a.idx, ...[...d.idx].map(k => k + a.pos.length / 3)]);
  assert.ok(closedRange(pos, idx, 0, a.idx.length).closed);
  assert.ok(!closedRange(pos, idx, a.idx.length, d.idx.length).closed);
});

test('stencil parity: a cap inside the part, none beside it', async () => {
  const { planeOf, stencilCount } = await load();
  const T = trisOf(box());
  const pl = planeOf({ axis: 'y', pos: 5, flip: true });                 // keep y >= 5, look from -y
  const dir = [0, 1, 0];
  assert.notEqual(stencilCount(T, [5.3, -50, 4.1], dir, pl), 0, 'through the cut face');
  assert.equal(stencilCount(T, [15, -50, 5], dir, pl), 0, 'beside the part');
  // without a cut a closed part always counts zero: no cap anywhere
  assert.equal(stencilCount(T, [5.3, -50, 4.1], dir, null), 0);
});

test('nested pieces: counted one by one, the inner one does not punch the outer cap', async () => {
  const { planeOf, stencilCount } = await load();
  const outer = trisOf(box([0, 0, 0], [10, 10, 10]));
  const inner = trisOf(box([3, 3, 3], [7, 7, 7]));
  const pl = planeOf({ axis: 'y', pos: 5, flip: true });
  const eye = [5.3, -50, 4.1], dir = [0, 1, 0];   // off the faces' diagonals
  // per piece (what section.js does: the stencil is cleared after each cap)
  assert.notEqual(stencilCount(outer, eye, dir, pl), 0);
  assert.notEqual(stencilCount(inner, eye, dir, pl), 0);
  // ONE stencil for both cannot say WHOSE cap a pixel is: where they nest it
  // holds 2, beside the inner piece 1 — the inner cap would be painted with
  // the outer one's hatch (or the reverse), and a parity test would even hole it
  assert.equal(stencilCount([...outer, ...inner], eye, dir, pl), 2);
  assert.equal(stencilCount([...outer, ...inner], [1.2, -50, 1.7], dir, pl), 1);
});

test('an open shell gives a count outside the part — which is why it gets no cap', async () => {
  const { planeOf, stencilCount } = await load();
  const open = trisOf(box([0, 0, 0], [10, 10, 10], 1));                  // no +z face
  const pl = planeOf({ axis: 'y', pos: 5, flip: true });
  // a ray passing ABOVE the box at the plane: nothing should be capped there,
  // yet the missing top lets one back face through
  assert.notEqual(stencilCount(open, [5, -50, 12], [0, 1, -0.1], pl), 0);
});

test('hash keys: cut=axis:pos, nocut like hide=', async () => {
  const { parseCut, formatCut, encodeKeys, decodeKeys } = await load();
  assert.deepEqual(parseCut('z:12.5'), { axis: 'z', pos: 12.5 });
  assert.deepEqual(parseCut('X:-3'), { axis: 'x', pos: -3 });
  for (const bad of ['', 'w:1', 'z:', 'z:abc', 'z:1:2', null]) assert.equal(parseCut(bad), null, bad);
  assert.equal(formatCut({ axis: 'y', pos: 34.800000000000004 }), 'y:34.8');
  const nodes = [{ id: 'n3', leaves: ['n3'] }, { id: 'n7', leaves: ['n7.0', 'n7.1', 'n7.2'] },
                 { id: 'n9', leaves: ['n9.0', 'n9.1'] }];
  const s = new Set(['n3', 'n7.2', 'n9.0', 'n9.1']);
  assert.equal(encodeKeys(s, nodes), 'n3,n7.2,n9');
  assert.deepEqual([...decodeKeys('n3,n7.2,n9,zz', nodes)].sort(), ['n3', 'n7.2', 'n9.0', 'n9.1']);
});

test('niceStep snaps to 1/2/5', async () => {
  const { niceStep } = await load();
  assert.equal(niceStep(0.13), 0.1);
  assert.equal(niceStep(0.26), 0.2);
  assert.equal(niceStep(4), 5);
  assert.equal(niceStep(0), 1);
});
