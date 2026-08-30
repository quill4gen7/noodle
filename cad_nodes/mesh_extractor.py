"""
Mesh / view extraction — runs INSIDE the execution subprocess, so it may import
`build123d`. Turns the transpiled graph's `__result__` into:

  - an STL file (for the existing Three.js viewer), and
  - a `view.json` summary: bbox, volume, area, centre, topology counts and a
    tessellated mesh {vertices, triangles} for direct rendering / AI inspection.

Everything is defensive: a missing capability degrades to null rather than
crashing the whole run.
"""

from __future__ import annotations

import json


def _is_mesh(value) -> bool:
    """True for a value on the `mesh` wire (PLAN_MESH_LANE.md).

    The Mesh class lives in the transpiler PREAMBLE — i.e. in the *generated
    script's* globals — so this module can't import it and isinstance is out.
    Mesh carries a `_noodle_mesh` marker exactly so this check stays honest."""
    return getattr(value, "_noodle_mesh", False)


def _as_shape(result):
    """Normalise __result__ (Part/Sketch/Solid/Compound/Mesh or list) to one shape."""
    if result is None or _is_mesh(result):
        return result
    if isinstance(result, (list, tuple)):
        shapes = [s for s in result if s is not None]
        if not shapes:
            return None
        if all(_is_mesh(s) for s in shapes):
            if len(shapes) == 1:
                return shapes[0]
            import trimesh
            merged = trimesh.util.concatenate([s.tm for s in shapes])
            return type(shapes[0])(merged)      # a Mesh again, engine-agnostic
        from build123d import Compound
        try:
            return Compound(children=shapes)
        except Exception:
            try:
                return Compound.make_compound(shapes)
            except Exception:
                return shapes[0]
    return result


def _num(fn, default=None):
    try:
        v = fn()
        return float(v)
    except Exception:
        return default


def _count(shape, attr):
    try:
        return len(getattr(shape, attr)())
    except Exception:
        return None


def _live_bbox(shape):
    """Approximate bounding box for the LIVE preview path. optimal=True (the
    build123d default) computes the exact box from geometry — ~2s on a complex
    filleted part, and it used to run up to 3x per execute (deflection scaling,
    view summary, preview entry). optimal=False uses curve/surface poles: <10ms,
    within ~0.5% (never smaller). Exports and picker signatures don't use it."""
    return shape.bounding_box(optimal=False)


def _deflection(shape, linear_frac: float) -> float:
    """Linear tessellation deflection scaled to the shape size, so big and
    small parts get comparable visual quality (and triangle counts). Heavy shapes
    (many faces — e.g. rounded engraved text) get a coarser deflection so the LIVE
    preview stays snappy; this only affects the on-screen mesh, never the exported
    STL/STEP (those tessellate via export_stl/export_step, not this path)."""
    try:
        bb = _live_bbox(shape)
        diag = (bb.size.X ** 2 + bb.size.Y ** 2 + bb.size.Z ** 2) ** 0.5
        frac = linear_frac
        try:
            nf = len(shape.faces())
            if nf > 400:
                frac *= 2.5
            elif nf > 120:
                frac *= 1.8
        except Exception:
            pass
        return max(diag * frac, 0.05)
    except Exception:
        return 0.5


# How many list items a panel serialises before truncating (Grasshopper shows
# all; this is a generous cap so the panel stays useful without huge payloads).
_PANEL_CAP = 200


def _summarize(value, _depth: int = 0):
    """Structured, JSON-safe rendering of any node output for the Panel widget.

    Standardised by type so the frontend can show it Grasshopper-style:
      scalar -> the value itself
      point  -> {kind:'point', x,y,z}
      plane  -> {kind:'plane', origin:[x,y,z], normal:[x,y,z]}
      shape  -> {kind:'shape', type, volume, area, faces, edges, vertices}
      list   -> {kind:'list', length, items:[...], truncated?}
    """
    try:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        # a mesh (the `mesh` wire) — report what actually matters about triangles
        if _is_mesh(value):
            return {"kind": "shape", "type": "Mesh", "volume": value.volume,
                    "area": value.area, "faces": value.n_tris,
                    "edges": None, "vertices": len(value.tm.vertices)}
        # vector / point / vertex: anything with X,Y,Z components
        if all(hasattr(value, a) for a in ("X", "Y", "Z")):
            return {"kind": "point", "x": _num(lambda: value.X),
                    "y": _num(lambda: value.Y), "z": _num(lambda: value.Z)}
        # plane / frame: origin + normal
        if hasattr(value, "origin") and hasattr(value, "z_dir"):
            o, z = value.origin, value.z_dir
            return {"kind": "plane",
                    "origin": [_num(lambda: o.X), _num(lambda: o.Y), _num(lambda: o.Z)],
                    "normal": [_num(lambda: z.X), _num(lambda: z.Y), _num(lambda: z.Z)]}
        # list / tuple / ShapeList
        if isinstance(value, (list, tuple)) or type(value).__name__ == "ShapeList":
            items = list(value)
            if _depth >= 4:                       # guard against deep nesting
                return {"kind": "list", "length": len(items), "items": []}
            shown = [_summarize(v, _depth + 1) for v in items[:_PANEL_CAP]]
            out = {"kind": "list", "length": len(items), "items": shown}
            if len(items) > _PANEL_CAP:
                out["truncated"] = len(items) - _PANEL_CAP
            return out
        if isinstance(value, dict):
            return {"kind": "dict", "keys": list(value.keys())[:32]}
        # build123d shape (solid / sketch / curve / face / edge …)
        if any(hasattr(value, a) for a in ("volume", "area", "wrapped")):
            return {"kind": "shape", "type": type(value).__name__,
                    "volume": _num(lambda: value.volume),
                    "area": _num(lambda: value.area),
                    "faces": _count(value, "faces"),
                    "edges": _count(value, "edges"),
                    "vertices": _count(value, "vertices")}
        return {"kind": "repr", "type": type(value).__name__, "repr": repr(value)[:200]}
    except Exception:
        return {"kind": "repr", "type": "unknown", "repr": repr(value)[:200]}


def _mesh_geometry(m) -> dict:
    """Vertices/triangles + bbox for a Mesh. Nothing to tessellate — it already IS
    triangles, which makes the mesh lane the cheapest thing here to draw."""
    verts, tris = m.arrays()
    return {"mesh": {"vertices": verts, "triangles": tris},
            "bbox": _bbox_of_coords(verts)}


def _mesh_view(m, with_mesh: bool) -> dict:
    """The `view.json` summary for a mesh result (the Mesh branch of extract_view)."""
    geom = _mesh_geometry(m)
    bb = geom["bbox"]
    view: dict = {
        "success": True,
        "kind": "Mesh",
        "bbox": dict(bb, approx=False) if bb else None,
        "volume": m.volume,
        "area": m.area,
        "center": None,
        "counts": {"vertices": len(geom["mesh"]["vertices"]), "edges": None,
                   "faces": m.n_tris, "solids": None},
        "watertight": m.watertight,
    }
    if bb:
        view["center"] = [(bb["min"][i] + bb["max"][i]) / 2 for i in range(3)]
    if with_mesh:
        view["mesh"] = geom["mesh"]
    return view


def extract_view(shape, linear_frac: float = 0.02, angular: float = 0.4,
                 with_mesh: bool = True) -> dict:
    shape = _as_shape(shape)
    if shape is None:
        return {"success": False, "error": "no result shape"}
    if _is_mesh(shape):
        return _mesh_view(shape, with_mesh)

    view: dict = {"success": True, "kind": type(shape).__name__}

    # Bounding box — approximate (poles-based, ~0.5% oversized, never smaller):
    # the exact box costs ~2s on complex parts and this one only drives viewer
    # framing + the info footer. `approx` flags it for agents (retroeng should
    # seal on slice_summary/volume, not on this box).
    try:
        bb = _live_bbox(shape)
        view["bbox"] = {
            "min": [bb.min.X, bb.min.Y, bb.min.Z],
            "max": [bb.max.X, bb.max.Y, bb.max.Z],
            "size": [bb.size.X, bb.size.Y, bb.size.Z],
            "approx": True,
        }
    except Exception:
        view["bbox"] = None

    view["volume"] = _num(lambda: shape.volume)
    view["area"] = _num(lambda: shape.area)

    try:
        c = shape.center()
        view["center"] = [c.X, c.Y, c.Z]
    except Exception:
        view["center"] = None

    view["counts"] = {
        "vertices": _count(shape, "vertices"),
        "edges": _count(shape, "edges"),
        "faces": _count(shape, "faces"),
        "solids": _count(shape, "solids"),
    }

    # Tessellated mesh (LOD: coarse for live preview, scaled to part size).
    # Skipped when per-node previews already carry the render geometry — the UIs
    # render from view["previews"] (STL fallback when there are none), never from
    # this, so meshing the terminal result too is pure redundant work.
    if with_mesh:
        try:
            verts, tris = shape.tessellate(_deflection(shape, linear_frac), angular)
            view["mesh"] = {
                "vertices": [[v.X, v.Y, v.Z] for v in verts],
                "triangles": [list(t) for t in tris],
            }
        except Exception as e:
            view["mesh"] = None
            view["mesh_error"] = str(e)

    return view


def _is_point(v) -> bool:
    """A bare point/vector: has X/Y/Z but isn't a topological shape (no edges).

    A build123d Vertex is BOTH — a topological shape that is nothing but a
    position — so the no-edges test alone rejects it and a picked vertex draws
    as nothing at all (it has no mesh and no wire; dots are the only way to see
    it). Name-check it, the way the transpiler's _as_point already does.
    """
    if type(v).__name__ == "Vertex":
        return True
    return (hasattr(v, "X") and hasattr(v, "Y") and hasattr(v, "Z")
            and not hasattr(v, "edges") and not hasattr(v, "faces"))


def _points_of(value):
    """If `value` is a point or a flat list of points (Vectors), return them as
    [[x,y,z], …]; else None. Used to draw DivideCurve / ConstructPoint outputs
    as dots, since they have no mesh or wire of their own."""
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    items = [it for it in items if it is not None]
    if items and all(_is_point(it) for it in items):
        return [[float(it.X), float(it.Y), float(it.Z)] for it in items]
    return None


def _polylines_of(shape, n: int = 32):
    """Sample each edge of `shape` into a polyline [[x,y,z], …]. Returns a list
    of polylines (one per edge), or None if the shape has no usable edges. This
    is the render path for curves/wires, which don't tessellate into triangles."""
    try:
        edges = list(shape.edges())
    except Exception:
        return None
    polys = []
    for e in edges:
        try:
            poly = [[(e @ (k / n)).X, (e @ (k / n)).Y, (e @ (k / n)).Z]
                    for k in range(n + 1)]
            polys.append(poly)
        except Exception:
            continue
    return polys or None


def _bbox_of_coords(coords) -> dict | None:
    """Axis-aligned bbox from a flat list of [x,y,z] points."""
    if not coords:
        return None
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    zs = [c[2] for c in coords]
    lo = [min(xs), min(ys), min(zs)]
    hi = [max(xs), max(ys), max(zs)]
    return {"min": lo, "max": hi, "size": [hi[i] - lo[i] for i in range(3)]}


def _preview_of(value, linear_frac: float = 0.02, angular: float = 0.4) -> dict | None:
    """Compact per-node preview: _preview_geom's geometry, plus any animation
    plan the node runtime attached to its result (`_noodle_anim` — today the
    Drop node's fall/topple timeline). The plan rides the preview entry as
    `anim`, reaches the editor through view.json, and lets the browser replay
    the motion at 60fps while the slider drags — no engine round trip."""
    # A fanned collide result is a LIST whose items each carry their own
    # keyframe plan — render them as a SCENE of independently-animated bodies
    # (one sub-mesh + anim each) so the browser can replay the whole pile, not
    # a single merged blob frozen at one t.
    # `_noodle_extra` are bodies that belong in the PICTURE but not in the output:
    # today a Drop container that MOVES (tilts, shakes, spins). They join the same
    # Scene, each carrying its own keyframe track, so the browser replays them
    # exactly like the falling parts — and a single part plus a moving container
    # is promoted to a Scene too, because otherwise the bowl would be invisible.
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    extras = []
    for v in items:
        for x in (getattr(v, "_noodle_extra", None) or []):
            extras.append(x)
    if extras or (isinstance(value, (list, tuple)) and any(
            getattr(v, "_noodle_anim", None) is not None for v in value)):
        bodies, lo, hi = [], [1e30, 1e30, 1e30], [-1e30, -1e30, -1e30]
        for v in items + extras:
            g = _preview_geom(v, linear_frac, angular)
            if g is None:
                continue
            g["anim"] = getattr(v, "_noodle_anim", None)
            # Which node drew this body. Only the extras (a Drop container) carry
            # one; the falling parts are the Drop's own output and stay None. The
            # viewer resolves colour/finish per body from it, so a glass jar can
            # pour steel bolts instead of the whole scene sharing one finish.
            g["owner"] = getattr(v, "_noodle_owner", None)
            bodies.append(g)
            bb = g.get("bbox")
            if bb:
                lo = [min(lo[i], bb["min"][i]) for i in range(3)]
                hi = [max(hi[i], bb["max"][i]) for i in range(3)]
        if bodies:
            box = ({"min": lo, "max": hi,
                    "size": [hi[i] - lo[i] for i in range(3)]}
                   if hi[0] > -1e30 else None)
            return {"kind": "Scene", "bodies": bodies, "bbox": box}

    entry = _preview_geom(value, linear_frac, angular)
    anim = getattr(value, "_noodle_anim", None)
    if entry is not None and anim is not None:
        entry["anim"] = anim
    return entry


def _merged_pieces(value, linear_frac: float, angular: float) -> dict | None:
    """One buffer, but with the seams remembered: every item of a fanned list is
    tessellated on its own and concatenated, and `parts` records how many
    triangles each contributed. Returns None if the pieces are not all meshable
    triangles (points and curves keep the old whole-value path)."""
    verts: list = []
    tris: list = []
    parts: list = []
    for item in value:
        g = _preview_geom(item, linear_frac, angular)
        if g is None or "mesh" not in g:
            return None                       # not a uniform bag of triangles
        off = len(verts)
        verts.extend(g["mesh"]["vertices"])
        tris.extend([[t[0] + off, t[1] + off, t[2] + off]
                     for t in g["mesh"]["triangles"]])
        parts.append(len(g["mesh"]["triangles"]))
    if not tris:
        return None
    entry: dict = {"kind": "Compound", "mesh": {"vertices": verts, "triangles": tris},
                   "parts": parts}
    entry["bbox"] = _bbox_of_coords(verts)
    return entry


def _preview_geom(value, linear_frac: float = 0.02, angular: float = 0.4) -> dict | None:
    """Compact per-node preview. Three render paths, tried in order:
      - points    : a Vector or list of Vectors -> dots
      - mesh      : a solid/sketch -> tessellated triangles
      - polylines : a curve/wire   -> sampled edges
    Returns None if the value isn't drawable geometry."""
    # 1) bare points (before _as_shape, which can't compound raw Vectors)
    pts = _points_of(value)
    if pts is not None:
        return {"kind": "Points", "points": pts, "bbox": _bbox_of_coords(pts)}

    # 1a) a LIST of drawable pieces: tessellate each one and remember where it
    # starts. `parts` (one triangle count per piece) is what lets the viewer give
    # every piece its own colour WITHOUT re-executing — colour is a display
    # setting and re-rendering reads the cached view.json, so the split has to be
    # in the data whether or not anyone is looking at it. It costs one int per
    # piece against megabytes of vertices; merging still happens, so an unrainbowed
    # array is still one buffer and one draw call.
    if isinstance(value, (list, tuple)) and len(value) > 1:
        merged = _merged_pieces(value, linear_frac, angular)
        if merged is not None:
            return merged

    shape = _as_shape(value)
    if shape is None:
        return None

    # 1b) a mesh: already triangles, so no tessellation step at all
    if _is_mesh(shape):
        entry = _mesh_geometry(shape)
        entry["kind"] = "Mesh"
        entry["volume"] = shape.volume
        return entry

    # 2) meshable surface/solid
    verts, tris = [], []
    try:
        verts, tris = shape.tessellate(_deflection(shape, linear_frac), angular)
    except Exception:
        verts, tris = [], []
    if verts and tris:
        entry: dict = {"kind": type(shape).__name__,
                       "mesh": {"vertices": [[v.X, v.Y, v.Z] for v in verts],
                                "triangles": [list(t) for t in tris]}}
        try:
            bb = _live_bbox(shape)
            entry["bbox"] = {"min": [bb.min.X, bb.min.Y, bb.min.Z],
                             "max": [bb.max.X, bb.max.Y, bb.max.Z],
                             "size": [bb.size.X, bb.size.Y, bb.size.Z]}
        except Exception:
            entry["bbox"] = None
        entry["volume"] = _num(lambda: shape.volume)
        return entry

    # 3) curve / wire -> polylines
    polys = _polylines_of(shape)
    if polys:
        flat = [p for poly in polys for p in poly]
        return {"kind": type(shape).__name__, "polylines": polys,
                "bbox": _bbox_of_coords(flat)}
    return None


def _extract_pieces(shape, linear_frac: float = 0.01, angular: float = 0.3) -> dict:
    """kind='shape': the pickable units are WHOLE objects from a list (or the
    children of a Compound), not decomposed sub-shapes. Each piece renders as a
    mesh (solid/face), polylines (curve) or a point, with a bbox-centre + size
    signature. Universal — one picker for any shape type."""
    if isinstance(shape, (list, tuple)) or type(shape).__name__ == "ShapeList":
        pieces = [s for s in shape if s is not None]
    elif shape is None:
        return {"success": False, "error": "no shape"}
    else:
        try:
            pieces = list(shape.children) if getattr(shape, "children", None) else []
        except Exception:
            pieces = []
        pieces = pieces or [shape]
    out: dict = {"success": True, "kind": "shape", "mesh": None}
    items: list[dict] = []
    for i, p in enumerate(pieces):
        try:
            bb = p.bounding_box()
            c = (bb.min + bb.max) * 0.5
            sz = (_num(lambda p=p: p.volume) or _num(lambda p=p: p.area)
                  or _num(lambda p=p: p.length) or 0.0)
            item = {"index": i, "sig": [c.X, c.Y, c.Z, float(sz)]}
            mesh = None
            try:
                fv, ft = p.tessellate(_deflection(p, linear_frac), angular)
                if ft:
                    mesh = {"vertices": [[v.X, v.Y, v.Z] for v in fv],
                            "triangles": [list(t) for t in ft]}
            except Exception:
                mesh = None
            if mesh:
                item["mesh"] = mesh
            else:
                polys = _polylines_of(p)
                if polys:
                    item["polys"] = polys
                else:
                    item["point"] = [c.X, c.Y, c.Z]
            items.append(item)
        except Exception:
            continue
    out["items"] = items
    return out


def _coplanar_edge_loops(shape) -> list:
    """Groups of edge indices that each form a closed loop lying in ONE plane —
    the boundary wires of every PLANAR face. Indices match enumerate(shape.edges())
    (the picker's item indices); edges are matched by their midpoint (e @ 0.5) to
    survive the identity mismatch between face.wires().edges() and shape.edges().
    Powers the picker's 'grab coplanar loop' expansion (click one edge -> its whole
    same-plane ring). Returns [] when the shape has no planar faces / on any error."""
    try:
        from build123d import GeomType
        edges = list(shape.edges())
        faces = list(shape.faces())
    except Exception:
        return []

    def _mid(e):
        try:
            m = e @ 0.5
            return (m.X, m.Y, m.Z)
        except Exception:
            return None

    mids = [_mid(e) for e in edges]

    def _nearest(pt):
        # midpoints of the SAME edge coincide (tol 1e-3); -1 if nothing matches.
        if pt is None:
            return -1
        best, bi = 1e-6, -1
        for i, m in enumerate(mids):
            if m is None:
                continue
            d = (m[0] - pt[0]) ** 2 + (m[1] - pt[1]) ** 2 + (m[2] - pt[2]) ** 2
            if d < best:
                best, bi = d, i
        return bi

    loops: list = []
    for f in faces:
        try:
            if f.geom_type != GeomType.PLANE:
                continue
            wires = list(f.wires())
        except Exception:
            continue
        for w in wires:
            try:
                grp = sorted({_nearest(_mid(e)) for e in w.edges()})
            except Exception:
                continue
            grp = [g for g in grp if g >= 0]
            if grp and grp not in loops:
                loops.append(grp)
    return loops


def extract_subshapes(shape, kind: str = "edge",
                      linear_frac: float = 0.01, angular: float = 0.3) -> dict:
    """For the interactive picker: a context mesh of `shape` plus its sub-shapes
    (edges as polylines, faces as triangle groups, vertices as points), each
    tagged with an index and a geometric signature for stable re-selection.

    Signature layout (all keep [x,y,z] anchor first, used for nearest-match):
      edge   -> [mid.x, mid.y, mid.z, length, dir.x, dir.y, dir.z]
      face   -> [c.x, c.y, c.z, area, n.x, n.y, n.z]
      vertex -> [x, y, z]
      shape  -> [centre.x, centre.y, centre.z, size]   (whole object from a list)
    """
    if kind == "shape":
        return _extract_pieces(shape, linear_frac, angular)
    shape = _as_shape(shape)
    if shape is None:
        return {"success": False, "error": "no shape"}
    out: dict = {"success": True, "kind": kind}

    try:
        verts, tris = shape.tessellate(_deflection(shape, linear_frac), angular)
        out["mesh"] = {"vertices": [[v.X, v.Y, v.Z] for v in verts],
                       "triangles": [list(t) for t in tris]}
    except Exception:
        out["mesh"] = None

    items: list[dict] = []
    if kind == "edge":
        for i, e in enumerate(shape.edges()):
            try:
                n = 18
                poly = [[(e @ (k / n)).X, (e @ (k / n)).Y, (e @ (k / n)).Z]
                        for k in range(n + 1)]
                mid, d = e @ 0.5, e % 0.5
                items.append({"index": i, "polyline": poly,
                              "sig": [mid.X, mid.Y, mid.Z, float(e.length),
                                      d.X, d.Y, d.Z]})
            except Exception:
                continue
        out["loops"] = _coplanar_edge_loops(shape)   # for 'grab coplanar loop'
    elif kind == "face":
        for i, f in enumerate(shape.faces()):
            try:
                fv, ft = f.tessellate(_deflection(shape, linear_frac), angular)
                c = f.center()
                nrm = f.normal_at(c)
                items.append({"index": i,
                              "mesh": {"vertices": [[v.X, v.Y, v.Z] for v in fv],
                                       "triangles": [list(t) for t in ft]},
                              "sig": [c.X, c.Y, c.Z, float(f.area),
                                      nrm.X, nrm.Y, nrm.Z]})
            except Exception:
                continue
    elif kind == "vertex":
        for i, v in enumerate(shape.vertices()):
            try:
                items.append({"index": i, "point": [v.X, v.Y, v.Z],
                              "sig": [v.X, v.Y, v.Z]})
            except Exception:
                continue

    out["items"] = items
    out["count"] = len(items)
    return out


# Same LRU cap as the transpiler PREAMBLE's _memo_put (the store is shared).
_MEMO_CAP = 256


def _cache_get(memo, key):
    if memo is None or key is None:
        return None
    v = memo.get(key)
    if v is not None:
        memo.pop(key, None)      # LRU touch
        memo[key] = v
    return v


def _cache_put(memo, key, value) -> None:
    if memo is None or key is None or value is None:
        return
    while len(memo) >= _MEMO_CAP:
        memo.pop(next(iter(memo)))
    memo[key] = value


def extract_and_write(result, stl_path: str, view_path: str, panels=None,
                      previews=None, linear_frac: float = 0.02,
                      angular: float = 0.4, errors=None, timings=None,
                      hashes=None, memo=None, cached_nodes=None) -> dict:
    """Write STL + view.json. Returns the view dict.

    hashes/memo (memo-mode runs on the warm worker): per-node content keys +
    the persistent store. Preview meshes and the terminal view summary — the
    two OCCT-heavy steps — are cached by (content key, LOD), so an unchanged
    node is never re-tessellated.

    cached_nodes: ids the memo store served without re-running, so the editor
    can tell a cache hit from a real re-execution (view["node_cached"])."""
    hashes = hashes or {}
    shape = _as_shape(result)
    # When previews carry the render geometry, skip the redundant terminal mesh.
    with_mesh = not previews
    vkey = hashes.get("__result__")
    vck = f"view:{vkey}:{int(with_mesh)}:{linear_frac}:{angular}" if vkey else None
    cached = _cache_get(memo, vck)
    if cached is not None:
        view = dict(cached)      # copy: node_errors/timings/stl get added below
    else:
        view = extract_view(shape, linear_frac, angular, with_mesh=with_mesh)
        if view.get("success"):
            _cache_put(memo, vck, dict(view))
    if errors:
        view["node_errors"] = dict(errors)
    if timings:
        view["node_timings"] = {k: round(float(v), 4) for k, v in timings.items()}
    if cached_nodes:
        view["node_cached"] = sorted(cached_nodes)

    if shape is not None and stl_path:
        try:
            if _is_mesh(shape):
                shape.tm.export(stl_path)     # already triangles — no meshing step
            else:
                from build123d import export_stl
                export_stl(shape, stl_path)
            view["stl"] = stl_path
        except Exception as e:
            view["stl_error"] = str(e)

    if panels:
        view["panels"] = {k: _summarize(v) for k, v in panels.items()}

    if previews:
        out: dict = {}
        for nid, val in previews.items():
            key = hashes.get(nid)
            pck = f"mesh:{key}:{linear_frac}:{angular}" if key else None
            entry = _cache_get(memo, pck)
            if entry is None:
                try:
                    entry = _preview_of(val, linear_frac, angular)
                except Exception:
                    entry = None
                _cache_put(memo, pck, entry)
            if entry:
                out[nid] = entry
        if out:
            view["previews"] = out

    if view_path:
        with open(view_path, "w") as f:
            json.dump(view, f)
    return view
