// ▣ Forme → Gabbia → Deforma: the cage that DEFORMS the shape (free-form
// deformation with 8 control points, trilinear).
//
// A shape is drawn from a unit geometry in [−0.5, 0.5]³ scaled by its size. In
// Deforma each of the cage's 8 corners can be moved on its own; the shape
// follows. The data is `ffd` = 8 displacements [dx, dy, dz] of the corners in
// the shape's OWN normalised frame (1 = the shape's size on that axis), in the
// order of the cage's corners: sx, sy, sz ∈ {−1, 1}, nested in that order, so
// corner i has sx = i & 4 ? 1 : −1, sy = i & 2 ? 1 : −1, sz = i & 1 ? 1 : −1.
// A zero (or missing) ffd is the undeformed shape.
//
// A point p of the unit cube moves by Σ wᵢ(p)·dᵢ with the trilinear weights
// wᵢ = Π_a (0.5 + s_a·p_a): 1 at its own corner, 0 at the other seven, so a
// corner moves exactly by its own d and the others stay put. The weight is
// strongest in the corner's own octant and fades linearly to ZERO on the three
// faces that do not touch it — those stay exactly where they were. That fade
// is what makes the body bend smoothly instead of tearing (a 2×2×2 lattice, as
// in any CAD/DCC "lattice" deformer), and what keeps the cage's edges straight
// lines between its corners: an edge of the shape IS an edge of the cage.
//
// The constraint (the same rule as the scale cage): corners never meet nor
// cross. Each corner stays in its OWN octant about the cage's centre, at least
// half the minimum side away from it along every axis — so two corners, which
// always differ in the sign of at least one axis, stay at least the minimum
// apart along that axis.
//
// Pure: no three.js, no DOM — tested in node (tests/ui/ffd.test.cjs).

export const FFD_MAX = 9;          // |corner| in units of the size: the backend refuses |d| ≥ 10

// the sign triple of corner i
export function cornerSign(i) {
  return [i & 4 ? 1 : -1, i & 2 ? 1 : -1, i & 1 ? 1 : -1];
}
export const SIGNS = [0, 1, 2, 3, 4, 5, 6, 7].map(cornerSign);

export function zeroFfd() { return SIGNS.map(() => [0, 0, 0]); }

// is it the undeformed shape? (null / missing counts as undeformed)
export function isDeformed(ffd, eps = 1e-6) {
  if (!ffd) return false;
  for (const d of ffd) for (const v of d) if (Math.abs(v) > eps) return true;
  return false;
}

// a deep copy (null stays null)
export function copyFfd(ffd) { return ffd ? ffd.map(d => [d[0], d[1], d[2]]) : null; }

// where corner i is, in the normalised frame: its rest place ± 0.5 plus its d
export function cornerAt(ffd, i) {
  const s = SIGNS[i], d = ffd ? ffd[i] : [0, 0, 0];
  return [0.5 * s[0] + d[0], 0.5 * s[1] + d[1], 0.5 * s[2] + d[2]];
}
export function corners(ffd) { return SIGNS.map((_, i) => cornerAt(ffd, i)); }

// the trilinear weights of a point of the unit cube
export function weights(x, y, z) {
  const w = new Array(8);
  for (let i = 0; i < 8; i++) {
    const s = SIGNS[i];
    w[i] = (0.5 + s[0] * x) * (0.5 + s[1] * y) * (0.5 + s[2] * z);
  }
  return w;
}

// one point, deformed
export function deformPoint(ffd, p) {
  if (!ffd) return [p[0], p[1], p[2]];
  const w = weights(p[0], p[1], p[2]), o = [p[0], p[1], p[2]];
  for (let i = 0; i < 8; i++) {
    const d = ffd[i];
    o[0] += w[i] * d[0]; o[1] += w[i] * d[1]; o[2] += w[i] * d[2];
  }
  return o;
}

// a whole position array (x, y, z, x, y, z, …), into a NEW array
export function deformPositions(pos, ffd) {
  const out = new Float32Array(pos.length);
  if (!isDeformed(ffd)) { out.set(pos); return out; }
  for (let k = 0; k < pos.length; k += 3) {
    const q = deformPoint(ffd, [pos[k], pos[k + 1], pos[k + 2]]);
    out[k] = q[0]; out[k + 1] = q[1]; out[k + 2] = q[2];
  }
  return out;
}

// Corner i wants to be at `want` (normalised frame). Keep it in its own octant,
// at least minN[a] / 2 from the centre along each axis (minN = the minimum side
// divided by the size, per axis), and within FFD_MAX. Returns the corner's
// place; its d is that minus its rest place.
export function constrainCorner(i, want, minN) {
  const s = SIGNS[i], out = [0, 0, 0];
  for (let a = 0; a < 3; a++) {
    const lo = Math.max(0.5 * (minN ? minN[a] : 0), 0), v = s[a] * want[a];
    out[a] = s[a] * Math.min(Math.max(v, lo), FFD_MAX);
  }
  return out;
}

// move corner i of `ffd` to `want` (constrained); a NEW ffd
export function moveCorner(ffd, i, want, minN) {
  const f = copyFfd(ffd) || zeroFfd(), c = constrainCorner(i, want, minN), s = SIGNS[i];
  f[i] = [c[0] - 0.5 * s[0], c[1] - 0.5 * s[1], c[2] - 0.5 * s[2]];
  return f;
}

// the box of the 8 corners, normalised: { min: [..], max: [..] }. A trilinear
// map keeps every point of the cube inside the convex hull of its corners, so
// this box holds the whole deformed shape.
export function cornerBox(ffd) {
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  for (const c of corners(ffd)) for (let a = 0; a < 3; a++) {
    min[a] = Math.min(min[a], c[a]); max[a] = Math.max(max[a], c[a]);
  }
  return { min, max };
}

// the 12 edges of the cage, as pairs of corner indices
export const CAGE_EDGES = (() => {
  const e = [];
  for (let i = 0; i < 8; i++) for (const b of [4, 2, 1]) if (!(i & b)) e.push([i, i | b]);
  return e;
})();
