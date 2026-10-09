// webui/view-plates.js — the plates that step aside when you zoom so close you
// can no longer read the word (PLAN_VIEW_TOOLS §3). The decision is pure: a
// projected rectangle, a crowding score with hysteresis, and where to go.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'view-plates.js')).href);
const near = (a, b, tol = 1e-6, msg = '') => assert.ok(Math.abs(a - b) <= tol, `${msg} ${a} ≉ ${b} (±${tol})`);

// THREE's PerspectiveCamera.projectionMatrix, column-major
function persp(fovDeg, aspect, n = 0.1, f = 1000) {
  const t = n * Math.tan(fovDeg * Math.PI / 360), h = 2 * t, w = aspect * h;
  const P = new Array(16).fill(0);
  P[0] = 2 * n / w; P[5] = 2 * n / h; P[10] = -(f + n) / (f - n); P[11] = -1; P[14] = -2 * f * n / (f - n);
  return P;
}
function ortho(l, r, t, b, n = 0.1, f = 1000) {
  const P = new Array(16).fill(0);
  P[0] = 2 / (r - l); P[5] = 2 / (t - b); P[10] = -2 / (f - n); P[12] = -(r + l) / (r - l); P[13] = -(t + b) / (t - b);
  P[14] = -(f + n) / (f - n); P[15] = 1;
  return P;
}
const VW = 1000, VH = 800;

test('spriteRect: a plate in mm, on screen in px — it doubles when the distance halves', async () => {
  const { spriteRect } = await load();
  const P = persp(50, VW / VH);
  const far = spriteRect([0, 0, -200], 30, 8, 0.5, 0, P, VW, VH);
  const close = spriteRect([0, 0, -100], 30, 8, 0.5, 0, P, VW, VH);
  near((close.x1 - close.x0) / (far.x1 - far.x0), 2, 1e-9);
  near((far.x0 + far.x1) / 2, VW / 2, 1e-9, 'centred horizontally');
  near(far.y1, VH / 2, 1e-9, 'center (0.5, 0): the bottom edge sits on the point');
  assert.ok(far.y0 < far.y1, 'y grows downwards');
  assert.equal(spriteRect([0, 0, 10], 30, 8, 0.5, 0, P, VW, VH), null, 'behind the camera');
});

test('spriteRect + pxToView: in ortho too, and a px offset lands exactly that many px away', async () => {
  const { spriteRect, pxToView } = await load();
  for (const P of [persp(50, VW / VH), ortho(-125, 125, 100, -100)]) {
    const z = -150, [ox, oy] = pxToView(40, -25, z, P, VW, VH);
    const a = spriteRect([3, 2, z], 0, 0, 0, 0, P, VW, VH), b = spriteRect([3 + ox, 2 + oy, z], 0, 0, 0, 0, P, VW, VH);
    near(b.x0 - a.x0, 40, 1e-6); near(b.y0 - a.y0, -25, 1e-6);
  }
});

test('crowding: wide plate, giant letters, big plate off screen — and a small one at the edge is fine', async () => {
  const { crowding, PLATE } = await load();
  const box = (x0, y0, w, h) => ({ x0, y0, x1: x0 + w, y1: y0 + h });
  assert.ok(crowding(box(400, 300, 200, 50), VW, VH) < 0.8, 'a normal plate');
  near(crowding(box(30, 300, PLATE.WIDTH * VW, 40), VW, VH), 1, 1e-9, 'PLATE.WIDTH of the viewport');
  assert.ok(crowding(box(400, 300, 300, 2 * PLATE.LETTER_PX / 0.5), VW, VH) >= 2, 'letters twice the limit');
  assert.ok(crowding(box(-20, 300, 120, 30), VW, VH) < 0.8, 'small plate cut by the edge: you panned, nothing to do');
  assert.ok(crowding(box(-200, 300, 450, 90), VW, VH) >= 1, 'a big plate running off the screen');
  assert.equal(crowding(null, VW, VH), 0);
  // the letter fraction matters: a 3-line label has small letters on a tall plate
  const tall = box(300, 200, 300, 200);
  assert.ok(crowding(tall, VW, VH, 0.2) < crowding(tall, VW, VH, 0.6));
});

test('hysteresis: in at 1.0, out at 0.8', async () => {
  const { hysteresis } = await load();
  assert.equal(hysteresis(false, 0.95), false);
  assert.equal(hysteresis(false, 1.0), true);
  assert.equal(hysteresis(true, 0.85), true, 'still on: no blinking while you orbit');
  assert.equal(hysteresis(true, 0.79), false);
});

test('escape: slides away from the zoom point, along the stem when the stem leads away', async () => {
  const { escape } = await load();
  // a plate just above the zoom point, room above it
  const r = { x0: 400, y0: 300, x1: 700, y1: 380 };
  const e = escape(r, [550, 420], VW, VH, [0, -1]);
  assert.equal(e.mode, 'slide');
  near(e.dx, 0, 1e-9); assert.ok(e.dy < 0, 'up, the way the stem points');
  const R = Math.max(44, 0.16 * Math.min(VW, VH));
  near(420 - (380 + e.dy), R, 1, 'just clear of the zone');
  // a stem pointing back at the zoom point is ignored: straight away instead
  const e2 = escape(r, [550, 420], VW, VH, [0, 1]);
  assert.equal(e2.mode, 'slide'); assert.ok(e2.dy < 0);
});

test('escape: no room to slide → fade; out of the zoom zone already → stay', async () => {
  const { escape } = await load();
  const huge = { x0: -100, y0: 100, x1: 1100, y1: 500 };           // wider than the screen
  assert.equal(escape(huge, [500, 300], VW, VH).mode, 'fade');
  const top = { x0: 300, y0: 10, x1: 700, y1: 120 };               // at the top, zoom just below it
  assert.equal(escape(top, [500, 140], VW, VH, [0, -1]).mode, 'fade', 'sliding up would leave the screen');
  const aside = { x0: 20, y0: 20, x1: 300, y1: 90 };
  assert.equal(escape(aside, [700, 600], VW, VH).mode, 'stay');
  // clear of the zoom point but cut by the top of the screen: giant pieces of
  // letters and a stem across the part — it fades (g64#a1 zoomed on its anchor)
  const cutTop = { x0: -100, y0: -250, x1: 1100, y1: 60 };
  assert.equal(escape(cutTop, [500, 600], VW, VH, [0, -1]).mode, 'fade');
});

test('escape: a slide that would land on the plate\'s own anchor fades instead', async () => {
  const { escape } = await load();
  // g64#a1 from above: the zoom point over the top of the plate, its anchor
  // under it — sliding down clears the zoom point and covers what it points at
  const r = { x0: 100, y0: 250, x1: 900, y1: 470 };
  assert.equal(escape(r, [500, 260], VW, VH).mode, 'slide', 'nothing to keep: it slides');
  assert.equal(escape(r, [500, 260], VW, VH, null, [[500, 560]]).mode, 'fade');
  assert.equal(escape(r, [500, 260], VW, VH, null, [[950, 120]]).mode, 'slide', 'an anchor elsewhere does not matter');
});

test('ease: lands in ~150 ms, never overshoots', async () => {
  const { ease } = await load();
  let v = 0;
  for (let t = 0; t < 150; t += 16) v = ease(v, 1, 16);
  assert.ok(v > 0.94 && v <= 1, `${v}`);
  assert.equal(ease(0.3, 1, 1e6), 1);
  assert.equal(ease(0.3, 1, 0), 0.3);
});

test('inRect', async () => {
  const { inRect } = await load();
  assert.ok(inRect({ x0: 0, y0: 0, x1: 10, y1: 10 }, 5, 5));
  assert.ok(!inRect({ x0: 0, y0: 0, x1: 10, y1: 10 }, 11, 5));
  assert.ok(!inRect(null, 1, 1));
});
