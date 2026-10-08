// webui/pen3d.js — ✎ the pen as a 3D pen: ink piles up only where you insist.
// Pure module, so it runs here on hand-made strokes on a flat part (z = 0).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'pen3d.js')).href);

// Draw a stroke sample by sample the way view.html does: every new sample asks
// liftAt with all the strokes so far (the current one included).
function draw(P, strokes, pts, w = 1) {
  const s = { k: strokes.length + 1, width: w, pts: [], lift: [] };
  strokes.push(s);
  for (const p of pts) {
    const l = P.liftAt(p, w, strokes, s.pts.length ? s : null);
    s.pts.push(p); s.lift.push(l);
  }
  return s;
}
// n loops of a circle of radius r round (cx, cy), `per` samples per loop
function circle(n, r = 2, per = 24, cx = 0, cy = 0) {
  const out = [];
  for (let i = 0; i <= n * per; i++) {
    const a = (2 * Math.PI * i) / per;
    out.push([cx + r * Math.cos(a), cy + r * Math.sin(a), 0]);
  }
  return out;
}
const line = (y, x0 = -10, x1 = 10, step = 0.4) => {
  const out = []; for (let x = x0; x <= x1 + 1e-9; x += step) out.push([x, y, 0]); return out;
};
const max = a => Math.max(...a);

test('a single line stays on the part', async () => {
  const P = await load();
  const s = draw(P, [], line(0));
  assert.equal(max(s.lift), 0);
  assert.equal(P.heightOf(s), 0);
});

test('crossing a line once or twice does not stack — only insisting does', async () => {
  const P = await load();
  const all = [];
  draw(P, all, line(0));
  const v1 = draw(P, all, line(0).map(([x]) => [0, x, 0]));          // a cross
  assert.equal(max(v1.lift), 0, 'second pass over the crossing: flat');
  const v2 = draw(P, all, line(0).map(([x]) => [0.1, x, 0]));
  assert.ok(max(v2.lift) > 0, 'third pass over the same spot: it climbs');
  assert.ok(max(v2.lift) <= P.PEN3D.layer * 1 + 1e-9, 'by ONE layer, not more');
});

test('circling one spot builds a heap, one layer per loop after the second', async () => {
  const P = await load();
  const tops = [];
  // read the MIDDLE of the last loop: its end closes over its own start, and
  // that point really has been covered once more
  for (const loops of [1, 2, 3, 4, 6]) {
    const s = draw(P, [], circle(loops));
    tops.push(s.lift[loops * 24 - 12]);
  }
  const L = P.PEN3D.layer;
  assert.equal(tops[0], 0, 'one loop: a ring on the part');
  assert.equal(tops[1], 0, 'two loops: still flat');
  assert.ok(tops[2] > 0 && tops[2] <= L + 1e-9, 'three loops: one layer');
  assert.ok(tops[3] > tops[2] && tops[3] <= 2 * L + 1e-9, 'four loops: two layers at most');
  assert.ok(tops[4] <= 4 * L + 1e-9, 'six loops: never more than one layer per loop');
});

test('the lift is a ramp, never a wall', async () => {
  const P = await load();
  const s = draw(P, [], circle(8));
  for (let i = 1; i < s.pts.length; i++) {
    const d = Math.hypot(...s.pts[i].map((v, k) => v - s.pts[i - 1][k]));
    assert.ok(Math.abs(s.lift[i] - s.lift[i - 1]) <= P.PEN3D.slope * d + 1e-9, `sample ${i}`);
  }
});

test('the pen\'s own wake is not old ink: a straight line never climbs on itself', async () => {
  const P = await load();
  const s = draw(P, [], line(0, -10, 10, 0.05));                      // very dense samples
  assert.equal(max(s.lift), 0);
});

test('a heap is local: a line drawn away from it stays flat', async () => {
  const P = await load();
  const all = [];
  draw(P, all, circle(6));
  const far = draw(P, all, line(20));
  assert.equal(max(far.lift), 0);
  const over = draw(P, all, line(0));                                  // straight across the heap
  assert.ok(max(over.lift) > 0, 'drawing across the heap rides over it');
  const ends = [over.lift[0], over.lift.at(-1)];
  assert.deepEqual(ends, [0, 0], 'and comes back down on the part');
});

test('heightOf = top of the tube above the part', async () => {
  const P = await load();
  const s = draw(P, [], circle(4), 1);
  assert.ok(Math.abs(P.heightOf(s) - (max(s.lift) + 1)) < 1e-12);
});
