// ✎ spray and ✎³ filament in /view — the materials for webui/ink.js's arrays.
//
// SPRAY, and why overlapping strokes of one colour come out as ONE even coat.
// Alpha blending accumulates: two soft strokes crossing are darker (more paint)
// where they cross, and a band made of one capsule per segment is darker at
// every joint. What a coat of spray paint looks like is the MAX of the strokes'
// coverage, not their sum — and the framebuffer cannot compute a max of alpha
// (no destination alpha on the canvas). The depth buffer can. Every ink
// fragment writes gl_FragDepth pulled toward the camera by its own coverage
// (a × r, in view-space millimetres), so for each pixel the strongest fragment
// is the nearest. Two passes over the same geometry with the same program:
//   1. depth only (no colour): the depth buffer keeps that nearest = max;
//   2. colour, depthFunc LessEqual, no depth write: only the fragment that won
//      blends — ONE blend per pixel, so no darker crossings, no seams.
// A stroke of ANOTHER colour starts a new RUN (ink.js nextRun) whose whole
// coverage range sits one `runStep` nearer still, and the runs are drawn in
// order (prepass, colour, prepass, colour…): the later colour covers the
// earlier one cleanly and its soft edge blends over it, not over the part.
// The runs are capped (RUN_MAX) so a long note never pulls its last colour
// visibly off the part. Both passes stay in the OPAQUE list (transparent:
// false, CustomBlending still blends) so ink on a piece inside 🔎 glass is in
// three's transmission target as it was, and a 👻 ghost blends over it.
//
// FILAMENT: a lit MeshStandardMaterial (scene lights + the RoomEnvironment
// IBL, tone mapped like the part) on ink.js's tube with a per-vertex shade.
import * as THREE from 'three';
import { sprayArrays, filamentArrays, nextRun, ALPHAS, alphaOf, STAR, IMG } from './ink.js';

export { nextRun, ALPHAS, alphaOf };
export const RUN_MAX = 4;
const runStep = { value: 0.5 };             // mm; ≥ the largest coverage pull (see bump())
const starU = { value: new THREE.Vector3(STAR.spacing, STAR.points, STAR.sharp) };
let rMax = 0;
function bump(r) {
  if (r <= rMax) return;
  rMax = r; runStep.value = 1.15 * rMax;    // a run's pull is a × r ≤ r
}

const VS = /* glsl */`
attribute vec4 ink;
attribute float grain;
attribute vec3 inkColor;
attribute vec2 inkRR;           // r (mm), run
attribute vec2 stamp;           // the segment's start along the stroke (radii), 1 = stars
varying vec4 vInk;
varying vec2 vStamp;
varying float vGrain;
varying vec3 vColor;
varying vec2 vRR;
varying float vViewZ;
varying vec3 vWorld;
varying float vNdv;
void main() {
  vInk = ink; vStamp = stamp; vGrain = grain; vColor = inkColor; vRR = inkRR;
  vec4 w = modelMatrix * vec4(position, 1.0);
  vWorld = w.xyz;
  vec4 mv = viewMatrix * w;
  vViewZ = mv.z;
  vec3 n = normalize(mat3(modelMatrix) * normal);
  vec3 toEye = isOrthographic ? vec3(viewMatrix[0][2], viewMatrix[1][2], viewMatrix[2][2]) : normalize(cameraPosition - w.xyz);
  vNdv = abs(dot(n, toEye));
  gl_Position = projectionMatrix * mv;
}`;
const FS = /* glsl */`
uniform float runStep;
uniform mat4 projectionMatrix;   // three declares it for the vertex stage only
varying vec4 vInk;
varying vec2 vStamp;
varying float vGrain;
varying vec3 vColor;
varying vec2 vRR;
varying float vViewZ;
varying vec3 vWorld;
varying float vNdv;
uniform vec3 star;               // spacing (radii), points, sharpness
uniform float imgSp;             // picture stamps: spacing (radii)
uniform sampler2D alphaMap;      // the picture alpha (grey in .r)
uniform sampler2D colorMap;      // the colour texture
uniform float useTex;
// an n-point star's signed distance (iq), outer radius 1; m in 2…n: 2 = sharpest
float sdStar(vec2 p, float n, float m) {
  float an = 3.141593 / n, en = 3.141593 / m;
  vec2 acs = vec2(cos(an), sin(an)), ecs = vec2(cos(en), sin(en));
  float bn = mod(atan(p.x, p.y), 2.0 * an) - an;
  p = length(p) * vec2(cos(bn), abs(sin(bn)));
  p -= acs;
  p += ecs * clamp(-dot(p, ecs), 0.0, acs.y / ecs.y);
  return length(p) * sign(p.x);
}
float hash(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
void main() {
  float x = vInk.x, L = vInk.z;
  float d = length(vec2(x - clamp(x, 0.0, L), vInk.y));
  // where the colour texture is read: per stamp for stamped tips, else running
  // along the stroke (one tile per diameter, mirrored by the sampler: no seam)
  vec2 tuv = vec2((vStamp.x + x) * 0.5, vInk.y * 0.5 + 0.5);
  float cov = -1.0;                // ≥ 0: a picture stamp's coverage, used as is
  if (vStamp.y > 1.5) {
    // 🖼 the picture stamped along the stroke, as the stars are
    float sp = imgSp, s = vStamp.x + x;
    float k0 = ceil(vStamp.x / sp - 1e-4), k1 = floor((vStamp.x + L) / sp + 1e-4);
    cov = 0.0;
    for (int i = -1; i <= 1; i++) {
      float k = floor(s / sp + 0.5) + float(i);
      if (k < k0 || k > k1) continue;
      vec2 uv = vec2(x - (k * sp - vStamp.x), vInk.y) * 0.5 + 0.5;
      if (any(lessThan(uv, vec2(0.0))) || any(greaterThan(uv, vec2(1.0)))) continue;
      float c = texture2D(alphaMap, vec2(uv.x, 1.0 - uv.y)).r;
      if (c > cov) { cov = c; tuv = uv; }
    }
  } else if (vStamp.y > 0.5) {
    // ★ stars stamped along the stroke at a fixed pace; each one is drawn by the
    // segment its centre falls in (the quad reaches 1 radius past both ends)
    float sp = star.x, s = vStamp.x + x;
    float k0 = ceil(vStamp.x / sp - 1e-4), k1 = floor((vStamp.x + L) / sp + 1e-4);
    float best = 1e3;
    for (int i = -1; i <= 1; i++) {
      float k = floor(s / sp + 0.5) + float(i);
      if (k < k0 || k > k1) continue;
      float a = hash(vec3(k, k * 1.7, 3.1)) * 6.2832;     // each star turned its own way
      vec2 q = vec2(x - (k * sp - vStamp.x), vInk.y);
      q = mat2(cos(a), -sin(a), sin(a), cos(a)) * q;
      float sd = sdStar(q / 0.95, star.y, star.z) * 0.95;
      if (sd < best) { best = sd; tuv = q * 0.5 + 0.5; }
    }
    d = 1.0 + best;              // the star's edge plays the capsule's edge (d = 1)
  }
  float fw = fwidth(d);
  // the fade band: the stroke's own softness, but never so wide that a thin
  // line loses its core (≥ ~1 px solid), never narrower than the antialiasing
  float soft = max(min(vInk.w, 1.0 - 1.2 * fw), min(1.5 * fw, 1.0));
  // spray speckle: a fixed field in model space (two strokes of one coat read
  // the same speckle, so the max of them stays even), only in the fading band
  float g = hash(floor(vWorld / max(vRR.x * 0.22, 1e-4))) - 0.5;
  float a = cov >= 0.0 ? cov : 1.0 - smoothstep(1.0 - soft, 1.0, d + g * soft * 0.35 * vGrain);
  if (a < 0.03) discard;
  // the pull's unit: the stroke's radius, but never under ~what the depth
  // buffer can tell apart at this distance (near = 0.1: its step grows as z²)
  float far = -vViewZ * 2e-3;
  float u = max(vRR.x, far);
  float z = vViewZ + a * u + vRR.y * max(runStep, 1.15 * far);
  float cz = projectionMatrix[2][2] * z + projectionMatrix[3][2];
  float cw = projectionMatrix[2][3] * z + projectionMatrix[3][3];
  gl_FragDepth = clamp((cz / cw) * 0.5 + 0.5, 0.0, 1.0);
  // matte: a little of the surface's own shading, no highlight
  vec3 col = useTex > 0.5 ? texture2D(colorMap, vec2(tuv.x, 1.0 - tuv.y)).rgb : vColor;
  gl_FragColor = vec4(col * (0.8 + 0.2 * vNdv), a);
  #include <colorspace_fragment>
}`;
// ── the pictures (webui/brush-img.js records: {id, kind, canvas}) ─────────
let blank = null;
const blankTex = () => blank || (blank = Object.assign(new THREE.DataTexture(new Uint8Array([255, 255, 255, 255]), 1, 1),
  { needsUpdate: true }));
// a texture per picture, made once and shared by every stroke that uses it;
// mirrored repeat, so a texture running along a stroke never shows a seam
export function brushTexture(b) {
  if (!b) return null;
  if (b.texture) return b.texture;
  const t = new THREE.CanvasTexture(b.canvas);
  t.wrapS = t.wrapT = THREE.MirroredRepeatWrapping;
  if (b.kind === 'tex') t.colorSpace = THREE.SRGBColorSpace;
  t.userData.shared = true;
  return (b.texture = t);
}
// the grey of an alpha picture, for the ✎³ relief (CPU side)
function brushGrey(b) {
  if (b.grey) return b.grey;
  const N = b.canvas.width, d = b.canvas.getContext('2d').getImageData(0, 0, N, N).data;
  const g = new Uint8Array(N * N);
  for (let i = 0; i < N * N; i++) g[i] = d[4 * i];
  // the canvas has v going DOWN; the tube's v goes round — orientation is free
  return (b.grey = { g, n: N });
}
// one material pair per (alpha picture, colour texture) — the same program
// for all of them, so the two passes of one stroke still match bit for bit
const mats = new Map();
function sprayMats(A, T) {
  const key = (A ? A.id : '') + '|' + (T ? T.id : '');
  if (mats.has(key)) return mats.get(key);
  const uniforms = { runStep, star: starU, imgSp: { value: IMG.spacing },
    alphaMap: { value: brushTexture(A) || blankTex() }, colorMap: { value: brushTexture(T) || blankTex() },
    useTex: { value: T ? 1 : 0 } };
  const base = { vertexShader: VS, fragmentShader: FS, uniforms, toneMapped: false, side: THREE.DoubleSide };
  const depth = new THREE.ShaderMaterial({ ...base, colorWrite: false, depthWrite: true,
    depthFunc: THREE.LessEqualDepth });
  const color = new THREE.ShaderMaterial({ ...base, depthWrite: false, depthFunc: THREE.LessEqualDepth,
    blending: THREE.CustomBlending, blendEquation: THREE.AddEquation,
    blendSrc: THREE.SrcAlphaFactor, blendDst: THREE.OneMinusSrcAlphaFactor,
    blendSrcAlpha: THREE.OneFactor, blendDstAlpha: THREE.OneMinusSrcAlphaFactor });
  // the same program for both passes (identical source), so pass 2's depth
  // is bit-for-bit pass 1's and LessEqual lets exactly the winner through
  depth.userData.shared = color.userData.shared = true;
  const m = { depth, color };
  mats.set(key, m);
  return m;
}

// s: a stroke as view.html keeps it (pts/nrm as Vector3, width, color, lift,
// run, pen3d, label?) → an Object3D. Shared materials are never disposed.
export function inkObject(s) {
  const p3 = v => [v.x, v.y, v.z];
  // a picture alpha whose picture is missing (not loaded) falls back to round
  const alpha = s.alpha === 'img' && !s.brushA ? 'normal' : s.alpha;
  const data = { pts: s.pts.map(p3), nrm: s.nrm.map(n => (n ? p3(n) : [0, 0, 1])), width: s.width,
                 lift: s.lift, label: s.label, alpha,
                 relief: alpha === 'img' && s.pen3d ? brushGrey(s.brushA) : null };
  const g = new THREE.Group();
  g.renderOrder = 2;
  if (s.pen3d) {
    const A = filamentArrays(data);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(A.position, 3));
    geo.setAttribute('normal', new THREE.BufferAttribute(A.normal, 3));
    const c = new THREE.Color(s.color), cols = new Float32Array(A.shade.length * 3);
    for (let i = 0; i < A.shade.length; i++) { cols[3 * i] = A.shade[i]; cols[3 * i + 1] = A.shade[i]; cols[3 * i + 2] = A.shade[i]; }
    geo.setAttribute('color', new THREE.BufferAttribute(cols, 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(A.uv, 2));
    geo.setIndex(A.index);
    if (A.relief) {
      // the relief moved the skin: the analytic normals no longer fit. Recompute,
      // then weld the seam (first and last vertex of each ring are one point)
      geo.computeVertexNormals();
      const nm = geo.attributes.normal, S = A.stride, rings = nm.count / S, v = new THREE.Vector3(), w = new THREE.Vector3();
      for (let q = 0; q < rings; q++) {
        const a = q * S, b = a + S - 1;
        v.fromBufferAttribute(nm, a).add(w.fromBufferAttribute(nm, b)).normalize();
        nm.setXYZ(a, v.x, v.y, v.z); nm.setXYZ(b, v.x, v.y, v.z);
      }
    }
    // not tone mapped: ACES turns a saturated green into a pastel one, and the
    // ink's colour is its meaning (red = wrong). Lit values stay under ~1 with
    // these settings, so nothing clips but the specular glint.
    const soft = alphaOf(s, 'normal') === 'soft';        // sfumato: a matte, melted bead
    // a colour texture wraps the tube (white × texture × the baked shade)
    const map = brushTexture(s.brushT);
    const m = new THREE.MeshPhysicalMaterial({ color: map ? 0xffffff : c, map, vertexColors: true, roughness: soft ? 0.75 : 0.5, metalness: 0,
      clearcoat: soft ? 0.25 : 0.7, clearcoatRoughness: soft ? 0.6 : 0.28,       // the glossy skin of extruded PLA: one soft highlight
      envMapIntensity: 0.35, toneMapped: false,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 });
    const mesh = new THREE.Mesh(geo, m); mesh.renderOrder = 2;
    g.add(mesh);
    return g;
  }
  const A = sprayArrays(data);
  bump(A.r);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(A.position, 3));
  geo.setAttribute('normal', new THREE.BufferAttribute(A.normal, 3));
  geo.setAttribute('ink', new THREE.BufferAttribute(A.ink, 4));
  geo.setAttribute('grain', new THREE.BufferAttribute(A.grain, 1));
  geo.setAttribute('stamp', new THREE.BufferAttribute(A.stamp, 2));
  const n = A.position.length / 3, c = new THREE.Color(s.color);
  const col = new Float32Array(n * 3), rr = new Float32Array(n * 2), run = Math.min(s.run || 0, RUN_MAX);
  for (let i = 0; i < n; i++) { col[3 * i] = c.r; col[3 * i + 1] = c.g; col[3 * i + 2] = c.b; rr[2 * i] = A.r; rr[2 * i + 1] = run; }
  geo.setAttribute('inkColor', new THREE.BufferAttribute(col, 3));
  geo.setAttribute('inkRR', new THREE.BufferAttribute(rr, 2));
  geo.setIndex(A.index);
  const M = sprayMats(alpha === 'img' ? s.brushA : null, s.brushT || null);
  const d = new THREE.Mesh(geo, M.depth), f = new THREE.Mesh(geo, M.color);
  d.renderOrder = 100 + 2 * run; f.renderOrder = 101 + 2 * run;
  g.add(d, f);
  return g;
}
// disposeObj() in view.html disposes every material it finds; the spray's are
// shared by every stroke, so they must survive it
export const isShared = m => !!(m && m.userData && m.userData.shared);
