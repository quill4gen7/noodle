# noodle — agent guide

A **node-based parametric CAD app** built on [build123d](https://build123d.readthedocs.io).
You wire nodes in a web editor; the backend **transpiles the graph to build123d
Python**, runs it in an isolated worker, and returns an STL + a mesh "view" for
the 3D viewport. It ships with an **in-app AI copilot** (natural language → graph)
and an **MCP server** exposing the same operations.

This file is the orientation doc for an AI agent picking up the project. It
covers what it is, how to run it, how it's laid out, and how to change it safely.

---

## 1. Run it

The app is containerized (`noodle` service). The image is **build123d-only**
on purpose — cadquery 2.7.0 pins an OCP/OCCT build that conflicts with build123d
(see `PLAN_NODE_CAD.md` and the memory note "build123d/cadquery OCP conflict").

```bash
docker compose up -d --build      # build + start
docker restart noodle         # after backend code changes (see §6)
docker logs -f noodle         # tail logs
```

- Node editor: <http://localhost:8090/nodes>   ·   code view: `/ui` (read-only
  build123d generated from the graph)   ·   health: `/health`
- The container runs as the non-root user **noodle (uid 1000)**, Python 3.10,
  serving `uvicorn server:app` on 8090.
- Volumes (see `docker-compose.yml`): `./projects` is read-write; `cad_nodes/`,
  `webui/`, `server.py`, `mcp_server.py` are mounted **read-only**, so
  host edits are visible to the container but the running process must be
  restarted to re-import them.

**Copilot LLM backend** (env in `docker-compose.yml`): defaults to a free local
**Ollama** at `host.docker.internal:11434` (`COPILOT_MODEL`, e.g. `qwen2.5`).
For a keyed OpenAI-compatible provider instead, set `COPILOT_BASE_URL` +
`COPILOT_API_KEY` + `COPILOT_MODEL` (Groq / OpenRouter / Gemini's OpenAI endpoint…).

## 2. Develop & verify without Docker

There is a host venv `.venv-b123d/` with build123d installed. Use it to transpile
and execute a graph directly — the fastest way to verify an engine change:

```bash
.venv-b123d/bin/python - <<'PY' 2>/dev/null   # 2>/dev/null hides fontconfig noise
import json, pathlib
from cad_nodes.graph import Graph
from cad_nodes.transpiler import transpile
from cad_nodes.executor import execute_graph
g = Graph.from_dict(json.loads(pathlib.Path("projects/<name>/graph.json").read_text()))
print(transpile(g))                                   # inspect generated build123d source
view = execute_graph(g, pathlib.Path("/tmp/work"), timeout=60).get("view")
print(view["success"], view.get("node_errors"))       # per-node errors if any
PY
```

Or drive the live server: `curl -s -X POST localhost:8090/api/graph/<name>/execute`.

## 3. Architecture & file map

```
server.py            FastAPI HTTP API (port 8090). Routes under /api/* :
                       projects list/delete, /api/graph/{name}/execute|code,
                       /api/graph/{name}/code?map=1 (code + editable param
                       source map), PATCH /api/graph/{name}/param (clamped
                       single-param edit; `_cb.<name>` targets a CodeBlock
                       override), /api/graph/{name}/codeblock/{id}/scan,
                       /api/nodes (catalog), /api/copilot/chat|status,
                       /api/aliases (GET the personal add-node search aliases)
                       + PUT /api/aliases/{node_type} (replace one node's, []
                       clears) — stored in projects/_aliases.json, see §6,
                       /api/agent/help (self-contained remote-agent guide =
                       cad_nodes/AGENT_HELP.md, also MCP cad_help/cad://help —
                       keep it in sync when the API surface changes),
                       /api/agent/tags (ToAgent provenance index, §7b),
                       /api/graph/{name}/slice_summary|section_outline (§7b),
                       /api/graph/{name}/screenshot (PNG of the viewport, §9 —
                       the agent's eyes; also MCP cad_screenshot),
                       /api/graph/{name}/progress (SSE: per-node execution events
                       of the run in flight, tailed from the workdir's
                       progress.jsonl — see transpiler `_ev`),
                       /api/system/health|logs|restart.
                       NOTE /execute runs via `asyncio.to_thread`: a graph run is
                       seconds of blocking CPU and must NOT hold the event loop,
                       or nothing else can be served meanwhile (the progress
                       stream included). Keep any new long route off the loop.
mcp_server.py        MCP server exposing the same cad_nodes.api operations.
webui/
  viewer.js          ★ the SHARED Three.js viewport (ES module served at
                       /static/viewer.js), imported by BOTH pages. `CadViewer`
                       owns the Z-up CAD scene (grid/lights/ViewHelper), the
                       animate loop, framing/resize, `loadSTL`, and the live
                       multi-mesh `renderPreviews(previews, {colorOf,wireOf,
                       onEmpty})` from a view.json. Page-specific behaviour stays
                       in the pages and hooks onto the exposed scene/camera/
                       previewGroup (nodes.html: the gizmo + click-to-select via
                       viewer.pick()). Both pages now render identically.
                       RENDERING (`makeMaterial`, HQ toggle in Settings): filmic
                       tone mapping + a RoomEnvironment IBL, and a per-node
                       `finish` — solid / glass (real `transmission`, not alpha) /
                       emissive / metal — plus `rainbow` (a hue per piece via the
                       golden angle, as geometry groups sharing one buffer).
                       SELECTIVE BLOOM, and it has to be selective: `transmission`
                       is not blending — three renders the scene to its own target
                       and samples it refracted, so an emitter seen THROUGH glass
                       arrives attenuated, falls under any luminance threshold, and
                       a bloom on the finished image stops dead at the glass. So
                       emitters get `GLOW_LAYER` (markGlow), render ALONE on black
                       (background nulled, or the clear colour blooms too), blur at
                       half res with threshold 0, and are composited ADDITIVELY on
                       top — which is what makes the glow cross the glass and
                       spread. Two things that bit: the glow target holds LINEAR
                       un-tone-mapped values (three tone maps only to the canvas)
                       and UnrealBloomPass returns emitters+blur, so the quad is
                       scaled down or the core blows white twice over; and
                       `emissiveIntensity` must stay BELOW 1 — past it ACES
                       desaturates the highlight and the glowing body goes flat
                       white. No refraction of the glow and no caustics: those need
                       rays. Costs ~0-1fps (glass dominates); off unless HQ and
                       something declares itself emissive.
  index.html         the `/ui` code view — generated build123d source (read-only
                       text) + STL preview. Parameter literals are highlighted
                       and click-to-edit via a terminal-style inline editor that
                       PATCHes the graph param and re-renders — non-destructive
                       (the code is regenerated; structure stays in nodes.html).
                       See PLAN_CODE_PARAMS.md.
  nodes.html         the node editor + 3D viewer (litegraph-style). Holds
                       WIRE_COLORS; INPUT_ACCEPTS is fetched at boot from
                       /api/wiretypes (derived from casts.py, §5 — the inline
                       literal is only an offline fallback).
                       Execution glow (beginExecGlow/glowEvent/drawExecGlow): the nodes
                       light up AS THEY RUN. openProgress() subscribes to the SSE
                       /api/graph/{name}/progress BEFORE POSTing /execute, so no start
                       event is missed; each event opens or closes a node's span. A
                       node executing right now breathes amber; when it finishes it
                       settles and fades — green if it really recomputed, cold blue if
                       the memo cache served it, red if it threw.
                       Cost badges (drawCostBadge, toolbar "Costi" toggle, remembered
                       in localStorage `noodle:settings:showCost`): the same story made
                       to stay — last run's wall-clock on each node's title bar, same
                       colour vocabulary (blue "cache" = the memo store served it and it
                       cost nothing, amber→green = a real recompute with the hue set by
                       cost, red = it threw). The editor doubles as a profiler.
                       parseCbParams() mirrors transpiler.parse_codeblock_params:
                       a CodeBlock's `#@param`s become live widgets + dynamic
                       input sockets (overrides in the `_cb` param namespace),
                       editable via the ✎ Edit code modal. A BroadcastChannel
                       ('noodle:link') cross-links the two views: clicking a
                       value in /ui selects+flashes the node here; selecting a
                       node here scrolls /ui to it (nodeByGraphId tracks on-disk
                       ids). The /ui code view also scrubs numbers by drag,
                       Tab-cycles spans, and Ctrl+Z-undoes param edits.
cad_nodes/
  catalog.py         ★ the node registry. Declarative NodeDef per node type:
                       sockets (typed wires), params (widgets+defaults), and a
                       code_template that the transpiler fills in. ADD NODES HERE.
  casts.py           ★ wire types + the cast registry (§5) — the ONE place wire
                       compatibility is defined; WIRE_COMPATIBLE (backend) and
                       INPUT_ACCEPTS (frontend, via /api/wiretypes) derive from it.
  transpiler.py      ★ Graph -> build123d source. Flat "algebra" assignments in
                       topo order; group nodes (BuildPart/BuildSketch) emit
                       nested `with` blocks. PREAMBLE injects runtime helpers
                       (_at, _pushpull, _section, _bbox_plane, _rotate,
                       _select_subshapes, _reanchor). Each node is wrapped in
                       try/except so one failing node is recorded in __errors__,
                       not fatal.
                       TRAP, paid for: build123d's algebra-mode fillet()/chamfer()
                       take NO target — they read `objects[0].topo_parent`, which
                       after a boolean still names the PRE-boolean operand. Round a
                       corner of a Union and you silently get that operand back,
                       the rest of the part deleted and no error raised. So every
                       rounding node passes its `part` EXPLICITLY and _reanchor()
                       re-points the picks at it (in place — chamfer()'s 2D branch
                       matches picks by TShape identity, so copies match nothing).
                       Never call bare fillet()/chamfer() in a code_template.
                       run(emit_map=True) / transpile_with_map() also return a
                       param<->code source map (sentinel-wrapped literals measured
                       on the final text) for the editable code view. A CodeBlock
                       transpiles like two connected nodes: `#@param` decls
                       (parse_codeblock_params) become the generated function's
                       named ARGUMENTS — body stays pure (declaration lines dropped),
                       each value appears once at the call site as an editable span
                       (override in node.params["_cb"], wired socket drives + fans
                       out). The body itself is an editable `code` span (kind=code).
                       transpile(memo=True) — the execute path ONLY, /ui code stays
                       clean — wraps each cacheable node in _memo_get/_memo_put
                       keyed by a content hash (params+code+upstream keys, immune
                       to var renumbering); non-deterministic nodes (Import*,
                       open(), random.) poison their lineage, display/export
                       side-effect nodes stay keyed but re-run. tests/test_memo.py.
                       In memo mode each node also brackets itself in `_ev()`
                       (PREAMBLE): a start/end NDJSON line appended+flushed to
                       __PROGRESS_PATH__ = the workdir's progress.jsonl (injected by
                       executor.build_script). That file is the ONLY progress channel
                       that works on BOTH paths — the warm worker redirects stdout
                       into a buffer during exec, and the cold subprocess has no pipe
                       home at all. The editor tails it over SSE and lights each node
                       AS IT RUNS.
  executor.py        Runs the generated script in a worker subprocess; captures
                       STL + view JSON + per-node errors. execute_graph(graph, workdir).
  worker.py / mesh_extractor.py   the subprocess + meshing. The warm worker owns
                       the persistent __MEMO__ store (LRU 256: node outputs,
                       preview meshes, view stats) — on a repeat run only the
                       dirty subtree re-executes/re-meshes (~8.5s -> ~0.5s on the
                       lego brick; cache dies with the worker = ⚙ warm toggle).
                       Live-path bboxes use optimal=False (~2s -> ~5ms each,
                       ≤1% oversized, view.bbox carries approx:true); exports
                       and picker signatures keep exact geometry.
  graph.py           Graph/Node/Connection dataclasses; from_dict/to_dict;
                       validate() (raises on incompatible wires / unknown sockets).
  api.py             High-level ops over a GraphStore: add_node, connect,
                       set_param, delete_node, execute, transpile. Shared by the
                       MCP server AND the copilot — the single source of truth.
  store.py           GraphStore: load/save projects/<name>/{graph,meta,view}.json
                       and output.stl.
  copilot.py         ★ in-app NL copilot (§7). OpenAI-compatible tool loop.
  screenshot.py      ★ the agent's EYES (§9): renders the viewport to a PNG by
                       driving headless Chromium over this server's own /nodes
                       page — the REAL viewer.js, so the picture an agent sees is
                       the picture the user sees. Warm browser (the cost is the
                       launch, not the frame). Camera presets + azim/elev/zoom,
                       frame-one-node, ortho. NOT a second renderer, on purpose.
  slice_summary.py   retro-engineering perception (§7b): slice_summary
                       (symbolic cross-sections; STEP exact, STL arc-fitted)
                       + section_outline (one exact section, edge by edge).
  toposort.py        topological sort + cycle detection.
  catalog … examples/  sample graphs used by tests.
projects/            saved graphs (written as uid 1000 — host-editable).
tests/               test_engine.py, test_api.py — pure-Python (no build123d).
PLAN_NODE_CAD.md     the full design doc + node roadmap (~150 planned nodes).
PLAN_THREADS.md      the Thread node (§5g): why threads are triangles, the four
                     profile families, and the clearance measurements.
PLAN_VIZ_ALGORITHMS.md  the "algorithms as geometry" example family (softmax,
                     gradient descent, determinant, CLT, Fourier, k-means…): the
                     pattern they share, the idioms, the gotchas, and what's next.
                     Read it before adding an explanatory example.
```

## 4. Data model

A project is `projects/<name>/graph.json`:

```jsonc
{
  "name": "demo",
  "nodes": [
    { "id": "n1", "type": "Sphere", "params": {"radius": 3},
      "position": [120,80], "preview": true }      // preview: per-node eye override
  ],
  "connections": [
    { "id": "l1", "from_node": "n1", "from_socket": "result",
      "to_node": "n2", "to_socket": "shape" }
  ],
  "groups": [                                        // optional, editor-only
    { "title": "Base body", "bounding": [20,40,300,760], "color": "#3f589e" }
  ]
}
```

`groups` are LiteGraph group boxes that visually cluster nodes (title + bounding
rect + colour). They're **editor-only metadata**: the engine never reads them, but
`Graph` carries them through `to_dict`/`from_dict` so logical grouping survives
save/reload and api/copilot round-trips. Serialized/restored in `nodes.html`
(`toGraphJSON`/`fromGraphJSON`); created with Ctrl+G (`groupSelected`).

`preview` is the per-node eye: `True`/`False` force it, absent = auto (draw only
terminal geometry nodes). **Every emit path must call `Transpiler._previewed`** —
it is the ONE gate, and an emit path that forgets it leaves the eye wired to
nothing, silently, for every node of that type (that was feedback
20260718-164854: selectors, CenterOfMass, TraceImage, OrientForPrint and
bypassed nodes all had dead toggles). Selectors are the special case: their
output wire is `selection`/`data`, which says how they may be WIRED, not what
they hold — at run time it is drawable sub-shapes, so they honour an EXPLICIT
eye but never auto-draw (a graph is full of wired selectors; drawing them all
unasked buries the part). `mesh_extractor._preview_geom` then dispatches on the
runtime VALUE, not the declared type: points → dots, solid/sketch → mesh, curve
→ polylines.

`params._ui` is another editor-only namespace (like CodeBlock's `_cb`): per-slider
drag window + step set via the slider's ⚙ (`{param: {min,max,step}}`). Sliders in
the editor are a custom `cadslider` widget — the drag window defaults to ±10
(clipped to catalog hard bounds, auto-grown to contain the value) and drag snaps
to the step; the typed ✎ field clamps only on the catalog's hard min/max. The
engine resolves params by catalog name, so it never sees `_ui`.

**PLAY** — a ▶ hotspot left of the ⚙ sweeps the param across its drag window on a
clock; speed (sweeps/second) and mode (`once` / `loop` / `pingpong`) live in the
same namespace, `_ui[param].play`. It drives the value through `w.callback`, i.e.
through the exact path a hand on the slider uses, so undo, the ✎ field and the
gizmo snapshots all behave as if it were being dragged — and that is also why it
composes with Live at no cost: `scheduleLive()`'s 120ms debounce is reset every
frame and never fires, so an anticipatable node (§6b) replays locally at 60fps
and exactly ONE exact re-bake lands when play stops (measured: 0 runs during a
sweep, 1 after). A node no fast path can anticipate would then show nothing until
stop, so play instead **pumps** the engine — one run at a time, the next starting
only when the last has landed, because `runGraph()` aborts whatever is in flight
and overlapping calls would starve every run but the last (measured: ~1.1 runs/s,
0 overlapping). The playing state is deliberately transient: never saved, and
loading a graph or deleting a node stops it.

Execution writes `output.stl` + `view.json` (meshes) alongside it.

## 5. Wire types

Typed wires gate which output may feed which input. The single source of truth
is **`cad_nodes/casts.py`** (see PLAN_DATA_PROTOCOL.md): a small cast registry
`CASTS[(src, dst)] -> coercion helper` from which both tables are **derived** —
`WIRE_COMPATIBLE` (backend, enforced hard by `Graph.validate()`) and
`INPUT_ACCEPTS` (frontend, fetched at boot from `/api/wiretypes`; the literal in
`nodes.html` is only a fallback for when the endpoint is absent). They can no
longer drift — to change compatibility, edit `casts.py` only.

Types (constants in `casts.py`): `solid` (3D B-Rep — the old `geometry`),
`surface` (2D sketch/face — the old `sketch`), `curve`, `plane`, `vector`
(points), `selection` (picked sub-shapes), `mesh` (triangles — §5c), `data`,
`tree` (declared, unused).
`data` is the universal bus (any output → a `data` input; a `data` output feeds
everything except `selection`/`tree`). Registered casts: `surface→solid`,
`solid↔plane` (transforms treat a plane like geometry), `curve→surface`
(closed curve → face via `_face`), `selection→vector`, `solid/surface→mesh`
(`_to_mesh`). A `Socket` can also widen per-socket via `accepts=[…]`, carry an
advisory `subtype` (legend only, not validation), or set `raw=True` to opt out of
automatic boundary casts. The transpiler applies a registered cast automatically
at the wire boundary (`Transpiler._cast`).

## 5c. The mesh lane (triangles)

**build123d cannot model meshes** — it treats them as an I/O format. `import_stl`
returns a `Face` with only a triangulation and no surface (`is_valid=False`,
`volume=0`, booleans refused outright); `Mesher.read` sews every triangle into a
planar B-Rep face — **300s** to open a 147k-triangle STL, **81s** per boolean. And
OCCT has no remesh/decimate/mesh-repair at all. So triangles get their own lane,
on **trimesh** (MIT — noodle stays MIT; pymeshlab is GPL-3 and is deliberately NOT
a dependency). Full findings + measurements: **`PLAN_MESH_LANE.md`**.

- Nodes (category `mesh`, `catalog.py` §12b): `ImportMesh`, `ToMesh`, `MeshFix`
  (merge verts, drop dup/degenerate faces + stray shards, fill holes, fix normals),
  `MeshInspect` (text health report → wire into a `Display`), `ExportMesh`,
  `MeshUnion`/`MeshSubtract`/`MeshIntersect` + `MeshSimplify` (**manifold3d**,
  Apache-2.0), and `MeshToSolid` — the only bridge back, guarded by `max_tris`.
- **Two engine gotchas, both load-bearing** (measured — PLAN_MESH_LANE.md §9):
  `trimesh.Trimesh(...)` defaults to `process=True`, which re-merges manifold3d's
  already-welded output and **silently breaks the manifold** — `_from_manifold` must
  pass `process=False`. And a simplify tolerance at/above the part's wall thickness
  (volume/area) *tears the part apart* while the triangle count climbs, so
  `MeshSimplify` verifies its own result (volume drift + `decompose()` piece count)
  and raises rather than returning a broken mesh.
- `Transpiler._cast` only fires on the **item-access** branch: `multiple` collectors
  and `list_access` sockets skip it (as the B-Rep `Union`/`Subtract` already did), so
  `_mesh_bool` coerces every item with `_as_mesh` itself. A solid reaches `MeshUnion`
  uncast — by design, and there's a test pinning it.
- Runtime: the `Mesh` class in the transpiler PREAMBLE wraps a `trimesh.Trimesh`.
  It carries a `_noodle_mesh` marker because `mesh_extractor` runs as an imported
  module and *cannot* import a class that lives in the generated script's globals —
  so it duck-types instead of `isinstance`.
- **Transforms are NOT duplicated.** `Move`/`Rotate`/`Scale`/`Mirror` take a mesh
  directly: their `shape` socket lists `accepts=[…, WIRE_MESH]`, the PREAMBLE
  helpers branch on `_is_mesh` and apply a 4×4 (`_mesh_matrix`) instead of a
  `Location`, and `output_follows="shape"` carries the mesh type back out. There is
  no `MeshMove` and there must not be. (Arrays/Align don't take meshes yet — their
  templates still build `Pos(…) * shape` inline.) `_at` — the `origin` socket every
  primitive gets, wrapped on by the EMITTER, not by the template — branches the same
  way: a mesh can't be `.moved()` nor go in a `Compound`, so it takes a `_mesh_matrix`
  and many origins concatenate instead. A helper behind an `origin` node must
  therefore NOT place its own result, or the translation lands twice
  (`tests/test_polyhedron.py::test_origin_is_applied_exactly_once`).
  `Rotate` also takes an optional
  `pivot` point and an `about` select — world (global axis, the default) / part (own
  bbox centre) / group (collective centre; under fan-out the emitter hoists ONE
  `_pivot_of(…)` out of the lambda so the ensemble turns rigidly); centres are
  measured on the tessellation, like `PlaceOnBed`.
- **The cast is asymmetric on purpose.** `solid/surface → mesh` is automatic
  (tessellation: milliseconds, safely lossy) — drop a `Box` straight into a mesh
  input and it just works. `mesh → solid` is **not** a cast: rebuilding a B-Rep from
  triangles costs ~300s, so it must stay an explicit guarded node (`MeshToSolid`,
  phase 2), never an implicit coercion that hangs the app for five minutes because
  someone wired a mesh into a `Fillet`.
- Previews cost nothing: a mesh IS triangles, so `mesh_extractor` hands the arrays
  straight to the viewer with no tessellation step.
- Not yet built (phase 3): hull/smooth/split/refine, and meshes through
  `ArrayLinear`/`ArrayPolar`/`Align` (their templates still build `Pos(…) * shape`
  inline, so they need helpers first — the four core transforms already work).
  Isotropic remesh has no non-GPL implementation that survives a real part — see
  `PLAN_MESH_LANE.md` §5.
- Example graph: `cad_nodes/examples/mesh-lane.json` (seeded into `projects/`). Tests: `tests/test_mesh_lane.py`.

## 5d. Print physics (category `print`)

Five nodes that answer what a slicer never asks: **which way up, and why**
(`catalog.py` §12c, runtime in the PREAMBLE, full notes in **`PLAN_PRINT_PHYSICS.md`**).
A printed part is anisotropic — the bond between layers is worth roughly a third to two
thirds of the material within one — so orientation decides **where the part breaks**.

- `PlaceOnBed` (lowest point → z=0; serves BOTH lanes: it measures on the mesh and moves
  the original, so a solid stays a solid), `Drop` (PlaceOnBed as a scrubbable FALL: a
  `timeline` slider 0→1, analytic bounce with restitution fixed per `material` — plastic
  0.55, lead 0.08, rubber 0.85… — then, with `settle` on, the part TOPPLES for real:
  `_settle_plan` walks the quasi-static cascade on the convex hull (com outside the
  contact patch → tip about the nearest support edge until the next facet lands, ≤40
  steps, replayed partially at scrub time). The energy guard — every step must strictly
  lower the com — is what stops a sphere rolling forever while letting the edge-balanced
  cube go over; balanced ties resolve deterministically. t=1 is always fully at rest;
  optional `plane` input. Gizmo `kind:"timeline"` (nodes.html): Edit-on-canvas shows a
  Z-only translate arrow — pull the part down to advance t, lift to rewind, one part
  height ≈ the full slider; wired `t` locks it. LIVE REPLAY: `_drop` attaches the whole
  journey as data to its result (`_noodle_anim`: bounce segs + topple steps, world
  coords, baked t); `_preview_of` lifts it into previews[id].anim, and nodes.html
  (`dropMatrixAt`/`applyDropAnim`) replays any t as pure matrix math at 60fps while the
  slider or a wired Number Slider drags (drag anticipation — see §6b), mesh pose =
  M(t)·M(t_baked)⁻¹ —
  the engine re-bakes exactly when the drag settles. COLLISIONS: the `collide` toggle —
  off by default, it costs real compute — un-fans multiple shapes wired into one Drop
  into ONE scene (`_drop_collide` → `_dyn_sim`) and runs REAL rigid-body dynamics
  (pybullet, DIRECT mode): every part is its convex hull, they all fall TOGETHER —
  colliding mid-air, pushing each other over, tumbling, stacking — simulated once at a
  fixed 1/240s step (deterministic per scene) in MILLIMETRES directly (so the fixed
  collision margin is sub-micron, not the ~1mm it becomes when shrunk to metres; CCD +
  hull-volume masses), recorded as 60Hz keyframes per body until the scene sleeps
  (restitutionVelocityThreshold=100mm/s + friction/damping, ~0.6-1s). Each returned
  shape carries its own keyframe plan (`_noodle_anim` kind "keys"); mesh_extractor emits
  a `{kind:"Scene", bodies:[...]}` preview, viewer.js builds a Group of independently-
  posable meshes, and nodes.html (`keyInterp`/`sceneBodyPose`) replays the whole pile
  LIVE (lerp+slerp) while the slider drags. Limits: falling parts are hulls, chaotic like
  real falling. THE CONTAINER: the `container` socket is an IMMOVABLE collider the parts
  fall into — a bowl, a tray, a crate. It is the one body that is NOT hulled: bullet allows
  a concave triangle soup for STATIC bodies only (`GEOM_FORCE_CONCAVE_TRIMESH`, mass 0,
  `_static_colliders` feeds it in bed coordinates), so a bowl keeps its cavity and really
  cradles what you pour in — verified against the analytic seat, balls resting on a
  spherical inner wall to <0.03mm. Wiring one implies scene mode whatever the `collide`
  toggle says (the emitter un-fans on `collide or container`), so a SINGLE part falls in
  too — and then a plain preview carries an anim of kind "keys", which `applyDropAnim`
  routes to `sceneBodyPose` instead of `dropMatrixAt`. It is not an output; with no
  motion wired it never moves, so preview the bowl node itself. A MOVING CONTAINER
  (`ContainerMotion` → the `motion` socket) is the exception, and §5d-bis below.
  GRIP: `grip` scales the friction of the whole
  scene (statics, parts, bed). It is not a detail — on a SLOPED static face high
  friction grabs a part and flings it sideways instead of letting it slide off, so
  `examples/galton-board.json` at grip 1 throws its balls to the walls (bimodal,
  hollow centre, gaussian fit −0.13) and at 0.15 gives a real bell (fit +0.81).
  MESH-LANE TRAP, paid for: `Mesh.__slots__` must list `_noodle_anim` (and now
  `_noodle_extra`). A build123d
  Shape takes any attribute, so the B-Rep lane carried the Drop timeline for free
  and nobody noticed that on the mesh lane the assignment hit the slots wall and was
  swallowed by `_drop`'s try/except — a dropped mesh simply never replayed, and a
  collide scene of meshes came back as one merged blob instead of N posable bodies),
  `PrintCheck` (report → Panel), `OverhangFaces`
  (the faces needing support, as a mesh of its own → its own colour in the viewer),
  `SupportVolume` (the support as a BODY), `OrientForPrint` (every stable pose scored; two
  outputs — the oriented mesh and the table saying why — from ONE search, via
  `_emit_orient`, modelled on `_emit_center`).
- **Support is a sweep and a boolean, not an estimate**: a prism from every overhanging
  triangle down to the bed, unioned (`manifold3d.batch_boolean`), minus the part *and the
  part shifted down by the clearance gap* (that second copy carves the space the support
  must leave, or it welds itself on). Checked against a pencil: a sphere of r=20 gives
  1.63 cm³ where the integral says 1.73. ~0.6 s at 20k triangles — so `OrientForPrint`
  uses it while the part is under `exact_below` triangles and the `area × height` proxy
  above, **all or nothing**, and the report says which. It is the ENVELOPE (a slicer fills
  it sparse) and it does not know about bridges — `PLAN_PRINT_PHYSICS.md` §5.
- **The weak plane** is the smallest cross-section perpendicular to Z: `manifold3d`'s
  `slice(z).area()` (~0.01 s for 80 sections, so scoring 100 poses is free). It is a
  property of the part *in this orientation*, and turning the part moves it.
- **Stable poses** = the convex-hull faces whose polygon contains the projected centre of
  mass — scipy, because trimesh's `compute_stable_poses` needs `networkx`+`shapely`, which
  are not in the image. Cluster hull normals by TOLERANCE, not by a rounded key.
- Two traps, both paid for: the faces resting **on the bed** must be excluded from the
  overhang (a flat base points down too, and counting it makes the one support-free
  orientation look worst), and `PlaceOnBed` must measure on the **tessellation**, not on
  `Shape.bounding_box()` — the fast OCCT box is oversized (hence `view.bbox.approx`), so a
  part dropped by it hovers above the bed.
- **Strength needs a load.** With a `load` vector the score is how much of it crosses the
  layers; with none declared the optimiser optimises for printability and will hand you the
  weakest possible part. That is the whole of `examples/print-orientation.json`.
- **§5d-bis. The container that MOVES** (`ContainerMotion` → `Drop.motion`): the bowl
  stops being furniture. It is a PRESCRIBED motion, not a simulated one — you dictate
  it and the parts inside answer only through contact and friction, which is why they
  lag, slide, climb the wall and spill instead of following rigidly. One node covers
  the lot because `cycles` picks the shape of the motion: 0 = a RAMP (tilt, pour, tip a
  crate) that goes there once and STAYS; >0 = an OSCILLATION about the start pose
  (shake, stir, vibrate) that always returns to it. `delay` waits (fill the bowl, THEN
  tilt); rotation is about the container's own centre unless a `pivot` is wired.
  - **`resetBaseVelocity` is load-bearing, and this was measured.**
    `resetBasePositionAndOrientation` ALONE does not carry the contents: it teleports
    the body, so the contact has zero relative velocity, friction has nothing to
    transmit and the tray slides out from under the part (a box on a tray translated
    50mm rode along **1.2%** — i.e. not at all). Pairing it with `resetBaseVelocity`
    every step gives **99.3%**. The obvious alternative — a real mass on a `JOINT_FIXED`
    constraint driven by `changeConstraint` — carries just as well (99.9%) and is still
    WRONG here: mass > 0 forbids `GEOM_FORCE_CONCAVE_TRIMESH`, so it would hull the bowl
    and throw away the cavity, which is the only reason `container` exists.
  - The motion is dictated in WORLD xyz and the colliders live in bed coordinates, so
    `_motion_driver` carries it over: `R_bed = Bᵀ R_world B`, and since bullet poses a
    body as `x → R x + pos`, turning about a pivot is ENTIRELY the `pos = p − R p` term
    (get it wrong and the bowl swings through the scene on an invisible arm).
  - **A driven rig must never let the scene fall asleep**: `_dyn_sim`'s 0.5s-of-calm
    exit would otherwise trigger BEFORE a slow tilt even begins and the pile would ride
    along frozen — hence the `tau <= _drive_until` guard, and `_t_max` grown to cover
    the motion. A shaker never settles, so it runs its full declared length.
  - **Drawing it needed no frontend change at all.** The container is not an output and
    must not become one, so the posed container rides the result as `_noodle_extra`;
    `mesh_extractor._preview_of` turns those into extra bodies of the same `Scene`
    preview, each with its own `kind:"keys"` track — which `viewer.js` already renders
    as independently-posable children and `sceneBodyPose` already replays at 60fps.
    A single part + a moving container is PROMOTED to a Scene for this reason (else the
    bowl would be invisible). Verified in the browser: scrubbing `t` moves all 4 bodies,
    bowl included. Preview the Drop, not the bowl, or you get a static ghost of it too.
  - Example: `examples/container-tilt.json` (balls land, then the bowl tips over its own
    rim and pours them out). Costs ~5ms per simulated second to drive.
- Tests: `tests/test_print.py`.

## 5e. Voronoi 3D + universal Populate

`PopulateGeometry` (display "Populate") and `Voronoi3D` are the point→partition
pair. Helpers in the transpiler PREAMBLE; both stay memo-cacheable because the
seed lives *inside* the helper (`np.random.RandomState`), not on the emitted line
(`_MEMO_NONDET` matches emitted-line substrings only).

- **Populate is universal** — one node, one `region` socket (`raw=True`,
  `accepts=[solid, mesh]`), dispatching in `_populate` on the runtime TOPOLOGY of
  what's wired (not duck-typing `position_at` — Edge *and* Face have one):
  nothing → the legacy `0..w × 0..h` box at z=0 (bit-exact with the old node);
  **open curve → 1D** along it, uniform by arc length (`edge.position_at(t)`, t is
  already normalized arc length); **closed curve / flat XY face → 2D** *really
  inside* the region (`Face.is_inside`, top-up rounds — not just its bbox); **curved
  face → 2.5D** on the surface, uniform by area (`trimesh.sample.sample_surface`);
  **solid / watertight mesh → 3D** inside the volume. The `raw` socket is
  load-bearing: without it the `curve→surface` (`_face`) and `solid→mesh`
  (`_to_mesh`) casts would fire at the wire and the helper could never tell a curve
  from a face. A *closed* curve is re-filled inside the helper (`_face`) — the
  legacy Rectangle/Circle-as-boundary idiom — while an open one scatters along.
- **3D volume fill has no rtree** (`trimesh.contains` needs it, absent from the
  image): point-in-mesh is `_winding_inside` — the generalized winding number in
  pure numpy (|w|>0.25 = inside), run on a manifold3d-simplified *proxy* when the
  mesh is heavy (>4k tris; it's only an inside oracle, so no verification like
  MeshSimplify does). Rejection loop, 24 rounds, then a clear "run Mesh Fix" error.
- **Voronoi3D** (`mesh` category, list output `cells`): `scipy.spatial.Voronoi` in
  3D with sites mirrored across the **6 planes** of the domain box (body bbox if
  wired, else the points' extent — the 3D analog of `_voronoi2d`'s mirror trick) so
  every kept cell is finite. Each cell = its Voronoi vertices, shrunk toward the
  centroid by `scale`, hulled straight into a Manifold (`Manifold.hull_points` — a
  Voronoi cell is convex, the hull IS the cell), then `cell ^ body`. Output is a
  list of `mesh` bodies (downstream is booleans; `MeshToSolid` is the explicit
  bridge back to B-Rep). Coplanar/degenerate points → clear `QhullError`-wrapped
  ValueError; cap 2000 points. ~0.1s for 60 cells clipped to a 5k-tri sphere.
- **The lattice** = Populate(volume of a body) → Voronoi3D(same body, scale<1) →
  `MeshSubtract` the shrunk cells from the body: the walls *between* cells become
  the part. `examples/voronoi-3d-lattice.json`. Tests:
  `tests/test_populate_voronoi3d.py` (pure-Python: wire shape + emission).

## 5f. Union fuses, Join sews — and they are different OCCT operations

A boolean merges shapes that **overlap** (`+`, BRepAlgoAPI); sewing stitches shapes
that merely **share a border** (BRepBuilderAPI_Sewing, reached through
`Face.sew_faces`/`Shell`, and `Wire.combine` for edges). Union used to be asked for
both and silently answered the second one wrong: two faces on different planes came
back as `f0 + f1` — a loose `Sketch` of 2 faces, no shell, no error, and in the
viewport it *looks* joined.

- **`Join`** (`boolean` category, `_join` in the PREAMBLE): one collector, curves +
  surfaces + solids. Faces win over edges when a shape has both (an input with faces
  is a surface; its edges are that surface's border). Six box faces → a closed shell
  → a real `Solid`; five → an open `Shell` (previewed fine, 4 triangles for an L).
  The closed test is load-bearing: `Solid(open_shell)` builds happily and returns an
  **invalid** solid (measured: volume 800 on a 1000 box) rather than raising.
  Pieces that do not touch are an **error**, not a silent Compound — that is the
  entire point of the node. `tolerance` reaches `Wire.combine` only; `sew_faces`
  has no tolerance argument and uses OCCT's own.
- **`Union` refuses what it cannot fuse**: when every atom is a face, `_coplanar_check`
  requires them planar and on one plane, else it raises and names Join. Solids are
  untouched (disjoint solids fusing into a multi-piece part stays legal and is used),
  and coplanar region merges keep working — `lego-brick` fuses `Text` glyph fragments
  that way, `axl cage` fuses `MakeFace`/`Fillet2D` output. Curves never could reach
  Union: there is no `curve→solid` cast, so `validate()` rejects the wire.
- Tests: `tests/test_join.py` (pure-Python contract); the sewing itself is exercised
  in the worker.

**What Join feeds — and three traps found downstream of it.** The obvious next node
after joining faces is `Shell` (thicken the open surface) or `Shell By Faces`:

- **A failed `Solid.thicken` POISONS its input.** BRepOffset registers its
  modifications on the input faces' TShapes, so a thicken that fails corrupts those
  faces for every node still holding them — measured: `Polyhedron → Join(all but one
  face) → Shell` made the *sibling* `Shell By Faces`, hollowing the same polyhedron
  through the left-out face, return volume **4728 on a part of 2536**, invalid,
  instead of 432. `_thicken` thickens a `deepcopy`. Same family as the `_reanchor`
  trap: OCCT hands out shared topology and a node must not scribble on what it did
  not build.
- **OCCT cannot thicken every open shell**, and says so badly: it returns a SHELL
  when it could not close the wall, and build123d's blind `TopoDS.Solid(...)` cast
  turns that into `Standard_TypeMismatch: TopoDS::Solid`. Sharp dihedral angles
  between many facets are its weak spot — of the platonic solids minus a face, the
  tetra/cube/dodeca thicken fine and the **octa/icosa never do**, at any tolerance,
  join mode or thickness (all swept). `_thicken` now raises a readable error naming
  the way that does work, and refuses to return a wall that fails `is_valid` (it
  renders like a part without being one).
- **`_shell_faces` used to swallow its own failure** (`except Exception: return
  _part`) — no error, no hollow, a node that quietly handed back its input. That is
  what an open surface wired into `Shell By Faces` did. It now rejects a non-solid
  with a message and propagates a real offset failure.
- **The route that works** for a hollow polyhedron with an opening: hollow the CLOSED
  solid and pick the openings there — `Polyhedron → FacesByArea/FacesByNormal →
  ShellByFaces`. Verified end-to-end (icosahedron, wall 0.5 → volume 432.1, valid,
  watertight) and on every platonic solid. Do NOT remove the faces first.

## 5g. Threads (category `fastener`)

One node, `Thread`, makes a real screw thread — ISO metric, trapezoidal lead
screw, UNC/UNF, ACME, tapered NPT — male or female, multi-start, left or right
handed. Runtime in the transpiler PREAMBLE (`_thread`), full notes and every
measurement in **`PLAN_THREADS.md`**. It is on the MESH lane, and that is the
whole story:

- **build123d 0.11 has no thread primitive** (they live in `bd_warehouse`, not a
  dependency), and OCCT cannot be made to do it. The helical sweep is fast on
  either lane (~0.03s), but fusing the rib to its core through the B-Rep kernel
  costs 2-8s and **gets it wrong without raising**: M6x1 came back as the bare
  core (volume 227.9, the thread silently gone) and M20x2.5 came back with volume
  **0**. Letting the section abut the core instead of overlapping it does not even
  build (`StdFail_NotDone`). manifold3d does the same union in ~0.02s, watertight,
  major diameter exact to 4 decimals.
- **Every family is the same trapezoid** with different numbers (half angle, crest
  flat, depth, taper), so one section builder covers all four. Inch sizes are
  stored as they are quoted (inches + TPI) and converted once — never transcribed.
- **Male and female differ ONLY in the root truncation** (17H/24 vs 15H/24 on the
  60° families). The `internal` result is not a female thread, it is **the TAP**:
  subtract it and it drills the hole and cuts the thread in one go.
- **`clearance` loosens the thread it is set on** — set it on ONE half of a pair or
  you get double the gap. Measured on M6x1 by boolean interference: tangent by
  construction at 0, free from 0.1mm up (residuals ≤0.013mm³ = 0.006% of the
  thread, non-monotone in facet count and sometimes negative — numerical noise,
  not contact). 0.3 is the FDM default because the printer's error dwarfs the
  model's.
- **An inverted winding is silent and catastrophic**: manifold3d reads it as
  NEGATIVE volume and SUBTRACTS the rib. The first build returned a M6 rod of
  180.5mm³ against a bare core of 227.9 — smaller than its own core, watertight,
  no error. The faces are reversed once, deliberately, with a comment.
- **The placement socket is `at`, NOT `origin`** — and a new node with an optional
  `shape` should copy this. The emitter wraps an `origin` socket around the node's
  WHOLE result (§4 / `_at`), which with `shape` wired would move the finished
  assembly, so a tapped hole could never leave the axis. `_thread` takes the point
  itself and places the thread BEFORE the boolean. Free bonus: a **list** of points
  drills a whole pattern of tapped holes in one node.
- Example: `examples/bolt-and-nut.json` (a bolt whose thread ADDS to its shank, a
  nut whose thread CUTS). Tests: `tests/test_thread.py`.

## 5b. Lists & fan-out (Grasshopper-style)

Inputs have a data-access mode (`Socket.list_access`):

- **item-access** (default): the input FANS OUT. Wire several connections into it
  (shift-drag in the editor) — or feed it a list-producing node — and the node
  runs once per item, producing a **list** output. Two points → one Circle → two
  circles. Scalars broadcast; shorter lists reuse their last item (longest-match).
- **list_access** (`Socket("list", …, list_access=True)`) and every `multiple`
  collector: consume the whole list as one value (List/Sort/Item/Slice…, Loft).

**Params as inputs:** an input socket that shares a param's name overrides the
widget when wired, and falls back to it when not (e.g. `Vector`/`ConstructPoint`
x/y/z, `Move` `offset`). Wire a list into such an input and the node fans out —
`Range → ConstructPoint.x → Move.offset` scatters one copy per position.

The transpiler wraps a fanned node as `_fanout(lambda …: <expr>, {…})` and tracks
which node outputs are lists (`_produces_list` + `_LIST_PRODUCERS`) so lists
propagate down a chain. List nodes live in the `data` category (ListCreate,
ListSort, ListItem, ListReverse, ListSlice, First/Last, Flatten, Concat, …);
`_sort` uses build123d `ShapeList.sort_by` for shapes, Python `sorted` otherwise.
Other list-producers: `Voronoi2D` (scipy → cell faces), `Voronoi3D` (scipy 3D +
`manifold3d.hull_points`/boolean → convex mesh **cells** clipped to a body, §5e),
`DivideSurface` (`Face.position_at` UV grid → points), `PopulateGeometry`
(universal scatter, §5e) — all fan out downstream (Extrude per cell, scatter per
point). scipy/numpy are available in the worker.
Frontend multi-connect = dynamic input slots sharing one socket name (see
`onConnectionsChange` + `fromGraphJSON` in `nodes.html`).

## 6. How to change things

**Add or edit a node** — almost always pure data in `catalog.py`:

```python
register(NodeDef("MyNode", "category", "My Node",
    inputs=[Socket("shape", WIRE_SOLID), Socket("plane", WIRE_PLANE, required=False)],
    params=[_f("amount", 1.0, 0.0, 100)],          # _f/_i = float/int slider helpers
    outputs=_geo(),                                 # _geo/_sk = solid/surface output
    code_template={"algebra": "my_op({shape}, {amount})"},
    description="..."))
```

`{socket}` → the upstream variable; `{param}` → the formatted value. If the node
needs runtime logic that doesn't fit one expression, add a helper to the
transpiler **PREAMBLE** and call it from the template (e.g. `_bbox_plane`,
`_rotate`). Wire compatibility changes go in `cad_nodes/casts.py` (§5) — the
frontend picks them up from `/api/wiretypes`.

**Search aliases.** `aliases=[…]` adds the words a user from another CAD would type into
the add-node search (`Split` answers to "cut" and "trim"): litegraph 0.7.18
matches ONLY the registered type path and its `searchbox_extras` are gated by the
same test, so nodes.html overrides the canvas INSTANCE's `onSearchBox`
(`nodeSearchRows` — the constructor sets `this.onSearchBox = null`, which would
shadow the prototype) and matches type + label + aliases itself.

**Personal aliases** are the same idea, user-side and hot: a node's right-click
menu → "🔎 Search aliases…" opens a chip modal (`editAliases`) where the built-in
ones sit LOCKED next to yours, which carry a red ✕. Every add/remove PUTs the
whole personal list to `/api/aliases/{type}` and re-draws from the response (so
the chips show what is really stored, server-side normalisation included; a
failed save rolls them back). That writes `projects/_aliases.json` (`{node_type: [word, …]}` — a file, so the
project/library listings that filter on `is_dir()` never see it; `_`-prefixed, so
`validate_graph_id` can never let a project collide with it). The editor loads
them at boot into `USER_ALIASES` and `aliasesOf()` merges the two lists, ranked
alike. The file is deliberately plain and greppable: **a word that earns its keep
gets promoted BY HAND into `NodeDef.aliases`** in catalog.py, and dropped from the
JSON. Catalog aliases are shown in the modal but never editable there — code owns them.

**Group nodes** (BuildPart/BuildSketch) use `is_group=True` + a `builder`
template and emit nested `with` blocks — see existing examples.

### 6b. Drag anticipation (the old ✥ fastDrag)

It is **not a mode of its own**: `fastDrag()` in nodes.html is `liveMode &&
fastEnabled`, so turning Live on turns it on. `fastEnabled` is the escape hatch
for a slow machine (Settings ⚙ checkbox, persisted in localStorage
`noodle:settings:fastDrag`, default on) — with it off, Live still works, it just
waits for each run. There is no toolbar button any more.

The contract: while a param drags, replay it locally in Three.js; when the drag
settles, `scheduleLive()` re-bakes it exactly. The local replay must therefore be
an *anticipation of the engine's answer*, never a different one.

**When you add a node, ask whether it can be anticipated.** Compatibility is
decided at DRAG TIME, not at node creation — it depends on the wiring and on
whether a preview mesh exists, so it cannot be a static flag on the NodeDef:

- `applyLocalTransform(node)` — the node's own preview moved as a delta from the
  baked params. Today: Move/Rotate/Scale (a `Location`) and Drop (its shipped
  `_noodle_anim`). Add a node here only if the transform is expressible as a
  matrix on the already-meshed preview.
- `applyDropTargets(node)` — a value node (Number Slider…) wired into a `Drop.t`,
  which replays each target instead of itself.

Both return **false** when they cannot help, and the caller falls through to the
plain debounced re-run. That fallback is what makes an unanticipated node correct
but merely slower — so when in doubt, return false. A node that anticipates
WRONGLY is far worse than one that does not anticipate at all.

**Apply / reload rules:**
- Backend Python change → `docker restart noodle` (process caches imports;
  the read-only mount alone isn't enough).
- Frontend (`webui/*.html`) change → hard-refresh the browser (Ctrl+Shift+R);
  the file is static and cached.
- Verify engine logic fast on the host with `.venv-b123d` (§2) before restarting.

**Gotchas:**
- The container runs as **uid 1000** (`noodle`), matching the typical host user,
  so `projects/` is host-editable. Projects created by pre-non-root images are
  root-owned — fix once with `sudo chown -R 1000:1000 projects feedback`. The
  canonical writer is still the server API (`/api/graph/{name}/...` or the UI).
- Project names are validated (`cad_nodes/store.py::validate_graph_id`): one
  path segment, `[A-Za-z0-9][A-Za-z0-9._ -]{0,63}`. Anything else is a 400
  (path-traversal guard) — keep any new route that touches `projects/` on
  `project_dir()`/`GraphStore.dir()`.
- Running build123d on the host prints noisy fontconfig warnings to **stderr** —
  redirect `2>/dev/null` and read stdout.
- The copilot/MCP both go through `cad_nodes.api`; new capabilities belong there
  so all three surfaces (UI, MCP, copilot) get them.

## 7. The AI copilot — scope & guardrails

`cad_nodes/copilot.py` drives an OpenAI-compatible tool loop bound to ONE graph.
Tools: `get_graph`, `get_node_def`, `add_node`, `copy_node`, `connect`,
`set_param`, `delete_node`, `execute`. It has **no tool that edits app code or
node definitions** — by construction it can only manipulate a graph.

Enforced policy (system prompt + tool layer):
- It **assembles workflows** from existing catalog nodes and **creates new custom
  nodes from scratch** (a custom node is a `CodeBlock` — arbitrary build123d code
  in its `code` param).
- It must **never** modify the app or any built-in node's behaviour.
- It may **not edit the `code` of a custom node that pre-existed the conversation**.
  `set_param` refuses an in-place code edit of such a node; the model is told to
  warn the user and use `copy_node` to work on a duplicate, leaving the original
  intact. (Nodes created in the current session are freely editable — they're
  tracked in `state["created"]`.)

If you extend the copilot, preserve these invariants.

## 7b. Retro-engineering ("retroeng") — STL/STEP → parametric graph

When the user says **"retroeng"** (e.g. *"fai il retroeng dell'STL che ti ho
passato"* / "reverse-engineer this part"), they mean the `PLAN_RETROENG.md`
workflow: rebuild an imported mesh/solid as a **parametric graph of catalog
nodes**. "The file I just passed" resolves through the **ToAgent tag index**:
in the editor the user tags an ImportSTL/ImportSTEP node with a `ToAgent` node
(label + auto-stamped save date); `GET /api/agent/tags` (= `api.agent_tags`,
MCP `cad_agent_tags`) lists every tag across ALL projects with the tagged
file's project-relative path. Pick the most recent (or label-matching) entry —
don't ask "which STL?".

The loop (validated on real parts: STEP rebuilt at Δvolume 0.05%, a 59k-tri
STL at +2.2% — see `projects/retro_nodes` and `projects/retromy`):

1. **Perceive** — `GET /api/graph/{name}/slice_summary?path=<file>&n=10`
   (`api.slice_summary`): symbolic cross-sections on all 3 axes (`circle r=3
   @(x,y)`, `rect 40x30`; dedup "z=a…b identical" ⇒ extrusion + its height).
   STEP sections are exact; STL is arc-fitted. The `text` field is the
   LLM-facing format. Omit `path` to slice the graph's OWN result.
2. **Microscope** where the summary is ambiguous —
   `.../section_outline?axis=z&pos=…`: ONE exact section, edge by edge.
   Mesh gotcha: a single section can drop loops near tangent surfaces —
   confirm with nearby sections or with per-section areas.
3. **Rebuild** with catalog nodes via `cad_nodes.api` (add_node / connect /
   set_param). Proceduralize, don't trace: constant section → Extrude; N equal
   circles in a regular layout → ArrayLinear/ArrayPolar with a count slider,
   not copies; small rounds → a downstream Fillet; overall dims → sliders.
   The user's stated intent about what to parameterize wins over defaults.
4. **Verify with the same tool** — execute, re-slice your own result
   (`slice_summary` without `path`) and diff the two summaries as text;
   comparing per-section AREAS localizes residuals; bbox + volume checksum
   is the final seal.

Not yet built (see PLAN_RETROENG.md): vision contact-sheet, gcode stripper,
numeric `cad_compare`.

## 8. Tests

`tests/` are pure-Python (no build123d needed): toposort, graph validation,
transpiler output, api ops.

```bash
python -m pytest tests/ -v        # pytest may need installing in your env
```

## 9. The agent's eyes — `/api/graph/{name}/screenshot`

`GET /api/graph/{name}/screenshot` → `image/png` (= `cad_screenshot` on MCP,
`api.screenshot`, code in `cad_nodes/screenshot.py`). Args: `view`
(`iso|front|back|left|right|top|bottom`) or `azim`+`elev`, `zoom`, `node`+
`isolate` (frame one node), `width`/`height`/`scale`, `projection`,
`chrome`, `run`. Headers: `X-Noodle-Ran`, `X-Noodle-Size-Mm`.

**Why it exists, concretely.** The Thread node (§5g) shipped with volume
1922mm³, a watertight mesh, an exact major diameter and 237 green tests — and
no thread on the bolt at all: the example wired a shank as fat as the nominal
diameter, so the union filled every groove. Nothing in the API could report
that. The first rendered picture did, immediately. **Numbers verify what you
thought to measure; a picture shows what you did not.**

- **It is the REAL viewer, not a second renderer.** Headless Chromium over this
  server's own `/nodes` page: same `viewer.js`, materials, finishes, selective
  bloom, same camera code. A numpy rasterizer was considered and rejected — it
  would be free to drift from the thing users actually look at, and blind to
  precisely the work that went into glass/emissive/rainbow/bloom.
- **No GPU**: SwiftShader, verified pixel-identical to hardware GL.
- **The browser is kept WARM**, like the execution worker: ~10s cold, **~1.5s**
  warm with `run=0`. Take extra angles freely; re-run only when geometry changed.
- **NOT on `asyncio.to_thread`** (unlike /execute) and deliberately: the work
  happens in the browser process, so the coroutine only awaits I/O. The graph run
  it triggers goes through /execute, which is already off the loop.
- **Two traps paid for.** `openGraph` is async, so calling `runGraph()` too early
  executes an EMPTY graph and the wait for previews then times out with nothing
  to explain it — wait for `lgraph._nodes.length > 0` first. And the first `iso`
  preset used a POSITIVE azimuth, which puts the camera behind anything modelled
  facing front: every default shot came back with its lettering mirrored. It is
  now front-right-top (-45°, true isometric 35.264°). Both were found by looking.
- **Deployment**: `playwright install --with-deps` resolves an UBUNTU package set
  and dies on Debian (`ttf-ubuntu-font-family has no installation candidate`),
  taking the browser with it — the Dockerfile lists the libs by hand, and
  `fonts-liberation` is the one that matters (without a font, every label in the
  viewport renders as a blank box). Browsers go to `/opt/playwright`, world-
  readable, because the server runs as uid 1000 and cannot read root's HOME.
  Only the **headless shell** is installed: `playwright install chromium` fetches
  the full browser too (549MB) and noodle never opens a window — the shell does
  WebGL2 through SwiftShader (ANGLE/Vulkan), verified. Measured cost of the whole
  feature: **1.87GB -> 2.6GB** (+730MB); installing both browsers made it 3.35GB.
  A missing browser is a **503**, not a 500.
- Tests: `tests/test_screenshot.py` (pure-Python: camera planning, the clamps,
  and that HTTP/MCP expose one operation rather than two).
