// Boolean drag anticipation — redo the dirty chain in the browser while a slider drags.
//
// The editor already anticipates Move/Rotate/Scale (a matrix on the node's own
// mesh) and the Drop/Animate timelines (§6b). Everything else waited for the
// engine, and on a real part the cost is not the node you touch: on `raccordo`
// a Fillet radius re-runs in 19ms, then the Union and Subtract downstream of it
// take ~800ms, plus ~330ms of meshing and transport per run.
//
// This module redoes that chain on MESHES with manifold-wasm (the same library
// the mesh lane uses in the worker): the inputs the drag cannot change are the
// baked meshes of those nodes (POST /api/graph/{name}/anticipate), and the
// chain from the dragged node to what is on screen is re-evaluated per frame.
// A boolean on the tessellation is the engine's answer up to the tessellation,
// so it honours §6b's rule: anticipate the engine, never a different answer.
// Anything it cannot redo FAITHFULLY (a Fillet, an Extrude, a wired pivot, an
// `origin`, an arc < 360) makes `plan()` refuse, and the caller falls back to
// the plain debounced re-run.
//
// Pure: no three.js, no DOM, no editor state — `plan()` takes a plain graph
// model and `evaluate()` returns typed arrays, so it runs in node too
// (tests/ui/anticipate.test.cjs).

// What each supported node needs. `geo`: geometry sockets (their sources are
// either in the dirty chain or fixed operands). `pins`: numeric params that may
// be wired from a value node. Any OTHER wired socket refuses the plan.
// `ok(params)` refuses settings the evaluator does not reproduce exactly.
const OPS = {
  Move:      { geo: ['shape'], pins: ['x', 'y', 'z'] },
  Rotate:    { geo: ['shape'], pins: ['angle'], ok: p => (p.about || 'world') === 'world' },
  Union:     { geo: ['shapes'] },
  Subtract:  { geo: ['a', 'b'] },
  Intersect: { geo: ['a', 'b'] },
  Box:       { geo: [], pins: ['width', 'height', 'depth'] },
  Cylinder:  { geo: [], pins: ['radius', 'height'], ok: p => +(p.arc ?? 360) >= 360 },
  Sphere:    { geo: [], pins: ['radius'], ok: p => +(p.arc ?? 360) >= 360 },
};
const VALUE_TYPES = new Set(['NumberInput', 'NumberSlider', 'IntegerSlider', 'Integer', 'Number']);

// Segments per full circle for the primitives this module builds itself. The
// baked operands are OCCT's tessellation; this only has to look the same.
const SEGMENTS = 48;

export function isValueNode(n) {
  return !!n && (VALUE_TYPES.has(n.type) || n.category === 'input') &&
         n.params && typeof +n.params.value === 'number' && !Number.isNaN(+n.params.value);
}

// The plain model plan() reads, from a graph as graph.json / toGraphJSON spell
// it and the catalog (type -> NodeDef, for categories and output slots).
export function modelFromGraph(g, catalog) {
  const nodes = {}, typeOf = {};
  for (const n of g.nodes) {
    typeOf[n.id] = n.type;
    nodes[n.id] = { type: n.type, category: catalog[n.type] && catalog[n.type].category,
                    params: { ...(n.params || {}) }, bypassed: !!(n.bypassed || n.bypass),
                    inputs: [] };
  }
  for (const c of g.connections) {
    if (!nodes[c.to_node] || !nodes[c.from_node]) continue;
    const outs = (catalog[typeOf[c.from_node]] || {}).outputs || [];
    const slot = Math.max(0, outs.findIndex(o => o.name === c.from_socket));
    nodes[c.to_node].inputs.push({ name: c.to_socket, from: c.from_node, slot });
  }
  return { nodes };
}

// model = { nodes: { id: { type, category, params, bypassed,
//                          inputs: [{ name, from, slot }] } } }
//   one entry per wired input slot (a `multiple` collector repeats its name);
//   `from` = source node id, `slot` = source output index.
// screen = ids whose preview is drawn right now.
// Returns { ok:true, order, leaves, outputs } or { ok:false, why }.
export function plan(model, dirty, screen) {
  const nodes = model.nodes;
  if (!nodes[dirty]) return { ok: false, why: 'unknown node' };
  const consumers = {};
  for (const [id, n] of Object.entries(nodes))
    for (const inp of n.inputs || []) (consumers[inp.from] ||= []).push(id);

  const down = new Set([dirty]), todo = [dirty];
  while (todo.length) for (const c of consumers[todo.pop()] || [])
    if (!down.has(c)) { down.add(c); todo.push(c); }
  const outputs = [...screen].filter(id => down.has(id));
  if (!outputs.length) return { ok: false, why: 'nothing on screen depends on it' };

  // the chain = what lies between the dragged node and what is drawn
  const chain = new Set(), up = [...outputs];
  while (up.length) {
    const id = up.pop();
    if (chain.has(id) || !down.has(id)) continue;
    chain.add(id);
    for (const inp of nodes[id].inputs || []) up.push(inp.from);
  }

  const leaves = new Set();
  for (const id of chain) {
    const n = nodes[id];
    if (n.bypassed) return { ok: false, why: `${id} is bypassed` };
    if (isValueNode(n)) continue;
    const op = OPS[n.type];
    if (!op) return { ok: false, why: `${n.type} (${id}) cannot be anticipated` };
    if (op.ok && !op.ok(n.params || {})) return { ok: false, why: `${n.type} (${id}): unsupported settings` };
    for (const inp of n.inputs || []) {
      const src = nodes[inp.from];
      if (op.geo.includes(inp.name)) {
        if (chain.has(inp.from)) continue;
        if (!src || isValueNode(src) || inp.slot) return { ok: false, why: `${id}.${inp.name}: unsupported source` };
        leaves.add(inp.from);
      } else if ((op.pins || []).includes(inp.name)) {
        if (!isValueNode(src)) return { ok: false, why: `${id}.${inp.name} is computed upstream` };
      } else {
        return { ok: false, why: `${id}.${inp.name} is wired` };   // origin, offset, pivot…
      }
    }
  }

  // topological order of the chain
  const order = [], seen = new Set();
  const visit = id => {
    if (seen.has(id)) return; seen.add(id);
    for (const inp of nodes[id].inputs || []) if (chain.has(inp.from)) visit(inp.from);
    order.push(id);
  };
  for (const id of chain) visit(id);
  return { ok: true, order, leaves: [...leaves], outputs };
}

// What the fixed operands were baked FROM: every node upstream of `ids`
// (type, params, bypass, wiring), as one string. A leaf is never downstream of
// the dragged node (it would then be in the chain), so a re-bake that changed
// only the dragged value leaves this equal and the fetched operands still hold
// — the next drag of the same node needs no new fetch.
export function upstreamSignature(model, ids) {
  const nodes = model.nodes, seen = new Set(), todo = [...ids];
  while (todo.length) {
    const id = todo.pop();
    if (seen.has(id) || !nodes[id]) continue;
    seen.add(id);
    for (const inp of nodes[id].inputs || []) todo.push(inp.from);
  }
  return JSON.stringify([...seen].sort().map(id => {
    const n = nodes[id], { _ui, ...params } = n.params || {};
    return [id, n.type, n.bypassed, params, n.inputs];
  }));
}

// ── manifold ────────────────────────────────────────────────────────────────

let _wasm = null;
export async function loadManifold(url = '/static/vendor/manifold-3d-3.5.4/manifold.js') {
  if (!_wasm) _wasm = (async () => {
    const Module = (await import(url)).default;
    const w = await Module(); w.setup();
    return w;
  })();
  return _wasm;
}

// Sharp-edge angle for the normals: below it a vertex is smoothed, above it
// split, which is how the baked OCCT preview looks (smooth within a face).
const SHARP_DEG = 30;

// A baked preview mesh ({vertices:[[x,y,z]…], triangles:[[a,b,c]…]}) → Manifold.
// OCCT shares the edge discretisation between adjacent faces, so welding by
// position (Mesh.merge) closes it. Throws if it is not a closed solid.
export function manifoldFromMesh(w, mesh, { sharpDeg = SHARP_DEG } = {}) {
  const v = mesh.vertices, t = mesh.triangles;
  const vp = new Float32Array(v.length * 3), tv = new Uint32Array(t.length * 3);
  for (let i = 0; i < v.length; i++) { vp[3*i] = v[i][0]; vp[3*i+1] = v[i][1]; vp[3*i+2] = v[i][2]; }
  for (let i = 0; i < t.length; i++) { tv[3*i] = t[i][0]; tv[3*i+1] = t[i][1]; tv[3*i+2] = t[i][2]; }
  const m = new w.Mesh({ numProp: 3, vertProperties: vp, triVerts: tv });
  m.merge();
  const man = new w.Manifold(m);
  const st = man.status();
  if (st !== 'NoError') { man.delete(); throw new Error(`operand is not a closed solid (${st})`); }
  return man;
}

// Re-evaluate the chain with the CURRENT params. `leaves`: id → Manifold
// (owned by the caller, kept across frames). Returns
// { id: { positions, normals, index, volume } } for every output, and frees
// every Manifold it made.
//
// `soup: true` — for an OPAQUE, non-wireframe preview only — leaves a Union
// that nothing downstream cuts as a list of its operands, drawn overlapped.
// The visible surface of A ∪ B is exactly what A and B drawn together show,
// so on screen it is the same answer; only the hidden inner faces differ,
// which is why glass and wireframe must not use it (and `volume` is then
// null). It is a large share of the frame: on raccordo the last Union, with
// the helical thread, is ~25ms of ~57. A Union that IS cut downstream (a
// Subtract, an Intersect) is fused there, at the same cost as before.
export function evaluate(w, p, model, leaves, { sharpDeg = SHARP_DEG, soup = false } = {}) {
  const { Manifold } = w;
  const env = {}, made = [];
  const own = m => { made.push(m); return m; };
  const nodes = model.nodes;
  // a value is a Manifold, or (soup mode) { parts: [Manifold…] } still unfused
  const isSoup = v => !!(v && v.parts);
  const fuse = v => isSoup(v) ? (v.parts.length === 1 ? v.parts[0] : own(Manifold.union(v.parts))) : v;
  const each = (v, f) => isSoup(v) ? { parts: v.parts.map(m => own(f(m))) } : own(f(v));
  try {
    for (const id of p.order) {
      const n = nodes[id], prm = n.params || {};
      if (isValueNode(n)) { env[id] = +prm.value; continue; }
      const val = k => {
        const inp = (n.inputs || []).find(i => i.name === k);
        return inp ? +(env[inp.from] ?? nodes[inp.from].params.value) : +prm[k];
      };
      const geo = k => (n.inputs || []).filter(i => i.name === k)
        .map(i => env[i.from] ?? leaves[i.from]).filter(Boolean);
      const one = k => { const g = geo(k); if (g.length !== 1) throw new Error(`${id}.${k}: expected one shape`); return g[0]; };
      const solid = k => fuse(one(k));
      const all = list => { list = list.map(fuse); return list.length === 1 ? list[0] : own(Manifold.union(list)); };
      let out;
      switch (n.type) {
        case 'Move': {
          const d = [val('x') || 0, val('y') || 0, val('z') || 0];
          out = each(one('shape'), m => m.translate(d)); break;
        }
        case 'Rotate': {
          const a = val('angle') || 0, ax = (prm.axis || 'Z').toUpperCase();
          const r = [ax === 'X' ? a : 0, ax === 'Y' ? a : 0, ax === 'Z' ? a : 0];
          out = each(one('shape'), m => m.rotate(r)); break;
        }
        case 'Union': {
          const g = geo('shapes');
          out = soup ? { parts: g.flatMap(v => isSoup(v) ? v.parts : [v]) } : all(g); break;
        }
        case 'Subtract': { const b = geo('b'); out = b.length ? own(solid('a').subtract(all(b))) : one('a'); break; }
        case 'Intersect': out = own(solid('a').intersect(solid('b'))); break;
        // build123d align: centred = CENTER on every axis, else MIN (bbox min at the origin)
        case 'Box': {
          const s = [val('width'), val('height'), val('depth')], c = prm.centered !== false;
          out = own(Manifold.cube(s, c)); break;
        }
        case 'Cylinder': {
          const r = val('radius'), h = val('height'), c = prm.centered !== false;
          const cyl = own(Manifold.cylinder(h, r, r, SEGMENTS, c));
          out = c ? cyl : own(cyl.translate([r, r, 0])); break;
        }
        case 'Sphere': out = own(Manifold.sphere(val('radius'), SEGMENTS)); break;
        default: throw new Error(`${n.type} has no evaluator`);
      }
      env[id] = out;
    }
    const res = {};
    for (const id of p.outputs) {
      const v = env[id];
      const parts = isSoup(v) ? v.parts : [v];
      const arrs = parts.map(m => {
        if (!m || typeof m.status !== 'function') throw new Error(`${id}: no shape`);
        const st = m.status();
        if (st !== 'NoError') throw new Error(`${id}: ${st}`);
        return toArrays(own(m.calculateNormals(0, sharpDeg)));
      });
      res[id] = concat(arrs);
      res[id].volume = isSoup(v) && parts.length > 1 ? null : parts[0].volume();
    }
    return res;
  } finally {
    for (const m of made) m.delete();
  }
}

function concat(arrs) {
  if (arrs.length === 1) return arrs[0];
  let nv = 0, ni = 0;
  for (const a of arrs) { nv += a.positions.length; ni += a.index.length; }
  const positions = new Float32Array(nv), normals = new Float32Array(nv), index = new Uint32Array(ni);
  let ov = 0, oi = 0;
  for (const a of arrs) {
    positions.set(a.positions, ov); normals.set(a.normals, ov);
    const base = ov / 3;
    for (let i = 0; i < a.index.length; i++) index[oi + i] = a.index[i] + base;
    ov += a.positions.length; oi += a.index.length;
  }
  return { positions, normals, index };
}

// Manifold (with normals in properties 3..5) → flat typed arrays for a BufferGeometry.
function toArrays(m) {
  const mesh = m.getMesh(0), np = mesh.numProp, vp = mesh.vertProperties;
  const nv = vp.length / np;
  const positions = new Float32Array(nv * 3), normals = new Float32Array(nv * 3);
  for (let i = 0; i < nv; i++) for (let k = 0; k < 3; k++) {
    positions[3*i+k] = vp[np*i+k];
    normals[3*i+k] = np >= 6 ? vp[np*i+3+k] : 0;
  }
  return { positions, normals, index: new Uint32Array(mesh.triVerts) };
}
