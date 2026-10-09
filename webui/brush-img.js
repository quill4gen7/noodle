// ✎ a picture as a BRUSH — the two little squares of the common row: one makes
// it the brush's ALPHA (its shape, as in ZBrush), the other its colour TEXTURE.
// Pictures arrive "taken a bit at random" (quill): a phone photo of a leaf on a
// table, a logo with a white margin, a PNG with transparency. So everything is
// automatic and nothing can fail on a picture that decodes:
//
//   1. the BACKGROUND is read off a ring a little INSIDE the border (per-
//      channel median; transparent pixels are background by definition) — the
//      outermost band of a photo is often a frame, a vignette or a scanner
//      edge, and taking that for the ground turned a leaf on a table inside
//      out (the table became the ink);
//   2. if the border is uniform, the subject is what differs from it, and the
//      picture is CROPPED to the subject's box (rows/columns with too few
//      subject pixels are noise, not subject); else a thin frame is trimmed;
//   3. ALPHA: grey levels with the right POLARITY (dark ink on a light ground
//      and light on dark both give ink = subject), the ground pushed to 0, the
//      contrast stretched between percentiles, and the edges FEATHERED by a
//      round falloff — so no stamp ever shows the square it came from;
//   4. TEXTURE: the same crop, kept square by covering (no ground fill), the
//      levels stretched; it is sampled with mirrored repeat, so it tiles with
//      no seam along a stroke.
//
// Pure arrays in, arrays out (no DOM): tests/ui/brush-img.test.cjs. The
// browser side (`loadBrushImage`) only decodes and encodes.

export const BRUSH_N = 128;          // the prepared square, both kinds

const lum = (r, g, b) => 0.299 * r + 0.587 * g + 0.114 * b;
const median = a => { const s = Float32Array.from(a).sort(); return s.length ? s[s.length >> 1] : 0; };
const pct = (sorted, q) => sorted[Math.min(sorted.length - 1, Math.max(0, Math.round(q * (sorted.length - 1))))];
const smooth = (a, b, x) => { const t = Math.max(0, Math.min(1, (x - a) / (b - a))); return t * t * (3 - 2 * t); };

// → { bg:[r,g,b], uniform, hasAlpha, box:{x0,y0,x1,y1} } on the source pixels
export function analyse(rgba, w, h) {
  // the frame trimmed off whatever happens (4%), the ground read on the next 4%
  const m = Math.min(w, h), f = Math.round(m * 0.04), ring = f + Math.max(1, Math.round(m * 0.04));
  const R = [], G = [], B = [];
  let transparent = 0, n = 0, hasAlpha = false;
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = 4 * (y * w + x);
    if (rgba[i + 3] < 250) hasAlpha = true;
    if (x < f || x >= w - f || y < f || y >= h - f) continue;              // the frame
    if (x >= ring && x < w - ring && y >= ring && y < h - ring) continue;
    n++;
    if (rgba[i + 3] < 128) { transparent++; continue; }
    R.push(rgba[i]); G.push(rgba[i + 1]); B.push(rgba[i + 2]);
  }
  const bg = [median(R), median(G), median(B)];
  const T = 48;
  const cut = hasAlpha && transparent > n * 0.5;
  const isFg = i => (cut ? rgba[i + 3] >= 128
    : Math.hypot(rgba[i] - bg[0], rgba[i + 1] - bg[1], rgba[i + 2] - bg[2]) > T);
  // how uniform the border is: share of border pixels close to the ground
  let close = transparent;
  for (let k = 0; k < R.length; k++) if (Math.hypot(R[k] - bg[0], G[k] - bg[1], B[k] - bg[2]) <= T) close++;
  const uniform = n > 0 && close / n > 0.6;
  hasAlpha = hasAlpha && transparent > n * 0.5;   // a cut-out (sticker PNG), not a stray translucent pixel
  const rows = new Int32Array(h), cols = new Int32Array(w);
  let fg = 0;
  for (let y = f; y < h - f; y++) for (let x = f; x < w - f; x++) {
    if (!isFg(4 * (y * w + x))) continue;
    rows[y]++; cols[x]++; fg++;
  }
  const inner = Math.max(1, (w - 2 * f) * (h - 2 * f));
  let box = null;
  if (uniform && fg > inner * 0.005 && fg < inner * 0.95) {
    // a row/column is subject when ≥ 2% of it is: a frame's remnant (a line
    // of one or two pixels down every row) and noise are not
    const mr = Math.max(2, (w - 2 * f) * 0.02), mc = Math.max(2, (h - 2 * f) * 0.02);
    let y0 = f, y1 = h - 1 - f, x0 = f, x1 = w - 1 - f;
    // a frame thicker than the 4% trimmed: a row/column that is ALL "subject"
    // at the edge is the frame's band, not the subject — peel it
    const fullR = 0.85 * (w - 2 * f), fullC = 0.85 * (h - 2 * f);
    for (let k = 0; k < 2; k++) {
      while (y0 < y1 && rows[y0] > fullR) y0++;
      while (y1 > y0 && rows[y1] > fullR) y1--;
      while (x0 < x1 && cols[x0] > fullC) x0++;
      while (x1 > x0 && cols[x1] > fullC) x1--;
    }
    while (y0 < h - 1 && rows[y0] < mr) y0++;
    while (y1 > y0 && rows[y1] < mr) y1--;
    while (x0 < w - 1 && cols[x0] < mc) x0++;
    while (x1 > x0 && cols[x1] < mc) x1--;
    if (x1 - x0 >= 3 && y1 - y0 >= 3) box = { x0, y0, x1: x1 + 1, y1: y1 + 1 };
  }
  if (!box) {                                    // no clear ground: trim a thin frame
    box = { x0: f, y0: f, x1: w - f, y1: h - f };
  }
  return { bg, uniform: !!uniform, hasAlpha, box };
}

// a square window on the source, centred on the box: `pad` = contain the box
// (alpha: the ground fills the rest), else cover it (texture: no ground)
function squareOf(box, contain) {
  const bw = box.x1 - box.x0, bh = box.y1 - box.y0;
  const side = contain ? Math.max(bw, bh) * 1.3 : Math.min(bw, bh);   // 1.3: the subject sits inside the feather
  const cx = (box.x0 + box.x1) / 2, cy = (box.y0 + box.y1) / 2;
  return { x: cx - side / 2, y: cy - side / 2, s: side };
}
// box-filtered resample of the window to N×N; outside the picture = `fill`
function resample(rgba, w, h, win, N, fill) {
  const out = new Float32Array(N * N * 4), st = win.s / N;
  const sub = Math.max(1, Math.min(6, Math.ceil(st)));
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
    let r = 0, g = 0, b = 0, a = 0;
    for (let v = 0; v < sub; v++) for (let u = 0; u < sub; u++) {
      const x = Math.floor(win.x + (i + (u + 0.5) / sub) * st), y = Math.floor(win.y + (j + (v + 0.5) / sub) * st);
      if (x < 0 || y < 0 || x >= w || y >= h) { r += fill[0]; g += fill[1]; b += fill[2]; a += fill[3]; continue; }
      const k = 4 * (y * w + x);
      r += rgba[k]; g += rgba[k + 1]; b += rgba[k + 2]; a += rgba[k + 3];
    }
    const q = sub * sub, o = 4 * (j * N + i);
    out[o] = r / q; out[o + 1] = g / q; out[o + 2] = b / q; out[o + 3] = a / q;
  }
  return out;
}

// → Uint8Array N×N: 255 = full ink
export function prepAlpha(rgba, w, h, N = BRUSH_N) {
  const A = analyse(rgba, w, h);
  const px = resample(rgba, w, h, squareOf(A.box, true), N, [...A.bg, A.hasAlpha ? 0 : 255]);
  const bgL = lum(...A.bg);
  const v = new Float32Array(N * N);
  let meanL = 0;
  for (let k = 0; k < N * N; k++) meanL += lum(px[4 * k], px[4 * k + 1], px[4 * k + 2]) / (N * N);
  // the subject's side: away from the ground when there is one, else the minority tone
  const darkInk = A.uniform ? bgL >= 128 : meanL >= 128;
  for (let k = 0; k < N * N; k++) {
    const L = lum(px[4 * k], px[4 * k + 1], px[4 * k + 2]), al = px[4 * k + 3] / 255;
    let t = darkInk ? (A.uniform ? bgL - L : 255 - L) : (A.uniform ? L - bgL : L);
    t = Math.max(0, t);
    // a transparent PNG: its own alpha is the shape; the tones only modulate it
    if (A.hasAlpha) t = al * (0.55 + 0.45 * Math.min(1, t / 128 + 0.3)) * 255;
    v[k] = t;
  }
  // levels: 2nd → 0, 98th → 1 (on the inked part, so a small subject is not crushed)
  const s = Float32Array.from(v).sort();
  const nz = s.filter(x => x > 1);
  const hi0 = pct(nz.length > N ? nz : s, 0.98);
  // the ground's own grain (a table, paper, sensor noise) must not become a
  // haze round the stamp — or bumps all over a ✎³ relief: the floor is where
  // most of the ground sits, never above a third of the ink
  const lo = A.hasAlpha ? 0 : A.uniform ? Math.min(pct(s, 0.35) + 4, 0.3 * hi0) : pct(s, 0.02);
  const hi = Math.max(lo + 8, hi0);
  const out = new Uint8Array(N * N), c = (N - 1) / 2;
  for (let j = 0; j < N; j++) for (let i = 0; i < N; i++) {
    const k = j * N + i, r = Math.hypot(i - c, j - c) / c;
    const t = Math.max(0, Math.min(1, (v[k] - lo) / (hi - lo)));
    out[k] = Math.round(255 * Math.pow(t, 0.9) * (1 - smooth(0.78, 0.99, r)));
  }
  return out;
}

// → Uint8ClampedArray N×N×4 (opaque)
export function prepTexture(rgba, w, h, N = BRUSH_N) {
  const A = analyse(rgba, w, h);
  const px = resample(rgba, w, h, squareOf(A.box, false), N, [...A.bg, 255]);
  const Ls = new Float32Array(N * N);
  for (let k = 0; k < N * N; k++) Ls[k] = lum(px[4 * k], px[4 * k + 1], px[4 * k + 2]);
  const s = Float32Array.from(Ls).sort();
  const lo = pct(s, 0.01), hi = Math.max(lo + 24, pct(s, 0.99));
  const out = new Uint8ClampedArray(N * N * 4);
  for (let k = 0; k < N * N; k++) {
    // a transparent pixel shows the ground it would have been printed on
    const al = px[4 * k + 3] / 255;
    for (let ch = 0; ch < 3; ch++) {
      const c = px[4 * k + ch] * al + A.bg[ch] * (1 - al);
      out[4 * k + ch] = 255 * (c - lo) / (hi - lo);
    }
    out[4 * k + 3] = 255;
  }
  return out;
}

// a tiny stable id for a prepared picture (the same file twice = one brush)
export function brushId(bytes) {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < bytes.length; i += 7) { h ^= bytes[i]; h = Math.imul(h, 16777619) >>> 0; }
  return h.toString(36);
}

// ── browser side ────────────────────────────────────────────────────────
// File → { kind, id, data: JPEG data URL, canvas } — never throws on a
// picture the browser can decode; a file it cannot decode rejects.
export async function loadBrushImage(file, kind) {
  const bmp = await createImageBitmap(file);
  const M = 512, k = Math.min(1, M / Math.max(bmp.width, bmp.height));
  const w = Math.max(1, Math.round(bmp.width * k)), h = Math.max(1, Math.round(bmp.height * k));
  const src = document.createElement('canvas'); src.width = w; src.height = h;
  const sx = src.getContext('2d', { willReadFrequently: true });
  sx.drawImage(bmp, 0, 0, w, h);
  if (bmp.close) bmp.close();
  return fromPixels(sx.getImageData(0, 0, w, h).data, w, h, kind);
}
export function fromPixels(rgba, w, h, kind) {
  const N = BRUSH_N, cv = document.createElement('canvas'); cv.width = cv.height = N;
  const cx = cv.getContext('2d'), img = cx.createImageData(N, N);
  if (kind === 'alpha') {
    const a = prepAlpha(rgba, w, h, N);
    for (let i = 0; i < N * N; i++) { img.data[4 * i] = img.data[4 * i + 1] = img.data[4 * i + 2] = a[i]; img.data[4 * i + 3] = 255; }
  } else img.data.set(prepTexture(rgba, w, h, N));
  cx.putImageData(img, 0, 0);
  const data = cv.toDataURL('image/jpeg', 0.9);
  return { kind, id: kind[0] + brushId(img.data), data, canvas: cv };
}
// a saved brush (its data URL) → the same record
export async function brushFromData(b) {
  const im = new Image(); im.src = b.data; await im.decode();
  const cv = document.createElement('canvas'); cv.width = cv.height = BRUSH_N;
  cv.getContext('2d').drawImage(im, 0, 0, BRUSH_N, BRUSH_N);
  return { kind: b.kind, id: b.id, data: b.data, canvas: cv };
}
