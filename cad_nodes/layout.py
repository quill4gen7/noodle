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
from typing import Optional

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
        return (w, 0.0) if getattr(node, "collapsed", False) else (w, h)

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

    if node is not None and getattr(node, "collapsed", False):
        # A COLLAPSED node is drawn as its title bar alone. Its width is
        # litegraph's `_collapsed_width` — never more than the full width, and
        # measured with canvas font metrics we cannot mirror — so keep the full
        # width: conservative, and a column is as wide as its widest node anyway.
        return max(w, MIN_WIDTH), 0.0
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


def _edge_weights(graph, ids) -> dict[tuple[str, str], float]:
    """
    One weight per wired PAIR (two wires between the same nodes are one pull):
    1 / fan-out of the source. A hub feeding fourteen nodes pulls each of them
    fourteen times less than a node feeding only that one — otherwise every
    consumer of a shared CodeBlock is dragged toward it, and the chain it
    actually belongs to (ListItem -> Move -> ...) is torn apart.
    """
    known = set(ids)
    pairs = {(c.from_node, c.to_node) for c in graph.connections
             if c.from_node in known and c.to_node in known and c.from_node != c.to_node}
    fan: dict[str, int] = {}
    for a, _ in pairs:
        fan[a] = fan.get(a, 0) + 1
    return {(a, b): 1.0 / fan[a] for a, b in pairs}


def _crossings(layers: list[list], ladj: dict) -> int:
    """Wire crossings between adjacent layers (all edges are adjacent here)."""
    total = 0
    for k in range(len(layers) - 1):
        pos_a = {n: i for i, n in enumerate(layers[k])}
        pos_b = {n: i for i, n in enumerate(layers[k + 1])}
        es = sorted((pos_a[a], pos_b[b]) for a in layers[k] for b in ladj.get(a, ()) if b in pos_b)
        # count inversions of the second coordinate (O(E^2), E per layer is small)
        for i in range(len(es)):
            for j in range(i + 1, len(es)):
                if es[i][0] != es[j][0] and es[i][1] > es[j][1]:
                    total += 1
    return total


def _order_columns(
    columns: dict[int, list[str]], graph, group_of: dict[str, int] | None = None,
    sweeps: int = 12, rank: dict[str, int] | None = None,
) -> None:
    """
    Reduce wire crossings: repeatedly sort each column by the weighted mean
    position of its neighbours in the adjacent one (the barycentre heuristic),
    alternating forward and backward passes, and keep the best order seen. In
    place.

    A wire spanning several columns is routed through one invisible DUMMY per
    column it crosses, so it takes part in the ordering of the columns in
    between instead of being invisible to them (Sugiyama's long-edge rule).

    `group_of` keeps a group box's members CONTIGUOUS within each column. Without
    it the barycentre happily interleaves two groups down the same columns, and
    the boxes refitted around them come out overlapping — visually worse than the
    unarranged graph, even though no two NODES collide.
    """
    group_of = group_of or {}
    ids = [n for col in columns.values() for n in col]
    if rank is None:
        rank = {n: k for k, col in columns.items() for n in col}
    # Every wired pair pulls equally here. (Down-weighting hubs, as `_align`
    # does, was measured to cost crossings in the ORDER: 844 vs 763 over the 112
    # saved and example graphs.)
    weights = {k: 1.0 for k in _edge_weights(graph, ids)}

    keys = sorted(columns)
    layers: dict[int, list] = {k: list(columns[k]) for k in keys}
    # up[n]/down[n]: (neighbour, weight) in the ADJACENT layers only.
    up: dict = {n: [] for n in ids}
    down: dict = {n: [] for n in ids}
    for (a, b), w in sorted(weights.items()):
        ra, rb = rank[a], rank[b]
        if rb <= ra:
            continue
        chain = [a] + [("~", a, b, k) for k in range(ra + 1, rb)] + [b]
        for k, d in zip(range(ra + 1, rb), chain[1:-1], strict=True):
            layers.setdefault(k, []).append(d)
            up[d], down[d] = [], []
        for u, v in zip(chain, chain[1:], strict=False):
            down[u].append((v, w))
            up[v].append((u, w))

    order = [k for k in sorted(layers)]
    ladj = {n: [v for v, _ in down[n]] for n in down}

    def cluster(n):
        return group_of.get(n, -1) if isinstance(n, str) else -1

    def sweep_once(forward: bool) -> None:
        seq = order if forward else list(reversed(order))
        for k in seq:
            col = layers[k]
            if len(col) < 2:
                continue
            nb_k = k - 1 if forward else k + 1
            if nb_k not in layers:
                continue
            pos = {n: i for i, n in enumerate(layers[nb_k])}
            here = {n: i for i, n in enumerate(col)}
            rel = up if forward else down
            b = {}
            for n in col:
                nb = [(pos[m], w) for m, w in rel[n] if m in pos]
                sw = sum(w for _, w in nb)
                # No neighbour on that side -> hold current place (stable sort).
                b[n] = sum(p * w for p, w in nb) / sw if sw else float(here[n])
            # Cluster key: a grouped node sorts by its GROUP's mean barycentre, so
            # the whole group travels together; an ungrouped one by its own.
            gmean: dict[int, float] = {}
            for gid in {cluster(n) for n in col} - {-1}:
                mem = [b[n] for n in col if cluster(n) == gid]
                gmean[gid] = sum(mem) / len(mem)
            col.sort(key=lambda n: (gmean.get(cluster(n), b[n]), b[n], here[n]))

    def snapshot():
        return {k: list(v) for k, v in layers.items()}

    best, best_x = snapshot(), _crossings([layers[k] for k in order], ladj)
    for s in range(sweeps):
        sweep_once(forward=(s % 2 == 0))
        x = _crossings([layers[k] for k in order], ladj)
        if x < best_x:
            best, best_x = snapshot(), x
        if best_x == 0:
            break
    for k in keys:
        columns[k][:] = [n for n in best[k] if isinstance(n, str)]


def _pava(col: list[str], target: dict, weight: dict, h: dict, gap: float) -> dict[str, float]:
    """
    Place one column's boxes as close as possible to their target tops, in their
    given ORDER and without overlap: minimise sum(w * (y - target)^2) subject to
    y[i+1] >= y[i] + h[i] + gap. Shifting each box by the heights stacked above it
    turns the constraint into a plain monotone one, which pool-adjacent-violators
    solves exactly in one pass.
    """
    off, acc = [], 0.0
    for n in col:
        off.append(acc)
        acc += h[n] + gap
    blocks: list[list[float]] = []          # [sum w, sum w*z, count]
    for i, n in enumerate(col):
        blocks.append([weight[n], weight[n] * (target[n] - off[i]), 1])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][0] > blocks[-1][1] / blocks[-1][0]:
            w, wz, c = blocks.pop()
            blocks[-1][0] += w
            blocks[-1][1] += wz
            blocks[-1][2] += c
    out, i = {}, 0
    for w, wz, c in blocks:
        for _ in range(int(c)):
            out[col[i]] = wz / w + off[i]
            i += 1
    return out


def _align(cols: list[list[str]], h: dict, weights: dict, gap: float, iters: int = 10) -> dict[str, float]:
    """
    Vertical placement that STRAIGHTENS wires: each node is pulled toward the
    weighted mean centre of its neighbours (predecessors on forward passes,
    successors on backward ones — ending on a two-sided pass), then its column is
    re-packed in order with `_pava`. A chain ListItem -> Move -> ToMesh comes out
    as one horizontal row instead of a staircase, and a hub settles in the
    middle of what it feeds. Returns box tops (title bar included), min 0.
    """
    ids = {n for col in cols for n in col}
    preds: dict[str, list] = {n: [] for n in ids}
    succs: dict[str, list] = {n: [] for n in ids}
    for (a, b), w in weights.items():
        if a in ids and b in ids:
            succs[a].append((b, w))
            preds[b].append((a, w))

    y: dict[str, float] = {}
    for col in cols:                                   # start: plain stacks
        acc = 0.0
        for n in col:
            y[n] = acc
            acc += h[n] + gap

    def pull(n, rel):
        nb = [(w, y[m] + h[m] / 2) for m, w in rel if m in y]
        sw = sum(w for w, _ in nb)
        if not sw:
            return y[n], 0.05                           # nothing to align to: stay put
        return sum(w * c for w, c in nb) / sw - h[n] / 2, 1.0

    for it in range(iters + 1):
        last = it == iters
        seq = cols if (it % 2 == 0 or last) else list(reversed(cols))
        for col in seq:
            if not col:
                continue
            t, w = {}, {}
            for n in col:
                rel = preds[n] + succs[n] if last else (preds[n] if it % 2 == 0 else succs[n])
                t[n], w[n] = pull(n, rel)
            y.update(_pava(col, t, w, h, gap))
    lo = min(y.values(), default=0.0)
    return {n: v - lo for n, v in y.items()}


def _pack_bands(order: list, shapes: dict, keys: list, gap: float) -> dict:
    """
    Offsets for each band, packed in `order`. What each column already holds is
    kept as (top, bottom) intervals, and a band goes at the SMALLEST offset where
    it collides with nothing in its columns — first fit, not "below everything
    so far", so a band living in one column can sit beside a wide one.
    """
    occupied: dict[int, list[tuple[float, float]]] = {k: [] for k in keys}
    offs = {}
    for b in order:
        ext = shapes[b][0]
        off = _first_fit(ext, occupied, gap)
        for k, (t, e) in ext.items():
            occupied[k].append((off + t, off + e))
        offs[b] = off
    return offs


def _first_fit(ext: dict, occupied: dict, gap: float) -> float:
    """Smallest offset >= 0 (for the band's top) at which every column interval
    of `ext`, shifted by it, clears what `occupied` holds by `gap`."""
    lo = -min(t for t, _ in ext.values())

    def fits(off):
        return all(off + t >= b + gap or off + e + gap <= a
                   for k, (t, e) in ext.items() for a, b in occupied[k])

    for off in sorted({lo} | {b + gap - t for k, (t, _) in ext.items()
                              for _, b in occupied[k] if b + gap - t > lo}):
        if fits(off):
            return off
    return lo   # unreachable: past every interval it always fits


# Box padding the refit adds around a group (see _refit_groups); a band that is
# a group reserves it so two boxes stacked in one column never touch.
_GROUP_PAD, _GROUP_HEAD = 24.0, 26.0


def arrange(
    graph,
    x_gap: float = 90.0,
    y_gap: float = 45.0,
    origin: tuple[float, float] = (80.0, 120.0),
    groups: Optional[str] = None,
) -> dict:
    """
    Lay the graph out left-to-right by dependency depth, in place.

    Columns are ranked by longest path and ordered within a column to reduce
    crossings (`_order_columns`); nodes are then placed vertically so wires run
    as straight as the order allows (`_align`), using each node's REAL size so
    nothing can overlap. Where a node sat before plays no part, so a node
    appended far away (a band of late additions below a gap) is pulled into
    the flow like any other.

    Group boxes are re-fitted around the members they held BEFORE the move —
    group membership is purely geometric (there is no member list), so it has to
    be resolved up front or every existing group ends up framing empty canvas.
    Each group is a rigid horizontal band, so boxes never cut across each other.

    `groups="auto"` first PROPOSES groups for every node not already in one
    (`propose_groups`: the parameters, the shared hubs, one per output chain)
    and adds them to the graph. It is opt-in: a plain arrange never invents
    annotations the user did not ask for.

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
    created: list[str] = []
    if groups == "auto":
        for prop in propose_groups(graph, _members=members):
            g = {"title": prop["title"], "bounding": [0, 0, 0, 0], "color": prop["color"]}
            graph.groups = list(graph.groups or []) + [g]
            members.append((g, prop["members"]))
            created.append(prop["title"])
    elif groups not in (None, "keep"):
        raise ValueError(f"groups must be None or 'auto', not {groups!r}")

    before = {n.id: tuple(n.position) for n in graph.nodes}
    sizes = {n.id: node_size(defs[n.id], n) for n in graph.nodes}
    group_of = _group_index(members)

    # The PANEL: named parameter sources, lifted out of the dependency flow and
    # gathered at the left where they are one click away. Sorted by name, which
    # is also how you order them — prefix the names and they sort that way.
    # A node the user put in a GROUP is left where it is: an explicit grouping is
    # a stronger statement about where a node belongs than its name is — unless
    # the group IS the panel's box ("Parametri", see _parameter_groups): then the
    # whole group is the panel, kept together.
    param_groups = _parameter_groups(graph, members, defs)
    panel = sorted(
        (n for n in graph.nodes
         if (n.id not in group_of and is_graph_parameter(defs[n.id], n))
         or group_of.get(n.id, -1) in param_groups),
        key=lambda n: (group_of.get(n.id, -1), (n.title or "").strip().lower(), n.id),
    )
    panel_ids = {n.id for n in panel}
    flow = [n.id for n in graph.nodes if n.id not in panel_ids]

    rank = _ranks(graph, flow)
    columns: dict[int, list[str]] = {}
    for nid in flow:  # seed each column in the graph's own node order
        columns.setdefault(rank[nid], []).append(nid)
    _order_columns(columns, graph, group_of, rank=rank)

    if not columns:  # a graph of nothing but parameters
        columns = {0: []}

    # Every group gets its own horizontal BAND. Placing each column on its own
    # lets a band drift vertically from one column to the next, and the boxes
    # refitted around it then cut across each other — which is what a group box
    # existing at all is meant to prevent. Ungrouped nodes share band -1.
    band_of = {nid: group_of.get(nid, -1) for nid in flow}
    seen: dict[int, list[float]] = {}
    for k in sorted(columns):
        for i, nid in enumerate(columns[k]):
            seen.setdefault(band_of[nid], []).append(i)
    bands = sorted(seen, key=lambda b: sum(seen[b]) / len(seen[b])) or [-1]

    h = {n: sizes[n][1] + NODE_TITLE_HEIGHT for n in flow}
    weights = _edge_weights(graph, flow)
    keys = sorted(columns)
    # Each band is aligned on its own (band-local coordinates), then packed.
    shapes: dict[int, tuple[dict, dict]] = {}
    for b in bands:
        cols_b = [[n for n in columns[k] if band_of[n] == b] for k in keys]
        local = _align(cols_b, h, weights, y_gap)
        ext: dict[int, tuple[float, float]] = {}
        for k, col in zip(keys, cols_b, strict=True):
            if col:
                ext[k] = (min(local[n] for n in col), max(local[n] + h[n] for n in col))
        if not ext:
            continue
        if b != -1:
            # A group is a RECTANGLE: it blocks every column it spans, padding
            # and title strip included, not just the cells its members occupy.
            lo = min(t for t, _ in ext.values()) - _GROUP_PAD - _GROUP_HEAD
            hi = max(e for _, e in ext.values()) + _GROUP_PAD
            ext = {k: (lo, hi) for k in keys if min(ext) <= k <= max(ext)}
        shapes[b] = (ext, local)

    # Pack in the barycentre order, and also narrow-bands-first: a wide group
    # packed early walls off every column it spans, so a one-column group
    # that could have sat beside it lands underneath instead. Keep the shorter.
    orders = [list(shapes)]
    narrow = sorted(shapes, key=lambda b: (len(shapes[b][0]), orders[0].index(b)))
    if narrow != orders[0]:
        orders.append(narrow)
    best = None
    for order in orders:
        offs = _pack_bands(order, shapes, keys, y_gap)
        height = max((offs[b] + e for b in order for _, e in shapes[b][0].values()), default=0.0)
        if best is None or height < best[0] - 1e-6:
            best = (height, offs)
    top: dict[str, float] = {}
    for b, (_, local) in shapes.items():
        for n, v in local.items():
            top[n] = best[1][b] + v   # `best` is set whenever `shapes` is not empty
    shift = min(top.values(), default=0.0)

    # The panel occupies its own column; the flow starts clear of it, with a wide
    # gutter so it reads as a separate thing rather than as column 0.
    panel_w = max((sizes[n.id][0] for n in panel), default=0.0)
    x = origin[0] + (panel_w + x_gap * 2 if panel else 0.0)
    for k in keys:
        col = columns[k]
        if not col:
            continue
        for nid in col:
            by_id[nid].position = (x, origin[1] + top[nid] - shift + NODE_TITLE_HEIGHT)
        x += max(sizes[i][0] for i in col) + x_gap

    y = origin[1]
    last_g = None
    for n in panel:
        g_n = group_of.get(n.id, -1)
        if last_g is not None and g_n != last_g:
            y += _GROUP_PAD * 2 + _GROUP_HEAD      # room between two panel groups
        last_g = g_n
        n.position = (origin[0], y + NODE_TITLE_HEIGHT)
        y += sizes[n.id][1] + NODE_TITLE_HEIGHT + y_gap

    _refit_groups(graph, members, defs, by_id)

    hits = overlapping_pairs(graph)
    if hits:  # the whole point of the size model — never ship a silent collision
        raise AssertionError(f"arrange() left {len(hits)} overlapping pairs: {hits[:5]}")

    moved = sum(1 for n in graph.nodes if tuple(n.position) != before[n.id])
    out = {
        "nodes": len(graph.nodes),
        "columns": len(columns),
        "moved": moved,
        "overlaps": 0,
        "panel": [n.title or defs[n.id].label for n in panel],
        # Groups can still overlap when one group's members genuinely feed
        # another's mid-graph: keeping both contiguous is then impossible. It is
        # reported rather than raised — the nodes are still correctly placed.
        "group_overlaps": _group_overlaps(graph),
    }
    if groups == "auto":
        out["groups_created"] = created
    return out


def _is_param_source(ndef: NodeDef, node, graph_in: set) -> bool:
    return ndef is not None and ndef.category == PANEL_CATEGORY and node.id not in graph_in


def _parameter_groups(graph, members, defs) -> set[int]:
    """
    Indices of the PANEL's own group boxes: titled like one ("Parametri",
    "Parameters", "Params" — what `propose_groups` creates) and made of nothing
    but parameter sources. Any other group, even one holding only sliders, is
    the user's statement about where those belong and stays in the flow.
    """
    fed = {c.to_node for c in graph.connections}
    by_id = {n.id: n for n in graph.nodes}
    return {
        i for i, (g, ids) in enumerate(members)
        if ids and str(g.get("title") or "").strip().lower() in _PANEL_GROUP_TITLES
        and all(_is_param_source(defs.get(m), by_id[m], fed) for m in ids if m in by_id)
    }


# ── automatic groups ──────────────────────────────────────────────────────

# Muted litegraph group colours, cycled over the proposed chains.
_AUTO_COLORS = ("#3f789e", "#8A8", "#b58b2a", "#88A", "#A88", "#8AA", "#b06634", "#a1309b")
PARAM_GROUP_TITLE = "Parametri"
_PANEL_GROUP_TITLES = {"parametri", "parameters", "params"}
# A node feeding this many distinct nodes is SHARED, not part of one chain.
HUB_FANOUT = 3


def _user_title(ndef: NodeDef, node) -> str:
    t = (getattr(node, "title", None) or "").strip()
    return "" if (not t or t in (ndef.label, ndef.type)) else t


def propose_groups(graph, hub_fanout: int = HUB_FANOUT, _members=None) -> list[dict]:
    """
    Propose group boxes that make a big graph readable, for every node that is
    not already in a group. Nothing is changed; `arrange(groups="auto")` applies
    them. Each proposal is `{title, members, color, kind}`:

    - **Parametri** (kind `params`): every parameter SOURCE — an input-category
      node nothing feeds (sliders, numbers, strings) — named or not.
    - **hubs** (kind `hub`): nodes feeding `hub_fanout`+ distinct nodes are
      shared by many chains and belong to none; connected hubs are boxed
      together, titled after the best-named one.
    - **chains** (kind `chain`): what is left falls apart into connected pieces
      once hubs and parameters are cut out — typically one per output
      (ListItem -> Move -> ToMesh -> PrintCheck -> Display). Each is titled by
      its most DOWNSTREAM user-given name, else "<hub>[index]" for a chain that
      starts at a list-item, else its last node's label. Lone nodes stay ungrouped.
    """
    defs = {n.id: catalog.REGISTRY.get(n.type) for n in graph.nodes}
    by_id = {n.id: n for n in graph.nodes if defs[n.id] is not None}
    if _members is None:
        _members = _group_members(graph, defs, by_id)
    grouped = {m for _, ids in _members for m in ids}
    fed = {c.to_node for c in graph.connections}
    succ: dict[str, set] = {i: set() for i in by_id}
    pred: dict[str, set] = {i: set() for i in by_id}
    for c in graph.connections:
        if c.from_node in by_id and c.to_node in by_id and c.from_node != c.to_node:
            succ[c.from_node].add(c.to_node)
            pred[c.to_node].add(c.from_node)

    free = [i for i in by_id if i not in grouped and defs[i].type != "Note"]
    params = [i for i in free if _is_param_source(defs[i], by_id[i], fed)]
    pset = set(params)
    hubs = {i for i in free if i not in pset and len(succ[i]) >= hub_fanout}
    rest = {i for i in free if i not in pset and i not in hubs}
    rank = _ranks(graph)

    def title_of(i):
        return _user_title(defs[i], by_id[i])

    def components(nodes: set) -> list[list[str]]:
        seen, out = set(), []
        for s in [i for i in by_id if i in nodes]:          # graph order: stable
            if s in seen:
                continue
            comp, stack = [], [s]
            seen.add(s)
            while stack:
                v = stack.pop()
                comp.append(v)
                for u in succ[v] | pred[v]:
                    if u in nodes and u not in seen:
                        seen.add(u)
                        stack.append(u)
            out.append(sorted(comp, key=lambda i: (rank.get(i, 0), list(by_id).index(i))))
        return out

    props: list[dict] = []
    if params:
        props.append({"title": PARAM_GROUP_TITLE, "members": params, "kind": "params",
                      "color": "#444"})

    for comp in components(hubs):
        named = [i for i in comp if title_of(i)]
        best = max(named, key=lambda i: len(succ[i])) if named else max(comp, key=lambda i: len(succ[i]))
        props.append({"title": title_of(best) or defs[best].label, "members": comp, "kind": "hub"})

    for comp in components(rest):
        if len(comp) < 2:
            continue
        named = [i for i in comp if title_of(i)]
        if named:
            title = title_of(named[-1])                  # most downstream name
        else:
            head = comp[0]
            idx = (by_id[head].params or {}).get("index")
            src = next(iter(sorted(pred[head] & hubs)), None) if idx is not None else None
            if src is not None:
                title = f"{title_of(src) or defs[src].label}[{idx}]"
            else:
                title = f"{defs[comp[-1]].label} {comp[-1]}"
        props.append({"title": title, "members": comp, "kind": "chain"})

    ci = 0
    for p in props:
        if "color" not in p:
            p["color"] = _AUTO_COLORS[ci % len(_AUTO_COLORS)]
            ci += 1
    return props


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
