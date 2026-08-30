"""
Node geometry and automatic graph layout.

Two things live here, and the first exists to make the second possible:

**The size model.** A node's on-canvas size is computed by litegraph in the
BROWSER, from its socket and widget count, and — except for a resized sticky
`Note` — it is never written to graph.json. So anything placing nodes
server-side (this module, `api.add_node`, the copilot, an agent writing
graph.json by hand) has historically been placing boxes whose height it could
not know. `node_size()` mirrors litegraph 0.7.18's `LGraphNode.computeSize`
exactly, over the same inputs the editor uses: the `NodeDef` plus the node's own
state.

Mirroring is a drift risk, so it is pinned: `tests/fixtures/node_sizes.json`
holds real sizes captured from a live editor, and `tests/test_layout.py` asserts
this module reproduces them. Regenerate the fixture with
`scripts/capture_node_sizes.py` after any change to how nodes.html builds
widgets. If that test fails, THIS file is wrong, not the fixture.

**The layout.** `arrange()` places a graph left-to-right by dependency depth,
using those real sizes so nodes cannot overlap, and re-fits any group boxes
around the members they had before the move.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from . import catalog
from .catalog import NodeDef, WIRE_CURVE, WIRE_SOLID, WIRE_SURFACE
from .toposort import toposort

# ── litegraph 0.7.18 constants (build/litegraph.js) ────────────────────────
NODE_TITLE_HEIGHT = 30
NODE_SLOT_HEIGHT = 20
NODE_WIDGET_HEIGHT = 20
NODE_WIDTH = 140
NODE_TEXT_SIZE = 14

# nodes.html floors every freshly built node's width at this (the ctor's
# `Math.max(this.size[0], 150)`).
MIN_WIDTH = 150

# Wire types whose nodes get a preview-eye widget (nodes.html: the eye goes on
# any node whose FIRST output can be drawn).
_EYE_WIRES = {WIRE_SOLID, WIRE_SURFACE, WIRE_CURVE}

# One extra button widget each (nodes.html node constructor).
_SELECT_TYPES = {"SelectEdge", "SelectFace", "SelectVertex", "SelectShape"}
_BUTTON_TYPES = _SELECT_TYPES | {"TraceImage"}

# Height reserved under the widgets for the curve mini-preview (`_curvePreviewH`).
_CURVE_PREVIEW_H = 96


def _text_w(text: str) -> float:
    """litegraph's `compute_text_size`: a fixed-ratio approximation, not metrics."""
    if not text:
        return 0.0
    return NODE_TEXT_SIZE * len(text) * 0.6


def _param_widget_count(p) -> int:
    """
    How many litegraph widgets one `Param` produces (nodes.html `addWidget`).

    The one that surprises: a float/int param with BOTH min and max makes TWO
    widgets — the cadslider plus the keyboard-editable `✎` field — while an
    unbounded one makes only the field. A `note` param makes none at all (its
    text is drawn on the body, so a single-line litegraph widget would strip
    the newlines).
    """
    if p.widget == "note":
        return 0
    if p.type in ("bool", "select") or p.widget in ("asset", "font"):
        return 1
    if p.type == "str":
        return 1
    if p.widget in ("curve", "curve3d"):
        return 1
    # float / int: bounded params get the slider AND the field.
    return 2 if (p.min is not None and p.max is not None) else 1


def _grow_inputs(ndef: NodeDef) -> list:
    """
    Inputs that carry a `＋ name` multi toggle — which is itself a widget, and
    the single biggest reason a naive `len(params)` height estimate comes out
    far too short. Everything except a list_access input can fan out.
    """
    return [s for s in (ndef.inputs or []) if s.multiple or not s.list_access]


def _codeblock_param_count(node) -> int:
    """`#@param` declarations in a CodeBlock's code, each of which adds a widget."""
    if node is None:
        return 0
    code = (node.params or {}).get("code")
    if not isinstance(code, str) or "#@param" not in code:
        return 0
    try:  # the transpiler owns the grammar; never let a parse error size a node
        from .transpiler import parse_codeblock_params

        return len(parse_codeblock_params(code))
    except Exception:
        return 0


def widget_count(ndef: NodeDef, node=None) -> int:
    """Total litegraph widgets on a node — the dominant term in its height."""
    n = len(_grow_inputs(ndef))
    for p in ndef.params or []:
        n += _param_widget_count(p)

    out0 = (ndef.outputs or [None])[0]
    if (out0 is not None and out0.wire_type in _EYE_WIRES) or ndef.type == "CodeBlock":
        n += 1  # preview eye
    if ndef.type in _BUTTON_TYPES:
        n += 1  # picker / trace button
    if ndef.gizmo:
        n += 1  # "Edit on canvas" toggle
    if ndef.type == "CodeBlock":
        n += 1 + _codeblock_param_count(node)  # "✎ Edit code" + live #@params
    return n


def _slot_rows(ndef: NodeDef, node=None) -> int:
    """
    Socket rows = max(inputs, outputs), counting the spare slots a multi-input
    shows while its `＋` toggle is on (node.multi, restored on reload).
    """
    n_in = len(ndef.inputs or [])
    if node is not None and getattr(node, "multi", None):
        grow = {s.name for s in _grow_inputs(ndef)}
        # Each toggled-on input keeps ONE empty spare slot beyond what is wired;
        # without the connection list, the spare is all we can know about.
        n_in += sum(1 for name in node.multi if name in grow)
    return max(n_in, len(ndef.outputs or []), 1)


def node_size(ndef: NodeDef, node=None) -> tuple[float, float]:
    """
    The node's canvas size [w, h], as litegraph would compute it.

    `node` is optional: pass it to account for per-node state (a resized Note,
    open multi-slots, a CodeBlock's `#@param` widgets). Without it you get the
    size of a freshly dropped node of this type.
    """
    # A sticky Note is resizable and its size IS persisted — trust it.
    if node is not None and getattr(node, "size", None):
        w, h = float(node.size[0]), float(node.size[1])
        return w, h

    n_widgets = widget_count(ndef, node)
    rows = _slot_rows(ndef, node)

    in_w = max((_text_w(s.name) for s in (ndef.inputs or [])), default=0.0)
    # An output's slot label shows its advisory subtype when it has one.
    out_w = max(
        (_text_w(s.subtype or s.name) for s in (ndef.outputs or [])), default=0.0
    )
    # A renamed node is drawn with ITS name, and the title is part of the width.
    title = (getattr(node, "title", None) or "") if node is not None else ""
    title_w = _text_w(title or ndef.label or ndef.type)

    w = max(in_w + out_w + 10, title_w, NODE_WIDTH)
    if n_widgets:
        w = max(w, NODE_WIDTH * 1.5)

    h = rows * NODE_SLOT_HEIGHT
    if n_widgets:
        h += n_widgets * (NODE_WIDGET_HEIGHT + 4) + 8
    h += 6  # litegraph's trailing margin

    # Node types nodes.html gives a floor or extra body space to.
    if ndef.type == "Note":
        w, h = 240.0, 130.0
    elif any(p.widget == "note" for p in (ndef.params or [])):  # Data / legacy Panel
        w, h = max(w, 200.0), max(h + 64, 150.0)
    elif ndef.type == "Display":
        w, h = max(w, 190.0), max(h + 60, 120.0)

    if any(p.widget == "curve" for p in (ndef.params or [])):
        w, h = max(w, 200.0), h + _CURVE_PREVIEW_H

    return max(w, MIN_WIDTH), h


# ── boxes & overlap ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Box:
    """A node's VISUAL footprint, title bar included."""

    x: float
    y: float
    w: float
    h: float

    def overlaps(self, other: "Box", gap: float = 0.0) -> bool:
        return (
            self.x < other.x + other.w + gap
            and other.x < self.x + self.w + gap
            and self.y < other.y + other.h + gap
            and other.y < self.y + self.h + gap
        )


def node_box(ndef: NodeDef, node) -> Box:
    """
    The node's footprint on the canvas.

    `node.position` is the body's top-left; litegraph draws the TITLE BAR in the
    30px ABOVE it. Layout that ignores this leaves nodes whose maths do not
    overlap but whose titles visibly collide.
    """
    w, h = node_size(ndef, node)
    x, y = float(node.position[0]), float(node.position[1])
    return Box(x, y - NODE_TITLE_HEIGHT, w, h + NODE_TITLE_HEIGHT)


def overlapping_pairs(graph, gap: float = 0.0) -> list[tuple[str, str]]:
    """Every pair of nodes whose visual boxes collide — `arrange`'s postcondition."""
    boxes: list[tuple[str, Box]] = []
    for n in graph.nodes:
        ndef = catalog.REGISTRY.get(n.type)   # .get() raises; unknown types are skipped
        if ndef is None:
            continue
        boxes.append((n.id, node_box(ndef, n)))

    hits = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes[i][1].overlaps(boxes[j][1], gap):
                hits.append((boxes[i][0], boxes[j][0]))
    return hits


# ── layout ────────────────────────────────────────────────────────────────


# A node of this category is a pure parameter source: no inputs, one value out
# (Number Slider, Integer, Number, Boolean, String).
PANEL_CATEGORY = "input"


def is_graph_parameter(ndef: NodeDef, node) -> bool:
    """
    Is this node a NAMED graph parameter — i.e. a control that belongs in the
    panel rather than in the dependency flow?

    Naming is the whole mark: a `Number Slider` still called "Number Slider" is
    just a node, but one the user renamed to "Altezza" is a knob they care about,
    and every knob in a graph is called "Number Slider" until it is renamed. That
    keeps the promotion free of a second piece of UI, and makes the panel exactly
    as long as the user's own labelling.
    """
    if ndef is None or ndef.category != PANEL_CATEGORY:
        return False
    title = (getattr(node, "title", None) or "").strip() if node is not None else ""
    # A title equal to the type's own label is not a name — the editor never
    # stores that one, but an agent writing graph.json by hand might.
    return bool(title) and title not in (ndef.label, ndef.type)


def _ranks(graph, ids: Optional[list[str]] = None) -> dict[str, int]:
    """
    Longest-path layering: a node sits one column right of its DEEPEST input, so
    every wire points forward. (Plain topological order is not enough — it would
    let a node land left of one of its own producers.)

    `ids` restricts the layering to a subset (the panel nodes are lifted out of
    the flow before this runs); edges to anything outside it are ignored.
    """
    ids = list(ids) if ids is not None else [n.id for n in graph.nodes]
    known = set(ids)
    edges = [
        (c.from_node, c.to_node)
        for c in graph.connections
        if c.from_node in known and c.to_node in known
    ]
    order = toposort(ids, edges)

    preds: dict[str, list[str]] = {i: [] for i in ids}
    for src, dst in edges:
        preds[dst].append(src)

    rank: dict[str, int] = {}
    for nid in order:
        rank[nid] = max((rank[p] + 1 for p in preds[nid]), default=0)
    return rank


def _order_columns(
    columns: dict[int, list[str]], graph, group_of: dict[str, int] | None = None,
    sweeps: int = 4
) -> None:
    """
    Reduce wire crossings: repeatedly sort each column by the mean position of
    its neighbours in the adjacent one (the barycentre heuristic), alternating
    forward and backward passes. In place.

    `group_of` keeps a group box's members CONTIGUOUS within each column. Without
    it the barycentre happily interleaves two groups down the same columns, and
    the boxes refitted around them come out overlapping — visually worse than the
    unarranged graph, even though no two NODES collide.
    """
    group_of = group_of or {}
    preds: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    succs: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for c in graph.connections:
        if c.from_node in succs and c.to_node in preds:
            succs[c.from_node].append(c.to_node)
            preds[c.to_node].append(c.from_node)

    for sweep in range(sweeps):
        keys = sorted(columns)
        if sweep % 2:  # backward pass: order on successors instead
            keys = list(reversed(keys))
        for k in keys:
            col = columns[k]
            pos = {nid: i for c in columns.values() for i, nid in enumerate(c)}
            rel = preds if sweep % 2 == 0 else succs
            # Precomputed: CPython's list.sort() blanks the list while it runs, so
            # a key function calling col.index() would raise ValueError.
            here = {nid: i for i, nid in enumerate(col)}

            def bary(nid: str, _rel=rel, _pos=pos, _here=here) -> float:
                nb = [_pos[x] for x in _rel[nid] if x in _pos]
                # No neighbour on that side → hold current place (stable sort).
                return sum(nb) / len(nb) if nb else float(_here[nid])

            b = {nid: bary(nid) for nid in col}
            # Cluster key: a grouped node sorts by its GROUP's mean barycentre, so
            # the whole group travels together; an ungrouped one by its own.
            gmean: dict[int, float] = {}
            for gid in {group_of[n] for n in col if n in group_of}:
                mem = [b[n] for n in col if group_of.get(n) == gid]
                gmean[gid] = sum(mem) / len(mem)
            col.sort(key=lambda n: (gmean.get(group_of.get(n, -1), b[n]), b[n], here[n]))


def arrange(
    graph,
    x_gap: float = 90.0,
    y_gap: float = 45.0,
    origin: tuple[float, float] = (80.0, 120.0),
) -> dict:
    """
    Lay the graph out left-to-right by dependency depth, in place.

    Columns are ranked by longest path, ordered within a column to reduce
    crossings, then stacked using each node's REAL size so nothing can overlap.
    Each column is finally centred on the previous one, which straightens the
    long diagonal wires.

    Group boxes are re-fitted around the members they held BEFORE the move —
    group membership is purely geometric (there is no member list), so it has to
    be resolved up front or every existing group ends up framing empty canvas.

    Returns a summary: columns, nodes moved, and the overlap count after (which
    is asserted to be 0).
    """
    if not graph.nodes:
        return {"nodes": 0, "columns": 0, "moved": 0, "overlaps": 0}

    defs = {n.id: catalog.REGISTRY.get(n.type) for n in graph.nodes}
    unknown = [nid for nid, d in defs.items() if d is None]
    if unknown:
        raise ValueError(f"Unknown node types, cannot size: {sorted(unknown)}")

    by_id = {n.id: n for n in graph.nodes}
    members = _group_members(graph, defs, by_id)
    before = {n.id: tuple(n.position) for n in graph.nodes}
    sizes = {n.id: node_size(defs[n.id], n) for n in graph.nodes}
    group_of = _group_index(members)

    # The PANEL: named parameter sources, lifted out of the dependency flow and
    # gathered at the left where they are one click away. Sorted by name, which
    # is also how you order them — prefix the names and they sort that way.
    # A node the user put in a GROUP is left where it is: an explicit grouping is
    # a stronger statement about where a node belongs than its name is.
    panel = sorted(
        (n for n in graph.nodes
         if is_graph_parameter(defs[n.id], n) and n.id not in group_of),
        key=lambda n: (n.title or "").strip().lower(),
    )
    panel_ids = {n.id for n in panel}
    flow = [n.id for n in graph.nodes if n.id not in panel_ids]

    rank = _ranks(graph, flow)
    columns: dict[int, list[str]] = {}
    for nid in flow:  # seed each column in the graph's own node order
        columns.setdefault(rank[nid], []).append(nid)
    _order_columns(columns, graph, group_of)

    if not columns:  # a graph of nothing but parameters
        columns = {0: []}

    # Every group gets its own horizontal BAND, spanning all columns. Centring
    # each column independently (the obvious thing) lets a band drift vertically
    # from one column to the next, and the boxes refitted around it then cut
    # across each other — which is what a group box existing at all is meant to
    # prevent. Ungrouped nodes share band -1.
    band_of = {nid: group_of.get(nid, -1) for nid in flow}
    seen: dict[int, list[float]] = {}
    for k in sorted(columns):
        for i, nid in enumerate(columns[k]):
            seen.setdefault(band_of[nid], []).append(i)
    bands = sorted(seen, key=lambda b: sum(seen[b]) / len(seen[b])) or [-1]

    def stack_h(ids: list[str]) -> float:
        if not ids:
            return 0.0
        return sum(sizes[i][1] + NODE_TITLE_HEIGHT for i in ids) + y_gap * (len(ids) - 1)

    need = {
        (k, b): stack_h([i for i in columns[k] if band_of[i] == b])
        for k in columns
        for b in bands
    }
    # Pack bands per COLUMN, not globally: two bands that never share a column
    # need no vertical separation at all, and stacking them anyway makes a wide
    # graph needlessly tall.
    band_top: dict[int, float] = {}
    bottom: dict[int, float] = {k: 0.0 for k in columns}
    for b in bands:
        cols_b = [k for k in columns if need[(k, b)] > 0]
        top = max((bottom[k] for k in cols_b), default=0.0)
        band_top[b] = top
        span = max((need[(k, b)] for k in cols_b), default=0.0)
        for k in cols_b:
            bottom[k] = top + span + y_gap

    # The panel occupies its own column; the flow starts clear of it, with a wide
    # gutter so it reads as a separate thing rather than as column 0.
    panel_w = max((sizes[n.id][0] for n in panel), default=0.0)
    x = origin[0] + (panel_w + x_gap * 2 if panel else 0.0)

    for k in sorted(columns):
        col = columns[k]
        if not col:
            continue
        for b in bands:
            ids = [i for i in col if band_of[i] == b]
            if not ids:
                continue
            span = max(need[(kk, b)] for kk in columns)
            # Centre this column's share within the band, so a short column reads
            # as aligned with the tall one instead of hugging its top.
            y = origin[1] + band_top[b] + (span - need[(k, b)]) / 2
            for nid in ids:
                by_id[nid].position = (x, y + NODE_TITLE_HEIGHT)
                y += sizes[nid][1] + NODE_TITLE_HEIGHT + y_gap
        x += max(sizes[i][0] for i in col) + x_gap

    y = origin[1]
    for n in panel:
        n.position = (origin[0], y + NODE_TITLE_HEIGHT)
        y += sizes[n.id][1] + NODE_TITLE_HEIGHT + y_gap

    _refit_groups(graph, members, defs, by_id)

    hits = overlapping_pairs(graph)
    if hits:  # the whole point of the size model — never ship a silent collision
        raise AssertionError(f"arrange() left {len(hits)} overlapping pairs: {hits[:5]}")

    moved = sum(1 for n in graph.nodes if tuple(n.position) != before[n.id])
    return {
        "nodes": len(graph.nodes),
        "columns": len(columns),
        "moved": moved,
        "overlaps": 0,
        "panel": [n.title for n in panel],
        # Groups can still overlap when one group's members genuinely feed
        # another's mid-graph: keeping both contiguous is then impossible. It is
        # reported rather than raised — the nodes are still correctly placed.
        "group_overlaps": _group_overlaps(graph),
    }


def _group_index(members) -> dict[str, int]:
    """node id -> the index of the SMALLEST group box containing it (innermost wins)."""
    out: dict[str, int] = {}
    order = sorted(
        range(len(members)),
        key=lambda i: -((members[i][0].get("bounding") or [0, 0, 0, 0])[2]
                        * (members[i][0].get("bounding") or [0, 0, 0, 0])[3]),
    )
    for i in order:  # largest first, so a nested group overwrites its container
        for nid in members[i][1]:
            out[nid] = i
    return out


def _group_overlaps(graph) -> int:
    """
    Pairs of group boxes that CUT ACROSS each other after the re-fit.

    One box wholly inside another is nesting, not a collision — it is what a
    sub-group looks like — so containment does not count.
    """
    boxes = [
        Box(*(float(v) for v in (g.get("bounding") or [0, 0, 0, 0])[:4]))
        for g in (graph.groups or [])
    ]

    def contains(a: Box, b: Box) -> bool:
        return (
            a.x <= b.x
            and a.y <= b.y
            and a.x + a.w >= b.x + b.w
            and a.y + a.h >= b.y + b.h
        )

    return sum(
        1
        for i in range(len(boxes))
        for j in range(i + 1, len(boxes))
        if boxes[i].overlaps(boxes[j])
        and not contains(boxes[i], boxes[j])
        and not contains(boxes[j], boxes[i])
    )


def _group_members(graph, defs, by_id) -> list[tuple[dict, list[str]]]:
    """
    Resolve each group box's members BEFORE anything moves.

    A group is a bare rectangle — membership is whatever happens to sit inside
    it, exactly as litegraph's `recomputeInsideNodes` decides it: by the node's
    top-left corner.
    """
    out = []
    for g in graph.groups or []:
        b = g.get("bounding") or [0, 0, 0, 0]
        gx, gy, gw, gh = (float(v) for v in b[:4])
        inside = []
        for n in graph.nodes:
            if defs.get(n.id) is None:
                continue
            px, py = float(n.position[0]), float(n.position[1])
            if gx <= px <= gx + gw and gy <= py <= gy + gh:
                inside.append(n.id)
        out.append((g, inside))
    return out


def _refit_groups(graph, members, defs, by_id, pad: float = 24.0, head: float = 26.0) -> None:
    """
    Re-fit each group box around its (pre-move) members, matching the editor's
    own `groupSelected` padding. A group left with no members keeps its box —
    dropping it would silently delete the user's annotation.
    """
    for g, ids in members:
        ids = [i for i in ids if i in by_id]
        if not ids:
            continue
        boxes = [node_box(defs[i], by_id[i]) for i in ids]
        x0 = min(b.x for b in boxes)
        y0 = min(b.y for b in boxes)
        x1 = max(b.x + b.w for b in boxes)
        y1 = max(b.y + b.h for b in boxes)
        g["bounding"] = [
            x0 - pad,
            y0 - pad - head + NODE_TITLE_HEIGHT,
            (x1 - x0) + pad * 2,
            (y1 - y0) + pad * 2 + head - NODE_TITLE_HEIGHT,
        ]
