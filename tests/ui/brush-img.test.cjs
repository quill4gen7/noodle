// webui/brush-img.js — a picture taken "a bit at random" becomes a clean brush:
// cropped to its subject, the right polarity, levels stretched, edges feathered.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const load = () => import(pathToFileURL(path.join(__dirname, '..', '..', 'webui', 'brush-img.js')).href);

// w×h picture, ground colour g, a disc of colour c at (cx, cy) radius r
function pic(w, h, g, c, cx, cy, r, alphaGround = 255) {
  const a = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = 4 * (y * w + x), on = Math.hypot(x - cx, y - cy) <= r;
    const k = on ? c : g;
    a[i] = k[0]; a[i + 1] = k[1]; a[i + 2] = k[2]; a[i + 3] = on ? 255 : alphaGround;
  }
  return a;
}
const at = (A, N, i, j) => A[j * N + i];

test('a dark subject on a light ground: cropped to it, ink = subject, ground = 0', async () => {
  const B = await load();
  const P = pic(400, 300, [235, 230, 220], [30, 40, 50], 300, 80, 40);   // off-centre, small
  const A = B.analyse(P, 400, 300);
  assert.ok(A.uniform);
  assert.ok(Math.abs(A.box.x0 - 260) <= 2 && Math.abs(A.box.x1 - 341) <= 2, JSON.stringify(A.box));
  const N = 64, a = B.prepAlpha(P, 400, 300, N);
  assert.ok(at(a, N, 32, 32) > 200, 'centre inked');                  // the subject fills the stamp
  assert.equal(at(a, N, 1, 1), 0);                                    // the ground is clear
  assert.equal(at(a, N, 63, 32), 0);                                  // the edge is feathered out
});

test('light on dark gives the same stamp; a transparent sticker uses its cut-out', async () => {
  const B = await load(), N = 64;
  const a = B.prepAlpha(pic(200, 200, [10, 10, 10], [250, 250, 250], 100, 100, 50), 200, 200, N);
  assert.ok(at(a, N, 32, 32) > 200 && at(a, N, 2, 2) === 0);
  const s = B.prepAlpha(pic(200, 200, [255, 255, 255], [255, 0, 0], 60, 60, 30, 0), 200, 200, N);
  assert.ok(at(s, N, 32, 32) > 150, `sticker centre ${at(s, N, 32, 32)}`);
  assert.equal(at(s, N, 2, 2), 0);
});

test('a busy photo with no ground: a frame is trimmed, nothing throws, edges fade', async () => {
  const B = await load(), N = 32, w = 120, h = 90;
  const P = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) { const v = (i * 2654435761) >>> 24; P[4 * i] = v; P[4 * i + 1] = 255 - v; P[4 * i + 2] = (v * 3) & 255; P[4 * i + 3] = 255; }
  const A = B.analyse(P, w, h);
  assert.ok(!A.uniform);
  const a = B.prepAlpha(P, w, h, N);
  assert.equal(at(a, N, 0, 0), 0);
  const t = B.prepTexture(P, w, h, N);
  assert.equal(t.length, N * N * 4);
  assert.ok(t.every((v, i) => i % 4 !== 3 || v === 255));
});

test('texture: cropped by covering the subject, levels stretched', async () => {
  const B = await load(), N = 32;
  // a dull, low-contrast photo with a white margin
  const P = pic(300, 300, [255, 255, 255], [120, 110, 100], 150, 150, 120);
  const t = B.prepTexture(P, 300, 300, N);
  // the centre is the subject, stretched away from mid-grey
  const L = i => 0.299 * t[i] + 0.587 * t[i + 1] + 0.114 * t[i + 2];
  assert.ok(L(4 * (16 * N + 16)) < 60, `centre ${L(4 * (16 * N + 16))}`);
  assert.ok(B.brushId(t).length > 2);
});

test('a photo with a dark FRAME: the frame is not the ground (the leaf stays the ink)', async () => {
  const B = await load(), w = 300, h = 220, N = 64;
  const P = pic(w, h, [214, 205, 190], [40, 80, 30], 210, 80, 35);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    if (x > 9 && x < w - 10 && y > 9 && y < h - 10) continue;
    const i = 4 * (y * w + x); P[i] = 90; P[i + 1] = 85; P[i + 2] = 80;
  }
  const A = B.analyse(P, w, h);
  assert.ok(A.bg[0] > 200, `ground ${A.bg}`);
  assert.ok(A.box.x0 > 160 && A.box.y1 < 130, JSON.stringify(A.box));
  const a = B.prepAlpha(P, w, h, N);
  assert.ok(at(a, N, 32, 32) > 200 && at(a, N, 3, 32) === 0);
});

test('the ground grain is cut: a noisy table leaves no haze', async () => {
  const B = await load(), w = 200, h = 200, N = 64;
  const P = pic(w, h, [210, 200, 185], [30, 60, 25], 100, 100, 40);
  let seed = 7; const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
  for (let i = 0; i < w * h; i++) if (Math.hypot(i % w - 100, (i / w | 0) - 100) > 42) {
    const n = (rnd() * 36 - 18) | 0; P[4 * i] += n; P[4 * i + 1] += n; P[4 * i + 2] += n; }
  const a = B.prepAlpha(P, w, h, N);
  let haze = 0; for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) if (Math.hypot(i - 32, j - 32) > 27 && a[j * N + i] > 20) haze++;
  assert.ok(haze < 20, `haze pixels ${haze}`);
  assert.ok(a[32 * N + 32] > 230);
});
