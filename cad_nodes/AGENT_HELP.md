# noodle — remote agent guide

You are talking to **noodle**, a node-based parametric CAD engine. A model is a
**graph** of typed nodes; the backend transpiles it to
[build123d](https://build123d.readthedocs.io) Python, executes it in an isolated
worker and returns a mesh preview + per-node errors. Everything you can do in
the web editor you can do through this API.

You fetched this guide from `GET /api/agent/help` (HTTP) or the `cad_help` tool
/ `cad://help` resource (MCP). Same engine, same operations, two transports:

- **MCP** (preferred) — tools named `cad_*`; run
  `docker exec -i noodle python mcp_server.py` (stdio) on the machine that hosts
  the container.
- **HTTP** — base URL `http://<host>:8090`, no auth, JSON in/out. Every MCP
  editing tool has an HTTP twin (table below).

The web editor at `http://<host>:8090/nodes` shows the SAME projects you edit
here — the user is often watching it; they reload to see your changes.

**Never hand-edit `graph.json`** (docker exec + sed, string replace...). The
editing calls below validate every value, keep ids stable, place new nodes and
save once. Hand edits bypass all of that, and an editor tab that is open on the
same project will simply overwrite them on its next save.

More detail on demand — `cad_help(topic=...)` / `GET /api/agent/help?topic=...`:
`screenshots` · `retroeng` · `print` · `threads` · `fluid`.

## The loop

1. **Discover**: `cad_get_node_catalog(query="fillet")` — one signature line per
   node type: `Type [category] in:(socket:wire, optional:wire?) out:(...)
   params:(name=default, ...)`. `cad_get_node_def("FilletChamfer")` for one type
   in full (ranges, options, description). HTTP: `GET /api/nodes?query=fillet`,
   `GET /api/nodes/{type}`; `GET /api/nodes` is the full JSON catalog.
2. **Read** an existing graph compactly: `cad_get_graph(graph_id)` — ids, types,
   titles, params, connections as `id: from.socket -> to.socket`. Long CodeBlock
   code is elided; `cad_get_graph(graph_id, node="n12")` gives one node in full.
   (Avoid `cad_get_code` for reading: it is the whole generated script.)
3. **Build / edit**: `cad_apply_ops` for anything more than one change — an
   atomic batch, validated, saved once (see *Editing*). Single changes:
   `cad_set_param`, `cad_edit_code`, `cad_add_node`, `cad_connect`.
4. **Execute**: `cad_execute(graph_id)` → `success`, `node_errors`
   (`node_id → message`), `warnings`, `slowest` nodes and a lean **view
   summary** (bbox / volume / area / counts / Panel values / per-preview
   kind+bbox+volume; floats rounded). No generated code and no meshes unless
   asked (`include_code=True`; `cad_get_view(fmt="mesh")`). HTTP:
   `POST /api/graph/{name}/execute?lean=1`.
   - **Try before committing**: `cad_execute(graph_id, overrides={"Height":
     {"value": 42}})` runs with those values WITHOUT saving them.
   - The live `bbox` is approximate (`approx: true` — poles-based, up to ~1%
     oversized, never smaller); treat **volume/area** as the exact figures.
5. **Look at it**: `cad_screenshot` (topic `screenshots`) — whenever you have
   built or changed geometry. A part can have the right volume, a watertight
   mesh and green tests and still be plainly wrong: a boolean that filled the
   feature it was meant to cut, an array pointing the wrong way, a part sunk
   through the bed. One picture settles it.
   **To SHOW the user a result, send a link, not pictures**: `cad_snapshot(graph_id,
   label=...)` freezes the current geometry as a generation and returns a `url`
   (`/view/<graph>/g<N>`) to a read-only 3D viewer where the user orbits it,
   hides pieces and inverts the selection. The link stays fixed on THAT result
   while the workflow keeps changing. Append `#hide=n3,n7.2` to open it with
   pieces hidden (a node id = all its pieces, `id.i` = its i-th piece).
   **Several alternatives to choose from?** Give each its own snapshot with a
   label that says what differs (`"B — wall 3mm, round lid"`), then send the
   gallery link `/views` (every generation of every project, as cards, newest
   first; `/views?p=<graph>` for one project) as well as the individual links.
   Each generation is called `<graph>/g<N>` everywhere the user sees it — the
   viewer header, the cards — so ask them to quote that. And when they say
   "this one", `cad_recent_gens()` (`GET /api/gens/recent`) tells you which one
   they last opened: the entry with `last_seen: true`.
   **A movement is one link, not several**: to show a lid open AND closed, a
   drawer in and out, an assembly exploding, do not send one link per pose —
   animate it. `Motion` (type `ContainerMotion`: move x/y/z, rotate rx/ry/rz,
   `duration`, `cycles` 0 = go once) → `Animate(shape, motion, t)`; for a hinge
   wire a `pivot` point on the hinge line (lid of a box whose back top edge is
   at y=20, z=15: pivot (0,20,15), rx=-105 lifts it backwards). Set the
   Animate's `t` to the pose you want as the resting one (0 = closed). The
   generation then carries the timeline (`timeline: {seconds}` in the result)
   and the viewer shows a ▶ player: only-Animate scenes default to there-and-
   back (open ⇄ close), anything with a Drop loops. Link params:
   `#play=1` autoplays, `#t=0.5` opens at that point, `#mode=pingpong|loop|once`;
   combine with `&`: `…/g3#hide=n2&play=1`.
   **Do not choreograph a sequence** (lid opens, head folds, lid closes):
   give each moving part ONE Animate with ONE Motion, wired from the part
   itself, `t` = 0 at rest. The player shows a slider and a ▶ per Animate
   (≡ Tracce), so the user plays them in whatever order they like — no
   `delay`/`hold` arithmetic to get right. Never feed an Animate into another
   Animate: the second moves the first's result FROZEN at its `t`, it does not
   play after it (lint `animate_chain`). Link a pose of the tracks with
   `#tt=<id>:0.6,<id>:1` (per-track t) and `#tracks=1` (panel open).
   **The user can DRAW on a generation for you** (✎ Disegna in the viewer):
   circle a hole in red, mark a fillet, write "questo si può fare meglio".
   When they say "guarda cosa ho segnato / disegnato", call `cad_notes()`
   (`GET /api/notes`): newest open notes first, each with `text`, the
   picture they were looking at with the strokes on it (`cad_note_image`, or
   `image_path`), and `marks`, one per gesture: `color_name`, `shape` (`loop`
   = circled, `line`, `dot`), `centre`/`bbox` in model mm and `on` = the
   node(s) it was drawn on — ids of the gen's FROZEN graph (`ref` `graph/gN#aK`).
   The main picture is only the LAST view: a mark drawn from another angle
   has `view` > 0 and its own `image_path` — `cad_note_image(..., mark=N)`
   shows it. Look at every mark's picture before acting on it. Aim
   `cad_measure` / a section at the centre to find the feature, fix it,
   snapshot, then `cad_note_done(graph, gen, id, reply="…see g8")` so the
   user sees it closed with your answer.
6. **Tidy and export**: `cad_arrange` lays the whole graph out (dependency
   order, real node sizes, no overlaps, named sliders gathered in a parameter
   panel on the left) — you never compute positions yourself. Then
   `cad_export(fmt="step" | "stl" | "gltf")`.

`cad_validate(graph_id)` checks a graph without running it (wiring, unconnected
required inputs, unknown / badly typed / out-of-range stored params).

## Editing

- **Node refs**: every editing call takes a node **id or its exact title** (the
  name shown on the node). An ambiguous title is an error listing the ids.
- **Params are validated** against the catalog: an unknown name is an error
  listing the valid ones, a bad type names `node.param`, an out-of-range number
  is clamped to the catalog range and reported in `notes`. A CodeBlock's
  `#@param` knobs are set by their bare name.
- **Positions**: omit them. New nodes are auto-placed — right of whatever they
  are wired to (in a batch), else right of the graph — and `cad_arrange` tidies
  the lot. Pass `position=[x, y]` only to put a node somewhere specific.
- **Ids are stable**: nothing renumbers existing nodes; new ones get
  `<type>_<n>` or the `id` you give.
- **`cad_apply_ops(graph_id, ops)`** — all or nothing; the error names the
  failing op index and nothing is saved:

  ```jsonc
  [ {"op": "add_node", "type": "Box", "params": {"width": 40}, "title": "Body"},
    {"op": "add_node", "type": "FilletChamfer", "params": {"size": 2}},
    {"op": "connect", "from": "$0.result", "to": "$1.part"},  // $N = node made by op N
    {"op": "set_param", "node": "Height", "params": {"value": 12}},
    {"op": "edit_code", "node": "n12", "old": "r = 3", "new": "r = 4"},
    {"op": "set_node", "node": "Body", "preview": true},       // title/preview/bypassed/color
    {"op": "disconnect", "from": "n3.result", "to": "n5.shape"}, // or {"op":"disconnect","id":"c7"}
    {"op": "remove", "node": "n9"} ]
  ```
  Also `{"op": "set_code", "node", "code"}`. Returns per-op results, the ids
  created (`"$0": "box_1"`), validation warnings and positions it placed.
- **`cad_edit_code(graph_id, node, old, new)`** — exact string replacement in
  a CodeBlock: `old` must occur exactly once (0 or 2+ matches is an error). Much
  cheaper than resending a whole script with `cad_set_code`.
- **`base_version`** (optional, on the write calls) — pass the `version` from
  your last read to have a write refused (HTTP 409) if the graph changed in the
  meantime. `version` is `null` where the server does not track versions yet.

**Long CodeBlocks.** Writing one big CodeBlock is fine — noodle reads its
structure for you (`cad_codeblock_sections`) and shows it to the user as nodes
(▦ Sezioni on the block). It reads best when each part starts with a header
comment line, `# ---- frontale ----`; without headers statements are grouped
by the variable they build. Use it to find where a part is made, which
section is slow, or where a run fails (`run=True` → `failed_in`).

## HTTP endpoints

| Endpoint | Purpose (MCP twin) |
|---|---|
| `GET /api/nodes?query=&compact=1` · `GET /api/nodes/{type}` | compact catalog · one type (`cad_get_node_catalog`, `cad_get_node_def`) |
| `GET /api/nodes` · `GET /api/wiretypes` | full JSON catalog · wire compatibility table |
| `GET /api/projects` · `DELETE /api/projects/{name}` | list / delete projects |
| `GET /api/graph/{name}/compact?node=&positions=0` | compact graph (`cad_get_graph`) |
| `POST /api/graph/{name}/ops` body=`{ops, base_version?}` | atomic batch (`cad_apply_ops`) |
| `POST /api/graph/{name}/set_param` body=`{node, params, base_version?}` | validated params by id or title (`cad_set_param`) |
| `POST /api/graph/{name}/edit_code` body=`{node, old, new, base_version?}` | CodeBlock str-replace (`cad_edit_code`) |
| `GET /api/graph/{name}/validate` | check without running (`cad_validate`) |
| `POST /api/graph/{name}/arrange` | tidy layout and save (`cad_arrange`) |
| `POST /api/graph/{name}/execute?lean=1` body=`{overrides?}` | run → lean summary (`cad_execute`); without `lean` the editor's full payload (code + meshes, ~1MB). A body that is a whole graph (`nodes`, `connections`) runs that snapshot instead of the file |
| `GET /api/graph/{name}/progress?run=<id>` · `POST /api/graph/{name}/runs/{id}/cancel` | per-node SSE events of the run you started with `?run=<id>` on execute · cancel it (a queued job never starts, a running one is killed). A run id is used once (409 on reuse) |
| `GET /health` | `{status, version, enabled, alive, busy, queued}`: `busy`/`queued` is the warm worker |
| `GET /api/graph/{name}/codeblock/{node}/sections` · `POST …/sections/run?section=sN` | a long CodeBlock read as the nodes it contains, unchanged (`cad_codeblock_sections`): sections from its `# ---- title ----` headers (else grouped by the variable each statement builds), kind params/quote/funcs/part/chain, line ranges, names flowing between sections, which section makes each output · run = time and numbers per section, `failed_in`, and section sN's shapes; saves nothing |
| `POST /api/graph/{name}` body=`{name,nodes,connections}` | create or overwrite the whole graph → `{warnings, param_issues?}` |
| `GET /api/graph/{name}` | the raw graph JSON |
| `PATCH /api/graph/{name}/param` body=`{node_id,param,value}` | single-param edit (the code view's) |
| `GET /api/graph/{name}/code` · `GET /api/graph/{name}/view` | generated build123d source · last run's full view |
| `GET`/`POST /api/graph/{name}/export/{fmt}` | export the RESULT + download (`step`/`stl`/`gltf`); POST body = optional unsaved graph. Also published as `exports/<name>.<ext>` (`cad_export`) |
| `GET`/`POST /api/graph/{name}/export/bundle` | bake every PREVIEWED node to STEP + STL, one pair per node, zipped with `manifest.json`; published as `exports/<name>_<date>.zip` (`cad_export_all`) |
| `GET /api/library` | every project's exports/assets; each export carries `source` from `exports/index.jsonl`: which node / button / bundle wrote it, and `fresh` = graph unchanged since |
| `POST /api/graph/{name}/import` (multipart `file`) | upload STEP/STL/SVG/DXF **and** add its Import node |
| `POST /api/graph/{name}/asset` (multipart `file`) · `GET .../assets` | upload into `assets/` without a node · list them |
| `GET /api/graph/{name}/screenshot?view=&node=&…` | **PNG of the viewport** (`cad_screenshot`) |
| `POST /api/graph/{name}/snapshot?label=&run=` · `GET .../gens` | freeze a generation → `{gen, url}` for the read-only viewer (`cad_snapshot`, `cad_list_gens`) — send the user the `url` |
| `GET /api/gens/recent?limit=&project=` · page `/views` | generations of every project, newest first, with `ref` (`graph/gN`), `seen` and `last_seen` = the one the user opened last (`cad_recent_gens`) |
| `GET /api/notes?project=&gen=&done=&points=` · `GET .../gens/{gen}/notes/{id}.jpg[?view=k]` · `PATCH .../gens/{gen}/notes/{id}` body `{done, reply}` | what the user DREW on a generation in the viewer (`cad_notes`, `cad_note_image`, `cad_note_done`) |
| `GET /api/agent/tags` | ToAgent provenance index (`cad_agent_tags`) |
| `GET /api/graph/{name}/slice_summary?path=&n=` · `.../section_outline?axis=&pos=&path=` | sections (`cad_slice_summary`, `cad_section_outline`) |
| `POST /api/graph/{name}/measure` body=`{queries:[…]}` | geometry facts by node ref `n5`/`n51.body`/`n51[3]` (`cad_measure`): `props`, `interference` (a+b, or every pair of a list node), `distance`, `section` (+svg), `probe`, `summary` — check fits and clashes instead of writing scripts |
| `GET /api/graph/{name}/lint` | soft findings (`cad_lint`): slider vs `#@param`, hidden `_cb` overrides, CodeBlock syntax, unassigned `#@out` |

Errors are `400` with a `detail` message meant to be read (bad param, bad
socket, ambiguous title...), `404` for an unknown project, `409` for a stale
`base_version`. Project names: one path segment,
`[A-Za-z0-9][A-Za-z0-9._ -]{0,63}`.

**Editing while a human has the graph open.** The editor applies your writes
live (it polls `GET /api/graph/{name}/version` and merges; changed nodes glow
amber) — no "reload before saving" is needed. To never overwrite the human's
edits, read `GET /api/graph/{name}/version?graph=1` → `{version, graph}` and
POST with `?base_version=<version>` (or a top-level `"base_version"` key): if
the graph changed since, you get **409** with the current `{version, graph}`
— re-apply your change to that and POST again. The response of a save carries
the new `version`. Without `base_version` a POST overwrites, as always. Node
ids are stable across editor saves.

## Graph JSON

```jsonc
{
  "name": "demo",
  "nodes": [
    { "id": "n1", "type": "Sphere", "params": {"radius": 3},
      "position": [120, 80], "preview": true, "title": "Ball" }
  ],
  "connections": [
    { "id": "l1", "from_node": "n1", "from_socket": "result",
      "to_node": "n2", "to_socket": "shape" }
  ]
}
```

`preview`: the viewport eye — `null` (auto) draws only terminal geometry,
`true`/`false` force it. `title` names a node; on an input node (Number
Slider, Integer, Boolean...) it also makes it a **graph parameter**, gathered
in the panel by `cad_arrange` — name the dimensions you want the user to tune.
`groups` (editor boxes) and the `_ui` / `_cb` param namespaces are editor-side
metadata — preserve them if present, never invent them.

## Wire types

`solid` (3D B-Rep) · `surface` (2D sketch/face) · `curve` · `plane` ·
`vector` (points) · `selection` (picked sub-shapes) · `mesh` (triangles) ·
`data` (universal bus: number/int/bool/str/list/domain — accepts and feeds
anything) · `tree`. Compatibility is enforced on connect with an explicit
error; widening casts (e.g. solid → mesh inputs) are applied automatically
where declared. The catalog tells you each socket's wire type — trust it.

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
  list-producing node) and they all fuse into one. It is dimension-agnostic and
  type-preserving — fusing 2D faces yields a `surface` (feed it straight into
  `Extrude`), fusing solids yields a `solid`. `Subtract` (`a` − `b`, `b` may be
  a list of tools) and `Intersect` stay two-input.
- **Fillet & chamfer are unified** — one node with a `mode` dropdown
  (`fillet`/`chamfer`) and a `size` param:
  - `FilletChamfer` — all edges of a solid.
  - `FilletChamferSelected` — only the sub-shapes from a `Select*` node: edges
    (3D) **or** vertices (2D corners via `SelectVertex`).
  - `FilletChamferCorners` — all corners of a 2D face/sketch; **outputs a curve**
    (the rounded outline). Fill with `MakeFace` or send straight to `Extrude`.
  - The old singles (`Fillet`, `Chamfer`, `Fillet2D`, …) are hidden/deprecated
    but still run for older graphs — prefer the unified nodes.

## Selections (Select* & predicate selectors)

Every selector — pick-based (`SelectEdge/Face/Vertex`) and predicate
(`FacesByNormal`, `EdgesByType`, `FacesByArea`, `EdgesByLength`, `FacesByType`,
`SubshapesByPosition`, `CombineSelection`) — has **two outputs**:

- `selection` (wire type `selection`) — drives a targeted op: `FilletChamferSelected`,
  `ExtrudeSelectedFace`, `ShellByFaces`, `CombineSelection`. Consumed whole.
- a **geometry** output that materialises the picked sub-shapes — `edges`→`curve`,
  `faces`→`surface`, `points`→`vector` (`SubshapesByPosition`/`CombineSelection`
  give `shapes`→`data`). It **fans out**: `SelectFace.faces → Extrude` extrudes
  each picked face.

Prefer **predicate** selectors: a rule ("every circular edge") survives a change
of geometry; a hand-picked list does not. `SelectShape` is different: it picks
WHOLE objects from a LIST (array copies, Voronoi cells…).

## Custom nodes (CodeBlock)

A `CodeBlock` runs build123d Python from its `code` param. Inputs `in_0`…`in_5`
are variables (unconnected ones are `None`); the code must assign `result`.
Declare knobs on a declaration line:

```python
teeth = 12        #@param int min=6 max=40
mode  = "spur"    #@param select=spur,helical
```

Each becomes a live slider, an editable value, and a **same-named input
socket** (wire a `Range` into it and the block fans out). Set it with
`cad_set_param(graph, node, {"teeth": 20})` — overrides live in `_cb`, the
source is never rewritten. Edit the code itself with `cad_edit_code`.

Prefer **named outputs** over returning a list that ListItem nodes unpack by
index: `#@out body: solid` (one per line) adds an OUTPUT socket carrying the
block's variable `body` (or `result["body"]`); `result` stays the first output.
A CodeBlock error reports `node_errors[id].line/col` relative to the block;
`lint` (in the execute result, or `cad_lint`) flags code that will not compile,
hidden `_cb` overrides and sliders that disagree with a `#@param`.

Use catalog nodes first; reach for CodeBlock only when no node fits. **Never
rewrite a CodeBlock the user made** — copy it and edit the copy.

## Retro-engineering, in one paragraph

"Retroeng the STL/STEP I passed" means: `GET /api/agent/tags`
(`cad_agent_tags`) finds the ToAgent-tagged file; `slice_summary` with that
`path` perceives it as symbolic cross-sections; rebuild it procedurally with
catalog nodes; verify with `slice_summary` without `path` on your own result.
Full loop: `cad_help(topic="retroeng")`.

## Cautions

- The engine executes graph code as **arbitrary Python, unsandboxed**, and the
  API is unauthenticated: it is meant for a trusted LAN. Don't put untrusted
  code in CodeBlocks.
- Execution overwrites `output.stl`/`view.json` per project (an `overrides`
  run too); the graph itself is only changed by your edits.
- Don't delete or overwrite projects you didn't create unless the user asks.

<!-- topics: each "## topic: <name>" section below is served on its own by
     cad_help(topic=<name>) / GET /api/agent/help?topic=<name>, and is not part
     of the default guide. -->

## topic: screenshots

`cad_screenshot` / `GET /api/graph/{name}/screenshot` → `image/png`. It drives
the app's own viewer (headless Chromium over `/nodes`), so the image is exactly
what the user sees — materials, glass, glow and all.

| arg | meaning |
|---|---|
| `view` | `iso` (default, front-right-top) · `front` `back` `left` `right` `top` `bottom` |
| `azim`, `elev` | degrees, instead of a preset. Azimuth in the XY plane from +X, elevation from it. The scene is **Z-up** |
| `zoom` | >1 pulls back, <1 closes in (default 1) |
| `node`, `isolate` | frame ONE node (id or title); `isolate=1` hides the rest. Any geometry node works, including an intermediate step that is not normally drawn — its eye is turned on for the shot and restored after (such a shot always re-runs) |
| `width`, `height`, `scale` | pixels (clamped to 4000) and device pixel ratio. Below ~600px wide the editor switches to its narrow layout — keep the default 900×700 |
| `projection` | `persp` or `ortho` — ortho reads better when checking alignment |
| `hq` | `0` turns off the high-quality path (glass/bloom): use it when a scene with several glass bodies times out |
| `chrome` | `1` keeps the legend/stat overlays (default: geometry only) |
| `run` | `0` reuses what is already rendered — cheap for extra angles (~1s vs ~10s). An edited graph is always re-run, so you never get the picture from before your edit |

Headers: `X-Noodle-Ran` (whether it re-executed) and `X-Noodle-Size-Mm` (the
framed bounds). Failures are never a 200: `400` for a bad argument or a node
with nothing to draw (a slider), `502` when the capture itself failed (with
the reason), `503` when the browser is missing from the deployment.

Habits that pay: take **two angles** when a shape is ambiguous from one;
`top`/`front` in `ortho` to check that things line up; isolate the node you
just changed; re-shoot with `run=0` for extra angles. The browser is kept warm
and needs no GPU.

## topic: retroeng

When the user says "retroeng the STL/STEP I passed": they tagged an
ImportSTL/ImportSTEP node with a **ToAgent** node in the editor.

1. `GET /api/agent/tags` (`cad_agent_tags`) — pick the newest /
   label-matching entry; it gives you the graph and the file's
   project-relative `path`. Don't ask which file.
2. **Perceive**: `slice_summary` with that `path` (`n≈10` per axis) — symbolic
   cross-sections on all 3 axes (`circle r=3 @(x,y)`, `rect 40x30`;
   "z=a…b identical" ⇒ an extrusion and its height). STEP is exact, STL is
   arc-fitted. The `text` field is the format meant for you.
3. **Microscope** where a line is ambiguous (`poly(…)`, unclear joins):
   `cad_section_outline(graph, axis="z", position=…, path=…)` /
   `GET .../section_outline?axis=z&pos=…&path=…` — ONE exact section, every loop
   edge by edge (LINE/CIRCLE, endpoints, radius + centre for arcs). Mesh
   sections can drop loops near tangent surfaces: confirm with nearby sections
   or per-section areas.
4. **Rebuild procedurally** in one `cad_apply_ops` batch: constant section →
   Extrude; repeated equal features → ArrayLinear/ArrayPolar driven by a count
   param, not copies; small rounds → a downstream FilletChamfer; key dimensions
   → titled Number Sliders. The user's stated parametrization intent wins.
5. **Verify with the same eyes**: execute, `slice_summary` **without** `path`
   (slices your own result), diff the two texts; per-section areas localize
   residuals; volume is the final checksum (the live bbox is approximate —
   compare sizes with ~1% tolerance). Then a screenshot next to the original.

Validated on real parts: a STEP rebuilt at Δvolume 0.05%, a 59k-triangle STL
at +2.2%.

## topic: print

Category `print`: nodes that answer what a slicer never asks — **which way up,
and why**. A printed part is anisotropic (the bond between layers is worth a
third to two thirds of the material), so orientation decides where it breaks.
Most take the `mesh` lane; a solid wired in is tessellated automatically.

- `PlaceOnBed` — lowest point to z=0 (measured on the tessellation, exact).
  A solid stays a solid.
- `PrintCheck` → a text report (wire into a Panel/Display): overhangs,
  supports, the weak plane.
- `OverhangFaces` — the faces that need support, as their own mesh (colour it).
- `SupportVolume` — the support as a BODY: a sweep of every overhanging
  triangle to the bed, minus the part and a clearance gap. It is the envelope
  (a slicer fills it sparse) and does not know about bridges.
- `OrientForPrint` — scores every stable pose; outputs the oriented mesh AND
  a report of why. **Strength needs a load**: wire a `load` vector and the
  score is how much of it crosses the layers; with none it optimises only
  printability and may hand you the weakest possible part.
- `Drop` — PlaceOnBed as a scrubbable fall (`t` 0→1, `material` sets the
  bounce; `settle` lets it topple onto a stable face). `collide=true` puts
  several shapes into ONE real rigid-body scene (costly); a `container` input
  is an immovable concave collider (a bowl, a tray); `motion` moves it;
  `wind` blows on it (topic `fluid`). Preview the Drop, not the bowl.
- `Motion` (type `ContainerMotion`) — a prescribed motion plan: move/rotate,
  `cycles=0` a one-way ramp (tilt, pour, unscrew), `>0` an oscillation (shake).
  `delay` waits, `pivot` sets the rotation centre.
- `Animate(shape, motion, t)` — the same Motion with NO physics: a lid
  unscrewing (`move z 12` + `rotate z 720`), a drawer sliding out. `hold` pads
  the timeline with stillness so one `t` slider can drive a short and a long
  motion together — pad the short clock, never rescale the wire. You rarely
  need it: /view gives every Animate its own slider, so independent parts need
  no common clock. Never chain Animate → Animate (lint `animate_chain`).

Examples in the gallery: `print-orientation`, `container-tilt`,
`jar-cap-unscrew`, `threaded-jar-pour`, `galton-board`.

## topic: threads

One node, `Thread` (category `fastener`): real screw threads — ISO metric,
trapezoidal, UNC/UNF, ACME, tapered NPT — male or female, multi-start, left or
right handed, as a watertight **mesh** (build123d has no thread primitive and
OCCT fuses them wrong; the mesh lane does it exactly in milliseconds).

- `kind=external` is a threaded rod; `kind=internal` is **the TAP**: subtract
  it from a body and it drills the hole and cuts the thread in one go. Wiring
  `shape` is the easy path — the node picks the boolean itself (external adds,
  internal cuts), so you cannot get the direction backwards.
- An external thread **brings its own core**: wire `shape` only for what you
  thread ONTO (a head, a flange). A shank as fat as the nominal diameter fills
  every groove — the thread silently disappears. Look at it (screenshot).
- **Place it with the `at` socket**, not `origin`: `at` positions the thread
  before the boolean. A list of points taps a whole hole pattern in one node.
- `clearance` loosens the thread it is set on — set it on ONE half of a
  mating pair, or you get double the gap. 0.3 mm is the FDM default.
- `size` picks a standard (`M6`, `Tr8x1.5`, `1/4-20 UNC`, …) or `custom` with
  `pitch` + `diameter`.

Example: `bolt-and-nut` (a thread that ADDS to a shank, a nut whose thread CUTS).

## topic: fluid

Category `fluid`. **`Wind`** is a plan, not geometry — it costs nothing and
drives two consumers:

- **`Drop.wind`** — *what happens to my part in this wind*: rigid bodies pushed
  by a fluid they do not disturb. Drag is computed per face of each part's
  hull, so a plate facing the gust tumbles and skids while an edge-on one barely
  moves. `medium` (air/water/oil/honey) adds buoyancy and drag; a liquid fills
  up to `level`. A wired Wind (or a non-vacuum medium) switches the Drop into
  scene mode by itself.
- **`WindTunnel`** — *what the part does to the fluid*: a real Lattice-Boltzmann
  solve around the part (~20 s at `quality=normal`, cached after), drawn as
  streamlines, plus a report for a Panel (drag coefficient, Reynolds, blockage
  ratio — above ~5% blockage the Cd is indicative only). `quality=draft` takes
  seconds and is for aiming. Cd is only comparable **at the same Reynolds**:
  compare two variants of a part in the same tunnel, not against textbook
  values across sizes.
- `Wind` shapes: `uniform` (steady stream), `jet` (a cone from `origin`: a fan,
  a nozzle), `vortex` (a swirl about the direction). `speed` is in mm/s;
  `turbulence`, `duration`, `delay`, `ramp` shape it in time.

Examples: `wind-drop` (the same plate twice, facing vs. edge-on) and
`wind-tunnel` (a blunt shape vs. a faired one).
