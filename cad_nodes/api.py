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

from . import catalog
from .executor import (execute_graph, export_graph, section_outline_file,
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

    TODO(versioning): the store gains a version/etag on the graph-versioning
    branch; this hook picks it up by duck-typing (`store.version(graph_id)`) so
    the agent write paths below need no change when the two are merged."""
    fn = getattr(store, "version", None)
    return fn(graph_id) if callable(fn) else None


def _check_base_version(store: GraphStore, graph_id: str, base_version) -> None:
    """Refuse a write whose `base_version` is not the current one (optimistic
    concurrency). A no-op while the store has no versions — see graph_version.
    TODO(versioning): may move into store.save() once versions exist."""
    if base_version is None:
        return
    current = graph_version(store, graph_id)
    if current is not None and str(current) != str(base_version):
        raise StaleGraphError(
            f"graph {graph_id!r} changed since version {base_version} (now "
            f"{current}); re-read it with cad_get_graph and retry")


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


def export(store: GraphStore, graph_id: str, fmt: str = "step") -> str:
    """Export the graph to a file; returns the path."""
    graph = store.load(graph_id)
    out = export_graph(graph, store.dir(graph_id), fmt)
    return str(out)


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
        if node.preview is not True:
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
