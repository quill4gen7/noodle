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
import { sprayArrays, filamentArrays, nextRun } from './ink.js';

export { nextRun };
export const RUN_MAX = 4;
const runStep = { value: 0.5 };             // mm; ≥ the largest coverage pull (see bump())
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
varying vec4 vInk;
varying float vGrain;
varying vec3 vColor;
varying vec2 vRR;
varying float vViewZ;
varying vec3 vWorld;
varying float vNdv;
void main() {
  vInk = ink; vGrain = grain; vColor = inkColor; vRR = inkRR;
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
varying float vGrain;
varying vec3 vColor;
varying vec2 vRR;
varying float vViewZ;
varying vec3 vWorld;
varying float vNdv;
float hash(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
void main() {
  float x = vInk.x, L = vInk.z;
  float d = length(vec2(x - clamp(x, 0.0, L), vInk.y));
  float fw = fwidth(d);
  // the fade band: the stroke's own softness, but never so wide that a thin
  // line loses its core (≥ ~1 px solid), never narrower than the antialiasing
  float soft = max(min(vInk.w, 1.0 - 1.2 * fw), min(1.5 * fw, 1.0));
  // spray speckle: a fixed field in model space (two strokes of one coat read
  // the same speckle, so the max of them stays even), only in the fading band
  float g = hash(floor(vWorld / max(vRR.x * 0.22, 1e-4))) - 0.5;
  float a = 1.0 - smoothstep(1.0 - soft, 1.0, d + g * soft * 0.35 * vGrain);
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
  gl_FragColor = vec4(vColor * (0.8 + 0.2 * vNdv), a);
  #include <colorspace_fragment>
}`;
let mats = null;
function sprayMats() {
  if (mats) return mats;
  const base = { vertexShader: VS, fragmentShader: FS, uniforms: { runStep }, toneMapped: false, side: THREE.DoubleSide };
  const depth = new THREE.ShaderMaterial({ ...base, colorWrite: false, depthWrite: true,
    depthFunc: THREE.LessEqualDepth });
  const color = new THREE.ShaderMaterial({ ...base, depthWrite: false, depthFunc: THREE.LessEqualDepth,
    blending: THREE.CustomBlending, blendEquation: THREE.AddEquation,
    blendSrc: THREE.SrcAlphaFactor, blendDst: THREE.OneMinusSrcAlphaFactor,
    blendSrcAlpha: THREE.OneFactor, blendDstAlpha: THREE.OneMinusSrcAlphaFactor });
  // the same program for both passes (identical source), so pass 2's depth
  // is bit-for-bit pass 1's and LessEqual lets exactly the winner through
  depth.userData.shared = color.userData.shared = true;
  return (mats = { depth, color });
}

// s: a stroke as view.html keeps it (pts/nrm as Vector3, width, color, lift,
// run, pen3d, label?) → an Object3D. Shared materials are never disposed.
export function inkObject(s) {
  const p3 = v => [v.x, v.y, v.z];
  const data = { pts: s.pts.map(p3), nrm: s.nrm.map(n => (n ? p3(n) : [0, 0, 1])), width: s.width,
                 lift: s.lift, label: s.label };
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
    geo.setIndex(A.index);
    // not tone mapped: ACES turns a saturated green into a pastel one, and the
    // ink's colour is its meaning (red = wrong). Lit values stay under ~1 with
    // these settings, so nothing clips but the specular glint.
    const m = new THREE.MeshPhysicalMaterial({ color: c, vertexColors: true, roughness: 0.5, metalness: 0,
      clearcoat: 0.7, clearcoatRoughness: 0.28,       // the glossy skin of extruded PLA: one soft highlight
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
  const n = A.position.length / 3, c = new THREE.Color(s.color);
  const col = new Float32Array(n * 3), rr = new Float32Array(n * 2), run = Math.min(s.run || 0, RUN_MAX);
  for (let i = 0; i < n; i++) { col[3 * i] = c.r; col[3 * i + 1] = c.g; col[3 * i + 2] = c.b; rr[2 * i] = A.r; rr[2 * i + 1] = run; }
  geo.setAttribute('inkColor', new THREE.BufferAttribute(col, 3));
  geo.setAttribute('inkRR', new THREE.BufferAttribute(rr, 2));
  geo.setIndex(A.index);
  const M = sprayMats();
  const d = new THREE.Mesh(geo, M.depth), f = new THREE.Mesh(geo, M.color);
  d.renderOrder = 100 + 2 * run; f.renderOrder = 101 + 2 * run;
  g.add(d, f);
  return g;
}
// disposeObj() in view.html disposes every material it finds; the spray's are
// shared by every stroke, so they must survive it
export const isShared = m => !!(m && m.userData && m.userData.shared);
