"""
Application layer — high-level graph operations shared by the REST server and
the MCP server. Pure Python (no `mcp`, no FastAPI); build123d only enters via
the executor subprocess.

Every function takes a `GraphStore` so callers control where graphs live.
Errors are raised as ValueError/KeyError; transport layers translate them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import shutil
import time

from . import catalog, export_index
from .executor import (execute_graph, export_bundle, export_graph, section_outline_file,
                       section_outline_graph, slice_summary_file,
                       slice_summary_graph)
from .graph import Connection, Graph, Node, ValidationError
from .store import GraphStore
from .transpiler import parse_codeblock_params, transpile, transpile_with_map


# --- orientation ----------------------------------------------------------
_TOPIC_MARK = "<!-- topics"
_TOPIC_HEAD = re.compile(r"^## topic: (\S+)[ \t]*$", re.M)


def agent_help(topic: str = "") -> str:
    """The self-contained remote-agent guide (cad_nodes/AGENT_HELP.md): what
    noodle is, the graph model, wire rules, the HTTP/MCP surface and the
    standard build + retro-engineering loops. Served on every surface so an
    agent on another machine can orient itself with one call.

    The file ends with `## topic: <name>` sections (screenshots, retroeng,
    print, ...) that are NOT part of the default guide — `topic=<name>`
    returns just that section, so detail costs nothing until it is wanted."""
    text = (Path(__file__).resolve().parent / "AGENT_HELP.md").read_text(
        encoding="utf-8")
    core, _, tail = text.partition(_TOPIC_MARK)
    heads = list(_TOPIC_HEAD.finditer(tail))
    topics = {m.group(1): tail[m.start():(heads[i + 1].start() if i + 1 < len(heads)
                                          else len(tail))].strip()
              for i, m in enumerate(heads)}
    if not topic:
        return core.rstrip() + "\n"
    if topic not in topics:
        raise ValueError(f"no help topic {topic!r}; topics: {', '.join(topics)}")
    return topics[topic] + "\n"


def help_topics() -> list[str]:
    text = (Path(__file__).resolve().parent / "AGENT_HELP.md").read_text(
        encoding="utf-8")
    return _TOPIC_HEAD.findall(text.partition(_TOPIC_MARK)[2])


# --- catalog --------------------------------------------------------------
def list_catalog(category: str = "") -> list[dict]:
    nodes = catalog.as_json()
    if category:
        nodes = [n for n in nodes if n.get("category") == category]
    return nodes


def get_node_def(node_type: str) -> dict:
    from dataclasses import asdict
    return asdict(catalog.get(node_type))


# --- graph lifecycle ------------------------------------------------------
def create_graph(store: GraphStore, name: str, description: str = "") -> str:
    if store.exists(name):
        raise ValueError(f"Graph {name!r} already exists")
    store.save(name, Graph(name=name), description)
    return name


def get_graph(store: GraphStore, graph_id: str) -> dict:
    return store.load(graph_id).to_dict()


def list_graphs(store: GraphStore) -> list[str]:
    return store.list()


def delete_graph(store: GraphStore, graph_id: str) -> bool:
    store.delete(graph_id)
    return True


# --- graph versions (hook) -------------------------------------------------
class StaleGraphError(ValueError):
    """A write was based on a graph version that is no longer the stored one."""


def graph_version(store: GraphStore, graph_id: str):
    """The stored graph's version, when the store keeps one; else None.

    Duck-typed (`store.version(graph_id)`, the content hash of graph_version.py)
    so a store without versions still works: then writes are never refused."""
    fn = getattr(store, "version", None)
    return fn(graph_id) if callable(fn) else None


def _check_base_version(store: GraphStore, graph_id: str, base_version) -> None:
    """Refuse a write whose `base_version` is not the current one (optimistic
    concurrency, same hash the editor's saves carry — see graph_version.py)."""
    if base_version is None:
        return
    current = graph_version(store, graph_id)
    if current is not None and str(current) != str(base_version):
        raise StaleGraphError(
            f"graph {graph_id!r} changed since version {base_version} (now "
            f"{current}); re-read it with cad_get_graph and retry")


# --- a CodeBlock read as nodes (cad_nodes/sections.py) ---------------------
def codeblock_sections(store: GraphStore, graph_id: str, node_ref: str) -> dict:
    """The sections a CodeBlock already contains: kinds, line ranges, the names
    flowing between them, which section makes each output. No run."""
    from . import sections
    graph = store.load(graph_id)
    node = resolve_node(graph, node_ref)
    if node.type != "CodeBlock":
        raise ValueError(f"{node.id!r} is a {node.type}, not a CodeBlock")
    return {"node": node.id, "title": node.title or "CodeBlock",
            **sections.compact(sections.analyze(node.params.get("code", "") or ""))}


def codeblock_sections_run(store: GraphStore, graph_id: str, node_ref: str,
                           section: Optional[str] = None) -> dict:
    """Run the block from an instrumented copy: per-section time and numbers,
    and the shapes `section` built (as view previews). Saves nothing."""
    from . import sections
    from .executor import codeblock_sections_run as _run
    graph = store.load(graph_id)
    node = resolve_node(graph, node_ref)
    res = _run(graph, node.id, store.dir(graph_id), section)
    res["analysis"] = sections.compact(res["analysis"])
    res["node"] = node.id
    return res


# --- node addressing -------------------------------------------------------
def resolve_node(graph: Graph, ref: str) -> Node:
    """Find a node by id, or else by its exact user-given title.

    Titles are what an agent reads on screen ("Altezza", "Scocca frontale"), so
    every editing op accepts either. An ambiguous title is an error naming the
    candidates rather than a silent pick of the first one."""
    ref = str(ref)
    for n in graph.nodes:
        if n.id == ref:
            return n
    titled = [n for n in graph.nodes if n.title == ref]
    if len(titled) == 1:
        return titled[0]
    if len(titled) > 1:
        raise KeyError(f"Title {ref!r} is ambiguous — nodes "
                       f"{', '.join(n.id for n in titled)}; use the id")
    raise KeyError(f"No node with id or title {ref!r} in graph")


# --- param validation ------------------------------------------------------
# Keys the EDITOR stores next to catalog params: `_`-prefixed UI state (`_ui`,
# `_cb` CodeBlock overrides), a selector's picked sub-shapes, a TraceImage's
# frozen contours. Accepted as-is; everything else must be a declared param.
_STATE_PARAMS = {"selection", "trace"}
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _coerce_clamp(kind: str, value, *, lo=None, hi=None, options=None,
                  where: str = "value", notes: Optional[list] = None,
                  optional: bool = False):
    """Coerce a value to its declared param type, clamping numerics to [lo, hi].

    Strict about garbage (a non-numeric string for a float, an unknown select
    option) — raises ValueError naming `where` — but lenient about spelling
    (`"12.5"`, `"true"`), because the code view sends strings. A clamp is not an
    error, but it is reported through `notes` so an agent learns its value was
    changed. Shared by built-in params and CodeBlock `#@param` overrides."""
    if value is None and optional:
        return None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE + _FALSE:
            return value.strip().lower() in _TRUE
        raise ValueError(f"{where} expects a bool (true/false), got {value!r}")
    if kind == "select":
        value = str(value)
        if options and value not in options:
            raise ValueError(f"{where}: {value!r} is not one of {list(options)}")
        return value
    if kind in ("int", "float"):
        if isinstance(value, bool) or isinstance(value, (list, dict, tuple)):
            raise ValueError(f"{where} expects {kind}, got {value!r}")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{where} expects {kind}, got {value!r}") from None
        if num != num or num in (float("inf"), float("-inf")):
            raise ValueError(f"{where} expects a finite {kind}, got {value!r}")
        asked = num
        if lo is not None:
            num = max(num, float(lo))
        if hi is not None:
            num = min(num, float(hi))
        if num != asked and notes is not None:
            notes.append(f"{where} clamped to {num:g} (asked {asked:g}, "
                         f"range [{lo if lo is not None else '-inf'}, "
                         f"{hi if hi is not None else 'inf'}])")
        return int(round(num)) if kind == "int" else num
    if kind == "str":
        if isinstance(value, (dict, list, tuple)):
            raise ValueError(f"{where} expects a string, got {type(value).__name__}")
        return str(value)
    return value            # structured params (curve, curve3d): stored as given


def _cb_decls(node: Node) -> list[dict]:
    return parse_codeblock_params(node.params.get("code", "")) \
        if node.type == "CodeBlock" else []


def _apply_param(node: Node, name: str, value, notes: Optional[list] = None):
    """Validate, coerce and store ONE param on an in-memory node; returns the
    stored value. A CodeBlock `#@param` is addressed as `_cb.<name>` or by its
    bare name, and lands in the `_cb` override namespace (never in the source).
    Raises ValueError naming the node, the param and the valid alternatives."""
    where = f"{node.id}.{name}"
    ndef = catalog.get(node.type)
    pdef = next((p for p in ndef.params if p.name == name), None)
    if pdef is not None and pdef.widget == "slider" and ndef.category == "input":
        # An input slider's catalog min/max is only its DEFAULT drag window —
        # the user widens it with ⚙ (`_ui[name]`), and the engine never clamps.
        # So a value outside the window widens the window instead of being cut.
        value = _coerce_clamp(pdef.type, value, where=where, notes=notes)
        ui = dict(node.params.get("_ui") or {})
        win = dict(ui.get(name) or {})
        lo = win.get("min", pdef.min)
        hi = win.get("max", pdef.max)
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            win["min"] = min(v for v in (lo, value) if v is not None)
            win["max"] = max(v for v in (hi, value) if v is not None)
            ui[name] = win
            node.params["_ui"] = ui
            if notes is not None:
                notes.append(f"{where}: slider window widened to "
                             f"[{win['min']:g}, {win['max']:g}] to fit {value:g}")
        node.params[name] = value
        return value
    if pdef is not None:
        value = _coerce_clamp(pdef.type, value, lo=pdef.min, hi=pdef.max,
                              options=pdef.options or None, where=where,
                              notes=notes, optional=pdef.optional)
        node.params[name] = value
        return value

    cb_name = name[4:] if name.startswith("_cb.") else name
    decl = next((d for d in _cb_decls(node) if d["name"] == cb_name), None)
    if decl is not None:
        value = _coerce_clamp(decl["type"], value, lo=decl["min"], hi=decl["max"],
                              options=decl["options"], where=where, notes=notes)
        overrides = dict(node.params.get("_cb") or {})
        overrides[cb_name] = value
        node.params["_cb"] = overrides
        return value
    if name.startswith("_cb."):
        raise ValueError(f"CodeBlock {node.id} declares no #@param {cb_name!r}")

    if name.startswith("_") or name in _STATE_PARAMS:
        node.params[name] = value          # editor-owned state, stored as-is
        return value

    valid = [p.name for p in ndef.params] + [d["name"] for d in _cb_decls(node)]
    raise ValueError(f"Node {node.id} ({node.type}) has no param {name!r}. "
                     f"Valid params: {', '.join(valid) or '(none)'}")


def _apply_params(node: Node, params: dict, notes: Optional[list] = None) -> dict:
    """Apply several params; a CodeBlock's `code` goes first so `#@param`
    names it declares resolve in the same call."""
    if not isinstance(params, dict):
        raise ValueError(f"params for {node.id} must be an object {{name: value}}")
    out = {}
    for name in sorted(params, key=lambda k: k != "code"):
        out[name] = _apply_param(node, name, params[name], notes)
    return out


def check_params(graph: Graph) -> list[str]:
    """Soft audit of a whole graph's stored params against the catalog (unknown
    names, uncoercible values). Nothing is changed. Used by `cli validate` and
    whole-graph saves, where a hard failure would break hand-edited graphs."""
    issues = []
    for n in graph.nodes:
        if n.type not in catalog.REGISTRY:
            continue
        probe = Node(id=n.id, type=n.type, params=dict(n.params))
        notes: list[str] = []
        for name, value in n.params.items():
            try:
                _apply_param(probe, name, value, notes)
            except ValueError as e:
                issues.append(str(e))
        issues += [f"out of range: {m}" for m in notes]
    return issues


# --- wiring validation -----------------------------------------------------
def _sockets_hint(graph: Graph, node_id: str, side: str) -> str:
    try:
        node = graph.node(node_id)
        ndef = catalog.get(node.type)
    except Exception:  # noqa: BLE001 - the hint must never mask the real error
        return ""
    names = [s.name for s in (ndef.inputs if side == "input" else ndef.outputs)]
    if side == "input":
        names += [d["name"] for d in _cb_decls(node)]
    return f" Valid {side}s of {node_id} ({node.type}): {', '.join(names) or '(none)'}"


def validation_report(graph: Graph) -> dict:
    """Everything checkable without running: {ok, error?, warnings,
    param_issues}. `error` is a hard wiring/type problem; `param_issues` are
    stored params that are unknown, badly typed or out of range."""
    # Out-of-range values still run (the engine does not clamp stored params;
    # only edits do), so they warn instead of failing the report.
    all_issues = check_params(graph)
    issues = [i for i in all_issues if not i.startswith("out of range")]
    ranged = [i for i in all_issues if i.startswith("out of range")]
    try:
        warnings = validate_graph(graph) + ranged
    except (ValidationError, KeyError, ValueError) as e:
        return {"ok": False, "error": str(e), "warnings": ranged,
                "param_issues": issues}
    return {"ok": not issues, "warnings": warnings, "param_issues": issues}


def validate_graph(graph: Graph) -> list[str]:
    """`Graph.validate()` with agent-friendly errors: a bad socket name comes
    back listing the node's real sockets. Returns the soft warnings."""
    try:
        return graph.validate()
    except ValidationError as e:
        msg = str(e)
        m = re.search(r"node (\S+) \(\w+\) has no (input|output) ", msg)
        if m:
            raise ValidationError(msg + "." + _sockets_hint(graph, m.group(1),
                                                            m.group(2))) from None
        raise


def _warnings_for(warnings: list[str], node_ids) -> list[str]:
    ids = set(node_ids)
    return [w for w in warnings
            if (m := re.match(r"Node (\S+) ", w)) and m.group(1) in ids]


# --- auto placement --------------------------------------------------------
_X_GAP, _Y_GAP = 90.0, 45.0


def _place_new(graph: Graph, new_ids: list[str]) -> dict:
    """Give nodes created without a position a free spot instead of (0,0): right
    of their upstream nodes when they have any, else right of the whole graph;
    then nudged down until nothing overlaps. Uses the real on-canvas sizes from
    `layout`. Returns {node_id: [x, y]} for what it placed."""
    from . import layout as _layout
    if not new_ids:
        return {}
    pending = set(new_ids)
    placed = [n for n in graph.nodes if n.id not in pending]
    by_id = {n.id: n for n in graph.nodes}

    def box(n):
        return _layout.node_box(catalog.get(n.type), n)

    # upstream first, so a chain of new nodes flows left to right
    order, seen = [], set()

    def visit(nid):
        if nid in seen:
            return
        seen.add(nid)
        for c in graph.connections:
            if c.to_node == nid and c.from_node in pending:
                visit(c.from_node)
        order.append(nid)
    for nid in new_ids:
        visit(nid)

    out = {}
    for nid in order:
        node = by_id[nid]
        ups = [by_id[c.from_node] for c in graph.connections
               if c.to_node == nid and c.from_node in by_id
               and c.from_node not in pending]
        if ups:
            x = max(box(u).x + box(u).w for u in ups) + _X_GAP
            y = sum(float(u.position[1]) for u in ups) / len(ups)
        elif placed:
            x = max(box(u).x + box(u).w for u in placed) + _X_GAP
            y = min(float(u.position[1]) for u in placed)
        else:
            x, y = 80.0, 120.0
        node.position = (round(x), round(y))
        for _ in range(500):
            mine = box(node)
            hit = [u for u in placed if mine.overlaps(box(u), 10.0)]
            if not hit:
                break
            y = max(box(u).y + box(u).h for u in hit) + _layout.NODE_TITLE_HEIGHT + 20
            node.position = (round(x), round(y))
        pending.discard(nid)
        placed.append(node)
        out[nid] = list(node.position)
    return out


# --- in-memory edit ops (no I/O; shared by single calls and apply_ops) -----
def _unique_node_id(graph: Graph, node_type: str) -> str:
    base = node_type.lower()
    existing = {n.id for n in graph.nodes}
    i = 1
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"


def _op_add_node(graph: Graph, node_type: str, params: Optional[dict] = None,
                 position=None, parent: Optional[str] = None,
                 node_id: Optional[str] = None, title: Optional[str] = None,
                 notes: Optional[list] = None) -> str:
    if node_type not in catalog.REGISTRY:
        close = [t for t in catalog.REGISTRY if node_type.lower() in t.lower()][:8]
        raise ValueError(f"Unknown node type {node_type!r}"
                         + (f". Did you mean: {', '.join(close)}?" if close else
                            ". See cad_get_node_catalog."))
    if node_id is not None:
        if any(n.id == node_id for n in graph.nodes):
            raise ValueError(f"Node id {node_id!r} already exists")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]*", str(node_id)):
            raise ValueError(f"Invalid node id {node_id!r}")
    nid = node_id or _unique_node_id(graph, node_type)
    node = Node(id=nid, type=node_type, parent=parent or None,
                position=tuple(position) if position is not None else (0.0, 0.0),
                title=title or None)
    _apply_params(node, params or {}, notes)
    graph.nodes.append(node)
    return nid


def _next_connection_id(graph: Graph) -> str:
    cid = f"c{len(graph.connections) + 1}"
    while any(c.id == cid for c in graph.connections):
        cid += "_"
    return cid


def _op_connect(graph: Graph, from_node: str, from_socket: str, to_node: str,
                to_socket: str) -> str:
    src = resolve_node(graph, from_node).id
    dst = resolve_node(graph, to_node).id
    cid = _next_connection_id(graph)
    graph.connections.append(Connection(cid, src, from_socket, dst, to_socket))
    try:
        validate_graph(graph)
    except ValidationError:
        graph.connections.pop()
        raise
    return cid


def _op_disconnect(graph: Graph, connection_id: Optional[str] = None,
                   from_node: Optional[str] = None, from_socket: Optional[str] = None,
                   to_node: Optional[str] = None, to_socket: Optional[str] = None
                   ) -> list[str]:
    """Remove a connection by id, or every one matching the given endpoints."""
    if connection_id:
        hit = [c for c in graph.connections if c.id == connection_id]
    else:
        if not (from_node or to_node):
            raise ValueError("disconnect needs `id` or from/to endpoints")
        f = resolve_node(graph, from_node).id if from_node else None
        t = resolve_node(graph, to_node).id if to_node else None
        hit = [c for c in graph.connections
               if (f is None or c.from_node == f)
               and (from_socket is None or c.from_socket == from_socket)
               and (t is None or c.to_node == t)
               and (to_socket is None or c.to_socket == to_socket)]
    if not hit:
        raise ValueError("no matching connection")
    ids = {c.id for c in hit}
    graph.connections = [c for c in graph.connections if c.id not in ids]
    return sorted(ids)


def _op_edit_code(graph: Graph, ref: str, old: str, new: str,
                  param: str = "code") -> dict:
    """Exact string replacement inside a node's source (CodeBlock `code` by
    default). Exactly one match is required, so an edit can never land in the
    wrong place: 0 or 2+ matches is an error saying which."""
    node = resolve_node(graph, ref)
    pdef = next((p for p in catalog.get(node.type).params if p.name == param), None)
    if pdef is None or pdef.type != "str":
        raise ValueError(f"Node {node.id} ({node.type}) has no text param {param!r}")
    if not old:
        raise ValueError("`old` must be a non-empty string")
    src = str(node.params.get(param, pdef.default or ""))
    n = src.count(old)
    if n == 0:
        raise ValueError(f"{node.id}.{param}: `old` text not found "
                         f"({len(src.splitlines())} lines searched) — "
                         "check whitespace/indentation, or read it with "
                         f"cad_get_graph(node={node.id!r})")
    if n > 1:
        raise ValueError(f"{node.id}.{param}: `old` matches {n} times — "
                         "include more surrounding context so it is unique")
    node.params[param] = src.replace(old, new, 1)
    line = src[:src.index(old)].count("\n") + 1
    return {"node": node.id, "param": param, "line": line}


def _op_remove(graph: Graph, ref: str) -> str:
    nid = resolve_node(graph, ref).id
    graph.nodes = [n for n in graph.nodes if n.id != nid]
    graph.connections = [c for c in graph.connections
                         if c.from_node != nid and c.to_node != nid]
    return nid


# Node attributes (not params) an agent may set: display and naming.
_NODE_PROPS = {"title": (str, type(None)), "preview": (bool, type(None)),
               "bypassed": (bool,), "color": (str, type(None)),
               "wireframe": (bool,), "finish": (str, type(None)),
               "parent": (str, type(None))}


def _op_set_node(graph: Graph, ref: str, **props) -> dict:
    """Set node attributes: title, preview (eye: true/false/null=auto),
    bypassed, color, wireframe, finish, parent."""
    node = resolve_node(graph, ref)
    for k, v in props.items():
        if k not in _NODE_PROPS:
            raise ValueError(f"unknown node property {k!r}; "
                             f"settable: {', '.join(sorted(_NODE_PROPS))}")
        if not isinstance(v, _NODE_PROPS[k]):
            raise ValueError(f"{node.id}.{k}: bad value {v!r}")
        setattr(node, k, None if v == "" else v)
    return {"node": node.id, **props}


# --- node / connection editing (load → op → save) ------------------------
def add_node(store: GraphStore, graph_id: str, node_type: str,
             params: Optional[dict] = None,
             position: Optional[tuple[float, float]] = None,
             parent: Optional[str] = None, title: Optional[str] = None) -> str:
    """Add a node (params validated against the catalog). Without `position`
    it is auto-placed in free space rather than stacked at (0,0)."""
    graph = store.load(graph_id)
    nid = _op_add_node(graph, node_type, params, position, parent, title=title)
    if position is None:
        _place_new(graph, [nid])
    store.save(graph_id, graph)
    return nid


def connect(store: GraphStore, graph_id: str, from_node: str, from_socket: str,
            to_node: str, to_socket: str) -> str:
    return connect_checked(store, graph_id, from_node, from_socket,
                           to_node, to_socket)["connection_id"]


def connect_checked(store: GraphStore, graph_id: str, from_node: str,
                    from_socket: str, to_node: str, to_socket: str) -> dict:
    """Connect and report the soft validation warnings touching either end
    (e.g. a required input still unconnected). Bad wiring raises."""
    graph = store.load(graph_id)
    cid = _op_connect(graph, from_node, from_socket, to_node, to_socket)
    conn = graph.connections[-1]
    warnings = _warnings_for(validate_graph(graph), (conn.from_node, conn.to_node))
    store.save(graph_id, graph)
    return {"connection_id": cid, "warnings": warnings}


def set_param(store: GraphStore, graph_id: str, node_id: str, params: dict,
              base_version=None) -> dict:
    """Set params on a node addressed by id OR exact title. Every value is
    validated against the catalog (or the CodeBlock's `#@param`s): an unknown
    name or an uncoercible value raises, naming the node and the param.
    Returns {node, params: <stored values>, version, notes?: [clamps]}."""
    _check_base_version(store, graph_id, base_version)
    graph = store.load(graph_id)
    node = resolve_node(graph, node_id)
    notes: list[str] = []
    stored = _apply_params(node, params, notes)
    store.save(graph_id, graph)
    out = {"node": node.id, "params": stored,
           "version": graph_version(store, graph_id)}
    if notes:
        out["notes"] = notes
    return out


def patch_param(store: GraphStore, graph_id: str, node_id: str,
                param: str, value) -> Any:
    """Structured single-param edit from the code view. Validates/clamps against
    the catalog Param (built-ins) or the `#@param` annotation (CodeBlock, when
    `param` is prefixed `_cb.`). Returns the stored value. Non-destructive: a
    CodeBlock override lives in a `_cb` namespace, never touching its source."""
    graph = store.load(graph_id)
    node = resolve_node(graph, node_id)
    if param.startswith("_cb.") and node.type != "CodeBlock":
        raise ValueError(f"Node {node_id} is {node.type}, not a CodeBlock")
    value = _apply_param(node, param, value)
    store.save(graph_id, graph)
    return value


def scan_codeblock(store: GraphStore, graph_id: str, node_id: str) -> list[dict]:
    """The `#@param` schema declared by a CodeBlock, merged with current
    overrides (so each entry reports its effective `value`)."""
    node = resolve_node(store.load(graph_id), node_id)
    if node.type != "CodeBlock":
        raise ValueError(f"Node {node_id} is {node.type}, not a CodeBlock")
    overrides = node.params.get("_cb") or {}
    schema = parse_codeblock_params(node.params.get("code", ""))
    for d in schema:
        d["value"] = overrides.get(d["name"], d["default"])
    return schema


def set_code(store: GraphStore, graph_id: str, node_id: str, code: str) -> bool:
    graph = store.load(graph_id)
    node = resolve_node(graph, node_id)
    if node.type != "CodeBlock":
        raise ValueError(f"Node {node_id} is {node.type}, not a CodeBlock")
    node.params["code"] = code
    store.save(graph_id, graph)
    return True


def edit_code(store: GraphStore, graph_id: str, node_id: str, old: str,
              new: str, param: str = "code", base_version=None) -> dict:
    """str-replace inside a CodeBlock's code: exactly one match of `old` is
    replaced by `new`, or nothing is saved. Cheaper and safer than resending a
    whole script with set_code."""
    _check_base_version(store, graph_id, base_version)
    graph = store.load(graph_id)
    out = _op_edit_code(graph, node_id, old, new, param)
    store.save(graph_id, graph)
    return {**out, "version": graph_version(store, graph_id)}


def set_node(store: GraphStore, graph_id: str, node_id: str,
             base_version=None, **props) -> dict:
    """Set node attributes (title, preview eye, bypassed, color, ...)."""
    _check_base_version(store, graph_id, base_version)
    graph = store.load(graph_id)
    out = _op_set_node(graph, node_id, **props)
    validate_graph(graph)                 # a bad `parent` must not be saved
    store.save(graph_id, graph)
    return {**out, "version": graph_version(store, graph_id)}


def delete_node(store: GraphStore, graph_id: str, node_id: str) -> bool:
    graph = store.load(graph_id)
    _op_remove(graph, node_id)
    store.save(graph_id, graph)
    return True


def delete_connection(store: GraphStore, graph_id: str, connection_id: str) -> bool:
    graph = store.load(graph_id)
    graph.connections = [c for c in graph.connections if c.id != connection_id]
    store.save(graph_id, graph)
    return True


# --- batch edits -----------------------------------------------------------
def _endpoint(op: dict, side: str) -> tuple[Optional[str], Optional[str]]:
    """`from`/`to` as "node.socket" (split on the LAST dot, so titles may
    contain dots), or the explicit `<side>_node` + `<side>_socket` keys."""
    if op.get(side):
        spec = str(op[side])
        if "." not in spec:
            raise ValueError(f"`{side}` must be 'node.socket', got {spec!r}")
        node, sock = spec.rsplit(".", 1)
        return node, sock
    return op.get(f"{side}_node"), op.get(f"{side}_socket")


OPS_HELP = (
    "ops: {op:'add_node', type, params?, position?, id?, title?, parent?} · "
    "{op:'connect', from:'node.socket', to:'node.socket'} · "
    "{op:'disconnect', id} or {op:'disconnect', from?, to?} · "
    "{op:'set_param', node, params} · {op:'edit_code', node, old, new} · "
    "{op:'set_code', node, code} · {op:'set_node', node, title?, preview?, "
    "bypassed?, color?, ...} · {op:'remove', node}. A node ref is an id, an "
    "exact title, or '$N' = the node created by op N (0-based) of this batch.")


def apply_ops(store: GraphStore, graph_id: str, ops: list[dict],
              base_version=None) -> dict:
    """Apply a batch of edits ATOMICALLY: all of them, validated, then ONE
    save — or, on the first failing op, nothing at all (the error names the op
    index). New nodes without a position are auto-placed after the batch, right
    of whatever they were wired to. See OPS_HELP for the op shapes."""
    if not isinstance(ops, list) or not ops:
        raise ValueError("ops must be a non-empty list. " + OPS_HELP)
    _check_base_version(store, graph_id, base_version)
    graph = store.load(graph_id)
    created: dict[str, str] = {}          # "$N" -> node id
    unplaced: list[str] = []
    results, notes = [], []

    def ref(r):
        r = str(r)
        if r.startswith("$") and r in created:
            return created[r]
        if r.startswith("$"):
            raise ValueError(f"{r} does not name a node created earlier in this batch")
        return r

    for i, op in enumerate(ops):
        try:
            if not isinstance(op, dict) or "op" not in op:
                raise ValueError("each op must be an object with an 'op' key")
            kind = op["op"]
            if kind == "add_node":
                nid = _op_add_node(graph, op.get("type") or op.get("node_type", ""),
                                   op.get("params"), op.get("position"),
                                   op.get("parent"), node_id=op.get("id"),
                                   title=op.get("title"), notes=notes)
                created[f"${i}"] = nid
                if op.get("position") is None:
                    unplaced.append(nid)
                results.append({"op": kind, "node": nid})
            elif kind == "connect":
                fn, fs = _endpoint(op, "from")
                tn, ts = _endpoint(op, "to")
                if not (fn and fs and tn and ts):
                    raise ValueError("connect needs from:'node.socket' and to:'node.socket'")
                cid = _op_connect(graph, ref(fn), fs, ref(tn), ts)
                results.append({"op": kind, "connection": cid})
            elif kind == "disconnect":
                fn, fs = _endpoint(op, "from") if (op.get("from") or op.get("from_node")) else (None, None)
                tn, ts = _endpoint(op, "to") if (op.get("to") or op.get("to_node")) else (None, None)
                removed = _op_disconnect(graph, op.get("id"),
                                         ref(fn) if fn else None, fs,
                                         ref(tn) if tn else None, ts)
                results.append({"op": kind, "removed": removed})
            elif kind == "set_param":
                node = resolve_node(graph, ref(op.get("node", "")))
                stored = _apply_params(node, op.get("params") or {}, notes)
                results.append({"op": kind, "node": node.id, "params": stored})
            elif kind == "edit_code":
                results.append({"op": kind, **_op_edit_code(
                    graph, ref(op.get("node", "")), op.get("old", ""),
                    op.get("new", ""), op.get("param", "code"))})
            elif kind == "set_code":
                node = resolve_node(graph, ref(op.get("node", "")))
                if node.type != "CodeBlock":
                    raise ValueError(f"Node {node.id} is {node.type}, not a CodeBlock")
                node.params["code"] = str(op.get("code", ""))
                results.append({"op": kind, "node": node.id})
            elif kind == "set_node":
                props = {k: v for k, v in op.items() if k not in ("op", "node")}
                results.append({"op": kind, **_op_set_node(
                    graph, ref(op.get("node", "")), **props)})
            elif kind in ("remove", "delete_node"):
                results.append({"op": kind, "removed": _op_remove(
                    graph, ref(op.get("node", "")))})
            else:
                raise ValueError(f"unknown op {kind!r}. " + OPS_HELP)
        except (ValueError, KeyError, TypeError, ValidationError) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else e
            raise ValueError(f"op #{i} ({op.get('op') if isinstance(op, dict) else op!r})"
                             f" failed, nothing saved: {msg}") from None

    warnings = validate_graph(graph)
    placed = _place_new(graph, [n for n in unplaced
                                if any(x.id == n for x in graph.nodes)])
    store.save(graph_id, graph)
    out = {"ok": True, "results": results,
           "created": {k: v for k, v in created.items()},
           "warnings": warnings, "version": graph_version(store, graph_id)}
    if placed:
        out["placed"] = placed
    if notes:
        out["notes"] = notes
    return out


# --- compact views for agents ----------------------------------------------
_LONG = 300


def _short(v, node_id: str):
    import json as _json
    if isinstance(v, str):
        if len(v) > _LONG:
            return (f"<{len(v.splitlines())} lines, {len(v)} chars — "
                    f"cad_get_graph(node={node_id!r}) for the full text>")
        return v
    try:
        s = _json.dumps(v)
    except (TypeError, ValueError):
        return repr(v)[:_LONG]
    if len(s) > _LONG:
        return f"<{type(v).__name__}, {len(s)} chars — cad_get_graph(node={node_id!r})>"
    return v


def get_graph_compact(store: GraphStore, graph_id: str, positions: bool = False,
                      node: Optional[str] = None) -> dict:
    """The graph as an agent wants to read it: nodes with type/title/params
    (editor-only `_ui` state dropped, long code/lists elided), connections as
    `id: from.socket -> to.socket` strings, positions only on request.
    `node=<id|title>` returns that one node in FULL (untruncated code) plus the
    connections touching it."""
    graph = store.load(graph_id)
    only = resolve_node(graph, node).id if node else None

    def ndict(n: Node) -> dict:
        d: dict = {"id": n.id, "type": n.type}
        if n.title:
            d["title"] = n.title
        params = {k: v for k, v in n.params.items() if k != "_ui"}
        d["params"] = params if only else {k: _short(v, n.id) for k, v in params.items()}
        for attr in ("preview", "parent", "finish", "color"):
            if getattr(n, attr) is not None:
                d[attr] = getattr(n, attr)
        if n.bypassed:
            d["bypassed"] = True
        if positions or only:
            d["position"] = [round(float(p)) for p in n.position]
        return d

    nodes = [ndict(n) for n in graph.nodes if only is None or n.id == only]
    conns = [f"{c.id}: {c.from_node}.{c.from_socket} -> {c.to_node}.{c.to_socket}"
             for c in graph.connections
             if only is None or only in (c.from_node, c.to_node)]
    out = {"name": graph.name, "version": graph_version(store, graph_id),
           "nodes": nodes, "connections": conns}
    if graph.groups and only is None:
        out["groups"] = [g.get("title", "") for g in graph.groups]
    return out


def compact_catalog(query: str = "", category: str = "",
                    include_hidden: bool = False, defaults: bool = True) -> str:
    """One line per node type: `Type [category] in:(sock:wire, opt:wire?)
    out:(sock:wire) params:(name=default, ...)`. `query` filters by substring
    over type, label, category, aliases and description (case-insensitive)."""
    q = (query or "").lower().strip()
    lines = []
    for t in sorted(catalog.REGISTRY):
        d = catalog.REGISTRY[t]
        if d.hidden and not include_hidden:
            continue
        if category and d.category != category:
            continue
        if q and not any(q in (s or "").lower() for s in
                         (t, d.label, d.category, d.description, *d.aliases)):
            continue
        ins = ", ".join(f"{s.name}:{s.wire_type}{'' if s.required else '?'}"
                        for s in d.inputs) or "-"
        outs = ", ".join(f"{s.name}:{s.wire_type}" for s in d.outputs) or "-"
        if defaults:
            ps = ", ".join(_param_sig(p) for p in d.params) or "-"
        else:
            ps = ", ".join(p.name for p in d.params) or "-"
        lines.append(f"{t} [{d.category}] in:({ins}) out:({outs}) params:({ps})")
    return "\n".join(lines)


def _param_sig(p) -> str:
    if p.type == "select":
        return f"{p.name}={p.default}|{'|'.join(o for o in p.options if o != p.default)}" \
            if len(p.options) <= 6 else f"{p.name}={p.default}(+{len(p.options) - 1})"
    if p.type in ("float", "int", "bool"):
        return f"{p.name}={p.default}"
    if p.type == "str":
        v = str(p.default or "")
        return f"{p.name}:str" if (len(v) > 24 or "\n" in v) else f"{p.name}={v!r}"
    return f"{p.name}:{p.type}"


def node_def_for_agent(node_type: str) -> dict:
    """One node type's full definition minus the codegen internals (templates,
    imports): sockets, params with type/default/range/options, description."""
    d = get_node_def(node_type)
    for k in ("code_template", "imports"):
        d.pop(k, None)
    for side in ("inputs", "outputs"):
        socks = []
        for s in d.get(side, []):
            t = {"name": s["name"], "wire_type": s["wire_type"]}
            if side == "inputs" and not s.get("required", True):
                t["optional"] = True
            for k in ("multiple", "list_access", "accepts", "subtype"):
                if s.get(k):
                    t[k] = s[k]
            socks.append(t)
        d[side] = socks
    for p in d.get("params", []):
        for k in ("code_map", "raw"):
            p.pop(k, None)
        for k in [k for k, v in p.items() if v in (None, [], {}, "", False)
                  and k not in ("default",)]:
            p.pop(k)
    return {k: v for k, v in d.items() if v not in (None, [], {}, "", False)
            or k in ("inputs", "outputs", "params")}


# --- lean execute results --------------------------------------------------
_HEAVY = {"mesh", "polylines", "points", "bodies", "anim", "items", "triangles",
          "vertices", "segments", "frames"}


def _round(v, sig: int = 6, dec: int = 4):
    """Round floats to `sig` significant digits and at most `dec` decimals,
    recursively — 38.000005 → 38.0, 1e-7 noise → 0.0."""
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return v
        return round(float(f"{v:.{sig}g}"), dec)
    if isinstance(v, dict):
        return {k: _round(x, sig, dec) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_round(x, sig, dec) for x in v]
    return v


def lean_view(view, keep_mesh: bool = False):
    """The view without tessellation: heavy per-preview payloads (meshes,
    polylines, point clouds, animation tracks) are dropped, floats rounded."""
    if not isinstance(view, dict):
        return view
    if keep_mesh:
        return view
    v = {k: x for k, x in view.items()
         if k not in ("mesh", "stl", "node_timings")}
    if isinstance(v.get("previews"), dict):
        v["previews"] = {
            nid: ({k: x for k, x in e.items() if k not in _HEAVY}
                  if isinstance(e, dict) else e)
            for nid, e in v["previews"].items()}
    return _round(v)


def summarize_execute(result: dict, include_code: bool = False,
                      include_mesh: bool = False) -> dict:
    """An executor result shaped for an agent: success, errors, per-node errors,
    warnings, the lean rounded view and the slowest nodes. The generated code
    (~hundreds of KB on a real graph) and stdout only on request."""
    if not isinstance(result, dict):
        return result
    if "error" in result and "success" not in result:
        return result                     # an api-level failure (_safe), as is
    out = {"success": bool(result.get("success"))}
    for k in ("errors", "error_detail", "node_errors", "warnings", "overrides",
              "override_notes"):
        if result.get(k):
            out[k] = result[k]
    view = result.get("view")
    if view:
        out["view"] = lean_view(view, keep_mesh=include_mesh)
    timings = result.get("node_timings") or {}
    slow = sorted(((t, n) for n, t in timings.items() if t >= 0.05), reverse=True)[:5]
    if slow:
        out["slowest"] = {n: round(t, 3) for t, n in slow}
        out["compute_s"] = round(sum(timings.values()), 3)
    if result.get("node_cached"):
        out["cached"] = len(result["node_cached"])
    if include_code:
        out["code"] = result.get("code")
        if result.get("stdout"):
            out["stdout"] = result["stdout"]
    return out


# --- code / execution / inspection ---------------------------------------
def get_code(store: GraphStore, graph_id: str) -> str:
    return transpile(store.load(graph_id))


def get_code_map(store: GraphStore, graph_id: str) -> dict:
    """Generated source + a param<->code source map for the editable code view."""
    code, params = transpile_with_map(store.load(graph_id))
    return {"code": code, "params": params}


def execute(store: GraphStore, graph_id: str, timeout: int = 120,
            overrides: Optional[dict] = None) -> dict:
    """Run the graph. `overrides={node_id_or_title: {param: value}}` runs it
    with those params changed IN MEMORY ONLY — validated exactly like
    set_param, never saved — to try a value before committing to it. (The run
    still refreshes the project's last view/output, like any run.)"""
    graph = store.load(graph_id)
    extra = apply_overrides(graph, overrides)
    result = execute_graph(graph, store.dir(graph_id), timeout=timeout)
    return {**result, **extra} if extra else result


def apply_overrides(graph: Graph, overrides: Optional[dict]) -> dict:
    """Apply `{node_id_or_title: {param: value}}` to an in-memory graph,
    validated like set_param. Returns {overrides: applied, override_notes?}
    to merge into the run's result ({} when there was nothing to apply)."""
    if not overrides:
        return {}
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be {node_id_or_title: {param: value}}")
    applied, notes = {}, []
    for ref, params in overrides.items():
        node = resolve_node(graph, ref)
        applied[node.id] = _apply_params(node, params, notes)
    out = {"overrides": applied}
    if notes:
        out["override_notes"] = notes
    return out


def get_view(store: GraphStore, graph_id: str) -> dict | None:
    return store.view(graph_id)


def get_panels(store: GraphStore, graph_id: str) -> dict:
    view = store.view(graph_id) or {}
    return view.get("panels", {})


def _resolve_asset(workdir, path: str):
    """Validate a project-relative STEP path (traversal-guarded)."""
    target = (workdir / path).resolve()
    if not target.is_relative_to(workdir.resolve()):
        raise ValueError("path escapes the project directory")
    if target.suffix.lower() not in (".step", ".stp", ".stl"):
        raise ValueError("only STEP/.stp (exact) and .stl (arc-fitted) files "
                         "are sliceable; gcode: fase 3")
    if not target.exists():
        raise ValueError(f"no such file {path!r} in the project")
    return target


def slice_summary(store: GraphStore, graph_id: str, path: Optional[str] = None,
                  n_per_axis: int = 10) -> dict:
    """Symbolic cross-section summary (retro-engineering perception+verify,
    PLAN_RETROENG fase 1). `path=None` slices the graph's OWN result;
    `path='assets/part.step'` (project-relative) slices that file. Returns the
    summary dict; its 'text' field is the LLM-facing symbolic format."""
    workdir = store.dir(graph_id)
    n = max(2, min(int(n_per_axis), 40))
    if path:
        return slice_summary_file(_resolve_asset(workdir, path), workdir, n)
    return slice_summary_graph(store.load(graph_id), workdir, n)


def section_outline(store: GraphStore, graph_id: str, axis: str = "z",
                    position: float = 0.0, path: Optional[str] = None) -> dict:
    """The 'microscope' companion of slice_summary: ONE exact section at
    `axis`=`position`, every loop edge by edge (type, 2D endpoints, radius/
    center for arcs). Use it where the symbolic summary is ambiguous."""
    workdir = store.dir(graph_id)
    if path:
        return section_outline_file(_resolve_asset(workdir, path), workdir,
                                    axis, position)
    return section_outline_graph(store.load(graph_id), workdir, axis, position)


def agent_tags(store: GraphStore) -> list[dict]:
    """The agent-facing provenance index: every ToAgent tag node across ALL
    projects, with label, date (stamped at save), workflow (graph id), node id
    and the upstream source it tags (node type + its file path when it is an
    Import node). This is how 'retro-engineer part X in workflow Y' resolves."""
    out = []
    for gid in store.list():
        try:
            graph = store.load(gid)
        except Exception:  # noqa: BLE001 — one broken project must not hide the rest
            continue
        for node in graph.nodes:
            if node.type != "ToAgent":
                continue
            source = None
            conn = next((c for c in graph.connections
                         if c.to_node == node.id and c.to_socket == "value"), None)
            if conn is not None:
                try:
                    src = graph.node(conn.from_node)
                    source = {"node_id": src.id, "type": src.type}
                    if src.params.get("path"):
                        source["path"] = src.params["path"]
                except KeyError:
                    pass
            out.append({"graph": gid, "node_id": node.id,
                        "label": node.params.get("label", ""),
                        "date": node.params.get("date", ""),
                        "source": source})
    return out


def node_labels(graph: Graph) -> dict:
    """{node_id: {"title", "type"}} — the worker only sees ids; files and the
    export index should carry the names the user reads on the canvas."""
    out = {}
    for n in graph.nodes:
        try:
            label = n.title or catalog.get(n.type).label or n.type
        except KeyError:
            label = n.title or n.type
        out[n.id] = {"title": label, "type": n.type}
    return out


def _publish(d: Path, src: Path, filename: str, via: str, graph: Graph, **meta) -> Path:
    """Copy a finished export into exports/ (so the library lists it) and give
    it its provenance line in exports/index.jsonl (cad_nodes/export_index.py)."""
    target = d / export_index.EXPORTS_DIR / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    export_index.record(d, filename, via,
                        graph=export_index.graph_key(graph.to_dict()), **meta)
    return target


def export(store: GraphStore, graph_id: str, fmt: str = "step",
           graph: Optional[Graph] = None) -> str:
    """Export the graph's RESULT to one file, publish it as exports/<name>.<ext>
    with a provenance line, and return the (immutable) run-dir path.
    `graph` = an unsaved snapshot to export instead of the stored one."""
    graph = graph if graph is not None else store.load(graph_id)
    d = store.dir(graph_id)
    out = export_graph(graph, d, fmt)
    _publish(d, out, f"{graph_id}{out.suffix}", "button", graph, fmt=out.suffix[1:])
    return str(out)


def export_all(store: GraphStore, graph_id: str,
               graph: Optional[Graph] = None) -> tuple[str, str, dict]:
    """Bake every PREVIEWED node (what the viewport shows) to STEP + STL, zip
    them with a manifest.json and publish the zip in exports/. Returns
    (zip path, published filename, manifest)."""
    graph = graph if graph is not None else store.load(graph_id)
    d = store.dir(graph_id)
    zpath, manifest = export_bundle(graph, d, node_labels(graph))
    fname = f"{graph_id}_{time.strftime('%Y%m%d-%H%M%S')}.zip"
    contents = [{"node": n.get("node"), "type": n.get("type"), "title": n.get("title"),
                 "files": n.get("files", []),
                 **({"skipped": n["skipped"]} if n.get("skipped") else {})}
                for n in manifest.get("nodes", [])]
    _publish(d, zpath, fname, "bundle", graph, fmt="zip", contents=contents)
    return str(zpath), fname, manifest


async def screenshot(store: GraphStore, graph_id: str, **opts):
    """Render the graph's viewport to a PNG. Returns (png_bytes, meta).

    The one op here that is async, because it drives a browser rather than the
    B-Rep kernel: it renders through the REAL viewer (headless Chromium over
    /nodes), so what an agent sees is exactly what the user sees. See
    cad_nodes/screenshot.py for why that matters more than it sounds.

    Kept in api.py like everything else so the HTTP route and the MCP tool are
    the same operation rather than two that drift.
    """
    from . import screenshot as _shot
    graph = store.load(graph_id)         # 404 on an unknown/invalid project id

    # `node` may be an id or a title, and may name a node that is not drawn
    # (an intermediate step: the viewport only shows terminal geometry unless
    # the node's eye is on). Isolating it then means turning its eye on for the
    # shot and back off after — a real save, since the viewer reads the graph
    # from disk, restored in `finally` to exactly what it was.
    restore = _NO_RESTORE
    if opts.get("node"):
        try:
            node = resolve_node(graph, opts["node"])
        except KeyError as e:
            raise ValueError(e.args[0] if e.args else str(e)) from None
        opts["node"] = node.id
        if not _drawn(graph, node):
            if not _drawable(node):
                raise ValueError(
                    f"node {node.id} ({node.type}) has no drawable output — "
                    "screenshot a geometry node (solid/surface/curve/mesh/points)")
            restore = node.preview
            node.preview = True
            store.save(graph_id, graph)
            opts["run"] = True
    try:
        png, meta = await _shot.render(graph_id, **opts)
    finally:
        if restore is not _NO_RESTORE:
            fresh = store.load(graph_id)
            try:
                fresh.node(opts["node"]).preview = restore
                store.save(graph_id, fresh)
            except KeyError:
                pass
    _check_png(png, graph_id)
    return png, meta


class ScreenshotFailed(RuntimeError):
    """The pipeline ran but produced no usable image — reported as an error,
    never handed back as a broken PNG with a success status."""


_NO_RESTORE = object()
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
# The smallest real render (64x64, flat background) is several hundred bytes;
# anything below this is an error page or a truncated capture, not a picture.
_MIN_PNG_BYTES = 200


def _check_png(png, graph_id: str) -> None:
    if not isinstance(png, (bytes, bytearray)) or not bytes(png).startswith(_PNG_SIG):
        head = bytes(png[:60]) if isinstance(png, (bytes, bytearray)) else png
        raise ScreenshotFailed(f"screenshot of {graph_id!r} is not a PNG: {head!r}")
    if len(png) < _MIN_PNG_BYTES:
        raise ScreenshotFailed(f"screenshot of {graph_id!r} is only {len(png)} "
                               "bytes — the capture failed")


def _drawn(graph: Graph, node: Node) -> bool:
    """Is this node drawn in the viewport as the graph stands? The
    transpiler's own rule (eye on/off, or auto = terminal geometry only),
    asked of it rather than copied."""
    from .transpiler import Transpiler
    return Transpiler(graph)._previewed(node, catalog.get(node.type))


def _drawable(node: Node) -> bool:
    """Can this node's output be drawn in the viewport if its eye is on?
    Mirrors the transpiler's `_previewed` (read-only import, not a copy)."""
    from .transpiler import _PREVIEWABLE, _SELECT_TYPES
    ndef = catalog.get(node.type)
    return (node.type == "CodeBlock" or node.type in _SELECT_TYPES
            or bool(ndef.outputs and ndef.outputs[0].wire_type in _PREVIEWABLE))


def arrange(store: GraphStore, graph_id: str, **opts) -> dict:
    """Tidy the graph's node positions, left-to-right by dependency depth.

    Nodes are placed using their REAL on-canvas size (cad_nodes/layout.py mirrors
    litegraph's own computeSize, pinned to a captured fixture), so the result is
    guaranteed free of overlapping nodes rather than merely spread out — the
    guarantee is asserted before the graph is saved. Group boxes are re-fitted
    around the members they had BEFORE the move, and members are kept in one
    y-band so two groups' boxes don't end up cutting across each other.

    Returns the layout summary: nodes, columns, moved, overlaps, group_overlaps.
    """
    from . import layout as _layout
    graph = store.load(graph_id)
    summary = _layout.arrange(graph, **opts)
    store.save(graph_id, graph)
    return summary


# --- graph version (optimistic concurrency, see graph_version.py) ---------
def read_versioned_graph(store: GraphStore, graph_id: str) -> dict:
    """`{version, graph}` read in one go. Pass `version` back as `base_version`
    to `write_graph` so a write never silently overwrites someone else's edit
    (the editor, a human) made in between."""
    from .graph_version import read_versioned
    version, data = read_versioned(store.dir(graph_id) / "graph.json")
    if version is None:
        raise KeyError(f"No graph {graph_id!r}")
    return {"version": version, "graph": Graph.from_dict(data or {}).to_dict()}


def write_graph(store: GraphStore, graph_id: str, graph: dict,
                base_version: Optional[str] = None) -> dict:
    """Validate and save a whole graph. With `base_version`, a graph changed
    since that version raises `graph_version.StaleGraphError` (its `.detail()` has
    the current version + graph to merge with) instead of being overwritten."""
    g = Graph.from_dict({**graph, "name": graph_id})
    g.validate()
    return {"version": store.save(graph_id, g, base_version=base_version)}


def propose_groups(store: GraphStore, graph_id: str) -> list[dict]:
    """Group boxes that would make the graph readable (Parametri, shared hubs,
    one per output chain), for nodes not already grouped. Read-only; apply them
    with `arrange(store, graph_id, groups="auto")`."""
    from . import layout as _layout
    return _layout.propose_groups(store.load(graph_id))


# --- generations: a link instead of a screenshot ---------------------------
# An agent reporting a result used to send pictures. A picture is one angle,
# chosen by the agent. A generation is the whole frozen scene, opened in the
# read-only viewer at /view/<graph>/<gen>: the user orbits it, hides pieces,
# inverts the selection — and the link keeps showing THAT result however the
# live workflow changes afterwards, because it reads a copy, not the project.
def _preview_drawable(e) -> bool:
    return bool(e) and any(k in e for k in ("mesh", "polylines", "points", "bodies"))


def _gen_pieces(graph: Graph, previews: dict) -> list[dict]:
    """Names for what the viewer will list: one entry per drawn node, with the
    count of its sub-pieces (fanned-out items / scene bodies)."""
    def title(nid):
        try:
            n = graph.node(nid)
        except KeyError:
            return nid, ""
        label = n.title
        if not label:
            try:
                label = catalog.get(n.type).label
            except KeyError:
                label = n.type
        return label, n.type

    out = []
    for nid, e in previews.items():
        if not _preview_drawable(e):
            continue
        t, ty = title(nid)
        p = {"id": nid, "title": t, "type": ty}
        if e.get("parts") and len(e["parts"]) > 1:
            p["parts"] = len(e["parts"])
        if e.get("bodies"):
            p["bodies"] = [title(b["owner"])[0] if b.get("owner") else None
                           for b in e["bodies"]]
        if _anim_seconds(e):
            p["animated"] = True
        out.append(p)
    return out


def _anim_seconds(e) -> float:
    """Length (s) of the timeline a preview carries — an Animate's or a Drop's
    `anim`, or a collide scene's per-body plans — 0 if it does not move."""
    plans = [e.get("anim")] + [b.get("anim") for b in (e.get("bodies") or [])]
    return max([float(a.get("T") or 0) for a in plans if isinstance(a, dict)] or [0.0])


def gen_url(graph_id: str, gen: str, base: str = "") -> str:
    """The viewer link for a generation. `base` is the server's public origin;
    without one, NOODLE_PUBLIC_URL, else localhost."""
    import os
    from urllib.parse import quote
    base = (base or os.environ.get("NOODLE_PUBLIC_URL") or "http://localhost:8090")
    return f"{base.rstrip('/')}/view/{quote(graph_id)}/{gen}"


def _timeline(view: dict):
    secs = [_anim_seconds(e) for e in (view.get("previews") or {}).values() if isinstance(e, dict)]
    longest = max(secs or [0.0])
    return {"seconds": round(longest, 3)} if longest > 0 else None


def snapshot(store: GraphStore, graph_id: str, label: str = "",
             run: bool = True, base_url: str = "", tags: Optional[list] = None,
             measures: Optional[list] = None) -> dict:
    """Freeze the graph's current result as a new generation and return its
    viewer link. `run=True` (default) executes first, so the generation is the
    graph AS SAVED NOW, not whatever last ran; `run=False` freezes the last
    view.json as is. An unchanged result is not duplicated: if it is identical
    to the newest generation (same geometry, same label) that one is returned
    with `reused: true`. `tags` (see tag_gen) label its pieces in the same call,
    `measures` (see measure_gen) pin dimensions on it."""
    out = _snapshot(store, graph_id, label, run, base_url)
    if tags is not None:
        # the gen exists by now: a bad tag must not read as a failed snapshot
        try:
            out["tags"] = tag_gen(store, graph_id, out["gen"], tags, base_url=base_url)["tags"]
        except ValueError as e:
            out["tags_error"] = str(e)
    if measures is not None:
        try:
            out["measures"] = measure_gen(store, graph_id, out["gen"], measures, base_url=base_url)["measures"]
        except ValueError as e:
            out["measures_error"] = str(e)
    return out


def _snapshot(store: GraphStore, graph_id: str, label: str, run: bool, base_url: str) -> dict:
    import datetime
    import hashlib
    import json as _json
    graph = store.load(graph_id)
    if run:
        result = execute_graph(graph, store.dir(graph_id), write_stl=False)
        if not result.get("success"):
            raise ValueError(f"execution failed, nothing to snapshot: "
                             f"{result.get('errors') or result.get('error_detail')}")
        view = result.get("view") or store.view(graph_id)
    else:
        view = store.view(graph_id)
    if not view or not any(_preview_drawable(e) for e in (view.get("previews") or {}).values()):
        raise ValueError(f"graph {graph_id!r} has nothing drawn to snapshot "
                         "(run it, and check a geometry node has its eye on)")
    digest = hashlib.sha1(_json.dumps(view.get("previews"), sort_keys=True)
                          .encode()).hexdigest()[:16]
    gens = store.list_gens(graph_id)
    if gens and gens[0].get("hash") == digest and gens[0].get("label", "") == label:
        meta = gens[0]
        return {**meta, "reused": True, "ref": gen_ref(graph_id, meta["gen"]),
                "url": gen_url(graph_id, meta["gen"], base_url)}
    meta = store.save_gen(graph_id, view, graph.to_dict(), {
        "label": label,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "version": store.version(graph_id),
        "hash": digest,
        "pieces": _gen_pieces(graph, view.get("previews") or {}),
        # the /view page plays it (▶, andata e ritorno…); None = a still
        "timeline": _timeline(view),
    })
    return {**meta, "reused": False, "ref": gen_ref(graph_id, meta["gen"]),
            "url": gen_url(graph_id, meta["gen"], base_url)}


# ---------------------------------------------------------------------------
# Tags — the agent labels the pieces of a generation it sends the user
# ---------------------------------------------------------------------------
# The opposite direction of a note: «coperchio v2», «foro M8 qui», «parete 2 mm»
# pinned to the pieces of a gen, drawn in /view as plates the user can read
# from any side. Stored beside the gen (tags.json), so the gen stays immutable.

_TAGS_MAX = 40


def _resolve_piece(meta: dict, ref) -> dict:
    """A piece of the FROZEN gen by node id or exact title."""
    pieces = meta.get("pieces") or []
    names = ", ".join(f"{p['id']} ({p.get('title') or p.get('type')})" for p in pieces) or "none"
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(f"tag: `node` must name a piece — the gen's pieces: {names}")
    for p in pieces:
        if p.get("id") == ref:
            return p
    hits = [p for p in pieces if (p.get("title") or "") == ref]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"tag: title {ref!r} is ambiguous ({', '.join(p['id'] for p in hits)}): use the id")
    raise ValueError(f"tag: no piece {ref!r} in {meta.get('graph')}/{meta.get('gen')} — its pieces: {names}")


def tag_gen(store: GraphStore, graph_id: str, gen: str, tags: list,
            replace: bool = True, base_url: str = "") -> dict:
    """Label pieces of generation `gen`: `tags` = [{text, node, at?, color?}].
    `node` is an id or a title of the gen's frozen pieces; `at` ([x,y,z] mm) is
    where the stem starts — without it the viewer anchors the tag on the piece
    itself. `replace=False` appends to the tags already there."""
    meta = store.load_gen(graph_id, gen, "meta")
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    old = [] if replace else store.load_gen_tags(graph_id, gen)
    if len(old) + len(tags) > _TAGS_MAX:
        raise ValueError(f"at most {_TAGS_MAX} tags per generation")
    out = [dict(t) for t in old]
    for i, t in enumerate(tags):
        if not isinstance(t, dict):
            raise ValueError(f"tag {i} must be an object")
        text = t.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 120:
            raise ValueError(f"tag {i}: text must be 1..120 chars")
        p = _resolve_piece(meta, t.get("node"))
        o = {"text": text.strip(), "node": p["id"], "title": p.get("title") or p.get("type")}
        if t.get("at") is not None:
            o["at"] = [round(_num(c, f"tag {i} at"), 4) for c in _check3(t["at"], f"tag {i} at")]
        if t.get("color") is not None:
            color = str(t["color"]).lower()
            if not re.fullmatch(r"#[0-9a-f]{6}", color):
                raise ValueError(f"tag {i}: color must be #rrggbb")
            o["color"] = color
        out.append(o)
    for k, o in enumerate(out, 1):
        o["tag"] = k
    store.save_gen_tags(graph_id, gen, out)
    return {"gen": gen, "ref": gen_ref(graph_id, gen), "url": gen_url(graph_id, gen, base_url),
            "tags": out}


def _check3(v, what):
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ValueError(f"{what} must be [x, y, z]")
    return v


def gen_tags(store: GraphStore, graph_id: str, gen: str) -> list:
    return store.load_gen_tags(graph_id, gen)


def gen_measures(store: GraphStore, graph_id: str, gen: str) -> list:
    return store.load_gen_measures(graph_id, gen)


# --- 📏 the agent's dimensions on a gen ---------------------------------------
# The other direction of the user's ↔ Metro: «parete 1,2 mm — sotto il minimo
# di 1,6», «interasse 30 ±0,1: controllalo», drawn ON the part in /view, coloured
# by `status` (ok green, check amber, fail red). Beside the gen in its own file
# (measures.json), separate from tags.json: separate checks, separate toggle.
# The agent must not GUESS points: `between: [refA, refB]` measures the gen's
# FROZEN graph on the real B-Rep (measure.py `distance`) and takes the two
# closest points and the exact value from there.

_GEN_MEASURES_MAX = 40
_GEN_MEASURE_KINDS = ("distance", "edge", "face_gap", "diameter", "radius")
_STATUSES = ("ok", "check", "fail")


def _point(v, what) -> list:
    if isinstance(v, dict):
        v = v.get("at")
    return [round(_num(c, what), 4) for c in _check3(v, what)]


def _between_points(store: GraphStore, graph_id: str, gen: str, pairs: dict) -> dict:
    """{index: (refA, refB)} → {index: result of a `distance` query} on the gen's
    frozen graph (one run for all of them; memo makes it cheap after a snapshot)."""
    from .executor import measure_graph
    graph = Graph.from_dict(store.load_gen(graph_id, gen, "graph"))
    idx = list(pairs)
    data = measure_graph(graph, store.dir(graph_id),
                         [{"op": "distance", "a": pairs[i][0], "b": pairs[i][1]} for i in idx])
    if not data.get("success"):
        raise ValueError(f"measure: the gen's graph did not run: "
                         f"{data.get('error') or data.get('errors') or data.get('error_detail')}")
    out = {}
    for i, r in zip(idx, data.get("results") or []):
        if r.get("error") or r.get("distance") is None:
            raise ValueError(f"measure {i}: between {list(pairs[i])}: {r.get('error') or 'no result'}")
        out[i] = r
    return out


def _piece_ref(piece: str) -> str:
    """A viewer piece key → a measure reference: "n8" stays, "n8.2" (the third
    item of a fanned-out node) is "n8[2]" — "n8.2" would read as an OUTPUT."""
    node, _, k = str(piece).partition(".")
    return f"{node}[{k}]" if k.isdigit() else node


def exact_measure(store: GraphStore, graph_id: str, gen: str, measure: dict) -> dict:
    """A dimension the browser took on the TESSELLATION, redone on the gen's
    frozen B-Rep (measure.py `exact`): the same vertex / circle / edge / planar
    face, found again on the real shape. Costs a run of the frozen graph (memo
    makes it cheap); a mesh-lane piece has no B-Rep and says so."""
    from .executor import measure_graph
    if not isinstance(measure, dict) or not isinstance(measure.get("a"), dict):
        raise ValueError("exact: give the measure as the viewer sends it ({kind, a, b?, …})")
    m = {"kind": measure.get("kind"), "a": measure["a"], "axis": measure.get("axis")}
    if isinstance(measure.get("b"), dict):
        m["b"] = measure["b"]
    for k, e in (("a", m["a"]), ("b", m.get("b"))):
        if e is not None:
            _point(e.get("at"), f"exact {k}")
            if not e.get("piece"):
                raise ValueError(f"exact: end {k} names no piece")
    refs = [_piece_ref(m["a"]["piece"])] + ([_piece_ref(m["b"]["piece"])] if "b" in m else [])
    graph = Graph.from_dict(store.load_gen(graph_id, gen, "graph"))
    data = measure_graph(graph, store.dir(graph_id), [{"op": "exact", "nodes": refs, "measure": m}])
    if not data.get("success"):
        raise ValueError(f"exact: the gen's graph did not run: {data.get('error') or data.get('errors')}")
    r = (data.get("results") or [{}])[0]
    if r.get("error"):
        err = r["error"]
        if "no geometry" in err and "Mesh" in err:
            err = "this piece is a mesh (no B-Rep to measure exactly): the ≈ value is all there is"
        raise ValueError(err)
    out = {k: r[k] for k in ("kind", "value", "a", "b", "found") if k in r}
    out["exact"] = True
    if isinstance(measure.get("value"), (int, float)):
        out["delta"] = round(r["value"] - float(measure["value"]), 4)
    return out


def measure_gen(store: GraphStore, graph_id: str, gen: str, measures: list,
                replace: bool = True, base_url: str = "") -> dict:
    """Pin DIMENSIONS on generation `gen`: `measures` = [{kind?, a, b, value?,
    circle?, between?, text?, expected?, tolerance?, status?, note?, node?,
    offset?, approx?}] — see cad_measure_gen. `replace=False` appends."""
    import math
    meta = store.load_gen(graph_id, gen, "meta")
    if not isinstance(measures, list):
        raise ValueError("measures must be a list")
    old = [] if replace else store.load_gen_measures(graph_id, gen)
    if len(old) + len(measures) > _GEN_MEASURES_MAX:
        raise ValueError(f"at most {_GEN_MEASURES_MAX} measures per generation")
    pairs = {}
    for i, m in enumerate(measures):
        if not isinstance(m, dict):
            raise ValueError(f"measure {i} must be an object")
        bt = m.get("between")
        if bt is not None:
            if not (isinstance(bt, (list, tuple)) and len(bt) == 2 and all(isinstance(x, str) and x for x in bt)):
                raise ValueError(f"measure {i}: between must be two node refs, e.g. [\"n5\", \"n7.body\"]")
            pairs[i] = tuple(bt)
    found = _between_points(store, graph_id, gen, pairs) if pairs else {}
    out = [dict(m) for m in old]
    for i, m in enumerate(measures):
        kind = m.get("kind") or ("diameter" if m.get("circle") else "distance")
        if kind not in _GEN_MEASURE_KINDS:
            raise ValueError(f"measure {i}: kind must be one of {', '.join(_GEN_MEASURE_KINDS)}")
        o: dict = {"kind": kind}
        if i in found:
            r = found[i]
            o["a"], o["b"] = _point(r["at_a"], f"measure {i} a"), _point(r["at_b"], f"measure {i} b")
            o["between"] = list(pairs[i])
            value = float(r["distance"])
        elif kind in ("diameter", "radius"):
            c = m.get("circle")
            if not isinstance(c, dict):
                raise ValueError(f"measure {i}: a {kind} needs circle {{center, axis, r}}")
            ax = _point(c.get("axis") or [0, 0, 1], f"measure {i} circle axis")
            na = math.hypot(*ax)
            if na < 1e-9:
                raise ValueError(f"measure {i}: circle axis must not be zero")
            r_ = _num(c.get("r"), f"measure {i} circle r")
            if not 0 < r_ < 1e6:
                raise ValueError(f"measure {i}: circle r out of range")
            o["circle"] = {"center": _point(c.get("center"), f"measure {i} circle center"),
                           "axis": [round(x / na, 4) for x in ax], "r": round(r_, 4)}
            value = 2 * r_ if kind == "diameter" else r_
        else:
            if m.get("a") is None or m.get("b") is None:
                raise ValueError(f"measure {i}: give a and b ([x,y,z] mm), or between: [refA, refB]")
            o["a"], o["b"] = _point(m["a"], f"measure {i} a"), _point(m["b"], f"measure {i} b")
            value = math.dist(o["a"], o["b"])
        if m.get("value") is not None:
            value = _num(m["value"], f"measure {i} value")
            if value < 0:
                raise ValueError(f"measure {i}: value must not be negative")
        o["value"] = round(value, 4)
        o["unit"] = "mm"
        o["approx"] = bool(m.get("approx"))
        for k, lim in (("text", 120), ("note", 500)):
            t = m.get(k)
            if t is not None:
                if not isinstance(t, str) or len(t) > lim:
                    raise ValueError(f"measure {i}: {k} must be a string of at most {lim} chars")
                if t.strip():
                    o[k] = t.strip()
        for k in ("expected", "tolerance"):
            if m.get(k) is not None:
                o[k] = round(_num(m[k], f"measure {i} {k}"), 4)
        if o.get("tolerance", 0) < 0:
            raise ValueError(f"measure {i}: tolerance must not be negative")
        st = m.get("status")
        if st is None and "expected" in o:
            # judged here when it can be: within tolerance = ok, else fail;
            # an expectation with no tolerance is something to check
            st = ("ok" if abs(o["value"] - o["expected"]) <= o["tolerance"] + 1e-9 else "fail") \
                if "tolerance" in o else "check"
        if st is not None:
            if st not in _STATUSES:
                raise ValueError(f"measure {i}: status must be one of {', '.join(_STATUSES)}")
            o["status"] = st
        if m.get("node") is not None:
            p = _resolve_piece(meta, m["node"])
            o["node"], o["title"] = p["id"], p.get("title") or p.get("type")
        if m.get("offset") is not None:
            off = _point(m["offset"], f"measure {i} offset")
            L = math.hypot(*off)
            if L > 0:
                o["n"], o["off"] = [round(x / L, 4) for x in off], round(L, 4)
        out.append(o)
    for k, o in enumerate(out, 1):
        o["measure"] = k
    store.save_gen_measures(graph_id, gen, out)
    return {"gen": gen, "ref": gen_ref(graph_id, gen), "url": gen_url(graph_id, gen, base_url),
            "measures": out}


def list_gens(store: GraphStore, graph_id: str, base_url: str = "") -> list[dict]:
    store.load(graph_id)                 # unknown project → KeyError
    return [{k: m.get(k) for k in ("gen", "label", "created", "version")}
            | {"url": gen_url(graph_id, m["gen"], base_url)}
            for m in store.list_gens(graph_id)]


def gen_ref(graph_id: str, gen: str) -> str:
    """The short name a generation goes by in conversation: `<graph>/g<N>`.
    The /view header and the /views cards show exactly this, so "the one I am
    looking at" can be said in words both sides resolve the same way."""
    return f"{graph_id}/{gen}"


def recent_gens(store: GraphStore, limit: int = 60, graph_id: str = "",
                base_url: str = "") -> list[dict]:
    """Every generation of every project (or of `graph_id`), newest first.

    Exists because an agent asked for several alternatives snapshots them one
    after another — often into several projects — and then "the second one"
    or "the blue one" is ambiguous. Each entry carries its `ref`
    (`graph/gN`, what the user can quote), its label, and `seen`: when the user
    last OPENED it in the viewer. The most recently seen is the one they mean
    when they say "this one"; `last_seen: true` marks it."""
    names = [graph_id] if graph_id else store.list()
    out = []
    for name in names:
        try:
            metas = store.list_gens(name)
        except ValueError:
            continue
        for m in metas:
            gen = m.get("gen")
            if not gen:
                continue
            extra = store.gen_extras(name, gen)
            pieces = m.get("pieces") or []
            out.append({
                "ref": gen_ref(name, gen), "graph": name, "gen": gen,
                "label": m.get("label", ""), "created": m.get("created"),
                "pieces": [p.get("title") for p in pieces],
                "animated": bool(m.get("timeline")),
                "thumb": extra["thumb"], "seen": extra["seen"],
                "notes": _open_notes(store, name, gen),
                "url": gen_url(name, gen, base_url),
            })
    out.sort(key=lambda e: (e["created"] or "", int(e["gen"][1:])), reverse=True)
    seen = [e for e in out if e["seen"]]
    last = max(seen, key=lambda e: e["seen"]) if seen else None
    if last:
        last["last_seen"] = True
    head = out[:max(1, int(limit))] if limit else out
    if last and last not in head:       # never cut the one the user means
        head.append(last)
    return head


# ---------------------------------------------------------------------------
# Notes for the agent — what the user DREW on a generation in /view
# ---------------------------------------------------------------------------
# The user zooms onto a hole that looks wrong, circles it in red on the model
# and writes "could be done better". That reaches the agent as a note: the
# picture they were looking at (strokes included), the sentence, and — so it
# is not just a picture — every stroke in model millimetres with the piece
# (node) it was drawn on, read off the gen's frozen graph. The stroke centre is
# where to point `cad_measure` / `section_outline`.

_NOTE_COLORS = {"#ef4444": "red", "#f59e0b": "orange", "#facc15": "yellow",
                "#22c55e": "green", "#3b82f6": "blue", "#ffffff": "white",
                "#111827": "black", "#d946ef": "magenta"}
_NOTE_MAX_STROKES = 300
_NOTE_MAX_POINTS = 4000


def _num(v, what):
    import math
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise ValueError(f"note: {what} must be a finite number")
    return float(v)


def _vec(v, what):
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        raise ValueError(f"note: {what} must be [x, y, z]")
    return [round(_num(c, what), 4) for c in v]


def _winding(points: list, c: list) -> float:
    """Signed angle (radians) the polyline sweeps round `c`, measured in the
    plane it mostly lies in (normal = sum of the consecutive cross products)."""
    import math
    v = [[p[k] - c[k] for k in range(3)] for p in points]
    cross = lambda a, b: [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],  # noqa: E731
                          a[0] * b[1] - a[1] * b[0]]
    n = [0.0, 0.0, 0.0]
    for a, b in zip(v, v[1:], strict=False):
        n = [x + y for x, y in zip(n, cross(a, b), strict=True)]
    if math.hypot(*n) < 1e-12:
        return 0.0
    total = 0.0
    for a, b in zip(v, v[1:], strict=False):
        cr = cross(a, b)
        total += math.atan2(sum(x * y for x, y in zip(cr, n, strict=True)) / math.hypot(*n),
                            sum(x * y for x, y in zip(a, b, strict=True)))
    return total


def _stroke_summary(points: list, width: float) -> dict:
    import math
    xs, ys, zs = zip(*points, strict=True)
    lo, hi = [min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]
    length = sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
    centre = [round(sum(c) / len(c), 3) for c in (xs, ys, zs)]
    # a stroke that winds round its own centre is a CIRCLE around something:
    # the thing is inside it, and the centre is the best guess at where. Judged
    # by the angle it sweeps (>= 270°), not by end meeting start — a circle
    # round a hole loses the arc that hung over the hole and arrives as a C.
    closed = len(points) > 6 and abs(_winding(points, centre)) >= 1.5 * math.pi
    return {"centre": centre, "bbox": [[round(v, 3) for v in lo], [round(v, 3) for v in hi]],
            "length_mm": round(length, 2),
            "shape": "dot" if len(points) == 1 else ("loop" if closed else "line")}


def _marks(strokes: list[dict]) -> list[dict]:
    """One entry per GESTURE — what the user meant to draw. A circle round a hole
    reaches here as several strokes, because the pen lifts wherever the circle
    leaves the surface (over the hole, across a silhouette); joined back in
    order they are a loop again, and that is what the agent needs to read."""
    by_g: dict[int, list[dict]] = {}
    # the strokes of a painted TEXT are not marks: the agent reads its words in
    # `labels`, not forty «line» marks for one word
    for s in strokes:
        if s.get("kind") != "text":
            by_g.setdefault(s["g"], []).append(s)
    out = []
    for ss in by_g.values():
        pts = [p for s in ss for p in s["points"]]
        m = {"mark": len(out) + 1, "color_name": ss[0]["color_name"], "color": ss[0]["color"]}
        m.update(_stroke_summary(pts, max(s["width_mm"] for s in ss)))
        nodes = []
        for s in ss:
            if s.get("node") and s["node"] not in [n["node"] for n in nodes]:
                nodes.append({k: s[k] for k in ("node", "title", "type") if k in s})
        m["on"] = nodes
        # a heap built with the 3D pen: how tall it stands off the part
        h = max((s.get("height_mm", 0) for s in ss), default=0)
        if h:
            m["height_mm"] = h
        # the picture this mark is in: the view its strokes were drawn from
        # (0 = the note's main picture, the final view)
        m["view"] = next((s["view"] for s in ss if s.get("view")), 0)
        out.append(m)
    return out


_NOTE_MAX_VIEWS = 24
_NOTE_MAX_LABELS = 60
_LABEL_SURFACES = {"plane", "cylinder", "sphere", "decal", "tag", "paint"}


def _labels(labels_in, n_views: int, titles: dict) -> list[dict]:
    """Text the user wrote ON the part (the T tool): validated like strokes."""
    import math
    if not isinstance(labels_in, list) or len(labels_in) > _NOTE_MAX_LABELS:
        raise ValueError(f"note: labels must be a list of at most {_NOTE_MAX_LABELS}")
    out = []
    for i, lb in enumerate(labels_in):
        if not isinstance(lb, dict):
            raise ValueError(f"note: label {i} must be an object")
        text = lb.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise ValueError(f"note: label {i} text must be 1..200 chars")
        unit = {}
        for k in ("normal", "up"):
            v = _vec(lb.get(k), f"label {i} {k}")
            norm = math.hypot(*v)
            if norm < 1e-6:
                raise ValueError(f"note: label {i} {k} must not be zero")
            unit[k] = [round(c / norm, 4) for c in v]
        size = lb.get("size_mm")
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise ValueError(f"note: label {i} size_mm must be [w, h]")
        size = [round(max(0.001, min(_num(v, f"label {i} size"), 1e4)), 3) for v in size]
        color = str(lb.get("color") or "#ef4444").lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", color):
            raise ValueError(f"note: label {i} color must be #rrggbb")
        style = lb.get("style") or "tag"          # old notes carry none: a plate
        if style not in ("paint", "tag", "decal"):
            raise ValueError(f"note: label {i} style must be 'paint', 'tag' or 'decal'")
        o = {"label": i + 1, "text": text.strip(), "style": style,
             "at": _vec(lb.get("at"), f"label {i} at"),
             "normal": unit["normal"], "up": unit["up"], "size_mm": size,
             "color": color, "color_name": _NOTE_COLORS.get(color, color)}
        # what the text was laid on (a decal is fitted when it can be): kept
        # with its fit, so the viewer redraws a saved note without refitting
        surface = lb.get("surface") or {"tag": "tag", "paint": "paint"}.get(style, "decal")
        if surface not in _LABEL_SURFACES:
            raise ValueError(f"note: label {i} surface must be one of {sorted(_LABEL_SURFACES)}")
        o["surface"] = surface
        if surface in ("cylinder", "sphere"):
            fit = lb.get("fit")
            if not isinstance(fit, dict):
                raise ValueError(f"note: label {i} on a {surface} needs its fit")
            f = {"centre": _vec(fit.get("centre"), f"label {i} fit centre"),
                 "radius": round(_num(fit.get("radius"), f"label {i} fit radius"), 4),
                 "convex": -1 if fit.get("convex") == -1 else 1}
            if not 0 < f["radius"] < 1e5:
                raise ValueError(f"note: label {i} fit radius out of range")
            if surface == "cylinder":
                a = _vec(fit.get("axis"), f"label {i} fit axis")
                na = math.hypot(*a)
                if na < 1e-6:
                    raise ValueError(f"note: label {i} fit axis must not be zero")
                f["axis"] = [round(c / na, 4) for c in a]
            o["fit"] = f
        v = lb.get("view")
        if v is not None:
            v = int(_num(v, f"label {i} view"))
            if not 0 <= v < n_views:
                raise ValueError(f"note: label {i} view {v} out of range")
            o["view"] = v + 1
        piece = str(lb["piece"])[:40] if lb.get("piece") else None
        if piece:
            node = piece.split(".")[0]
            o.update(piece=piece, node=node)
            if node in titles:
                o["title"] = titles[node].get("title")
                o["type"] = titles[node].get("type")
        out.append(o)
    return out


_MEASURE_KINDS = ("distance", "edge", "diameter", "radius", "face_gap", "angle")
_MEASURE_SNAPS = ("vertex", "circle_center", "edge", "face", "free")
_NOTE_MAX_MEASURES = 50


def _measure_end(e, what: str, titles: dict) -> dict:
    """One end of a dimension: a point ON the part and what it was snapped to."""
    if not isinstance(e, dict):
        raise ValueError(f"note: {what} must be an object")
    o = {"at": _vec(e.get("at"), f"{what} at")}
    snap = e.get("snap") or "free"
    if snap not in _MEASURE_SNAPS:
        raise ValueError(f"note: {what} snap must be one of {', '.join(_MEASURE_SNAPS)}")
    o["snap"] = snap
    if e.get("normal") is not None:
        o["normal"] = _vec(e["normal"], f"{what} normal")
    piece = str(e["piece"])[:40] if e.get("piece") else None
    if piece:
        node = piece.split(".")[0]
        o.update(piece=piece, node=node)
        if node in titles:
            o["title"] = titles[node].get("title")
    if isinstance(e.get("circle"), dict):
        c = e["circle"]
        o["circle"] = {"center": _vec(c.get("center"), f"{what} circle center"),
                       "axis": _vec(c.get("axis"), f"{what} circle axis"),
                       "r": round(_num(c.get("r"), f"{what} circle r"), 4)}
    return o


def _measures(items, n_views: int, titles: dict) -> list[dict]:
    """Dimensions the user took ON the part (📏): two ends — one for an edge or
    a circle — the value the browser measured and whether it is approximate
    (a tessellated curve). The browser measures; the server only checks the
    shape and keeps it, so a reader gets «12,40 mm between here and there»."""
    if not isinstance(items, list) or len(items) > _NOTE_MAX_MEASURES:
        raise ValueError(f"note: measures must be a list of at most {_NOTE_MAX_MEASURES}")
    out = []
    for i, m in enumerate(items):
        if not isinstance(m, dict):
            raise ValueError(f"note: measure {i} must be an object")
        kind = m.get("kind")
        if kind not in _MEASURE_KINDS:
            raise ValueError(f"note: measure {i} kind must be one of {', '.join(_MEASURE_KINDS)}")
        o = {"kind": kind, "a": _measure_end(m.get("a"), f"measure {i} a", titles)}
        if m.get("b") is not None:
            o["b"] = _measure_end(m["b"], f"measure {i} b", titles)
        elif kind in ("distance", "face_gap", "angle"):
            raise ValueError(f"note: measure {i} ({kind}) needs two ends, a and b")
        v = _num(m.get("value"), f"measure {i} value")
        if v < 0:
            raise ValueError(f"note: measure {i} value must not be negative")
        o["value"] = round(v, 4)
        o["unit"] = "deg" if kind == "angle" else "mm"
        o["approx"] = bool(m.get("approx"))
        if m.get("exact"):                   # verified on the B-Rep before sending
            o["exact"] = True
        if m.get("axis") is not None:
            if m["axis"] not in ("x", "y", "z"):
                raise ValueError(f"note: measure {i} axis must be x, y or z")
            o["axis"] = m["axis"]
        t = m.get("text")
        if t is not None:
            if not isinstance(t, str) or len(t) > 200:
                raise ValueError(f"note: measure {i} text must be at most 200 chars")
            if t.strip():
                o["text"] = t.strip()
        vw = m.get("view")
        if vw is not None:
            vw = int(_num(vw, f"measure {i} view"))
            if not 0 <= vw < n_views:
                raise ValueError(f"note: measure {i} view {vw} out of range")
            o["view"] = vw + 1
        out.append(o)
    return out


def _link_labels(marks: list[dict], strokes: list[dict], labels: list[dict]) -> list[dict]:
    """Which marks each label is written next to — «qui 8 mm» beside a red
    circle is a fact about THAT circle. Distance from the label's centre to the
    nearest point of each mark; near = within 1.5 label sizes, and the closest
    one within 2 sizes is kept even if none is that near. Marks get the texts
    back (`labels`), so a reader of marks alone does not miss them either."""
    import math
    pts: dict[int, list] = {}
    for s in strokes:
        if s.get("kind") != "text":         # same filter, same order as _marks
            pts.setdefault(s.get("g"), []).extend(s["points"])
    by_mark = list(pts.values())            # same order as _marks (gesture insertion)
    out = []
    for lb in labels:
        size = max(lb["size_mm"])
        dist = sorted((min(math.dist(lb["at"], p) for p in ps), k + 1)
                      for k, ps in enumerate(by_mark) if ps)
        near = [m for d, m in dist if d <= 1.5 * size]
        if not near and dist and dist[0][0] <= 2 * size:
            near = [dist[0][1]]
        lb = {**lb, "near_marks": near}
        if dist:
            lb["nearest_mark_mm"] = round(dist[0][0], 2)
        out.append(lb)
        for m in near:
            if "text" in lb:                 # a placed image has no words to lend
                marks[m - 1].setdefault("labels", []).append(lb["text"])
    return out


def measure_phrase(m: dict) -> str:
    """«Ø ≈ 8,00» / «12,40 mm» — the way the viewer writes a dimension."""
    v = m.get("value", 0)
    num = (f"{v:.1f}" if m.get("unit") == "deg" else f"{v:.2f}").replace(".", ",")
    ap = "≈ " if m.get("approx") else ""
    k = m.get("kind")
    if k == "angle":
        return f"{ap}{num}°"
    if k == "diameter":
        return f"Ø {ap}{num}"
    if k == "radius":
        return f"R {ap}{num}"
    return f"{ap}{num} mm"


_SHAPE_KINDS = ("box", "cylinder", "sphere")
_NOTE_MAX_SHAPES = 30


def _shapes(items, n_views: int, titles: dict) -> list[dict]:
    """Basic shapes the user PLACED on the part (▣ Forme): a cube, a cylinder or
    a sphere, with its size, centre and orientation in model mm — «a Ø 6
    cylinder here» as data. `size` is [x, y, z] in the shape's own frame (a
    cylinder: [Ø, Ø, height], its axis = local z = `axis`); `anchor` is the
    point of the surface it sits on, `normal` that surface's normal. A shape
    bent by the ▣ Deforma cage also carries `ffd` — the 8 cage corners'
    offsets [dx, dy, dz] in its own frame, 1 = its size, corners ordered
    sx, sy, sz ∈ {−1, 1} nested — and `corners`, those 8 corners in the world
    (mm), so a text-only reader sees the bent shape without interpolating."""
    import math
    if not isinstance(items, list) or len(items) > _NOTE_MAX_SHAPES:
        raise ValueError(f"note: shapes must be a list of at most {_NOTE_MAX_SHAPES}")
    out = []
    for i, sh in enumerate(items):
        if not isinstance(sh, dict):
            raise ValueError(f"note: shape {i} must be an object")
        kind = sh.get("kind")
        if kind not in _SHAPE_KINDS:
            raise ValueError(f"note: shape {i} kind must be one of {', '.join(_SHAPE_KINDS)}")
        size = _vec(sh.get("size"), f"shape {i} size")
        if not all(0 < x < 1e5 for x in size):
            raise ValueError(f"note: shape {i} size must be positive")
        q = sh.get("quat")
        if not isinstance(q, (list, tuple)) or len(q) != 4:
            raise ValueError(f"note: shape {i} quat must be [x, y, z, w]")
        q = [_num(c, f"shape {i} quat") for c in q]
        nq = math.sqrt(sum(c * c for c in q))
        if nq < 1e-6:
            raise ValueError(f"note: shape {i} quat must not be zero")
        o = {"kind": kind, "center": _vec(sh.get("center"), f"shape {i} center"), "size": size,
             "quat": [round(c / nq, 5) for c in q]}
        # the shape's own z axis in the world, from the quaternion
        x, y, z, w = o["quat"]
        o["axis"] = [round(2 * (x * z + w * y), 4), round(2 * (y * z - w * x), 4), round(1 - 2 * (x * x + y * y), 4)]
        for k in ("anchor", "normal"):
            if sh.get(k) is not None:
                o[k] = _vec(sh[k], f"shape {i} {k}")
        ffd = sh.get("ffd")
        if ffd is not None:
            if not isinstance(ffd, (list, tuple)) or len(ffd) != 8:
                raise ValueError(f"note: shape {i} ffd must be 8 [dx, dy, dz] offsets")
            ffd = [_vec(d, f"shape {i} ffd") for d in ffd]
            if not all(abs(v) < 10 for d in ffd for v in d):
                raise ValueError(f"note: shape {i} ffd offsets must be finite and below 10")
            if any(v for d in ffd for v in d):
                o["ffd"] = [[round(v, 4) for v in d] for d in ffd]
        corners = sh.get("corners")
        if corners is not None:
            if not isinstance(corners, (list, tuple)) or len(corners) != 8:
                raise ValueError(f"note: shape {i} corners must be 8 points")
            corners = [_vec(c, f"shape {i} corners") for c in corners]
            if "ffd" in o:
                o["corners"] = corners
        color = str(sh.get("color") or "").lower()
        if color:
            if not re.fullmatch(r"#[0-9a-f]{6}", color):
                raise ValueError(f"note: shape {i} color must be #rrggbb")
            o["color"] = color
        piece = str(sh["piece"])[:40] if sh.get("piece") else None
        if piece:
            node = piece.split(".")[0]
            o.update(piece=piece, node=node)
            if node in titles:
                o["title"] = titles[node].get("title")
        v = sh.get("view")
        if v is not None:
            v = int(_num(v, f"shape {i} view"))
            if not 0 <= v < n_views:
                raise ValueError(f"note: shape {i} view {v} out of range")
            o["view"] = v + 1
        out.append(o)
    return out


def shape_phrase(sh: dict) -> str:
    x, y, z = sh["size"]
    f = lambda v: f"{v:.2f}".replace(".", ",")
    if sh["kind"] == "sphere":
        return f"sphere Ø {f(x)}" if abs(x - y) < 1e-3 and abs(y - z) < 1e-3 else f"ellipsoid {f(x)} × {f(y)} × {f(z)}"
    if sh["kind"] == "cylinder":
        return f"cylinder Ø {f(x)} × {f(z)} mm"
    return f"box {f(x)} × {f(y)} × {f(z)} mm"


def _link_shapes(marks: list[dict], strokes: list[dict], shapes: list[dict]) -> list[dict]:
    """The user's shapes for the agent: a one-line `summary` and the marks
    drawn on or next to them (`near_marks`; the mark lists them under `shapes`)."""
    import math
    pts: dict[int, list] = {}
    for s in strokes:
        if s.get("kind") != "text":
            pts.setdefault(s.get("g"), []).extend(s["points"])
    by_mark = list(pts.values())
    out = []
    for sh in shapes:
        sh = {k: v for k, v in sh.items() if k != "near_marks"}
        reach = max(2.0, 0.75 * max(sh["size"]))
        near = [k + 1 for k, ps in enumerate(by_mark) if ps and min(math.dist(sh["center"], p) for p in ps) <= reach]
        c = ", ".join(f"{v:.2f}" for v in sh["center"])
        a = ", ".join(f"{v:.2f}" for v in sh["axis"])
        sh["summary"] = (f"{shape_phrase(sh)}" + (", deformed by its cage (see corners)," if sh.get("ffd") else "")
                         + f" centred at ({c}), axis ({a})"
                         + (f", on {sh.get('title') or sh['node']}" if sh.get("node") else ""))
        sh["near_marks"] = near
        out.append(sh)
        for k in near:
            marks[k - 1].setdefault("shapes", []).append(shape_phrase(sh))
    return out


_MEASURE_SAYS = {"distance": "distance", "edge": "edge length", "face_gap": "gap between parallel faces",
                 "diameter": "diameter", "radius": "radius", "angle": "angle"}


def _link_measures(marks: list[dict], strokes: list[dict], measures: list[dict]) -> list[dict]:
    """The user's dimensions, for the agent: each with a one-line `summary`
    a text-only model can read («face_gap 8,00 mm on Move (face → face)»), and
    `near_marks` = the marks drawn at either end (within a quarter of the
    dimension, ≥ 2 mm) — a red circle round a hole plus «Ø ≈ 8,00» on it is one
    remark. Each such mark lists the dimension under `measures`."""
    import math
    pts: dict[int, list] = {}
    for s in strokes:
        if s.get("kind") != "text":
            pts.setdefault(s.get("g"), []).extend(s["points"])
    by_mark = list(pts.values())
    out = []
    for m in measures:
        m = {k: v for k, v in m.items() if k != "near_marks"}
        ends = [e for e in (m.get("a"), m.get("b")) if isinstance(e, dict)]
        reach = max(2.0, 0.25 * float(m.get("value") or 0))
        circ = (m.get("a") or {}).get("circle") if isinstance(m.get("a"), dict) else None
        if circ:                             # a circled hole: the pen is ON the rim, the Ø at its centre
            reach = max(reach, 1.5 * float(circ.get("r") or 0))
        near = [k + 1 for k, ps in enumerate(by_mark)
                if ps and any(min(math.dist(e["at"], p) for p in ps) <= reach for e in ends)]
        on = [e.get("title") or e.get("node") for e in ends if e.get("title") or e.get("node")]
        snaps = " → ".join(e.get("snap", "free") for e in ends)
        summary = f"{_MEASURE_SAYS.get(m.get('kind'), m.get('kind'))} {measure_phrase(m)}"
        if on:
            summary += f" on {' / '.join(dict.fromkeys(on))}"
        summary += f" ({snaps})"
        if m.get("axis"):
            summary += f", along {m['axis'].upper()} only"
        if m.get("text"):
            summary += f" — «{m['text']}»"
        m["summary"], m["near_marks"] = summary, near
        out.append(m)
        for k in near:
            marks[k - 1].setdefault("measures", []).append(measure_phrase(m))
    return out


def _camera(cam, what="camera") -> Optional[dict]:
    if not (isinstance(cam, dict) and cam.get("position") and cam.get("target")):
        return None
    out = {"position": _vec(cam["position"], what), "target": _vec(cam["target"], what),
           "ortho": bool(cam.get("ortho"))}
    for k in ("zoom", "aspect"):          # ortho zoom; width/height of the canvas
        if cam.get(k) is not None:
            out[k] = round(max(1e-6, min(_num(cam[k], f"{what} {k}"), 1e6)), 5)
    return out


_NOTE_MAX_IMAGES = 8
_NOTE_IMAGE_BYTES = 4 * 1024 * 1024


def _image_kind(data: bytes) -> str:
    """png | jpg from the magic bytes, as the photos are checked."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    raise ValueError("note: a placed image must be a PNG or a JPEG")


def add_note(store: GraphStore, graph_id: str, gen: str, payload: dict,
             jpeg: Optional[bytes] = None,
             view_jpegs: Optional[list] = None,
             image_blobs: Optional[list] = None) -> dict:
    """Validate and store what the /view page sends. `payload` = {text, strokes:
    [{color, width, points:[[x,y,z]…], normals?, piece?}], camera?, t?, hide?}."""
    import datetime
    if not isinstance(payload, dict):
        raise ValueError("note: expected a JSON object")
    text = payload.get("text") or ""
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError("note: text must be a string of at most 4000 chars")
    strokes_in = payload.get("strokes") or []
    if not isinstance(strokes_in, list) or len(strokes_in) > _NOTE_MAX_STROKES:
        raise ValueError(f"note: strokes must be a list of at most {_NOTE_MAX_STROKES}")
    labels_in = payload.get("labels") or []
    n_labels = len(labels_in) if isinstance(labels_in, list) else 0
    images_in = payload.get("images") or []
    if not isinstance(images_in, list) or len(images_in) > _NOTE_MAX_IMAGES:
        raise ValueError(f"note: images must be a list of at most {_NOTE_MAX_IMAGES}")
    image_blobs = list(image_blobs or [])
    if len(image_blobs) != len(images_in) or not all(image_blobs):
        raise ValueError("note: every placed image needs its picture")
    for b in image_blobs:
        if len(b) > _NOTE_IMAGE_BYTES:
            raise ValueError(f"note: a placed image is over {_NOTE_IMAGE_BYTES // (1024 * 1024)} MB")
    kinds = [_image_kind(b) for b in image_blobs]
    measures_in = payload.get("measures") or []
    shapes_in = payload.get("shapes") or []
    if (not strokes_in and not labels_in and not images_in and not measures_in and not shapes_in
            and not text.strip()):
        raise ValueError("note: nothing drawn and nothing written")
    views_in = payload.get("views") or []
    if not isinstance(views_in, list) or len(views_in) > _NOTE_MAX_VIEWS:
        raise ValueError(f"note: views must be a list of at most {_NOTE_MAX_VIEWS}")
    views = [{"camera": _camera(v.get("camera") if isinstance(v, dict) else None, f"view {i}")}
             for i, v in enumerate(views_in)]
    meta = store.load_gen(graph_id, gen, "meta")
    titles = {p.get("id"): p for p in (meta.get("pieces") or [])}
    strokes, total = [], 0
    for i, s in enumerate(strokes_in):
        if not isinstance(s, dict):
            raise ValueError(f"note: stroke {i} must be an object")
        pts = [_vec(p, f"stroke {i} point") for p in (s.get("points") or [])]
        if not pts:
            raise ValueError(f"note: stroke {i} has no points")
        total += len(pts)
        if total > _NOTE_MAX_POINTS:
            raise ValueError(f"note: more than {_NOTE_MAX_POINTS} points in total")
        color = str(s.get("color") or "#ef4444").lower()
        if not re.fullmatch(r"#[0-9a-f]{6}", color):
            raise ValueError(f"note: stroke {i} color must be #rrggbb")
        width = max(0.001, min(_num(s.get("width", 1.0), f"stroke {i} width"), 1e4))
        piece = s.get("piece")
        piece = str(piece)[:40] if piece else None
        node = piece.split(".")[0] if piece else None
        g = s.get("g", i)
        out = {"color": color, "color_name": _NOTE_COLORS.get(color, color),
               "width_mm": round(width, 3), "g": int(_num(g, f"stroke {i} g")), "points": pts}
        if s.get("label") is not None:          # a letter of painted text (the T tool)
            k = int(_num(s["label"], f"stroke {i} label"))
            if not 0 <= k < n_labels:
                raise ValueError(f"note: stroke {i} label {k} out of range")
            out.update(kind="text", label=k + 1)
        v = s.get("view")
        if v is not None:
            v = int(_num(v, f"stroke {i} view"))
            if not 0 <= v < len(views):
                raise ValueError(f"note: stroke {i} view {v} out of range")
            out["view"] = v + 1                # 1-based, as the picture file: aK.v1.jpg
        if s.get("normals") and len(s["normals"]) == len(pts):
            out["normals"] = [_vec(n, f"stroke {i} normal") for n in s["normals"]]
        # ✎ the 3D pen: where the user circled one spot the ink piled up, and
        # each sample sits `lifts[i]` mm above the part along its normal
        if s.get("lifts") is not None:
            lifts = s["lifts"]
            if not isinstance(lifts, list) or len(lifts) != len(pts):
                raise ValueError(f"note: stroke {i} lifts must be one number per point")
            lifts = [round(_num(v, f"stroke {i} lift"), 4) for v in lifts]
            if any(not 0 <= v <= 1e4 for v in lifts):
                raise ValueError(f"note: stroke {i} lifts must be 0..10000 mm")
            if max(lifts) > 0:
                out["lifts"] = lifts
                out["height_mm"] = round(max(lifts) + width, 3)
        if node:
            out["piece"] = piece
            out["node"] = node
            if node in titles:
                out["title"] = titles[node].get("title")
                out["type"] = titles[node].get("type")
        out.update(_stroke_summary(pts, width))
        strokes.append(out)
    labels = _labels(labels_in, len(views), titles)
    # a placed image is placed exactly like a label: same frame, same checks
    images = []
    for i, im in enumerate(images_in):
        if not isinstance(im, dict):
            raise ValueError(f"note: image {i} must be an object")
        (o,) = _labels([{**im, "text": "image", "style": "decal"}], len(views), titles)
        o = {"image": i + 1, **{k: v for k, v in o.items()
                                if k not in ("label", "text", "style", "color", "color_name")}}
        images.append(o)
    measures = _measures(measures_in, len(views), titles)
    shapes = _shapes(shapes_in, len(views), titles)
    marks = _marks(strokes)
    note = {"text": text.strip(),
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "marks": marks, "strokes": strokes, "done": None}
    if labels:
        note["labels"] = _link_labels(marks, strokes, labels)
    if images:
        note["images"] = _link_labels(marks, strokes, images)
    if measures:
        note["measures"] = measures
    if shapes:
        note["shapes"] = shapes
    cam = _camera(payload.get("camera"))
    if cam:
        note["camera"] = cam
    if views:
        note["views"] = views
    if isinstance(payload.get("t"), (int, float)):
        note["t"] = round(min(max(float(payload["t"]), 0.0), 1.0), 4)
    if isinstance(payload.get("hide"), str):
        note["hide"] = payload["hide"][:500]
    return store.save_gen_note(graph_id, gen, note, jpeg, view_jpegs,
                               list(zip(kinds, image_blobs, strict=True)))


def _open_notes(store: GraphStore, graph_id: str, gen: str) -> int:
    try:
        return sum(1 for n in store.list_gen_notes(graph_id, gen) if not n.get("done"))
    except (KeyError, ValueError):
        return 0


def _note_for_agent(n: dict, base_url: str, points: bool) -> dict:
    g, gen, nid = n["graph"], n["gen"], n["id"]
    from urllib.parse import quote
    base = gen_url(g, gen, base_url).rsplit("/view/", 1)[0]
    out = {
        "ref": f"{gen_ref(g, gen)}#{nid}", "graph": g, "gen": gen, "id": nid,
        "text": n.get("text", ""), "created": n.get("created"), "done": n.get("done"),
        "url": f"{gen_url(g, gen, base_url)}#note={nid}",
        # marks = one per gesture (what the user drew); the raw strokes, split
        # wherever the pen left the surface, only on request
        # recomputed on read: a better reading of the strokes reaches old notes too
        "marks": (_marks(n["strokes"]) if n.get("strokes") and all("g" in x for x in n["strokes"])
                  else n.get("marks") or []),
    }
    if n.get("labels"):
        # recomputed like the marks; a mark gets the texts written next to it
        out["labels"] = [{k: v for k, v in lb.items() if k not in ("near_marks", "nearest_mark_mm")}
                         for lb in n["labels"]]
        out["labels"] = _link_labels(out["marks"], n.get("strokes") or [], out["labels"])
    if n.get("images"):
        out["images"] = _link_labels(out["marks"], n.get("strokes") or [],
                                     [{k: v for k, v in im.items() if k not in ("near_marks", "nearest_mark_mm")}
                                      for im in n["images"]])
        for im in out["images"]:
            im["image_url"] = f"{base}/api/graph/{quote(g)}/gens/{gen}/notes/{nid}/img/{im['image']}"
            im["image_path"] = f"projects/{g}/gens/{gen}/notes/{im.get('file', '')}"
            if im.get("view"):                 # the photo of the view it was placed from
                im["view_image_path"] = f"projects/{g}/gens/{gen}/notes/{nid}.v{im['view']}.jpg"
    if n.get("measures"):
        out["measures"] = _link_measures(out["marks"], n.get("strokes") or [], n["measures"])
    if n.get("shapes"):
        out["shapes"] = _link_shapes(out["marks"], n.get("strokes") or [], n["shapes"])
    if points:
        out["strokes"] = n.get("strokes", [])
    for k in ("t", "hide", "camera"):
        if n.get(k) is not None:
            out[k] = n[k]
    if n.get("image"):
        out["image_url"] = f"{base}/api/graph/{quote(g)}/gens/{gen}/notes/{nid}.jpg"
        out["image_path"] = f"projects/{g}/gens/{gen}/notes/{nid}.jpg"
    # a mark drawn from another angle than the last one is NOT in the main
    # picture: it gets the picture of its own view
    for m in out["marks"] + out.get("labels", []):
        if m.get("view"):
            k = m["view"]
            m["image_url"] = f"{base}/api/graph/{quote(g)}/gens/{gen}/notes/{nid}.jpg?view={k}"
            m["image_path"] = f"projects/{g}/gens/{gen}/notes/{nid}.v{k}.jpg"
    if n.get("views"):
        out["views"] = len(n["views"])
    return out


def list_notes(store: GraphStore, graph_id: str = "", gen: str = "", limit: int = 20,
               include_done: bool = False, points: bool = False,
               base_url: str = "") -> list[dict]:
    """Notes the user drew in /view, NEWEST FIRST, across every project (or one,
    or one generation). Open ones only unless `include_done`. `points=True`
    adds every stroke's raw points + surface normals (mm, model frame)."""
    if gen and not graph_id:
        raise ValueError("gen needs graph_id")
    names = [graph_id] if graph_id else store.list()
    out = []
    for name in names:
        try:
            gens = [gen] if gen else [m["gen"] for m in store.list_gens(name) if m.get("gen")]
        except ValueError:
            continue
        for g in gens:
            try:
                notes = store.list_gen_notes(name, g)
            except KeyError:
                if gen:
                    raise
                continue
            for n in notes:
                if include_done or not n.get("done"):
                    out.append(_note_for_agent(n, base_url, points))
    out.sort(key=lambda e: (e["created"] or "", int(e["gen"][1:]), int(e["id"][1:])),
             reverse=True)
    return out[:max(1, int(limit))] if limit else out


def gen_notes_raw(store: GraphStore, graph_id: str, gen: str) -> list[dict]:
    """What /view draws: every note of the gen, with its points."""
    return store.list_gen_notes(graph_id, gen)


def resolve_note(store: GraphStore, graph_id: str, gen: str, note_id: str,
                 reply: str = "", done: bool = True) -> dict:
    """Mark a note handled (the viewer shows it ticked, with `reply` under it),
    or reopen it with done=False."""
    import datetime
    if not isinstance(reply, str) or len(reply) > 4000:
        raise ValueError("reply must be a string of at most 4000 chars")
    state = ({"when": datetime.datetime.now().isoformat(timespec="seconds"),
              "reply": reply.strip()} if done else None)
    return store.update_gen_note(graph_id, gen, note_id, done=state)


def note_asset(store: GraphStore, graph_id: str, gen: str, note_id: str, k: int) -> tuple:
    """(bytes, media type) of the k-th picture the user placed with a note."""
    p = store.gen_note_asset(graph_id, gen, note_id, k)
    return p.read_bytes(), "image/png" if p.suffix == ".png" else "image/jpeg"


def note_image(store: GraphStore, graph_id: str, gen: str, note_id: str,
               view: int = 0, mark: int = 0) -> bytes:
    """A note's picture: the main one (the final view), the `view`-th other
    view, or — with `mark` — whichever picture that mark is visible in."""
    if mark:
        note = next((n for n in store.list_gen_notes(graph_id, gen) if n.get("id") == note_id), None)
        if note is None:
            raise KeyError(f"No note {note_id!r} on {graph_id}/{gen}")
        marks = _marks(note.get("strokes") or [])
        if not 1 <= mark <= len(marks):
            raise ValueError(f"note {note_id} has marks 1..{len(marks)}, not {mark}")
        view = marks[mark - 1]["view"]
    if view < 0:
        raise ValueError("view must be >= 0")
    p = store.gen_note_image(graph_id, gen, note_id, view)
    if not p.exists():
        raise KeyError(f"No picture {view or ''} for note {note_id!r} on {graph_id}/{gen}")
    return p.read_bytes()
