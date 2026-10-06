// webui/anticipate.js — the boolean drag anticipation. Pure module, so it runs
// here with the vendored manifold-wasm: what plan() agrees to redo, what it must
// refuse (§6b: anticipating WRONGLY is worse than not anticipating), and that the
// evaluator matches build123d's conventions (align, rotation sign, Subtract).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.resolve(__dirname, '..', '..');
const load = () => import(pathToFileURL(path.join(ROOT, 'webui', 'anticipate.js')).href);
const WASM = pathToFileURL(path.join(ROOT, 'webui', 'vendor', 'manifold-3d-3.5.4', 'manifold.js')).href;

const CATALOG = {
  NumberSlider: { category: 'input', outputs: [{ name: 'result' }] },
  Box: { category: 'primitives_3d', outputs: [{ name: 'result' }] },
  Cylinder: { category: 'primitives_3d', outputs: [{ name: 'result' }] },
  Move: { category: 'transform', outputs: [{ name: 'result' }] },
  Rotate: { category: 'transform', outputs: [{ name: 'result' }] },
  Subtract: { category: 'boolean', outputs: [{ name: 'result' }] },
  Union: { category: 'boolean', outputs: [{ name: 'result' }] },
  Fillet: { category: 'modifiers', outputs: [{ name: 'result' }] },
  OrientForPrint: { category: 'print', outputs: [{ name: 'result' }, { name: 'report' }] },
};

// box - move(cyl) -> union(sweep-ish leaf) -> shown ; the slider drives the cylinder radius
function graph(over = {}) {
  const g = {
    nodes: [
      { id: 's', type: 'NumberSlider', params: { value: 5 } },
      { id: 'box', type: 'Box', params: { width: 20, height: 20, depth: 20, centered: true } },
      { id: 'cyl', type: 'Cylinder', params: { radius: 5, height: 40, arc: 360, centered: true } },
      { id: 'mv', type: 'Move', params: { x: 3, y: 0, z: 0 } },
      { id: 'sub', type: 'Subtract', params: {} },
      { id: 'other', type: 'Fillet', params: { radius: 1 } },
      { id: 'u', type: 'Union', params: {} },
    ],
    connections: [
      { from_node: 's', from_socket: 'result', to_node: 'cyl', to_socket: 'radius' },
      { from_node: 'cyl', from_socket: 'result', to_node: 'mv', to_socket: 'shape' },
      { from_node: 'box', from_socket: 'result', to_node: 'sub', to_socket: 'a' },
      { from_node: 'mv', from_socket: 'result', to_node: 'sub', to_socket: 'b' },
      { from_node: 'sub', from_socket: 'result', to_node: 'u', to_socket: 'shapes' },
      { from_node: 'other', from_socket: 'result', to_node: 'u', to_socket: 'shapes' },
    ],
  };
  return over(g) || g;
}

test('plan: the chain from the dragged node to the screen, with fixed operands', async () => {
  const { plan, modelFromGraph } = await load();
  const p = plan(modelFromGraph(graph(g => g), CATALOG), 'mv', ['u']);
  assert.equal(p.ok, true, p.why);
  assert.deepEqual(p.order, ['mv', 'sub', 'u']);
  assert.deepEqual(p.leaves.sort(), ['box', 'cyl', 'other']);   // the Fillet is an OPERAND, not in the chain
  assert.deepEqual(p.outputs, ['u']);
});

test('plan: a value node drives a primitive through its pin', async () => {
  const { plan, modelFromGraph } = await load();
  const p = plan(modelFromGraph(graph(g => g), CATALOG), 's', ['u']);
  assert.equal(p.ok, true, p.why);
  assert.deepEqual(p.order, ['s', 'cyl', 'mv', 'sub', 'u']);
});

test('plan refuses what it cannot redo faithfully', async () => {
  const { plan, modelFromGraph } = await load();
  const cases = {
    'a Fillet in the chain': g => { g.connections.find(c => c.to_node === 'u' && c.from_node === 'sub').to_node = 'other';
                                    g.connections.find(c => c.to_node === 'other').to_socket = 'part'; },
    'a wired origin': g => { g.nodes.push({ id: 'pt', type: 'NumberSlider', params: { value: 1 } });
                             g.connections.push({ from_node: 'pt', from_socket: 'result', to_node: 'cyl', to_socket: 'origin' }); },
    'an arc < 360': g => { g.nodes.find(n => n.id === 'cyl').params.arc = 180; },
    'a bypassed node': g => { g.nodes.find(n => n.id === 'sub').bypassed = true; },
    'a pin computed upstream': g => { g.connections.find(c => c.to_socket === 'radius').from_node = 'box'; },
  };
  for (const [what, edit] of Object.entries(cases)) {
    const g = graph(g => { edit(g); return g; });
    const dirty = what === 'an arc < 360' || what === 'a wired origin' || what === 'a pin computed upstream' ? 'cyl' : 'mv';
    const target = what === 'a Fillet in the chain' ? ['other'] : ['u'];
    const r = plan(modelFromGraph(g, CATALOG), dirty, target);
    assert.equal(r.ok, false, what);
  }
  // Rotate about its own part centre: the pivot is not reproduced, so refuse
  const g = graph(g => { g.nodes.push({ id: 'rot', type: 'Rotate', params: { angle: 30, axis: 'Z', about: 'part' } });
    g.connections.find(c => c.to_node === 'sub' && c.to_socket === 'b').from_node = 'rot';
    g.connections.push({ from_node: 'mv', from_socket: 'result', to_node: 'rot', to_socket: 'shape' }); return g; });
  assert.equal(plan(modelFromGraph(g, CATALOG), 'mv', ['u']).ok, false, 'Rotate about part');
  // an operand from a second output socket: its preview is not that socket
  const g2 = graph(g => { g.nodes.push({ id: 'ofp', type: 'OrientForPrint', params: {} });
    g.connections.find(c => c.to_socket === 'a').from_node = 'ofp';
    g.connections.find(c => c.to_socket === 'a').from_socket = 'report'; return g; });
  assert.equal(plan(modelFromGraph(g2, CATALOG), 'mv', ['u']).ok, false, 'operand from slot 1');
  // nothing drawn depends on it
  assert.equal(plan(modelFromGraph(graph(g => g), CATALOG), 'mv', ['box']).ok, false, 'not on screen');
});

// a cube in the preview format the engine ships: 4 vertices PER FACE, as OCCT
// tessellates face by face — manifoldFromMesh must weld them into a solid.
function cubeMesh(s, [ox, oy, oz] = [0, 0, 0]) {
  const vertices = [], triangles = [];
  const faces = [[0, 1, 2, 3], [4, 7, 6, 5], [0, 4, 5, 1], [1, 5, 6, 2], [2, 6, 7, 3], [3, 7, 4, 0]];
  const c = [[0,0,0],[s,0,0],[s,s,0],[0,s,0],[0,0,s],[s,0,s],[s,s,s],[0,s,s]].map(([x,y,z]) => [x+ox, y+oy, z+oz]);
  for (const f of faces) {   // wound so the normals point out of the cube (CW seen from outside -> reversed)
    const b = vertices.length; for (const i of f) vertices.push(c[i]);
    triangles.push([b, b + 2, b + 1], [b, b + 3, b + 2]);
  }
  return { vertices, triangles };
}

test('evaluate matches build123d: Subtract of a moved cylinder, align, rotation sign', async () => {
  const { plan, modelFromGraph, loadManifold, manifoldFromMesh, evaluate } = await load();
  const w = await loadManifold(WASM);
  const leaf = manifoldFromMesh(w, cubeMesh(10, [-5, -5, -5]));    // the Fillet operand stands in as a cube
  assert.ok(Math.abs(leaf.volume() - 1000) < 1e-6, 'per-face mesh welded into a closed solid');

  const g = graph(g => g);
  const model = modelFromGraph(g, CATALOG);
  const p = plan(model, 's', ['u']);
  const box = w.Manifold.cube([20, 20, 20], true);
  const res = evaluate(w, p, model, { box, other: leaf });
  // box 20^3 minus a through-cylinder r=5 (48 segments, inscribed), cube operand is inside the hole's box
  const cyl = 0.5 * 48 * 25 * Math.sin(2 * Math.PI / 48) * 20;
  assert.ok(res.u.volume > 8000 - cyl - 1 && res.u.volume < 8000, `volume ${res.u.volume}`);
  assert.equal(res.u.positions.length % 3, 0);
  assert.equal(res.u.normals.length, res.u.positions.length);

  // non-centred cylinder: build123d Align.MIN puts the BBOX min at the origin
  const g2 = graph(g => { g.nodes.find(n => n.id === 'cyl').params.centered = false; return g; });
  const m2 = modelFromGraph(g2, CATALOG);
  const r2 = evaluate(w, plan(m2, 'cyl', ['mv']), m2, {});
  const min = [0, 1, 2].map(k => Math.min(...Array.from({ length: r2.mv.positions.length / 3 }, (_, i) => r2.mv.positions[3 * i + k])));
  assert.deepEqual(min.map(v => +v.toFixed(6)), [3, 0, 0]);         // + the Move's x=3

  // Rotate +90 about X takes +Z to -Y (right hand) — build123d's Rot(90,0,0)
  const g3 = { nodes: [
      { id: 'c', type: 'Cylinder', params: { radius: 1, height: 10, arc: 360, centered: false } },
      { id: 'r', type: 'Rotate', params: { angle: 90, axis: 'X', about: 'world' } }],
    connections: [{ from_node: 'c', from_socket: 'result', to_node: 'r', to_socket: 'shape' }] };
  const m3 = modelFromGraph(g3, CATALOG);
  const r3 = evaluate(w, plan(m3, 'c', ['r']), m3, {});
  const ys = Array.from({ length: r3.r.positions.length / 3 }, (_, i) => r3.r.positions[3 * i + 1]);
  assert.ok(Math.min(...ys) < -9.99 && Math.max(...ys) < 1e-6, `y range ${Math.min(...ys)}..${Math.max(...ys)}`);
  box.delete(); leaf.delete();
});

// soup: a Union nothing cuts downstream is drawn as its operands, overlapped —
// the same visible surface, without the boolean (on raccordo the last Union,
// with the thread, was ~25ms of a ~57ms frame). A Union that IS cut must still
// be fused first, or the Subtract would only cut one of its pieces.
test('evaluate soup: trailing Union unfused, a cut Union still fused', async () => {
  const { plan, modelFromGraph, loadManifold, manifoldFromMesh, evaluate } = await load();
  const w = await loadManifold(WASM);
  const far = manifoldFromMesh(w, cubeMesh(4, [30, 0, 0]));          // disjoint from the box
  const box = w.Manifold.cube([20, 20, 20], true);
  const g = graph(g => g), model = modelFromGraph(g, CATALOG), p = plan(model, 's', ['u']);
  const fused = evaluate(w, p, model, { box, other: far });
  const soup = evaluate(w, p, model, { box, other: far }, { soup: true });
  assert.equal(soup.u.volume, null, 'a soup has no single volume');
  // disjoint pieces: the soup is exactly the fused triangles, as two pieces
  assert.equal(soup.u.index.length, fused.u.index.length);
  const bbox = r => [0, 1, 2].map(k => { const v = Array.from({ length: r.positions.length / 3 }, (_, i) => r.positions[3 * i + k]);
                                           return [Math.min(...v), Math.max(...v)].map(x => +x.toFixed(4)); });
  assert.deepEqual(bbox(soup.u), bbox(fused.u));
  assert.ok(soup.u.index.every(i => i < soup.u.positions.length / 3), 'indices rebased per piece');

  // Union -> Subtract: the cut must see the WHOLE union (box ∪ far) minus a slab through both
  const g2 = { nodes: [
      { id: 'a', type: 'Box', params: { width: 10, height: 10, depth: 10, centered: true } },
      { id: 'b', type: 'Box', params: { width: 10, height: 10, depth: 10, centered: true } },
      { id: 'mb', type: 'Move', params: { x: 20, y: 0, z: 0 } },
      { id: 'u', type: 'Union', params: {} },
      { id: 'cut', type: 'Box', params: { width: 100, height: 100, depth: 2, centered: true } },
      { id: 'sub', type: 'Subtract', params: {} }],
    connections: [
      { from_node: 'b', from_socket: 'result', to_node: 'mb', to_socket: 'shape' },
      { from_node: 'a', from_socket: 'result', to_node: 'u', to_socket: 'shapes' },
      { from_node: 'mb', from_socket: 'result', to_node: 'u', to_socket: 'shapes' },
      { from_node: 'u', from_socket: 'result', to_node: 'sub', to_socket: 'a' },
      { from_node: 'cut', from_socket: 'result', to_node: 'sub', to_socket: 'b' }] };
  const CAT2 = { ...CATALOG, Intersect: { category: 'boolean', outputs: [{ name: 'result' }] } };
  const m2 = modelFromGraph(g2, CAT2), p2 = plan(m2, 'mb', ['sub']);
  assert.equal(p2.ok, true, p2.why);
  const ca = w.Manifold.cube([10, 10, 10], true), cb = w.Manifold.cube([10, 10, 10], true);
  const cut = w.Manifold.cube([100, 100, 2], true);
  assert.deepEqual(p2.leaves.sort(), ['a', 'b', 'cut']);
  const r2 = evaluate(w, p2, m2, { a: ca, b: cb, cut }, { soup: true });
  assert.ok(Math.abs(r2.sub.volume - 2 * (1000 - 200)) < 1e-6, `volume ${r2.sub.volume}`);
  box.delete(); far.delete(); ca.delete(); cb.delete(); cut.delete();
});

test('upstreamSignature: changes with what the operands are made of, not with the dragged value', async () => {
  const { modelFromGraph, upstreamSignature } = await load();
  const base = graph(g => g);
  const sig = g => upstreamSignature(modelFromGraph(g, CATALOG), ['box', 'other']);
  const s0 = sig(base);
  assert.equal(sig(graph(g => { g.nodes.find(n => n.id === 's').params.value = 9; return g; })), s0, 'dragged value');
  assert.equal(sig(graph(g => { g.nodes.find(n => n.id === 'box').params._ui = { width: { min: 0 } }; return g; })), s0, '_ui');
  assert.notEqual(sig(graph(g => { g.nodes.find(n => n.id === 'box').params.width = 21; return g; })), s0, 'operand param');
  assert.notEqual(sig(graph(g => { g.nodes.find(n => n.id === 'other').bypassed = true; return g; })), s0, 'operand bypass');
});
