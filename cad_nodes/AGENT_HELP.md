# noodle — remote agent guide

You are talking to **noodle**, a node-based parametric CAD engine. A model is a
**graph** of typed nodes; the backend transpiles it to
[build123d](https://build123d.readthedocs.io) Python, executes it in an isolated
worker and returns a mesh preview + per-node errors. Everything you can do in
the web editor you can do through this API.

You fetched this guide from `GET /api/agent/help` (HTTP) or the `cad_help` tool
/ `cad://help` resource (MCP). Same engine, two transports:

- **HTTP** — base URL `http://<host>:8090`, no auth, JSON in/out.
- **MCP** — tools named `cad_*`; run `docker exec -i noodle python mcp_server.py`
  (stdio) on the machine that hosts the container.

The web editor at `http://<host>:8090/nodes` shows the SAME projects you edit
here — the user is often watching it; they reload to see your changes.

## The loop

1. **Discover**: `GET /api/nodes` (= `cad_get_node_catalog`) — every node type
   with its sockets (name + wire type), params (type, default, min/max) and
   description. Filter client-side by `category`.
2. **Build**: create a project, add nodes, wire them, set params
   (endpoints below). Node positions `[x, y]` matter only visually — spread
   nodes left→right so the user can read the graph (~200px steps).
3. **Execute**: `POST /api/graph/{name}/execute` (= `cad_execute`). Returns
   `node_errors` (`node_id → message`) — fix the offending node, re-run.
4. **Verify numerically**: the view **summary** (bbox / volume / area / face &
   edge counts) is in the execute response; don't pull the tessellated mesh
   unless you truly need triangles (`cad_get_view fmt="mesh"`). The live
   `bbox` is approximate (`approx: true` — poles-based, up to ~1% oversized,
   never smaller); treat **volume/area** as the exact figures.
5. **Look at it**: `GET /api/graph/{name}/screenshot` (= `cad_screenshot`)
   returns a PNG of the real viewport. Do this whenever you have built or
   changed geometry — numbers do not catch everything. A part can have the
   right volume, a watertight mesh and a green test suite and still be plainly
   wrong: a boolean that filled the feature it was meant to cut, an array
   pointing the wrong way, a part sunk through the bed. One picture settles it.
6. **Iterate** param changes; **export** when done: `step | stl | gltf`.

## Looking at the result

`GET /api/graph/{name}/screenshot` → `image/png`. It drives the app's own
viewer, so the image is exactly what the user sees.

| arg | meaning |
|---|---|
| `view` | `iso` (default, front-right-top) · `front` `back` `left` `right` `top` `bottom` |
| `azim`, `elev` | degrees, instead of a preset. Azimuth in the XY plane from +X, elevation from it. The scene is **Z-up** |
| `zoom` | >1 pulls back, <1 closes in (default 1) |
| `node`, `isolate` | frame ONE node's preview by id; `isolate=1` hides the rest |
| `width`, `height`, `scale` | pixels (clamped to 4000) and device pixel ratio |
| `projection` | `persp` or `ortho` — ortho reads better when checking alignment |
| `chrome` | `1` keeps the legend/stat overlays (default: geometry only) |
| `run` | `0` reuses what is already rendered — cheap for extra angles (~1.5s vs ~10s) |

Response headers: `X-Noodle-Ran` (whether it re-executed) and
`X-Noodle-Size-Mm` (the framed bounds). A `503` means the browser is missing
from the deployment, not that your graph is wrong.

Take **two angles** when a shape is ambiguous from one — and `top`/`front` in
`ortho` when you are checking that things line up rather than how they look.

## HTTP endpoints

Over HTTP the graph is written **whole** — build the `{name, nodes, connections}`
JSON yourself and POST it; only single params have a granular edit. (Granular
add_node/connect/delete tools exist on MCP: `cad_add_node`, `cad_connect`,
`cad_set_param`, `cad_delete_node`, `cad_delete_connection`.)

| Endpoint | Purpose |
|---|---|
| `GET /api/nodes` · `GET /api/wiretypes` | node catalog · wire compatibility table |
| `GET /api/projects` · `DELETE /api/projects/{name}` | list / delete projects |
| `POST /api/graph/{name}` body=`{name,nodes,connections}` | create or overwrite the whole graph |
| `GET /api/graph/{name}` | read the graph JSON back |
| `PATCH /api/graph/{name}/param` body=`{node_id,param,value}` | clamped single-param edit |
| `POST /api/graph/{name}/execute` | run → `{view, code, node_errors, warnings}` |
| `GET /api/graph/{name}/code` | generated build123d source |
| `GET /api/graph/{name}/view` | last execution's view again |
| `GET /api/graph/{name}/export/{fmt}` | export + download (`step`/`stl`/`gltf`; other formats via Export* nodes in-graph) |
| `POST /api/graph/{name}/import` (multipart `file`) | upload STEP/STL/SVG/DXF **and** add its Import node |
| `POST /api/graph/{name}/asset` (multipart `file`) | upload into `assets/` without adding a node |
| `GET /api/graph/{name}/assets` | list the project's imported files |
| `GET /api/agent/tags` | ToAgent provenance index (`cad_agent_tags`) |
| `GET /api/graph/{name}/slice_summary?path=&n=` | symbolic sections (`cad_slice_summary`) |
| `GET /api/graph/{name}/section_outline?axis=&pos=&path=` | one exact section (`cad_section_outline`) |
| `GET /api/graph/{name}/screenshot?view=&node=&…` | **PNG of the viewport** (`cad_screenshot`) — see below |

Project names: one path segment, `[A-Za-z0-9][A-Za-z0-9._ -]{0,63}` — anything
else is rejected (400).

## Graph JSON

```jsonc
{
  "name": "demo",
  "nodes": [
    { "id": "n1", "type": "Sphere", "params": {"radius": 3},
      "position": [120, 80], "preview": true }
  ],
  "connections": [
    { "id": "l1", "from_node": "n1", "from_socket": "result",
      "to_node": "n2", "to_socket": "shape" }
  ]
}
```

`groups` (editor boxes) and the `_ui` / `_cb` param namespaces are editor-side
metadata — preserve them if present, never invent them.

## Wire types

`solid` (3D B-Rep) · `surface` (2D sketch/face) · `curve` · `plane` ·
`vector` (points) · `selection` (picked sub-shapes) · `data` (universal bus:
number/int/bool/str/list/domain — accepts and feeds anything) · `tree`.
Compatibility is enforced on connect with an explicit error; widening casts
(e.g. curve→solid inputs on transforms) are applied automatically where declared.
The catalog tells you each socket's wire type — trust it, don't guess.

## Lists & fan-out (Grasshopper-style)

- Default inputs are **item-access**: feed them a LIST (several connections
  into one socket, or a list-producing node like `Range`, `Voronoi2D`,
  `DivideSurface`) and the node runs **once per item**, outputting a list that
  keeps fanning out downstream. Scalars broadcast; shorter lists repeat their
  last item.
- `List*` nodes (ListCreate/Sort/Item/Slice/Flatten/…) and collectors like
  `Loft` consume the whole list as ONE value.
- **Params double as inputs**: an input socket with the same name as a param
  overrides the widget when wired (e.g. `Move.offset`, `Vector.x/y/z`). Wire a
  list into one and the node fans out — `Range → ConstructPoint.x → Move.offset`
  scatters copies.

## Booleans & fillet/chamfer

- **`Union`** has ONE collector input `shapes`: wire many shapes into it (or a
  list-producing node like a fanned `MakeFace`) and they all fuse into one. It is
  dimension-agnostic and type-preserving — fusing 2D faces yields a `surface`
  (feed it straight into `Extrude`), fusing solids yields a `solid`. Use it for
  2D region booleans too (fuse faces from `MakeFace`), not just 3D. `Subtract`
  (`a` − `b`, `b` may be a list of tools) and `Intersect` stay two-input.
- **Fillet & chamfer are unified** — one node with a `mode` dropdown
  (`fillet`/`chamfer`) and a `size` param:
  - `FilletChamfer` — all edges of a solid.
  - `FilletChamferSelected` — only the sub-shapes from a `Select*` node: edges
    (3D) **or** vertices (2D corners via `SelectVertex`).
  - `FilletChamferCorners` — all corners of a 2D face/sketch; **outputs a curve**
    (the rounded outline). Feed a closed curve or a `MakeFace`; fill with
    `MakeFace` or send straight to `Extrude`.
  - The old singles (`Fillet`, `Chamfer`, `Fillet2D`, `Chamfer2D`,
    `Fillet/ChamferSelectedEdges`) are **hidden/deprecated** but still run for
    older graphs — prefer the unified nodes.

## Selections (Select* & predicate selectors)

Every selector — pick-based (`SelectEdge/Face/Vertex`) and predicate
(`FacesByNormal`, `EdgesByType`, `FacesByArea`, `EdgesByLength`, `FacesByType`,
`SubshapesByPosition`, `CombineSelection`) — has **two outputs**:

- `selection` (wire type `selection`) — drives a targeted op: `FilletChamferSelected`,
  `ExtrudeSelectedFace`, `ShellByFaces`, `CombineSelection`. Consumed whole.
- a **geometry** output that materialises the picked sub-shapes as usable
  geometry — `edges`→`curve`, `faces`→`surface`, `points`→`vector`
  (`SubshapesByPosition`/`CombineSelection` give `shapes`→`data` since the kind
  varies). It **fans out**, so `SelectFace.faces → Extrude` extrudes each picked
  face, `EdgesByType.edges → Sweep`/`Loft` uses the picked edges as curves, and
  `SelectVertex.points → …` scatters on the vertices.

`SelectShape` is different: it picks **WHOLE objects from a LIST** (not
sub-shapes of one object) — universal across any shape type (solids, faces,
curves, Voronoi cells, array copies). Feed it a list-producing node
(`ArrayLinear`, `Voronoi2D`, a fanned output); its `shapes` output is the
selected objects (type-preserving, fans out downstream).

## Custom nodes (CodeBlock)

A `CodeBlock` node runs arbitrary build123d Python from its `code` param.
Lines like `#@param radius: float = 5.0` become live sliders + input sockets.
Use catalog nodes first; reach for CodeBlock only when no node fits. Never
rewrite a CodeBlock the user made — copy it and edit the copy.

## Retro-engineering ("retroeng")

When the user says "retroeng the STL/STEP I passed": they tagged an
ImportSTL/ImportSTEP node with a **ToAgent** node in the editor.

1. `GET /api/agent/tags` — pick the newest / label-matching entry; it gives you
   the graph and the file's project-relative `path`. Don't ask which file.
2. Perceive: `slice_summary` with that `path` (`n≈10` per axis) — symbolic
   cross-sections (`circle r=3 @(x,y)`, `rect 40x30`; "z=a…b identical" ⇒
   an extrusion and its height). STEP is exact, STL is arc-fitted.
3. Where a line is ambiguous (`poly(…)`) use `section_outline` at that height —
   one exact section, edge by edge. Mesh sections can drop loops near tangent
   surfaces: confirm with nearby sections or per-section areas.
4. Rebuild **procedurally** with catalog nodes: constant section → Extrude;
   repeated equal features → ArrayLinear/ArrayPolar driven by a count param,
   not copies; small rounds → a downstream FilletChamfer; key dims → params. The
   user's stated parametrization intent wins.
5. Verify with the same eyes: execute, `slice_summary` **without** `path`
   (slices your own result), diff the two texts; per-section areas localize
   residuals; volume is the final checksum (the live bbox is approximate —
   compare sizes with ~1% tolerance).

## Cautions

- The engine executes graph code as **arbitrary Python, unsandboxed**, and the
  API is unauthenticated: it is meant for a trusted LAN. Don't put untrusted
  code in CodeBlocks.
- Execution overwrites `output.stl`/`view.json` per project; the graph itself
  is only changed by your edits. Prefer editing via the API (it validates and
  clamps) over hand-writing whole-graph JSON.
- Don't delete or overwrite projects you didn't create unless the user asks.
