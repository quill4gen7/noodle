"""
noodle — MCP server (Fase 2).

Exposes the node-graph engine to AI agents via the Model Context Protocol.
Thin wrappers over `cad_nodes.api`; state is shared with the REST server through
the same projects directory ($CAD_PROJECTS_DIR, default /app/projects).

Run standalone over stdio (for MCP Inspector / clients):
    python mcp_server.py

Or mount the SSE app into another ASGI server via `mcp.sse_app()`.
"""

import json

from mcp.server.fastmcp import FastMCP, Image

from cad_nodes import api
from cad_nodes.store import GraphStore

mcp = FastMCP("noodle")
STORE = GraphStore()


def _safe(fn, *args, **kwargs):
    """Run an api call, converting exceptions to a structured error dict."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - surface message to the agent
        return {"error": f"{type(e).__name__}: {e}"}


def _lean_view(view, keep_mesh: bool = False):
    """Drop heavy tessellated meshes (top-level + per-node previews) and round
    floats, so the agent gets summaries, not megabytes of triangles."""
    return api.lean_view(view, keep_mesh=keep_mesh)


# ===========================================================================
# Tools — orientation
# ===========================================================================
@mcp.tool()
def cad_help(topic: str = "") -> str:
    """START HERE if this is your first noodle call: the orientation guide
    (markdown) — what noodle is, the graph model, wire types, list fan-out,
    every tool, and the build loop. `topic=` returns one detail section
    instead: screenshots, retroeng, print, threads, fluid."""
    try:
        return api.agent_help(topic)
    except Exception as e:  # noqa: BLE001 - an unknown topic lists the real ones
        return f"error: {e}"


# ===========================================================================
# Tools — graph lifecycle
# ===========================================================================
@mcp.tool()
def cad_create_graph(name: str, description: str = "") -> str:
    """Create a new empty graph. Returns the graph_id."""
    return _safe(api.create_graph, STORE, name, description)


@mcp.tool()
def cad_list_graphs() -> list:
    """List existing graph ids."""
    return api.list_graphs(STORE)


@mcp.tool()
def cad_delete_graph(graph_id: str) -> bool:
    """Delete a graph and all its files."""
    return _safe(api.delete_graph, STORE, graph_id)


# ===========================================================================
# Tools — node / connection editing
# ===========================================================================
@mcp.tool()
def cad_add_node(graph_id: str, node_type: str, params: dict = None,
                 position: list = None, parent: str = "", title: str = "") -> str:
    """Add a node. params = {param: value}, validated against the catalog
    (unknown names/bad types are errors). Omit `position` and the node is
    auto-placed in free space. `title` names it (addressable by that name).
    Returns node_id. Several nodes + wires at once: cad_apply_ops."""
    return _safe(api.add_node, STORE, graph_id, node_type, params or {},
                 tuple(position) if position else None, parent or None,
                 title or None)


@mcp.tool()
def cad_connect(graph_id: str, from_node_id: str, from_socket: str,
                to_node_id: str, to_socket: str) -> dict:
    """Connect an output socket to an input socket (nodes by id or title).
    Returns {connection_id, warnings}; a bad socket name lists the real ones."""
    return _safe(api.connect_checked, STORE, graph_id, from_node_id,
                 from_socket, to_node_id, to_socket)


@mcp.tool()
def cad_set_param(graph_id: str, node_id: str, params: dict,
                  base_version: str = "") -> dict:
    """Update parameters of a node addressed by id OR exact title. Values are
    validated (unknown param -> error listing valid names; bad type -> error
    naming node.param; out-of-range -> clamped and reported in `notes`).
    A CodeBlock's #@param knobs are set by their bare name."""
    return _safe(api.set_param, STORE, graph_id, node_id, params,
                 base_version or None)


@mcp.tool()
def cad_set_code(graph_id: str, node_id: str, code: str) -> bool:
    """Set the Python code of a CodeBlock node."""
    return _safe(api.set_code, STORE, graph_id, node_id, code)


@mcp.tool()
def cad_delete_node(graph_id: str, node_id: str) -> bool:
    """Remove a node and its connections."""
    return _safe(api.delete_node, STORE, graph_id, node_id)


@mcp.tool()
def cad_delete_connection(graph_id: str, connection_id: str) -> bool:
    """Remove a specific connection."""
    return _safe(api.delete_connection, STORE, graph_id, connection_id)


# ===========================================================================
# Tools — execution / inspection / export
# ===========================================================================
@mcp.tool()
def cad_execute(graph_id: str, overrides: dict = None,
                include_code: bool = False) -> dict:
    """Execute the graph. Returns success, errors, per-node errors, warnings,
    the slowest nodes and a lean view summary (bbox/volume/area/counts/panels,
    per-preview kind/bbox/volume; floats rounded; no meshes — cad_get_view
    fmt='mesh' for those). The generated code (hundreds of KB on a real
    graph) only with include_code=True — or read it with cad_get_code.

    `overrides={node_id_or_title: {param: value}}` runs with those values
    WITHOUT saving them: try a dimension, read the result, then commit it
    with cad_set_param if it is right."""
    result = _safe(api.execute, STORE, graph_id, overrides=overrides or None)
    return api.summarize_execute(result, include_code=include_code)


@mcp.tool()
def cad_slice_summary(graph_id: str, path: str = "", n_per_axis: int = 10) -> dict:
    """Symbolic cross-section summary (retro-engineering 'eyes'). Slices the
    shape with ~n_per_axis planes per axis (X/Y/Z) and returns compact text:
    bbox+volume checksum, then per-axis stacks with identical consecutive
    sections merged into intervals; each loop is classified (circle/rect/
    rrect/slot, poly fallback) with its holes. Empty `path`: slice the graph's
    OWN result. `path='assets/part.step'` (project-relative): slice that file.
    Rebuild loop: slice the target, build the graph, execute, slice again
    without path, compare the two texts, fix, repeat."""
    return _safe(api.slice_summary, STORE, graph_id, path or None, n_per_axis)


@mcp.tool()
def cad_section_outline(graph_id: str, axis: str = "z", position: float = 0.0,
                        path: str = "") -> dict:
    """The 'microscope' companion of cad_slice_summary: ONE exact section at
    axis=position, every loop edge by edge (LINE/CIRCLE, projected endpoints,
    radius+center for arcs). Empty `path`: section the graph's own result;
    `path='assets/part.step'`: section that file. Use it where a summary line
    is ambiguous (poly fallback, unclear joins)."""
    return _safe(api.section_outline, STORE, graph_id, axis, position, path or None)


@mcp.tool()
async def cad_screenshot(graph_id: str, view: str = "iso", azim: float = None,
                         elev: float = None, zoom: float = 1.0,
                         node: str = "", isolate: bool = False,
                         width: int = 900, height: int = 700,
                         run: bool = True) -> Image:
    """SEE the graph's geometry — render its viewport to a PNG you can look at.

    Numbers do not catch everything: a part can have the right volume, a
    watertight mesh and a green test suite while being visibly wrong (a boolean
    that filled the feature it was meant to cut, a part sunk through the bed, an
    array pointing the wrong way). Take a picture when you have built or changed
    something and want to know it is right.

    `view` is one of iso / front / back / left / right / top / bottom, or give
    `azim`+`elev` in degrees (azimuth in the XY plane from +X, elevation from
    it; the scene is Z-up). `zoom` > 1 pulls back. `node` (id or title)
    frames one node's output — any geometry node, including an intermediate
    step that is not normally drawn — and `isolate` hides the rest.
    `run=False` reuses what is already on screen instead of re-executing —
    cheap for extra angles. A failed capture is an error, never a blank image.
    """
    try:
        png, _meta = await api.screenshot(
            STORE, graph_id, view=view, azim=azim, elev=elev, zoom=zoom,
            node=node, isolate=isolate, width=width, height=height, run=run)
    except Exception as e:  # noqa: BLE001 - an agent cannot see a traceback
        raise RuntimeError(f"{type(e).__name__}: {e}") from e
    return Image(data=png, format="png")


@mcp.tool()
def cad_snapshot(graph_id: str, label: str = "", run: bool = True) -> dict:
    """SHOW the user a result: freeze the graph's current geometry as a new
    GENERATION and get back a `url` to the read-only 3D viewer. Send that link
    instead of screenshots — the user orbits it, hides pieces, inverts the
    selection, and it keeps showing THIS result after the workflow changes.
    `label` names it ("v2 thicker wall"). `run=True` executes first so the
    generation matches the graph as saved; an identical result to the newest
    generation is reused, not duplicated. Append `#hide=n3,n7` to the url to
    open it with those nodes' pieces hidden (e.g. a lid, to show the inside).
    If the graph has Animate/Drop nodes the viewer PLAYS them (`timeline` in the
    result): send one link for a movement — open ⇄ close — rather than one per
    pose; `#play=1` autoplays, `#t=0.5` / `#mode=pingpong|loop|once`."""
    return _safe(api.snapshot, STORE, graph_id, label=label, run=run)


@mcp.tool()
def cad_list_gens(graph_id: str) -> list:
    """The frozen generations of a graph, newest first, each with its viewer url."""
    return _safe(api.list_gens, STORE, graph_id)


@mcp.tool()
def cad_recent_gens(limit: int = 30, graph_id: str = "") -> list:
    """Generations across ALL projects (or one), newest first: `ref`
    (`graph/gN` — the name the user sees in the viewer header and on the
    /views gallery cards, so "graph/g3" is unambiguous), label, pieces, and
    `seen` = when the user last opened it. `last_seen: true` marks the one on
    their screen most recently: when they say "this one" / "questa", that is it.
    The gallery of every proposal is `/views` — send that link when you have
    made several alternatives to choose from."""
    return _safe(api.recent_gens, STORE, limit=limit, graph_id=graph_id)


@mcp.tool()
def cad_agent_tags() -> list:
    """Provenance index: every 'To Agent' tag node across ALL projects —
    label, date (auto-stamped at save), graph, node id and the tagged source
    (e.g. an ImportSTEP's file path). Resolve 'part X in workflow Y' here,
    then cad_slice_summary(graph_id=<that graph>, path=<that path>)."""
    return _safe(api.agent_tags, STORE)


@mcp.tool()
def cad_get_view(graph_id: str, fmt: str = "json") -> dict:
    """Read the last execution's view. fmt='mesh' includes the tessellated
    mesh; fmt='json' (default) omits it."""
    view = api.get_view(STORE, graph_id)
    if view is None:
        return {"error": "No view yet; call cad_execute first."}
    return _lean_view(view, keep_mesh=(fmt == "mesh"))


@mcp.tool()
def cad_get_code(graph_id: str) -> dict:
    """Return the build123d Python code the graph transpiles to."""
    return _safe(lambda: {"code": api.get_code(STORE, graph_id)})


@mcp.tool()
def cad_get_panels(graph_id: str) -> dict:
    """Read the values of all Panel nodes from the last execution."""
    return _safe(api.get_panels, STORE, graph_id)


@mcp.tool()
def cad_export(graph_id: str, fmt: str = "step") -> str:
    """Export the model. fmt: step | stl | gltf. Returns the file path."""
    return _safe(api.export, STORE, graph_id, fmt)


@mcp.tool()
def cad_codeblock_sections(graph_id: str, node: str, run: bool = False,
                           section: str = "") -> dict:
    """Read a long CodeBlock as the nodes it already contains, without changing
    it: sections (from its `# ---- title ----` headers, else grouped by the
    variable each statement builds), kind (params | quote | funcs | part |
    chain = a later step on a shape an earlier section made), line ranges,
    params used, the names flowing between sections, which section makes each
    output and each item of a `result = [...]` list. `node` = id or title.
    run=True also runs the block from an instrumented copy (saves nothing):
    seconds per section, the numbers each leaves behind, and `failed_in` if it
    raised; `section="sN"` adds that section's shapes as view previews."""
    if run or section:
        def _r(store, gid):
            res = api.codeblock_sections_run(store, gid, node, section or None)
            if res.get("view"):                       # meshes are for the viewer, not for you
                res["view"] = {"previews": sorted((res["view"].get("previews") or {}).keys())}
            return res
        return _safe(_r, STORE, graph_id)
    return _safe(api.codeblock_sections, STORE, graph_id, node)


@mcp.tool()
def cad_export_all(graph_id: str) -> dict:
    """Bake every PREVIEWED node (what the viewport shows) to STEP + STL, zipped
    with a manifest.json, into the project's exports/ (indexed in
    exports/index.jsonl with the node each file came from). Returns
    {path, file, manifest}."""
    def _run(store, gid):
        path, fname, manifest = api.export_all(store, gid)
        return {"path": path, "file": fname, "manifest": manifest}
    return _safe(_run, STORE, graph_id)


@mcp.tool()
def cad_get_node_catalog(filter_category: str = "", query: str = "",
                         full: bool = False):
    """Node types, one line each: `Type [category] in:(socket:wire, opt:wire?)
    out:(socket:wire) params:(name=default, ...)`. `query` filters by substring
    (type, label, description, aliases); `filter_category` by category.
    full=True returns the complete JSON list instead (large). One type in
    detail (ranges, options, description): cad_get_node_def."""
    if full:
        return api.list_catalog(filter_category)
    return api.compact_catalog(query=query, category=filter_category) or \
        f"no node type matches query={query!r} category={filter_category!r}"


# ===========================================================================
# Tools — agent editing: compact reads, small edits, atomic batches
# (one contiguous section; every tool here is a thin wrapper over cad_nodes.api)
# ===========================================================================
@mcp.tool()
def cad_get_graph(graph_id: str, node: str = "", positions: bool = False) -> dict:
    """Read a graph compactly: nodes (id, type, title, params — editor-only
    `_ui` state dropped, long code elided), connections as
    'id: from.socket -> to.socket', and `version`. Positions only with
    positions=True. `node=<id or title>` returns that ONE node in full
    (untruncated CodeBlock code) plus the connections touching it."""
    return _safe(api.get_graph_compact, STORE, graph_id, positions, node or None)


@mcp.tool()
def cad_get_node_def(node_type: str) -> dict:
    """One node type in detail: input/output sockets with wire types, params
    with type/default/min/max/options, and its description."""
    return _safe(api.node_def_for_agent, node_type)


@mcp.tool()
def cad_edit_code(graph_id: str, node: str, old: str, new: str,
                  base_version: str = "") -> dict:
    """Edit a CodeBlock's code by exact string replacement: `old` must occur
    EXACTLY once (0 or 2+ matches is an error and nothing is saved). Far
    cheaper than resending the script with cad_set_code. `node` = id or title.
    Read the current code with cad_get_graph(node=...)."""
    return _safe(api.edit_code, STORE, graph_id, node, old, new,
                 base_version=base_version or None)


@mcp.tool()
def cad_set_node(graph_id: str, node: str, title: str = None,
                 preview: bool = None, bypassed: bool = None,
                 color: str = None, base_version: str = "") -> dict:
    """Set node attributes (not params): `title` (a name you can address it
    by; on an input slider it also promotes it to a graph parameter),
    `preview` (the viewport eye: true/false), `bypassed`, `color` (hex)."""
    props = {k: v for k, v in (("title", title), ("preview", preview),
                               ("bypassed", bypassed), ("color", color))
             if v is not None}
    return _safe(api.set_node, STORE, graph_id, node,
                 base_version=base_version or None, **props)


@mcp.tool()
def cad_apply_ops(graph_id: str, ops: list, base_version: str = "") -> dict:
    """Apply a batch of edits ATOMICALLY — all validated, one save — or none
    at all (the error names the failing op index). Ops:
      {op:'add_node', type, params?, position?, id?, title?}
      {op:'connect', from:'node.socket', to:'node.socket'}
      {op:'disconnect', id} | {op:'disconnect', from?, to?}
      {op:'set_param', node, params}
      {op:'edit_code', node, old, new}      {op:'set_code', node, code}
      {op:'set_node', node, title?, preview?, bypassed?, color?}
      {op:'remove', node}
    A node ref is an id, an exact title, or '$N' = the node created by op N
    (0-based) of this same batch. New nodes without a position are placed
    right of whatever they got wired to. Returns results, created ids,
    validation warnings and the new version."""
    return _safe(api.apply_ops, STORE, graph_id, ops, base_version or None)


@mcp.tool()
def cad_arrange(graph_id: str) -> dict:
    """Tidy the whole graph's layout (left-to-right by dependency, real node
    sizes, guaranteed no overlaps; named sliders gathered as a parameter
    panel on the left). Saves. Returns the layout summary."""
    return _safe(api.arrange, STORE, graph_id)


@mcp.tool()
def cad_validate(graph_id: str) -> dict:
    """Check a graph without running it: wiring errors (bad socket names list
    the real ones), soft warnings (unconnected required inputs) and stored
    params that are unknown, badly typed or out of range."""
    return _safe(lambda: api.validation_report(STORE.load(graph_id)))


# ===========================================================================
# Tools — geometry facts + lint (cad_nodes/measure.py, cad_nodes/lint.py).
# Self-contained block; imports are local.
# ===========================================================================
@mcp.tool()
def cad_measure(graph_id: str, queries: list) -> dict:
    """Geometry FACTS about node outputs — ask instead of writing a script.
    Reference nodes as "n5", a named output "n51.body", a list item "n51[3]".
    queries: list of
      {"op":"props","node":"n5","each":true}  bbox/volume/area/center/valid/
          solids/shells (solids>1 on one part = it is split in pieces)
      {"op":"interference","a":"n5","b":"n7"}  overlap volume + its bbox
      {"op":"interference","node":"n51"}  every pair inside a list value
          (or "nodes":[...]); only clashing pairs are listed unless "all":true
      {"op":"distance","a":"n5","b":"n7"}  min distance + closest points
      {"op":"section","node":"n5","axis":"z","offset":3,"svg":false,
          "outline":false}  regions/holes/area/bbox2d at that cut
      {"op":"probe","node":"n5","points":[[x,y,z],...]}  in | on | out
      {"op":"summary","node":"n5","n":10}  slice_summary of that node only
    Each query answers alone (a bad one carries "error"). Numbers rounded."""
    from cad_nodes.executor import measure_graph
    return _safe(lambda: measure_graph(STORE.load(graph_id), STORE.dir(graph_id),
                                       queries))


@mcp.tool()
def cad_lint(graph_id: str) -> dict:
    """Soft findings on a graph (no run): sliders whose value/min/max disagree
    with the CodeBlock #@param they drive, hidden per-block `_cb` overrides
    (the value that runs is not the one in the code), CodeBlocks that do not
    compile (block-relative line/col), #@out never assigned. Call it after
    cad_set_code; cad_execute also returns it as `lint`."""
    from cad_nodes.lint import lint_graph
    return _safe(lambda: {"lint": lint_graph(STORE.load(graph_id))})


# ===========================================================================
# Resources
# ===========================================================================
@mcp.resource("cad://help")
def res_help() -> str:
    return api.agent_help()


@mcp.resource("cad://nodes")
def res_nodes() -> str:
    return json.dumps(api.list_catalog(), indent=2)


@mcp.resource("cad://nodes/{node_type}")
def res_node(node_type: str) -> str:
    return json.dumps(api.get_node_def(node_type), indent=2)


@mcp.resource("cad://graph/{graph_id}")
def res_graph(graph_id: str) -> str:
    return json.dumps(api.get_graph(STORE, graph_id), indent=2)


@mcp.resource("cad://graph/{graph_id}/code")
def res_graph_code(graph_id: str) -> str:
    return api.get_code(STORE, graph_id)


@mcp.resource("cad://graph/{graph_id}/view")
def res_graph_view(graph_id: str) -> str:
    return json.dumps(api.get_view(STORE, graph_id), indent=2)


# ===========================================================================
# Prompts
# ===========================================================================
@mcp.prompt()
def cad_design(descrizione: str) -> str:
    # (argument names are part of the MCP interface: kept as they were)
    return f"""Design a part for: "{descrizione}"

1. cad_help once if you have not read it; cad_get_node_catalog(query=...) to
   find node types, cad_get_node_def(type) for one type's params and ranges.
2. cad_create_graph, then build it in ONE cad_apply_ops batch: add_node the
   primitives, connect booleans (Union/Subtract/Intersect) and modifiers
   (FilletChamfer...). Name the key dimensions with titled NumberSliders.
3. cad_execute; fix node_errors; check bbox/volume in the view summary.
4. Try values with cad_execute(overrides=...) before committing them with
   cad_set_param; cad_screenshot to LOOK at the result.
5. cad_arrange to tidy the canvas, cad_export(fmt='step') when right."""


@mcp.prompt()
def cad_modify(graph_id: str, istruzioni: str) -> str:
    return f"""Modify graph {graph_id} as follows: "{istruzioni}"

1. cad_get_graph({graph_id}) — compact; cad_get_graph(node=...) for one
   node's full code. (Avoid cad_get_code: it is the whole generated script.)
2. Small edits: cad_set_param (id or title), cad_edit_code (str-replace in a
   CodeBlock); several at once: cad_apply_ops (atomic).
3. cad_execute and verify (view summary, cad_screenshot).
4. Export only when satisfied."""


@mcp.prompt()
def cad_analyze(graph_id: str) -> str:
    return f"""Analyze graph {graph_id}:
1. cad_execute if it has not run yet
2. the view summary: volume, area, bbox, center, counts (faces/edges/solids)
3. cad_get_panels for Panel nodes; cad_slice_summary for the cross-sections
4. cad_screenshot from a couple of angles
5. a structural summary of the model (cad_get_graph)."""


if __name__ == "__main__":
    mcp.run()
