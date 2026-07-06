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
                       /api/system/health|logs|restart.
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
  index.html         the `/ui` code view — generated build123d source (read-only
                       text) + STL preview. Parameter literals are highlighted
                       and click-to-edit via a terminal-style inline editor that
                       PATCHes the graph param and re-renders — non-destructive
                       (the code is regenerated; structure stays in nodes.html).
                       See PLAN_CODE_PARAMS.md.
  nodes.html         the node editor + 3D viewer (litegraph-style). Holds
                       WIRE_COLORS and INPUT_ACCEPTS — a MIRROR of the backend
                       wire-compatibility table that MUST be kept in sync (§5).
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
                       code_template that the transpiler fills in. Also defines
                       the wire types and WIRE_COMPATIBLE table. ADD NODES HERE.
  transpiler.py      ★ Graph -> build123d source. Flat "algebra" assignments in
                       topo order; group nodes (BuildPart/BuildSketch) emit
                       nested `with` blocks. PREAMBLE injects runtime helpers
                       (_at, _pushpull, _section, _bbox_plane, _rotate,
                       _select_subshapes). Each node is wrapped in try/except so
                       one failing node is recorded in __errors__, not fatal.
                       run(emit_map=True) / transpile_with_map() also return a
                       param<->code source map (sentinel-wrapped literals measured
                       on the final text) for the editable code view. A CodeBlock
                       transpiles like two connected nodes: `#@param` decls
                       (parse_codeblock_params) become the generated function's
                       named ARGUMENTS — body stays pure (declaration lines dropped),
                       each value appears once at the call site as an editable span
                       (override in node.params["_cb"], wired socket drives + fans
                       out). The body itself is an editable `code` span (kind=code).
  executor.py        Runs the generated script in a worker subprocess; captures
                       STL + view JSON + per-node errors. execute_graph(graph, workdir).
  worker.py / mesh_extractor.py   the subprocess + meshing.
  graph.py           Graph/Node/Connection dataclasses; from_dict/to_dict;
                       validate() (raises on incompatible wires / unknown sockets).
  api.py             High-level ops over a GraphStore: add_node, connect,
                       set_param, delete_node, execute, transpile. Shared by the
                       MCP server AND the copilot — the single source of truth.
  store.py           GraphStore: load/save projects/<name>/{graph,meta,view}.json
                       and output.stl.
  copilot.py         ★ in-app NL copilot (§7). OpenAI-compatible tool loop.
  toposort.py        topological sort + cycle detection.
  catalog … examples/  sample graphs used by tests.
projects/            saved graphs (written as uid 1000 — host-editable).
tests/               test_engine.py, test_api.py — pure-Python (no build123d).
PLAN_NODE_CAD.md     the full design doc + node roadmap (~150 planned nodes).
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

Execution writes `output.stl` + `view.json` (meshes) alongside it.

## 5. Wire types

Typed wires gate which output may feed which input. The table lives in TWO places
that **must stay in sync**:

- backend: `cad_nodes/catalog.py` → `WIRE_COMPATIBLE` (`output_type -> {accepted input types}`),
  enforced hard by `Graph.validate()`.
- frontend: `webui/nodes.html` → `INPUT_ACCEPTS` (`input_type -> [accepted output types]`,
  the inverse), which decides whether a wire can be dragged.

Types: `geometry` (solids), `sketch` (2D), `curve`, `plane`, `vector` (points),
`selection` (picked sub-shapes), `data`, `tree`. `data` is the universal wire
(accepts anything AND feeds anything). `geometry` and `plane` are interchangeable
so transforms treat a plane like any geometry.

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
Other list-producers: `Voronoi2D` (scipy → cell faces), `DivideSurface`
(`Face.position_at` UV grid → points) — both fan out downstream (Extrude per
cell, scatter per point). scipy/numpy are available in the worker.
Frontend multi-connect = dynamic input slots sharing one socket name (see
`onConnectionsChange` + `fromGraphJSON` in `nodes.html`).

## 6. How to change things

**Add or edit a node** — almost always pure data in `catalog.py`:

```python
register(NodeDef("MyNode", "category", "My Node",
    inputs=[Socket("shape", WIRE_GEOMETRY), Socket("plane", WIRE_PLANE, required=False)],
    params=[_f("amount", 1.0, 0.0, 100)],          # _f/_i = float/int slider helpers
    outputs=_geo(),                                 # _geo/_sk = geometry/sketch output
    code_template={"algebra": "my_op({shape}, {amount})"},
    description="..."))
```

`{socket}` → the upstream variable; `{param}` → the formatted value. If the node
needs runtime logic that doesn't fit one expression, add a helper to the
transpiler **PREAMBLE** and call it from the template (e.g. `_bbox_plane`,
`_rotate`). If you change wire types, update `INPUT_ACCEPTS` in `nodes.html` too.

**Group nodes** (BuildPart/BuildSketch) use `is_group=True` + a `builder`
template and emit nested `with` blocks — see existing examples.

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

## 8. Tests

`tests/` are pure-Python (no build123d needed): toposort, graph validation,
transpiler output, api ops.

```bash
python -m pytest tests/ -v        # pytest may need installing in your env
```
