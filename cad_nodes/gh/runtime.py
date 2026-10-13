"""Runtime of the Grasshopper components (category `grasshopper`).

GH_PREAMBLE is appended to the transpiler's PREAMBLE, so everything in it runs in
the worker with build123d loaded — the same rules as the PREAMBLE itself: no
import of cad_nodes, helpers prefixed (`_gh_`), and a helper must never place
its own result when the node has an `origin` socket (none of these do).

What these nodes promise is GRASSHOPPER's behaviour, not noodle's, where the two
differ: a plane is the frame a curve is drawn in, a number fed to a domain input
means [0, n], a positive Offset Curve distance goes to the RIGHT of the curve's
direction about the plane normal (outward on a counter-clockwise loop), angles are
radians unless the node's `degrees` is on. Several helpers return a dict keyed by
the node's output socket names: those nodes are `multi_out` (see catalog.py).

Conventions measured against build123d 0.11 / OCCT 7.8 — notes inline where OCCT
and Rhino part ways.
"""

GH_PREAMBLE = r'''

# ---------------------------------------------------------------------------
# Grasshopper components (cad_nodes/gh/runtime.py)
# ---------------------------------------------------------------------------

def _gh_nested(_v):
    """A list holding lists = a TREE: one branch per sub-list."""
    return _is_seq(_v) and any(_is_seq(x) for x in _v)


def _gh_fanout(_fn, _items, _lists):
    """Grasshopper data matching, branch by branch. `_items` are the node's
    item-access inputs, `_lists` its list-access ones. When any of them is a
    tree (a list of lists) the node runs once per branch — a flat value or list
    is ONE branch, reused for every branch (GH's longest-list on branches) —
    and the result is a list of per-branch results. Without trees it is
    noodle's ordinary item fan-out, the list inputs passed whole."""
    tree_i = [k for k, v in _items.items() if _gh_nested(v)]
    tree_l = [k for k, v in _lists.items() if _gh_nested(v)]
    if not tree_i and not tree_l:
        r = (_fanout(lambda **_kw: _fn(**_kw, **_lists), _items))
        # ONE item fanned is one branch: its list output IS the flat list, not a
        # tree of one branch (or every list in a GH graph would nest one level
        # deeper at each step)
        if isinstance(r, list) and len(r) == 1 and (_is_seq(r[0]) or isinstance(r[0], dict)) \
                and any(_is_seq(v) for v in _items.values()):
            return r[0]
        return r
    n = max([len(_items[k]) for k in tree_i] + [len(_lists[k]) for k in tree_l])
    if n > _FANOUT_MAX:
        raise ValueError(f"{n} branches exceed the {_FANOUT_MAX} limit")
    pick = lambda v, i: list(v)[min(i, len(v) - 1)] if len(v) else None
    out = []
    for i in range(n):
        it = {k: (pick(v, i) if k in tree_i else v) for k, v in _items.items()}
        ls = {k: (pick(v, i) if k in tree_l else v) for k, v in _lists.items()}
        r = _gh_fanout(_fn, it, ls)
        # a branch that yields ONE value is still a branch: keep it a list, or
        # the tree would flatten into a plain list and the next node would pair
        # these values item by item instead of branch by branch
        out.append(r if _is_seq(r) else [r])
    return out


def _gh_items(_v):
    """A wired value as a flat list (None -> [])."""
    if _v is None:
        return []
    if _is_seq(_v):
        return [x for x in _flatten(_v) if x is not None]
    return [_v]


def _gh_num(_v, _default=0.0):
    if _v is None:
        return _default
    if _is_seq(_v):
        _v = _gh_items(_v)
        return float(_v[0]) if _v else _default
    return float(_v)


def _gh_point(_v):
    """GH's point cast: a Vector, a Vertex, a plane's origin, a 3-tuple."""
    if _v is None:
        return None
    if isinstance(_v, Plane):
        return Vector(_v.origin)
    if isinstance(_v, Vector):
        return _v
    try:
        return _pt(_v)
    except Exception:
        return None


def _gh_vec(_v, _default=(0, 0, 1)):
    if _v is None:
        return Vector(*_default)
    if isinstance(_v, Vector):
        return _v
    if _is_seq(_v) and len(_v) == 3 and all(isinstance(c, (int, float)) for c in _v):
        return Vector(*_v)
    p = _gh_point(_v)
    return p if p is not None else Vector(*_default)


def _gh_plane(_v):
    """GH's plane cast: a Plane as is, a point -> the XY plane through it, a
    closed planar CURVE or a planar face -> the plane it lies in (origin at its
    area centroid, x along its first segment — GH reads a Bounding Rectangle
    straight into Move To Plane / Plane Surface that way), nothing -> world XY."""
    if _v is None:
        return Plane.XY
    if _is_seq(_v):
        it = _gh_items(_v)
        _v = it[0] if it else None
        if _v is None:
            return Plane.XY
    if isinstance(_v, Plane):
        return _v
    if isinstance(_v, Location):
        return Plane(_v)
    if isinstance(_v, (Wire, Edge, Face, Shell, Solid, Compound)) or hasattr(_v, "wires"):
        f = None
        if isinstance(_v, (Wire, Edge)):
            w = _gh_curve(_v)
            if w is not None and w.is_closed:
                try:
                    f = Face(w)
                except Exception:
                    f = None
        else:
            f = _gh_face(_v)
        if f is not None and f.geom_type == GeomType.PLANE:
            n = f.normal_at()
            w = f.outer_wire()
            x = w.edges()[0].tangent_at(0)
            x = (x - n * x.dot(n)).normalized()
            if isinstance(_v, (Wire, Edge)) and _gh_signed_area(_v, Plane(f.center(), x, n)) < 0:
                n = -n                       # the curve's own sense of turning
            return Plane(f.center(CenterOf.MASS), x, n)
        raise ValueError("a curve or surface used as a plane must be planar (and a curve closed)")
    p = _gh_point(_v)
    return Plane.XY.move(Location(p)) if p is not None else Plane.XY


def _gh_bbox(_s):
    """The EXACT bounding box, from the geometry, never from a tessellation.
    build123d's bounding_box() reads the triangulation stored on the edges,
    and once ANY face sharing those edges has been tessellated off the XY
    plane (Populate does it, the preview does it) that triangulation is wrong
    for the curve: a profile 0 x 180 x 90 in x = 220 measures 90 x 270 x 180.
    Six lines reproduce it; it bit ikeaLack #99 -> Bounding Box -> Voronoi.
    The core PREAMBLE's _bbox is the one implementation (the whole engine uses
    it now); this name stays for the GH helpers that already call it."""
    return _bbox(_s)

def _gh_from_local(_pl, _s):
    """A shape drawn in a plane's local frame, brought back to the world with
    the transform BAKED into its geometry. Plane.from_local_coords only sets a
    Location and keeps the local geometry underneath; a face later built on
    those edges and tessellated (Populate does, on any face off the XY plane)
    stores polygons that ignore the Location, and every bounding box measured
    afterwards — of the curve itself, by Bounding Box, by Voronoi 3D — comes
    out as garbage (ikeaLack #99: 262 x 473 x 398 for a curve 0 x 210 x 135
    lying in x = 220). Points pass through."""
    out = _pl.from_local_coords(_s)
    if not hasattr(out, "wrapped") or isinstance(out, Vector):
        return out
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    loc = out.wrapped.Location()
    if loc.IsIdentity():
        return out
    base = out.wrapped.Located(type(loc)())
    baked = BRepBuilderAPI_Transform(base, loc.Transformation(), True).Shape()
    return _gh_wrap(baked)

def _gh_world_plane(_kind, _origin=None):
    """XY / XZ / YZ Plane components. Their axes match Rhino's: XZ has normal -Y
    (x=X, y=Z), YZ has normal +X (x=Y, y=Z) — build123d's Plane.XZ/YZ agree."""
    pl = {"XY": Plane.XY, "XZ": Plane.XZ, "YZ": Plane.YZ}[_kind]
    o = _gh_point(_origin)
    return Plane(o if o is not None else (0, 0, 0), pl.x_dir, pl.z_dir)


def _gh_domain(_v, _default=(0.0, 1.0)):
    """GH's domain cast: [a, b] as is, a number n -> [0, n]."""
    if _v is None:
        return [float(_default[0]), float(_default[1])]
    if isinstance(_v, dict) and "u" in _v:
        return list(_v["u"])
    if _is_seq(_v):
        it = list(_v)
        if len(it) == 2 and not _is_seq(it[0]):
            return [float(it[0]), float(it[1])]
        if len(it) == 1:
            return _gh_domain(it[0], _default)
        nums = [float(x) for x in _flatten(it)]
        return [min(nums), max(nums)] if nums else list(_default)
    return [0.0, float(_v)]


def _gh_shapes(_v):
    """The build123d shapes in a wired value (curves, faces, solids…), flat."""
    out = []
    for it in _gh_items(_v):
        if isinstance(it, (Vector, Plane)) or _is_mesh(it):
            continue
        if hasattr(it, "wrapped"):
            out.append(it)
    return out


def _gh_list(_v):
    """A LIST output as GH hands it on: always a list, a single item included.
    Fanned over several inputs, these lists stack into a tree (one branch per
    input) — unwrapping a lone item would flatten that tree and pair the next
    node's data item by item instead of branch by branch."""
    if _v is None:
        return []
    if isinstance(_v, (list, tuple, ShapeList)):
        return list(_v)
    return [_v]

def _gh_curve(_v):
    """One curve as a Wire (an Edge is wrapped; a face gives its outer wire)."""
    if _v is None:
        return None
    if _is_seq(_v):
        it = _gh_items(_v)
        _v = it[0] if it else None
        if _v is None:
            return None
    if isinstance(_v, Wire):
        return _v
    if isinstance(_v, Edge):
        return Wire([_v])
    if hasattr(_v, "outer_wire") and isinstance(_v, Face):
        return _v.outer_wire()
    try:
        ws = _v.wires()
        if ws:
            return ws[0]
        es = _v.edges()
        if es:
            return Wire(list(es))
    except Exception:
        pass
    return None


def _gh_face(_v):
    if _v is None:
        return None
    if isinstance(_v, Face):
        return _v
    try:
        fs = _v.faces()
        return fs[0] if fs else None
    except Exception:
        return None


def _gh_out(_v):
    """A result list as GH would hand it on: one item unwrapped, else a list."""
    if _v is None:
        return None
    if isinstance(_v, (list, tuple, ShapeList)):
        _v = list(_v)
        if not _v:
            return None
        return _v[0] if len(_v) == 1 else _v
    return _v


def _gh_wrap(_sh):
    """A raw OCP TopoDS_Shape as the build123d class of its type (build123d
    0.11's Shape.cast is abstract on the base class and answers None)."""
    from build123d.topology.shape_core import downcast
    from OCP.TopAbs import TopAbs_ShapeEnum as _T
    d = downcast(_sh)
    cls = {_T.TopAbs_COMPOUND: Compound, _T.TopAbs_SOLID: Solid,
           _T.TopAbs_SHELL: Shell, _T.TopAbs_FACE: Face, _T.TopAbs_WIRE: Wire,
           _T.TopAbs_EDGE: Edge, _T.TopAbs_VERTEX: Vertex}.get(_sh.ShapeType(), Compound)
    return cls(d)


# --- vectors, planes, points ------------------------------------------------

def _gh_unit(_axis, _factor=1.0):
    f = _gh_num(_factor, 1.0)
    return Vector(*[f if a == _axis else 0.0 for a in "XYZ"])


def _gh_deconstruct(_p):
    p = _gh_point(_p)
    if p is None:
        return {"x_component": None, "y_component": None, "z_component": None}
    return {"x_component": p.X, "y_component": p.Y, "z_component": p.Z}


def _gh_average(_items):
    it = _gh_items(_items)
    if not it:
        return None
    if all(isinstance(x, (int, float)) for x in it):
        return sum(it) / len(it)
    pts = [_gh_point(x) for x in it]
    pts = [p for p in pts if p is not None]
    if not pts:
        return None
    s = Vector(0, 0, 0)
    for p in pts:
        s = s + p
    return s / len(pts)


# --- domains ------------------------------------------------------------------

def _gh_construct_domain2(_u, _v):
    return {"u": _gh_domain(_u), "v": _gh_domain(_v)}


def _gh_face_uv(_f):
    """The UV domain of a face as OCCT parameterises it. NOT Rhino's numbers in
    general (a revolve runs in radians in both, an extrusion's V does not), so a
    domain is only meaningful against the face it was read from."""
    from OCP.BRepTools import BRepTools
    u0, u1, v0, v1 = BRepTools.UVBounds_s(_f.wrapped)
    return {"u": [u0, u1], "v": [v0, v1]}


def _gh_deconstruct_domain2(_d, _reparameterize=False):
    if _d is None:
        return {"u_min": None, "u_max": None, "v_min": None, "v_max": None}
    if not isinstance(_d, dict):
        f = _gh_face(_d)
        if f is None:
            raise ValueError("Deconstruct Domain²: wire a 2D domain or a surface")
        # GH's Reparameterize flag on a surface input: its domain becomes 0..1
        _d = {"u": [0.0, 1.0], "v": [0.0, 1.0]} if _reparameterize else _gh_face_uv(f)
    return {"u_min": _d["u"][0], "u_max": _d["u"][1],
            "v_min": _d["v"][0], "v_max": _d["v"][1]}


# --- curves ---------------------------------------------------------------------

def _gh_rectangle(_plane, _x, _y, _r=0.0):
    """Rectangle on a plane from two domains (a number n = [0, n]), drawn
    counter-clockwise about the plane normal like Rhino's — that is what makes a
    positive Offset Curve distance go outward on it."""
    pl = _gh_plane(_plane)
    x0, x1 = _gh_domain(_x)
    y0, y1 = _gh_domain(_y)
    if abs(x1 - x0) < 1e-12 or abs(y1 - y0) < 1e-12:
        raise ValueError("Rectangle: a side of zero length")
    xa, xb = min(x0, x1), max(x0, x1)
    ya, yb = min(y0, y1), max(y0, y1)
    r = abs(_gh_num(_r, 0.0))
    if r > 1e-9:
        r = min(r, (xb - xa) / 2 - 1e-9, (yb - ya) / 2 - 1e-9)
        w = Polyline((xa, ya), (xb, ya), (xb, yb), (xa, yb), close=True)
        w = w.fillet_2d(r, w.vertices())
    else:
        w = Polyline((xa, ya), (xb, ya), (xb, yb), (xa, yb), close=True)
    length = w.length
    return {"rectangle": _gh_from_local(pl, w), "length": length}


def _gh_circle(_plane, _r):
    pl = _gh_plane(_plane)
    r = _gh_num(_r, 1.0)
    return _gh_from_local(pl, Wire([Edge.make_circle(abs(r))]))


def _gh_signed_area(_w, _pl):
    """Signed area of a (closed) curve projected on a plane: > 0 when it runs
    counter-clockwise about the plane normal."""
    loc = _pl.to_local_coords(_w)
    pts = [loc.position_at(i / 96.0) for i in range(96)]
    a = 0.0
    for i in range(len(pts)):
        p, q = pts[i], pts[(i + 1) % len(pts)]
        a += p.X * q.Y - q.X * p.Y
    return a / 2.0


def _gh_curve_plane(_w):
    """The plane a planar curve lies in (best guess for a straight one)."""
    try:
        return Plane(Face(_w)) if _w.is_closed else Plane(_w)
    except Exception:
        pass
    try:
        return Plane(_w.edges()[0].position_at(0), z_dir=(0, 0, 1))
    except Exception:
        return Plane.XY


_GH_CORNERS = {0: Kind.INTERSECTION, 1: Kind.INTERSECTION, 2: Kind.ARC,
               3: Kind.TANGENT, 4: Kind.INTERSECTION}


def _gh_offset_curve(_curve, _distance, _plane=None, _corners=1, _both=False, _cap=0):
    """Offset Curve. Rhino's sign: a positive distance offsets to the RIGHT of the
    curve's direction about the plane normal — outward on a counter-clockwise
    loop, inward on a clockwise one. OCCT's offset_2d offsets a CLOSED wire
    outward for a positive distance whatever its direction, so the sign is
    corrected by the loop's orientation; an OPEN wire goes LEFT/RIGHT by side.
    The curve is moved into the plane's local frame first so 'right' means the
    same thing as in Rhino.

    `both` is Pufferfish's "Both Sides": True offsets to -distance as well, a
    NUMBER is the distance of the second offset (barattolo wires -0.45 against
    0.45). `cap` (Pufferfish Cap Type: 0 none, 1 flat, 2 round) closes the two
    offsets of an OPEN curve into one loop — a slot round each ray."""
    w = _gh_curve(_curve)
    if w is None:
        return None
    d = _gh_num(_distance, 1.0)
    if isinstance(_both, bool) or _both is None:
        d2 = -d if _both else None
    else:
        d2 = _gh_num(_both, 0.0) or None
    if abs(d) < 1e-12 and d2 is None:
        return [w]
    pl = _gh_plane(_plane) if _plane is not None else _gh_curve_plane(w)
    kind = _GH_CORNERS.get(int(_gh_num(_corners, 1)), Kind.INTERSECTION)
    # a DEEP copy: to_local_coords only moves the Location and shares the
    # edges, and a Face built on the same curve upstream (Boundary Surfaces in
    # ikeaLack) has already hung pcurves on them — BRepOffset then reads those
    # and throws the offset out of the plane (bbox 88..351 on a curve lying in
    # x=220). Same family as _thicken's deepcopy: never offset shared topology.
    flat = pl.to_local_coords(_copy.deepcopy(w))

    def _one(dd):
        if w.is_closed:
            ccw = _gh_signed_area(w, pl) > 0
            return flat.offset_2d(dd if ccw else -dd, kind=kind)
        side = Side.RIGHT if dd > 0 else Side.LEFT
        return flat.offset_2d(abs(dd), kind=kind, side=side, closed=False)

    outs = [_one(d)] + ([_one(d2)] if d2 is not None else [])
    cap = int(_gh_num(_cap, 0))
    if cap and len(outs) == 1 and not w.is_closed:
        # Pufferfish caps a ONE-sided offset too: the loop runs out along the
        # source and back along its offset (Rhino: ikeaLack #80 is 916 long,
        # twice the profile plus the two caps)
        outs = [flat, outs[0]]
    if cap and len(outs) == 2 and not w.is_closed:
        # OCCT's one-sided offset hands its edges back in no particular order
        # (the LEFT offset of a line can start mid-way): chain them, and run
        # each the way the source curve runs, before closing the slot
        s0 = flat.position_at(0)

        def _orient(o):
            ch = Wire.combine(list(o.edges()), tol=1e-6)[0]
            es = _gh_chain(list(ch.edges()))
            if (es[0].position_at(0) - s0).length > (es[-1].position_at(1) - s0).length:
                es = [Edge(e.wrapped.Reversed()) for e in reversed(es)]
            return Wire(es)

        a, b = _orient(outs[0]), _orient(outs[1])
        a0, a1 = a.position_at(0), a.position_at(1)
        b0, b1 = b.position_at(0), b.position_at(1)
        if cap == 2:
            m1, m0 = flat.position_at(1), flat.position_at(0)
            t1, t0 = flat.tangent_at(1), flat.tangent_at(0)
            r1, r0 = (a1 - b1).length / 2, (a0 - b0).length / 2
            e1 = Edge.make_three_point_arc(a1, m1 + t1 * r1, b1)
            e0 = Edge.make_three_point_arc(b0, m0 - t0 * r0, a0)
        else:
            e1, e0 = Edge.make_line(a1, b1), Edge.make_line(b0, a0)
        # out along a, across the far cap, BACK along b, across the near cap
        back = [Edge(e.wrapped.Reversed()) for e in reversed(b.edges())]
        loop = Wire(list(a.edges()) + [e1] + back + [e0])
        return [_gh_from_local(pl, loop)]
    return [_gh_from_local(pl, o) for o in outs]     # a list output in GH

def _gh_join_curves(_curves, _preserve=False):
    """Join Curves: chain whatever touches end to end and hand back EVERY chain
    — curves that touch nothing come back on their own. (noodle's Join refuses
    pieces that do not touch; GH just returns several curves.)"""
    es = []
    for c in _gh_items(_curves):
        w = _gh_curve(c)
        if w is not None:
            es += list(w.edges())
    if not es:
        return None
    try:
        ws = list(Wire.combine(es, tol=1e-4))
    except Exception:
        ws = [Wire([e]) for e in es]
    return _gh_list(ws)

def _gh_chain(_edges):
    """Edges reordered (and flipped where needed) into one head-to-tail chain."""
    es = list(_edges)
    if len(es) < 2:
        return es
    # start from an edge whose start no other edge ends at
    def _key(p):
        return (round(p.X, 6), round(p.Y, 6), round(p.Z, 6))
    ends = {}
    for e in es:
        for p in (e.position_at(0), e.position_at(1)):
            ends[_key(p)] = ends.get(_key(p), 0) + 1
    first = next((e for e in es if ends[_key(e.position_at(0))] == 1),
                 next((e for e in es if ends[_key(e.position_at(1))] == 1), es[0]))
    if ends[_key(first.position_at(0))] != 1 and ends[_key(first.position_at(1))] == 1:
        first = Edge(first.wrapped.Reversed())
    out, rest = [first], [e for e in es if e is not first and not e.is_same(first)]
    while rest:
        tip = out[-1].position_at(1)
        nxt = None
        for e in rest:
            if (e.position_at(0) - tip).length < 1e-6:
                nxt = e
                break
            if (e.position_at(1) - tip).length < 1e-6:
                nxt = Edge(e.wrapped.Reversed())
                break
        if nxt is None:
            break
        rest = [e for e in rest if not e.is_same(nxt)]
        out.append(nxt)
    return out

def _gh_end_points(_curve):
    w = _gh_curve(_curve)
    if w is None:
        return {"start": None, "end": None}
    return {"start": w.position_at(0), "end": w.position_at(1)}


def _gh_curve_middle(_curve):
    w = _gh_curve(_curve)
    return None if w is None else w.position_at(0.5)


def _gh_control_points(_curve):
    """Control points as Rhino lists them. A polyline (degree 1) gives its
    vertices — closed ones END ON THE START AGAIN, as Rhino does (a rectangle
    has five). A single NURBS edge gives its poles, weights and knots."""
    w = _gh_curve(_curve)
    if w is None:
        return {"points": None, "weights": None, "knots": None}
    es = w.edges()
    if len(es) == 1 and es[0].geom_type not in (GeomType.LINE,):
        e = es[0]
        try:
            from OCP.GeomConvert import GeomConvert
            from OCP.BRep import BRep_Tool
            c = BRep_Tool.Curve_s(e.wrapped, 0, 1)
            bs = GeomConvert.CurveToBSplineCurve_s(c)
            pts = [Vector(bs.Pole(i).X(), bs.Pole(i).Y(), bs.Pole(i).Z())
                   for i in range(1, bs.NbPoles() + 1)]
            ws = [bs.Weight(i) for i in range(1, bs.NbPoles() + 1)]
            ks = []
            for i in range(1, bs.NbKnots() + 1):
                ks += [bs.Knot(i)] * bs.Multiplicity(i)
            return {"points": pts, "weights": ws, "knots": ks[1:-1]}
        except Exception:
            pass
    pts = [e.position_at(0) for e in es]
    pts.append(es[-1].position_at(1))
    n = len(pts)
    return {"points": pts, "weights": [1.0] * n,
            "knots": [float(i) for i in range(n)]}


def _gh_nurbs_curve(_vertices, _degree=3, _periodic=False):
    """NURBS curve FROM CONTROL POINTS (not through them): uniform knots, clamped
    when open, periodic when asked — Rhino's Curve.CreateControlPointCurve."""
    from OCP.Geom import Geom_BSplineCurve
    from OCP.TColgp import TColgp_Array1OfPnt
    from OCP.TColStd import TColStd_Array1OfReal, TColStd_Array1OfInteger
    from OCP.gp import gp_Pnt
    pts = [_gh_point(p) for p in _gh_items(_vertices)]
    pts = [p for p in pts if p is not None]
    per = bool(_periodic)
    if per and len(pts) > 2 and (pts[0] - pts[-1]).length < 1e-9:
        pts = pts[:-1]
    n = len(pts)
    if n < 2:
        raise ValueError("Nurbs Curve: needs at least 2 points")
    deg = max(1, min(int(_gh_num(_degree, 3)), n - 1 if not per else n - 1))
    P = TColgp_Array1OfPnt(1, n)
    for i, p in enumerate(pts):
        P.SetValue(i + 1, gp_Pnt(p.X, p.Y, p.Z))
    if per:
        K = TColStd_Array1OfReal(1, n + 1)
        M = TColStd_Array1OfInteger(1, n + 1)
        for i in range(n + 1):
            K.SetValue(i + 1, float(i))
            M.SetValue(i + 1, 1)
    else:
        nk = n - deg + 1
        K = TColStd_Array1OfReal(1, nk)
        M = TColStd_Array1OfInteger(1, nk)
        for i in range(nk):
            K.SetValue(i + 1, float(i))
            M.SetValue(i + 1, deg + 1 if i in (0, nk - 1) else 1)
    c = Geom_BSplineCurve(P, K, M, deg, per)
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    e = Edge(BRepBuilderAPI_MakeEdge(c).Edge())
    return {"curve": e, "length": e.length, "domain": [0.0, float(n if per else n - deg)]}


def _gh_discontinuity(_curve, _level=1):
    """Where a curve breaks its continuity: the joints between its edges whose
    tangents differ (level 1, G1) — for level 2 the joints whose curvature
    differs too, which on a polyline is the same set. A closed curve reports its
    seam when it is a corner; an open one never reports its ends."""
    w = _gh_curve(_curve)
    if w is None:
        return {"points": None, "parameters": None}
    es = w.edges()
    pts, ts = [], []
    total = w.length or 1.0
    acc = 0.0
    n = len(es)
    joints = range(n) if w.is_closed else range(1, n)
    lengths = [e.length for e in es]
    for j in joints:
        a, b = es[j - 1], es[j]
        ta, tb = a.tangent_at(1), b.tangent_at(0)
        if (ta - tb).length > 1e-6:
            pts.append(b.position_at(0))
            ts.append(sum(lengths[:j]) / total if j else 0.0)
    # a single smooth edge: look for kinks inside it (an imported polyline
    # B-spline of degree 1 is one edge with interior knots of full multiplicity)
    if n == 1 and not pts:
        try:
            from OCP.BRep import BRep_Tool
            from OCP.GeomConvert import GeomConvert
            c = GeomConvert.CurveToBSplineCurve_s(BRep_Tool.Curve_s(es[0].wrapped, 0, 1))
            d = c.Degree()
            lo, hi = c.FirstParameter(), c.LastParameter()
            for i in range(1, c.NbKnots() + 1):
                k, m = c.Knot(i), c.Multiplicity(i)
                inner = lo < k < hi
                if m >= d and (inner or (w.is_closed and i == 1)):
                    p = c.Value(k)
                    pts.append(Vector(p.X(), p.Y(), p.Z()))
                    ts.append((k - lo) / (hi - lo))
        except Exception:
            pass
    return {"points": pts, "parameters": ts}


def _gh_close_curve(_curve):
    w = _gh_curve(_curve)
    if w is None:
        return {"closed": None, "result_boolean": False}
    if w.is_closed:
        return {"closed": w, "result_boolean": True}
    a, b = w.position_at(1), w.position_at(0)
    try:
        c = Wire(list(w.edges()) + [Edge.make_line(a, b)])
        return {"closed": c, "result_boolean": True}
    except Exception:
        return {"closed": w, "result_boolean": False}


def _gh_extend_curve(_curve, _start=0.0, _end=0.0):
    """Extend Curve (line extension): a positive length grows the end along its
    tangent, a NEGATIVE one shortens it — GH accepts both, and ikeaLack uses
    -15 to pull a guide line in from both ends."""
    w = _gh_curve(_curve)
    if w is None:
        return None
    L = w.length
    s, e = _gh_num(_start, 0.0), _gh_num(_end, 0.0)
    es0 = w.edges()
    if len(es0) == 1 and es0[0].geom_type == GeomType.LINE:
        # a LINE extends as a line does in Rhino: plain arithmetic on the
        # infinite line, so a negative length may run past the other end
        # (barattolo pushes a unit Vector Star ray out to 2..41 that way)
        p0, p1 = w.position_at(0), w.position_at(1)
        d = (p1 - p0).normalized()
        a, b = p0 - d * s, p1 + d * e
        if (b - a).length < 1e-9 or (b - a).dot(d) <= 0:
            raise ValueError("Extend Curve: the line collapses to nothing")
        return Edge.make_line(a, b)
    t0 = (-s / L) if s < 0 else 0.0
    t1 = (1.0 + e / L) if e < 0 else 1.0
    if t1 - t0 <= 1e-9:
        raise ValueError("Extend Curve: the negative lengths eat the whole curve")
    if t0 > 0 or t1 < 1:
        if len(w.edges()) == 1:
            w = Wire([w.edges()[0].trim(t0, t1)]) if hasattr(Edge, "trim") else w
        else:
            pts = [w.position_at(t0 + (t1 - t0) * i / 64.0) for i in range(65)]
            w = Wire([Spline(*pts)])
    es = list(w.edges())
    if s > 0:
        p, t = w.position_at(0), w.tangent_at(0)
        es.insert(0, Edge.make_line(p - t * s, p))
    if e > 0:
        p, t = w.position_at(1), w.tangent_at(1)
        es.append(Edge.make_line(p, p + t * e))
    return Wire(es) if len(es) > 1 else es[0]


def _gh_divide_curve(_curve, _count=10, _kinks=False):
    """Divide Curve: count SEGMENTS of equal length -> count+1 points on an open
    curve, count on a closed one (the seam is not repeated), with the unit
    tangents and the normalised length parameters."""
    w = _gh_curve(_curve)
    if w is None:
        return {"points": None, "tangents": None, "parameters": None}
    n = max(1, int(_gh_num(_count, 10)))
    m = n if w.is_closed else n + 1
    ts = [i / n for i in range(m)]
    return {"points": [w.position_at(t) for t in ts],
            "tangents": [w.tangent_at(t) for t in ts],
            "parameters": ts}


def _gh_length(_curve):
    w = _gh_curve(_curve)
    return None if w is None else w.length


def _gh_bounding_box(_content, _plane=None):
    """Bounding Box aligned with a plane (world XY by default). GH's box may be
    FLAT — the box of a planar curve — and a flat box is not a solid: it comes
    back as the rectangle FACE, which is what Area/Voronoi 3D/Scale want of it."""
    pl = _gh_plane(_plane)
    s = _gh_shapes(_content)
    pts = [_gh_point(x) for x in _gh_items(_content) if isinstance(x, Vector)]
    if not s and not pts:
        return None
    lo, hi = [], []
    for x in s:
        bb = _gh_bbox(pl.to_local_coords(x))
        lo.append(bb.min)
        hi.append(bb.max)
    for q in pts:
        q = pl.to_local_coords(q)
        lo.append(q)
        hi.append(q)
    a = Vector(min(v.X for v in lo), min(v.Y for v in lo), min(v.Z for v in lo))
    b = Vector(max(v.X for v in hi), max(v.Y for v in hi), max(v.Z for v in hi))
    d = b - a
    dims = [d.X, d.Y, d.Z]
    # flat = thinner than 0.1% of the box: the exact bbox of a spline carries a
    # few hundredths of margin, and a 0.05 mm "box" round a planar curve is a
    # flat box in GH's eyes, not a solid
    eps = max(max(dims) * 1e-3, 1e-9)
    if min(dims) > eps:
        box = Box(d.X, d.Y, d.Z, align=(Align.MIN,) * 3).moved(Location(a.to_tuple()))
        return _gh_from_local(pl, box)
    flat = [i for i in range(3) if dims[i] <= eps]
    if len(flat) > 1:
        return _gh_from_local(pl, Edge.make_line(a, b)) if d.length > 1e-9 else None
    k = flat[0]
    i, j = [x for x in range(3) if x != k]
    mid = [(a.X + b.X) / 2, (a.Y + b.Y) / 2, (a.Z + b.Z) / 2]

    def P(ui, uj):
        c = list(mid)
        c[i] = (a.to_tuple()[i], b.to_tuple()[i])[ui]
        c[j] = (a.to_tuple()[j], b.to_tuple()[j])[uj]
        return tuple(c)
    face = Face(Polyline(P(0, 0), P(1, 0), P(1, 1), P(0, 1), close=True))
    return _gh_from_local(pl, face)


def _gh_length_of(_curve):
    """Length of a curve — and, as GH casts a one-face brep to its boundary, the
    perimeter of a surface."""
    w = _gh_curve(_curve)
    return None if w is None else w.length


# --- transforms --------------------------------------------------------------------

def _gh_move_to_point(_geom, _a, _b):
    a, b = _gh_point(_a), _gh_point(_b)
    if a is None:
        a = Vector(0, 0, 0)
    if b is None:
        b = Vector(0, 0, 0)
    v = b - a
    return {"moved": _move(_geom, v, 0, 0, 0), "vector": v}


def _gh_scale(_geom, _center, _factor):
    """Scale about a centre (world origin by default) — negative factors allowed,
    as in GH (a mirror through the centre)."""
    if _geom is None:
        return None
    c = _gh_point(_center) or Vector(0, 0, 0)
    f = _gh_num(_factor, 0.5)
    if isinstance(_geom, Vector):
        return c + (_geom - c) * f
    return _gh_scale_nu(_geom, Plane.XY.move(Location(c)), f, f, f)


def _gh_scale_nu(_geom, _plane, _x, _y, _z):
    """Non-uniform scale along a plane's axes. A gp_GTrsf: a B-Rep through
    BRepBuilderAPI_GTransform (it turns every surface into a B-spline, which is
    what scaling unequally really does to a circle), a point by matrix."""
    if _geom is None:
        return None
    pl = _gh_plane(_plane)
    sx, sy, sz = _gh_num(_x, 1), _gh_num(_y, 1), _gh_num(_z, 1)
    if isinstance(_geom, Vector):
        l = pl.to_local_coords(_geom)
        return _gh_from_local(pl, Vector(l.X * sx, l.Y * sy, l.Z * sz))
    if _is_mesh(_geom):
        raise ValueError("Scale NU: meshes are not supported yet")
    if abs(sx - sy) < 1e-12 and abs(sy - sz) < 1e-12:
        from OCP.gp import gp_Trsf, gp_Pnt
        t = gp_Trsf()
        t.SetScale(gp_Pnt(*pl.origin.to_tuple()), sx)
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        r = BRepBuilderAPI_Transform(_geom.wrapped, t, True).Shape()
        return _gh_wrap(r)
    from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ
    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    local = pl.to_local_coords(_geom)
    g = gp_GTrsf()
    g.SetVectorialPart(gp_Mat(sx, 0, 0, 0, sy, 0, 0, 0, sz))
    g.SetTranslationPart(gp_XYZ(0, 0, 0))
    r = BRepBuilderAPI_GTransform(local.wrapped, g, True).Shape()
    return _gh_from_local(pl, _gh_wrap(r))


# --- surfaces & breps -----------------------------------------------------------------

def _gh_boundary_surfaces(_edges):
    """Boundary Surfaces: planar closed curves -> trimmed planar faces. Curves
    nest even-odd on a shared plane, as GH nests them: a curve inside another is
    a HOLE in it, a curve inside that hole an island (a face again) — so a
    rectangle and its offset give a frame, and a logo of letters keeps the
    counters of its o's and a's.

    Cheap tests first (plane, bbox, then one point-in-face), because a lid logo
    can bring two hundred curves and a boolean per pair took minutes. An
    identical copy is NOT a hole (GH's closed polylines repeat their first
    point, so a list of corners can hold the same rectangle twice)."""
    ws = []
    for it in _gh_items(_edges):
        w = _gh_curve(it)
        if w is not None and w.is_closed:
            ws.append(w)
    if not ws:
        return []
    faces = []
    for w in ws:
        try:
            faces.append(Face(w))
        except Exception:
            pass
    faces.sort(key=lambda f: -f.area)
    info = []
    for f in faces:
        try:
            n = f.normal_at()
        except Exception:
            n = None
        bb = _gh_bbox(f)
        info.append((f, n, f.center(), bb))

    def _contains(a, b):
        fa, na, ca, ba = info[a]
        fb, nb, cb, bbb = info[b]
        if na is None or nb is None or abs(abs(na.dot(nb)) - 1) > 1e-6:
            return False
        if abs((cb - ca).dot(na)) > 1e-6:
            return False
        if fb.area >= fa.area * (1 - 1e-6):
            return False
        tol = 1e-6
        if (bbb.min.X < ba.min.X - tol or bbb.min.Y < ba.min.Y - tol or bbb.min.Z < ba.min.Z - tol
                or bbb.max.X > ba.max.X + tol or bbb.max.Y > ba.max.Y + tol
                or bbb.max.Z > ba.max.Z + tol):
            return False
        # a point of b's boundary, not its centre (the centre of a ring is
        # outside it)
        p = fb.outer_wire().position_at(0)
        try:
            return fa.is_inside(p)
        except Exception:
            return False

    parent = [None] * len(info)
    for b in range(len(info)):
        for a in range(b - 1, -1, -1):          # smallest container first
            if _contains(a, b):
                parent[b] = a
                break
    depth = [0] * len(info)
    for b in range(len(info)):
        depth[b] = 0 if parent[b] is None else depth[parent[b]] + 1
    out = []
    for a in range(len(info)):
        if depth[a] % 2:
            continue                             # a hole of its parent
        holes = [info[b][0].outer_wire() for b in range(len(info))
                 if parent[b] == a and depth[b] % 2]
        f = info[a][0]
        if holes:
            try:
                f = Face(f.outer_wire(), holes)
            except Exception:
                for h in holes:
                    f = f - Face(h)
        out.append(f)
    return _gh_list(out)

def _gh_to_surface(_v):
    """A GH Surface (or Brep) PARAMETER's cast: a closed planar curve becomes
    the planar surface it bounds, one per curve (no nesting — that is Boundary
    Surfaces); faces and breps pass as they are. barattolo feeds a curve into a
    Surface param and extrudes it: without the cast the extrusion is a wall,
    not a solid, and the Solid Union after it gets nothing."""
    out = []
    for x in _gh_items(_v):
        if isinstance(x, (Wire, Edge)):
            w = _gh_curve(x)
            if w is not None and w.is_closed:
                try:
                    out.append(Face(w))
                    continue
                except Exception:
                    pass
            raise ValueError("a Surface parameter can only take a CLOSED PLANAR curve")
        out.append(x)
    return _gh_list(out)

def _gh_extrude(_base, _direction):
    """Extrude along a VECTOR (its length is the distance): a curve becomes a
    surface, a planar face a closed solid — GH's Extrude, not noodle's."""
    if _base is None:
        return None
    v = _gh_vec(_direction)
    out = []
    for s in _gh_shapes(_base) or [_base]:
        if isinstance(s, (Face, Shell)) or (hasattr(s, "faces") and s.faces()
                                             and not isinstance(s, (Wire, Edge))):
            fs = s.faces() if not isinstance(s, Face) else [s]
            for f in fs:
                out.append(Solid.extrude(f, v))
        else:
            w = _gh_curve(s)
            if w is None:
                continue
            fs = [Face.extrude(e, v) for e in w.edges()]
            out.append(fs[0] if len(fs) == 1 else Shell(fs))
    return _gh_out(out)


def _gh_revolve_unsplit(_profile, _axis, _domain, _degrees=False):
    """Revolve a profile curve about an axis LINE over an angle domain [a, b]
    (radians, or degrees when the node says so) into one surface."""
    w = _gh_curve(_profile)
    ax = _gh_curve(_axis)
    if w is None or ax is None:
        return None
    p0, p1 = ax.position_at(0), ax.position_at(1)
    a0, a1 = _gh_domain(_domain, (0.0, 2 * math.pi))
    if not _degrees:
        a0, a1 = math.degrees(a0), math.degrees(a1)
    axis = Axis(p0, (p1 - p0).normalized())
    start = w.rotate(axis, a0) if abs(a0) > 1e-12 else w
    fs = [Face.revolve(e, a1 - a0, axis) for e in start.edges()]
    return fs[0] if len(fs) == 1 else Shell(fs)


def _gh_isotrim(_surface, _domain, _reparameterize=False):
    """Isotrim: the part of a surface inside a UV domain (absolute parameters of
    THIS face as OCCT parameterises it — see _gh_face_uv — or 0..1 of it when
    the surface input is reparameterized)."""
    f = _gh_face(_surface)
    if f is None or _domain is None:
        return None
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    srf = BRep_Tool.Surface_s(f.wrapped)
    u0, u1 = sorted(_domain["u"])
    v0, v1 = sorted(_domain["v"])
    if _reparameterize:
        uv = _gh_face_uv(f)
        su, sv = uv["u"][1] - uv["u"][0], uv["v"][1] - uv["v"][0]
        u0, u1 = uv["u"][0] + su * u0, uv["u"][0] + su * u1
        v0, v1 = uv["v"][0] + sv * v0, uv["v"][0] + sv * v1
    mk = BRepBuilderAPI_MakeFace(srf, u0, u1, v0, v1, 1e-6)
    if not mk.IsDone():
        raise ValueError("Isotrim: the domain is outside the surface")
    return Face(mk.Face())


def _gh_deconstruct_brep(_brep):
    s = _gh_shapes(_brep)
    if not s:
        return {"faces": None, "edges": None, "vertices": None}
    faces, edges, verts = [], [], []
    for x in s:
        faces += list(x.faces())
        edges += list(x.edges())
        verts += [Vector(v.X, v.Y, v.Z) for v in x.vertices()]
    return {"faces": faces, "edges": edges, "vertices": verts}


def _gh_area(_geom):
    """Area + area centroid. A closed planar curve counts as the region it
    bounds (GH does the same), a face/brep as its surface."""
    s = _gh_shapes(_geom)
    if not s:
        return {"area": None, "centroid": None}
    tot, acc = 0.0, Vector(0, 0, 0)
    for x in s:
        if isinstance(x, (Wire, Edge)):
            w = _gh_curve(x)
            if not w.is_closed:          # GH: an open curve has no area
                continue
            x = Face(w)
        a = x.area
        c = x.center(CenterOf.MASS) if a > 0 else x.center()
        tot += a
        acc = acc + c * a
    if not tot:
        return {"area": None, "centroid": None}
    return {"area": tot, "centroid": acc / tot}


def _gh_brep_join(_breps):
    """Brep Join: sew faces/shells that share edges; a closed result is a solid."""
    s = _gh_shapes(_breps)
    if not s:
        return {"breps": None, "closed": None}
    try:
        r = _join(s, 1e-3, True)
    except Exception:
        r = Compound(s)
    rs = r if _is_seq(r) else [r]
    return {"breps": _gh_list(rs),
            "closed": [isinstance(x, Solid) for x in rs]}


def _gh_offset_surface(_surface, _distance, _both=False, _solid=False):
    f = _gh_face(_surface) if not isinstance(_surface, (Shell, Solid)) else None
    d = _gh_num(_distance, 1.0)
    src = f if f is not None else _surface
    if _solid:
        return Solid.thicken(src, d)
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffsetShape
    mk = BRepOffsetAPI_MakeOffsetShape()
    mk.PerformBySimple(src.wrapped, d)
    return _gh_wrap(mk.Shape())


# --- intersections & splits -------------------------------------------------------------

def _gh_brep_brep(_a, _b):
    """Brep | Brep: the intersection curves of two breps, joined into loops."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    A, B = _gh_shapes(_a), _gh_shapes(_b)
    if not A or not B:
        return {"curves": None, "points": None}
    a = A[0] if len(A) == 1 else Compound(A)
    b = B[0] if len(B) == 1 else Compound(B)
    sec = BRepAlgoAPI_Section(a.wrapped, b.wrapped)
    sec.Build()
    edges = list(_gh_wrap(sec.Shape()).edges())
    if not edges:
        return {"curves": None, "points": None}
    try:
        ws = list(Wire.combine(edges, tol=1e-4))
    except Exception:
        ws = edges
    return {"curves": _gh_list(ws), "points": None}


def _gh_split_by(_shape, _tools):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
    from OCP.TopTools import TopTools_ListOfShape
    args, tl = TopTools_ListOfShape(), TopTools_ListOfShape()
    args.Append(_shape.wrapped)
    for t in _tools:
        tl.Append(t.wrapped)
    sp = BRepAlgoAPI_Splitter()
    sp.SetArguments(args)
    sp.SetTools(tl)
    sp.Build()
    if not sp.IsDone():
        raise ValueError("split failed")
    return _gh_wrap(sp.Shape())


def _gh_surface_split(_surface, _curves):
    """Surface Split: cut a surface into fragments along curves lying on it (Pull
    Curve first if they only lie near it). Fragment ORDER is OCCT's, not
    Rhino's — pick fragments by a measure (Sort List on area/length), never by a
    fixed index copied from the .gh."""
    f = _gh_face(_surface)
    cs = _gh_shapes(_curves)
    if f is None:
        return None
    if not cs:
        return f
    r = _gh_split_by(f, cs)
    frags = list(r.faces())
    # Rhino's order (measured, ikeaLack #134/#218/#237/#241): fragment 0 is the
    # RESIDUE — the piece still bounded by the surface's own outer edge — then
    # the pieces in the order of the cutting curves that bound them. Not by
    # area: #218's 47 cells come in curve order, not sorted.
    outer = f.outer_wire()
    probes = [outer.position_at(i / 16.0) for i in range(16)]

    def _touches_outer(g):
        return sum(1 for p in probes if g.distance_to(p) < 1e-5)

    def _first_curve(g):
        for k, c in enumerate(cs):
            m = c.position_at(0.5) if hasattr(c, "position_at") else c.center()
            if g.distance_to(m) < 1e-5:
                return k
        return len(cs)

    if len(frags) > 1:
        res = max(range(len(frags)), key=lambda i: _touches_outer(frags[i]))
        rest = [g for i, g in enumerate(frags) if i != res]
        rest.sort(key=_first_curve)
        frags = [frags[res]] + rest
    return _gh_list(frags)


def _gh_trim_with_brep(_curve, _brep):
    """Trim with Brep: split a curve by a brep's skin and sort the pieces into
    inside / outside by their midpoints."""
    w = _gh_curve(_curve)
    B = _gh_shapes(_brep)
    if w is None or not B:
        return {"inside": None, "outside": None}
    b = B[0]
    r = _gh_split_by(w, [b])
    ins, outs = [], []
    for e in r.edges():
        m = e.position_at(0.5)
        try:
            inside = b.is_inside(m)
        except Exception:
            inside = False
        (ins if inside else outs).append(e)
    def _j(es):
        if not es:
            return None
        try:
            return _gh_list(list(Wire.combine(es, tol=1e-4)))
        except Exception:
            return _gh_list(es)
    return {"inside": _j(ins), "outside": _j(outs)}


def _gh_pull_curve(_curve, _surface):
    """Pull Curve: the curve projected onto a surface along the surface normals
    (closest-point projection), as Rhino's PullToBrepFace."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_NormalProjection
    w = _gh_curve(_curve)
    f = _gh_face(_surface) if not isinstance(_surface, (Shell, Solid)) else _surface
    if w is None or f is None:
        return None
    np_ = BRepOffsetAPI_NormalProjection(f.wrapped)
    np_.Add(w.wrapped)
    from OCP.GeomAbs import GeomAbs_Shape
    np_.SetParams(1e-5, 1e-4, GeomAbs_Shape.GeomAbs_C1, 14, 16)
    np_.Build()
    if not np_.IsDone():
        raise ValueError("Pull Curve: projection failed")
    es = list(_gh_wrap(np_.Shape()).edges())
    if not es:
        return None
    try:
        return _gh_list(list(Wire.combine(es, tol=1e-4)))
    except Exception:
        return _gh_list(es)


def _gh_pull_point(_point, _geom):
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.gp import gp_Pnt
    p = _gh_point(_point)
    G = _gh_shapes(_geom)
    if p is None or not G:
        return {"closest_point": None, "distance": None}
    g = G[0] if len(G) == 1 else Compound(G)
    v = BRepBuilderAPI_MakeVertex(gp_Pnt(p.X, p.Y, p.Z)).Vertex()
    ext = BRepExtrema_DistShapeShape(v, g.wrapped)
    if not ext.IsDone() or ext.NbSolution() == 0:
        return {"closest_point": None, "distance": None}
    q = ext.PointOnShape2(1)
    return {"closest_point": Vector(q.X(), q.Y(), q.Z()), "distance": ext.Value()}


def _gh_voronoi3d(_points, _box):
    """Voronoi 3D as GH builds it: one convex cell per point, clipped to a BOX,
    as B-Rep SOLIDS (so Brep | Brep, Solid Difference… work on them exactly).
    scipy's Voronoi with every site mirrored across the six box faces closes
    every cell; each cell is the convex hull of its vertices, built face by face
    from the hull's coplanar facets."""
    import numpy as _np
    from scipy.spatial import Voronoi as _Vor, ConvexHull as _Hull
    pts = [_gh_point(p) for p in _gh_items(_points)]
    P = _np.array([[p.X, p.Y, p.Z] for p in pts if p is not None], float)
    if len(P) < 1:
        return {"cells": None, "boundary": None}
    if _box is not None:
        bb = _gh_bbox(_gh_shapes(_box)[0])
        lo = _np.array([bb.min.X, bb.min.Y, bb.min.Z])
        hi = _np.array([bb.max.X, bb.max.Y, bb.max.Z])
        # a FLAT box (Bounding Box of a planar curve, as ikeaLack does): give it
        # a little depth so the cells close; they are meant to be cut by the
        # surface lying in it (Brep | Brep), which then sees the 2D pattern
        thin = (hi - lo) < 1e-6
        if thin.any():
            pad = max((hi - lo).max() * 0.01, 1e-3)
            lo = _np.where(thin, lo - pad, lo)
            hi = _np.where(thin, hi + pad, hi)
    else:
        lo, hi = P.min(0), P.max(0)
        pad = (hi - lo).max() * 0.1 + 1e-6
        lo, hi = lo - pad, hi + pad
    sites = [P]
    for ax in range(3):
        for b in (lo[ax], hi[ax]):
            M = P.copy()
            M[:, ax] = 2 * b - M[:, ax]
            sites.append(M)
    vor = _Vor(_np.vstack(sites))
    cells = []
    for i in range(len(P)):
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        V = _np.clip(vor.vertices[reg], lo, hi)
        try:
            h = _Hull(V)
        except Exception:
            continue
        cells.append(_gh_hull_solid(V, h))
    bx = Box(*(hi - lo), align=(Align.MIN,) * 3).moved(Location(tuple(lo)))
    return {"cells": cells, "boundary": bx}


def _gh_hull_solid(_V, _h):
    """A convex hull (scipy) as a B-Rep solid: merge the triangles of each plane
    into one polygon face, then sew."""
    import numpy as _np
    groups = {}
    for k, eq in enumerate(_h.equations):
        key = None
        for g in groups:
            if _np.allclose(eq, _h.equations[g], atol=1e-7):
                key = g
                break
        groups.setdefault(key if key is not None else k, []).append(k)
    faces = []
    for g, simplices in groups.items():
        idx = sorted({int(i) for s in simplices for i in _h.simplices[s]})
        pts = _V[idx]
        n = _h.equations[g][:3]
        c = pts.mean(0)
        u = pts[0] - c
        if _np.linalg.norm(u) < 1e-12:
            u = pts[1] - c
        u = u / _np.linalg.norm(u)
        v = _np.cross(n, u)
        ang = _np.arctan2((pts - c) @ v, (pts - c) @ u)
        poly = [tuple(map(float, p)) for p in pts[_np.argsort(ang)]]
        dedup = [poly[0]]
        for p in poly[1:]:
            if _np.linalg.norm(_np.array(p) - _np.array(dedup[-1])) > 1e-9:
                dedup.append(p)
        if len(dedup) >= 3:
            faces.append(Face(Polyline(*dedup, close=True)))
    return Solid(Shell(faces))


# --- lists ----------------------------------------------------------------------------------

def _gh_list_item(_list, _index=0, _wrap=True):
    it = list(_list) if _is_seq(_list) else ([] if _list is None else [_list])
    if not it:
        return None
    i = int(_gh_num(_index, 0))
    n = len(it)
    if _wrap:
        return it[i % n]
    if -n <= i < n:
        return it[i]
    return None


def _gh_dispatch(_list, _pattern):
    it = list(_list) if _is_seq(_list) else ([] if _list is None else [_list])
    pat = _gh_items(_pattern) or [True, False]
    a, b = [], []
    for k, x in enumerate(it):
        (a if bool(pat[k % len(pat)]) else b).append(x)
    return {"list_a": a, "list_b": b}


def _gh_sort_list(_keys, _values=None):
    """Sort List: sort the keys and reorder the values the same way."""
    ks = list(_keys) if _is_seq(_keys) else ([] if _keys is None else [_keys])
    order = sorted(range(len(ks)), key=lambda i: ks[i])
    vs = list(_values) if _is_seq(_values) else ([] if _values is None else [_values])
    return {"keys": [ks[i] for i in order],
            "values_a": [vs[i] for i in order if i < len(vs)] if vs else None}


# ===========================================================================
# barattolo.gh: math, logic, lists, trees
# ===========================================================================

def _gh_negative(_v):
    """-x — of a vector too, as GH's Negative does."""
    if _v is None:
        return None
    if isinstance(_v, Vector):
        return -_v
    return -float(_v)


def _gh_list_length(_list):
    """List Length as GH counts: a single item is a list of one, nothing is 0."""
    if _list is None:
        return 0
    return len(_list) if _is_seq(_list) else 1


def _gh_radians(_d):
    return None if _d is None else math.radians(float(_d))


def _gh_concatenate(_a, _b):
    return ("" if _a is None else str(_a)) + ("" if _b is None else str(_b))


def _gh_repeat_data(_data, _length):
    """Repeat Data: the list repeated CYCLICALLY until it is `length` long
    (List Repeat repeats a whole list `count` times — not the same thing)."""
    it = _gh_items(_data)
    n = int(_gh_num(_length, len(it)))
    if not it or n <= 0:
        return []
    return [it[i % len(it)] for i in range(n)]


def _gh_list_indices(_list):
    it = list(_list) if _is_seq(_list) else ([] if _list is None else [_list])
    return list(range(len(it)))


def _gh_merge(*_streams):
    out = []
    for s in _streams:
        if s is None:
            continue
        if isinstance(s, _GhTree):
            out.append(s)
        elif _is_seq(s):
            out.extend(list(s))
        else:
            out.append(s)
    return out


def _gh_range(_domain, _steps=10):
    a, b = _gh_domain(_domain)
    n = max(1, int(_gh_num(_steps, 10)))
    return [a + (b - a) * i / n for i in range(n + 1)]


def _gh_larger(_a, _b):
    a, b = _gh_num(_a), _gh_num(_b)
    return {"larger_than": a > b, "or_equal_to": a >= b}


def _gh_smaller(_a, _b):
    a, b = _gh_num(_a), _gh_num(_b)
    return {"smaller_than": a < b, "or_equal_to": a <= b}


def _gh_bool(_v):
    if isinstance(_v, str):
        return _v.strip().lower() in ("true", "1", "yes")
    return bool(_v)


def _gh_gate_or(_a, _b):
    return _gh_bool(_a) or _gh_bool(_b)


def _gh_invert_boolean(_b):
    v = not _gh_bool(_b)
    return {"inverted": v, "number": 1 if v else 0}


def _gh_weave(_pattern, *_streams):
    """Weave: interleave the streams following an index pattern (default 0,1,…)
    until one stream runs out."""
    st = [list(_gh_items(s)) for s in _streams if s is not None]
    if not st:
        return []
    pat = [int(_gh_num(x)) for x in _gh_items(_pattern)] or list(range(len(st)))
    pos = [0] * len(st)
    out = []
    while True:
        moved = False
        for k in pat:
            if 0 <= k < len(st) and pos[k] < len(st[k]):
                out.append(st[k][pos[k]])
                pos[k] += 1
                moved = True
            elif 0 <= k < len(st):
                return out
        if not moved:
            return out


def _gh_split_list(_list, _index=0):
    it = list(_list) if _is_seq(_list) else ([] if _list is None else [_list])
    i = int(_gh_num(_index, 0))
    return {"list_a": it[:i], "list_b": it[i:]}


def _gh_replace_nulls(_items, _repl):
    it = list(_items) if _is_seq(_items) else ([] if _items is None else [_items])
    rs = _gh_items(_repl)
    out, n = [], 0
    for k, x in enumerate(it):
        if x is None and rs:
            out.append(rs[min(n, len(rs) - 1)])
            n += 1
        else:
            out.append(x)
    return {"items": out, "count": n}


def _gh_graft(_v):
    """Graft Tree: every item into a branch of its own — a list becomes a list
    of one-item lists, and a tree is grafted inside each branch. It is what a
    GH input's [graft] flag does, and it is NOT a no-op: Move To Plane with two
    curves grafted and eleven planes makes eleven copies of EACH curve
    (barattolo's nozzle and its bore), ungrafted it pairs them item by item."""
    if _v is None:
        return []
    if not _is_seq(_v):
        return [[_v]]
    return [_gh_graft(x) if _is_seq(x) else [x] for x in _v]

class _GhTree(dict):
    """A Grasshopper data tree: {path tuple: [items]}. Deliberately a dict, not a
    list: noodle fans out over lists, and a tree must travel as ONE value until
    Tree Branch takes a branch out of it."""
    _noodle_tree = True


def _gh_path(_p):
    if isinstance(_p, tuple):
        return _p
    if isinstance(_p, (int, float)):
        return (int(_p),)
    s = str(_p).strip().strip("{}")
    return tuple(int(x) for x in s.replace(",", ";").split(";") if x.strip() != "")


def _gh_create_tree(_data, _paths):
    """Create Tree: each item goes to the branch its path names (a list of paths
    as long as the data, as GH wants it)."""
    d = list(_data) if _is_seq(_data) else ([] if _data is None else [_data])
    ps = list(_paths) if _is_seq(_paths) else ([] if _paths is None else [_paths])
    t = _GhTree()
    for k, x in enumerate(d):
        p = _gh_path(ps[min(k, len(ps) - 1)]) if ps else (0,)
        t.setdefault(p, []).append(x)
    return t


def _gh_tree_branch(_tree, _path):
    """Tree Branch: the items of one branch. Several trees wired in (a Data
    param merging them, as barattolo does) are merged first."""
    trees = [x for x in (_tree if _is_seq(_tree) else [_tree]) if isinstance(x, dict)]
    t = _GhTree()
    for tr in trees:
        for p, v in tr.items():
            t.setdefault(p, []).extend(v)
    return t.get(_gh_path(_path), [])


# ===========================================================================
# barattolo.gh: curves
# ===========================================================================

def _gh_bounding_rectangle(_geometry, _plane=None, _union=False, _slide=0.0):
    """Bounding Rectangle: the smallest rectangle aligned with a plane that holds
    the geometry seen from the plane's normal. `union` -> one rectangle for all
    of it, else one per item. (The Axis/Slide alignment inputs are not built:
    the rectangle is aligned with the plane's own x axis.)"""
    pl = _gh_plane(_plane)
    items = _gh_items(_geometry)

    def _one(group):
        pts = []
        for g in group:
            if isinstance(g, Vector):
                pts.append(pl.to_local_coords(g))
                continue
            try:
                bb = _gh_bbox(pl.to_local_coords(g))
                pts += [bb.min, bb.max]
            except Exception:
                pass
        if not pts:
            return None
        xa, xb = min(p.X for p in pts), max(p.X for p in pts)
        ya, yb = min(p.Y for p in pts), max(p.Y for p in pts)
        za, zb = min(p.Z for p in pts), max(p.Z for p in pts)
        # Slide: WHERE along the plane normal the rectangle sits, 0 at the
        # bottom of the geometry and 1 at its top (barattolo slides one to the
        # top of the jar and draws its axis from the base circle up to it)
        z = za + (zb - za) * _gh_num(_slide, 0.0)
        w = Polyline((xa, ya, z), (xb, ya, z), (xb, yb, z), (xa, yb, z), close=True)
        return _gh_from_local(pl, w), xb - xa, yb - ya

    groups = [items] if (_union or len(items) <= 1) else [[x] for x in items]
    rs = [r for r in (_one(g) for g in groups) if r is not None]
    if not rs:
        return {"bounding": None, "dimension_x": None, "dimension_y": None}
    return {"bounding": _gh_out([r[0] for r in rs]),
            "dimension_x": _gh_out([r[1] for r in rs]),
            "dimension_y": _gh_out([r[2] for r in rs])}


def _gh_polygon(_plane, _radius, _segments=6, _fillet=0.0):
    """Polygon: a regular polygon INSCRIBED in a circle of `radius` (its corners
    on the circle), first corner on the plane's x axis."""
    pl = _gh_plane(_plane)
    r = _gh_num(_radius, 1.0)
    n = max(3, int(_gh_num(_segments, 6)))
    pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
           for i in range(n)]
    w = Polyline(*pts, close=True)
    f = _gh_num(_fillet, 0.0)
    if f > 1e-9:
        w = w.fillet_2d(f, w.vertices())
    w = _gh_from_local(pl, w)
    return {"polygon": w, "length": w.length}


def _gh_interpolate(_vertices, _degree=3, _periodic=False):
    pts = [_gh_point(p) for p in _gh_items(_vertices)]
    pts = [p for p in pts if p is not None]
    if len(pts) < 2:
        raise ValueError("Interpolate: needs at least 2 points")
    per = bool(_periodic)
    if per and (pts[0] - pts[-1]).length < 1e-9:
        pts = pts[:-1]
    e = Spline(*pts, periodic=per) if len(pts) > 2 else Edge.make_line(pts[0], pts[1])
    return {"curve": e, "length": e.length, "domain": [0.0, 1.0]}


def _gh_rebuild_curve(_curve, _degree=3, _count=10):
    """Rebuild Curve: a smooth B-spline through `count` points spread evenly
    along the curve (periodic when it is closed). OCCT's least-squares fit
    (GeomAPI_PointsToBSpline) was tried first and BLEW UP on closed glyph
    outlines with sharp corners — barattolo's text came back 4 m wide — while
    an interpolation through evenly spread samples stays on the curve."""
    w = _gh_curve(_curve)
    if w is None:
        return None
    n = max(int(_gh_num(_degree, 3)) + 1, int(_gh_num(_count, 10)))
    closed = w.is_closed
    m = n if closed else n
    ts = [i / (m if closed else m - 1) for i in range(m)]
    pts = [w.position_at(t) for t in ts]
    if closed:
        return Spline(*pts, periodic=True)
    return Spline(*pts)

def _gh_simplify_curve(_curve, _tol=None, _angle=None):
    """Simplify Curve: collinear line segments merged, B-splines that are really
    lines or arcs replaced by them. `simplified` says whether anything changed."""
    w = _gh_curve(_curve)
    if w is None:
        return {"curve": None, "simplified": False}
    es = list(w.edges())
    out, changed = [], False
    for e in es:
        if out and e.geom_type == GeomType.LINE and out[-1].geom_type == GeomType.LINE:
            a = out[-1]
            if (a.tangent_at(0) - e.tangent_at(0)).length < 1e-6:
                out[-1] = Edge.make_line(a.position_at(0), e.position_at(1))
                changed = True
                continue
        out.append(e)
    if not changed:
        return {"curve": w, "simplified": False}
    return {"curve": Wire(out), "simplified": True}


def _gh_kind_of(_curve, _tol=1e-6):
    """'arc' | 'circle' | 'ellipse' | None for one curve (a single edge)."""
    w = _gh_curve(_curve)
    if w is None or len(w.edges()) != 1:
        return None
    e = w.edges()[0]
    if e.geom_type == GeomType.CIRCLE:
        return "circle" if e.is_closed else "arc"
    if e.geom_type == GeomType.ELLIPSE:
        return "ellipse"
    return None


def _gh_is_arc_circle_ellipse(_curve, _tol=1e-12):
    k = _gh_kind_of(_curve)
    return {"arc": k == "arc", "circle": k == "circle", "ellipse": k == "ellipse"}


def _gh_arc_circle_ellipse(_curve, _type=0, _tol=1e-12):
    return _gh_curve(_curve) if _gh_kind_of(_curve) else None


def _gh_fillet_curve(_curve, _parameter, _radius):
    """Fillet (curve): round the corner nearest the point at normalised
    parameter t."""
    w = _gh_curve(_curve)
    if w is None:
        return {"curve": None, "parameter": None}
    t = _gh_num(_parameter, 0.0)
    es = w.edges()
    if all(e.geom_type == GeomType.LINE for e in es):
        t = min(max(t / len(es), 0.0), 1.0)   # a polyline's parameters are vertex indices
    p = w.position_at(t)
    vs = sorted(w.vertices(), key=lambda v: (Vector(v.X, v.Y, v.Z) - p).length)
    r = _gh_num(_radius, 1.0)
    try:
        f = w.fillet_2d(r, [vs[0]])
    except Exception:
        f = w
    return {"curve": f, "parameter": t}


def _gh_project(_geometry, _plane=None):
    """Project: geometry flattened onto a plane along its normal."""
    from OCP.GeomProjLib import GeomProjLib
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    pl = _gh_plane(_plane)
    if _geometry is None:
        return None
    if isinstance(_geometry, Vector):
        l = pl.to_local_coords(_geometry)
        return _gh_from_local(pl, Vector(l.X, l.Y, 0))
    o, z = pl.origin, pl.z_dir
    from OCP.Geom import Geom_Plane
    gpl = Geom_Plane(gp_Pln(gp_Pnt(o.X, o.Y, o.Z), gp_Dir(z.X, z.Y, z.Z)))
    es = []
    for e in _geometry.edges():
        c = BRep_Tool.Curve_s(e.wrapped, 0, 1)
        if c is None:
            continue
        a, b = BRep_Tool.Range_s(e.wrapped)
        from OCP.Geom import Geom_TrimmedCurve
        pc = GeomProjLib.ProjectOnPlane_s(Geom_TrimmedCurve(c, a, b), gpl,
                                          gp_Dir(z.X, z.Y, z.Z), True)
        es.append(Edge(BRepBuilderAPI_MakeEdge(pc).Edge()))
    if not es:
        return None
    try:
        ws = list(Wire.combine(es, tol=1e-6))
        return _gh_out(ws)
    except Exception:
        return _gh_out(es)


def _gh_region_union(_curves, _plane=None):
    """Region Union: the outline of the union of closed planar regions."""
    fs = []
    for c in _gh_items(_curves):
        w = _gh_curve(c)
        if w is not None and w.is_closed:
            fs.append(Face(w))
    if not fs:
        return None
    u = fs[0]
    for f in fs[1:]:
        u = u + f
    try:
        u = u.clean()        # merge the coplanar pieces the fuse leaves split
    except Exception:
        pass
    ws = []
    for f in u.faces():
        ws.append(f.outer_wire())
        ws += list(f.inner_wires())
    return _gh_list(ws)


def _gh_vector_star(_plane=None, _steps=4, _type=1, _dims=False):
    """Vector Star: unit vectors fanned evenly round the plane's z axis
    (`steps` of them), with the lines that draw them."""
    pl = _gh_plane(_plane)
    n = max(1, int(_gh_num(_steps, 4)))
    vs = [pl.x_dir * math.cos(2 * math.pi * i / n) + pl.y_dir * math.sin(2 * math.pi * i / n)
          for i in range(n)]
    o = pl.origin
    return {"vectors": vs, "lines": [Edge.make_line(o, o + v) for v in vs]}


# ===========================================================================
# barattolo.gh: surfaces & breps
# ===========================================================================

def _gh_rotate(_geometry, _angle, _plane=None, _degrees=False):
    """Rotate about the plane's normal through its origin (a point gives the XY
    plane through it). Radians, as GH, unless `degrees`."""
    if _geometry is None:
        return None
    pl = _gh_plane(_plane)
    a = _gh_num(_angle, 0.0)
    if not _degrees:
        a = math.degrees(a)
    ax = Axis(pl.origin, pl.z_dir)
    if isinstance(_geometry, Vector):
        return Vector(Vertex(_geometry.X, _geometry.Y, _geometry.Z).rotate(ax, a).center())
    if _is_mesh(_geometry):
        raise ValueError("Rotate (GH): meshes not supported yet — use noodle's Rotate")
    return _geometry.rotate(ax, a)


def _gh_move_to_plane(_geometry, _plane=None, _above=True, _below=True):
    """Move To Plane: slide geometry along the plane normal until it TOUCHES the
    plane — from above (its lowest point lands on it) if `above`, from below
    (its highest point) if `below`. Geometry crossing the plane stays put."""
    if _geometry is None:
        return None
    pl = _gh_plane(_plane)
    loc = pl.to_local_coords(_geometry)
    if isinstance(loc, Vector):
        lo = hi = loc.Z
    else:
        bb = _gh_bbox(loc)
        lo, hi = bb.min.Z, bb.max.Z
    dz = 0.0
    if lo > 0 and _above:
        dz = -lo
    elif hi < 0 and _below:
        dz = -hi
    v = pl.z_dir * dz
    return _move(_geometry, v, 0, 0, 0) if not isinstance(_geometry, Vector) else _geometry + v


def _gh_plane_surface(_plane, _x, _y):
    pl = _gh_plane(_plane)
    x0, x1 = _gh_domain(_x)
    y0, y1 = _gh_domain(_y)
    w = Polyline((x0, y0), (x1, y0), (x1, y1), (x0, y1), close=True)
    return _gh_from_local(pl, Face(w))


def _gh_brep_edges(_brep):
    """Brep Edges: naked (used by one face), interior (two), non-manifold (more)."""
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    s = _gh_shapes(_brep)
    naked, inner, nonman = [], [], []
    for x in s:
        m = TopTools_IndexedDataMapOfShapeListOfShape()
        TopExp.MapShapesAndAncestors_s(x.wrapped, TopAbs_EDGE, TopAbs_FACE, m)
        for i in range(1, m.Extent() + 1):
            e = Edge(m.FindKey(i))
            n = m.FindFromIndex(i).Size()
            if e.length < 1e-9:
                continue
            (naked if n == 1 else inner if n == 2 else nonman).append(e)
    return {"naked": naked, "interior": inner, "non_manifold": nonman}


def _gh_fillet_edge(_shape, _edges, _radii, _blend=0, _metric=0):
    """Fillet Edge: round the edges listed BY INDEX, each with its own radius
    (the radii list pairs with the edges, the last one repeating). The index is
    into OCCT's edge order, which is not always Rhino's — check the result.
    A radius equal to the face it runs over (r 2.2 on a disc 2.2 thick, in
    barattolo) is legal in Rhino and refused by OCCT at exact equality: it is
    retried a hair smaller (0.1%) rather than failing."""
    s = _gh_shapes(_shape)
    if not s:
        return None
    part = s[0]
    allE = list(part.edges())
    idx = [int(_gh_num(i)) for i in _gh_items(_edges)]
    rs = [_gh_num(r) for r in _gh_items(_radii)] or [1.0]
    by_r = {}
    for k, i in enumerate(idx):
        if 0 <= i < len(allE):
            by_r.setdefault(rs[min(k, len(rs) - 1)], []).append(allE[i])
    if not by_r:
        return part
    out = part
    for rad, es in by_r.items():
        # the picks must be edges of the CURRENT part: after a first fillet the
        # old edges are gone, so re-find them by position
        cur = list(out.edges())
        picks = []
        for e in es:
            m = e.center()
            best = min(cur, key=lambda c: (c.center() - m).length)
            picks.append(best)
        try:
            out = out.fillet(rad, picks)
        except Exception:
            try:
                out = out.fillet(rad * 0.999, picks)
            except Exception as e:
                smooth = [allE.index(es[k]) if es[k] in allE else k
                          for k, p in enumerate(picks) if _gh_is_smooth_edge(out, p)]
                if smooth:
                    raise ValueError(
                        f"Fillet Edge: edge index {smooth} is a SMOOTH seam (two faces "
                        "meeting tangent, e.g. where an earlier fillet ends) in OCCT's "
                        "edge order — nothing to round there. The .gh index was counted "
                        "in Rhino's order and means another edge: pick it by geometry "
                        "(Brep Edges -> a test -> Dispatch) or fix the index.") from e
                raise
    return out


def _gh_is_smooth_edge(_shape, _edge, _tol_deg=1.0):
    """True when the two faces on an edge meet tangent (their normals agree at
    its midpoint) — a seam a fillet cannot round."""
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
    m = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(_shape.wrapped, TopAbs_EDGE, TopAbs_FACE, m)
    i = m.FindIndex(_edge.wrapped)
    if i == 0:
        return False
    fs = [Face(f) for f in m.FindFromIndex(i)]
    if len(fs) != 2:
        return False
    p = _edge.position_at(0.5)
    n0, n1 = fs[0].normal_at(p), fs[1].normal_at(p)
    return abs(n0.dot(n1)) > math.cos(math.radians(_tol_deg))

def _gh_cap_holes(_brep):
    """Cap Holes: close the planar openings of an open brep with flat faces."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Sewing
    s = _gh_shapes(_brep)
    if not s:
        return None
    x = s[0]
    if isinstance(x, Solid):
        return x
    naked = _gh_brep_edges(x)["naked"]
    caps = []
    if naked:
        for w in Wire.combine(naked, tol=1e-4):
            try:
                caps.append(Face(w))
            except Exception:
                pass
    sew = BRepBuilderAPI_Sewing(1e-4)
    for f in list(x.faces()) + caps:
        sew.Add(f.wrapped)
    sew.Perform()
    sh = _gh_wrap(sew.SewedShape())
    try:
        shells = sh.shells() if not isinstance(sh, Shell) else [sh]
        sol = Solid(shells[0])
        if sol.volume < 0:
            from OCP.BRepLib import BRepLib
            BRepLib.OrientClosedSolid_s(sol.wrapped)
        return sol if sol.is_valid else sh
    except Exception:
        return sh


def _gh_is_planar(_surface, _interior=False):
    f = _gh_face(_surface)
    if f is None:
        return {"planar": None, "plane": None}
    ok = f.geom_type == GeomType.PLANE
    return {"planar": ok, "plane": Plane(f) if ok else None}


def _gh_is_surface_trimmed(_surface):
    """Trimmed = the face does not cover its whole underlying surface."""
    f = _gh_face(_surface)
    if f is None:
        return None
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    u0, u1, v0, v1 = _gh_face_uv(f)["u"] + _gh_face_uv(f)["v"]
    full = Face(BRepBuilderAPI_MakeFace(BRep_Tool.Surface_s(f.wrapped), u0, u1, v0, v1, 1e-6).Face())
    return abs(full.area - f.area) > 1e-6 * max(full.area, 1e-9)


def _gh_trim_with_plane(_brep, _plane, _direction=True, _cap=True):
    """Trim With Plane: cut a brep with a plane and keep one side — the side the
    plane normal points to when `direction` is on, the other one when off.
    `cap` closes the cut (a solid) or leaves it open (a shell)."""
    s = _gh_shapes(_brep)
    if not s:
        return None
    pl = _gh_plane(_plane)
    keep = Keep.TOP if _gh_bool(_direction) else Keep.BOTTOM
    r = split(s[0], bisect_by=pl, keep=keep)
    if not _gh_bool(_cap):
        cut = [f for f in r.faces() if f.geom_type == GeomType.PLANE
               and abs(pl.to_local_coords(f.center()).Z) < 1e-6
               and abs(abs(f.normal_at().dot(pl.z_dir)) - 1) < 1e-6]
        keepf = [f for f in r.faces() if all(f is not c for c in cut)]
        return Shell(keepf) if keepf else r
    return r


def _gh_collision_one_many(_collider, _obstacles):
    """Collision One|Many: does the collider touch (or overlap) any obstacle?
    `index` is the first obstacle it hits, -1 if none."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    c = _gh_shapes(_collider)
    if isinstance(_collider, Vector):
        c = [Vertex(_collider.X, _collider.Y, _collider.Z)]
    obs = _gh_shapes(_obstacles)
    if not c or not obs:
        return {"collision": False, "index": -1}
    for k, o in enumerate(obs):
        d = BRepExtrema_DistShapeShape(c[0].wrapped, o.wrapped)
        if d.IsDone() and d.Value() < 1e-6:
            return {"collision": True, "index": k}
        try:
            if o.volume > 0 and o.is_inside(c[0].center()):
                return {"collision": True, "index": k}
        except Exception:
            pass
    return {"collision": False, "index": -1}


def _gh_extrude_to_plane(_shape, _direction, _plane1, _plane2=None):
    """Extrude to Plane: extrude along a direction from where the shape meets
    plane 1 to plane 2 (planes, or planar faces standing for them). With one
    plane: from the shape itself up to it."""
    s = _gh_shapes(_shape)
    if not s:
        return None
    v = _gh_vec(_direction).normalized()

    def _plane_of(p):
        if p is None:
            return None
        if isinstance(p, Plane):
            return p
        f = _gh_face(p)
        return Plane(f) if f is not None else _gh_plane(p)

    p1, p2 = _plane_of(_plane1), _plane_of(_plane2)
    out = []
    for x in s:
        c = x.center()

        def _hit(pl):
            den = v.dot(pl.z_dir)
            if abs(den) < 1e-12:
                raise ValueError("Extrude to Plane: the direction is parallel to the plane")
            return (pl.origin - c).dot(pl.z_dir) / den

        if p2 is None:
            t0, t1 = 0.0, _hit(p1)
        else:
            t0, t1 = _hit(p1), _hit(p2)
        base = _move(x, v * t0, 0, 0, 0)
        out.append(_gh_extrude(base, v * (t1 - t0)))
    return _gh_out(out)


def _gh_extrude_tapered(_profile, _direction, _angle=0.0, _corner=0, _merge=True,
                        _degrees=False):
    """Extrude Tapered: extrude a planar profile along a vector, the walls
    leaning in by `angle` (radians unless `degrees`)."""
    w = _gh_curve(_profile)
    if w is None:
        return None
    v = _gh_vec(_direction)
    a = _gh_num(_angle, 0.0)
    if not _degrees:
        a = math.degrees(a)
    f = Face(w) if w.is_closed else None
    if f is None:
        raise ValueError("Extrude Tapered: the profile must be closed")
    return extrude(f, amount=v.length, dir=v.normalized(), taper=a)


def _gh_brep_plane(_brep, _plane):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Section
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir
    s = _gh_shapes(_brep)
    if not s:
        return {"curves": None, "points": None}
    pl = _gh_plane(_plane)
    o, z = pl.origin, pl.z_dir
    sec = BRepAlgoAPI_Section(s[0].wrapped, gp_Pln(gp_Pnt(o.X, o.Y, o.Z), gp_Dir(z.X, z.Y, z.Z)))
    sec.Build()
    es = list(_gh_wrap(sec.Shape()).edges())
    if not es:
        return {"curves": None, "points": None}
    try:
        ws = list(Wire.combine(es, tol=1e-4))
    except Exception:
        ws = es
    return {"curves": _gh_list(ws), "points": None}


def _gh_volume(_geometry):
    s = _gh_shapes(_geometry)
    if not s:
        return {"volume": None, "centroid": None}
    tot, acc = 0.0, Vector(0, 0, 0)
    for x in s:
        v = x.volume
        tot += v
        acc = acc + x.center(CenterOf.MASS) * v
    return {"volume": tot, "centroid": acc / tot if tot else None}


def _gh_loft_options(_closed=False, _adjust=False, _rebuild=0, _refit=0.0, _type=0):
    return {"closed": _gh_bool(_closed), "type": int(_gh_num(_type, 0))}


def _gh_loft(_curves, _options=None):
    """Loft through section curves -> a surface. Loft type 2 (Straight) gives a
    ruled loft; `closed` loops back to the first section."""
    ws = [_gh_curve(c) for c in _gh_items(_curves)]
    ws = [w for w in ws if w is not None]
    if len(ws) < 2:
        raise ValueError("Loft: needs at least two curves")
    o = _options if isinstance(_options, dict) else {}
    if o.get("closed"):
        ws = ws + [ws[0]]
    ruled = o.get("type") == 2
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    th = BRepOffsetAPI_ThruSections(False, ruled)
    for w in ws:
        th.AddWire(w.wrapped)
    th.Build()
    return _gh_wrap(th.Shape())


def _gh_sweep2(_rail1, _rail2, _sections, _same_height=False):
    """Sweep2: sections swept along rail 1 with rail 2 steering their twist (the
    second rail is the binormal guide)."""
    r1, r2 = _gh_curve(_rail1), _gh_curve(_rail2)
    secs = [_gh_curve(c) for c in _gh_items(_sections)]
    secs = [w for w in secs if w is not None]
    if r1 is None or not secs:
        return None
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    mk = BRepOffsetAPI_MakePipeShell(r1.wrapped)
    if r2 is not None:
        mk.SetMode(r2.wrapped, True)
    for w in secs:
        mk.Add(w.wrapped, False, False)
    mk.Build()
    if not mk.IsDone():
        raise ValueError("Sweep2: the sweep failed")
    return _gh_wrap(mk.Shape())


def _gh_unroll(_polysurface, _plane=None, _points=None, _curves=None):
    """Unroll Polysurface for the developable faces noodle meets on a jar: a
    plane (laid flat as is), a cylinder (u·r by v) and a cone. The face is laid
    in the target plane with u along x; points and curves lying on the face go
    along through their (u, v) — that is what lets a decoration be drawn flat
    and wrapped back with Surface Morph."""
    from OCP.BRepTools import BRepTools
    f = _gh_face(_polysurface)
    if f is None:
        return {"unrolled": None, "unrolled_points": None, "unrolled_curves": None}
    pl = _gh_plane(_plane)
    gt = f.geom_type

    def _flat(u, v):
        if gt == GeomType.CYLINDER:
            r = f.radius if hasattr(f, "radius") else 1.0
            return Vector(u * r, v, 0)
        if gt == GeomType.CONE:
            from OCP.BRep import BRep_Tool
            from OCP.Geom import Geom_ConicalSurface
            cs = Geom_ConicalSurface.DownCast(BRep_Tool.Surface_s(f.wrapped))
            r0, a = cs.RefRadius(), cs.SemiAngle()
            sl = r0 / math.sin(a) + v if abs(math.sin(a)) > 1e-12 else v
            phi = u * math.sin(a)
            return Vector(sl * math.cos(phi), sl * math.sin(phi), 0)
        return Vector(u, v, 0)

    if gt == GeomType.PLANE:
        lay = lambda q: _gh_from_local(pl, Plane(f).to_local_coords(q))
        flat = lay(f)
    elif gt in (GeomType.CYLINDER, GeomType.CONE):
        uv = _gh_face_uv(f)
        n = 48
        us = [uv["u"][0] + (uv["u"][1] - uv["u"][0]) * i / n for i in range(n + 1)]
        vs = [uv["v"][0], uv["v"][1]]
        if gt == GeomType.CYLINDER:
            a, b = _flat(us[0], vs[0]), _flat(us[-1], vs[1])
            flat = Face(Polyline((a.X, a.Y), (b.X, a.Y), (b.X, b.Y), (a.X, b.Y), close=True))
        else:
            ring0 = [_flat(u, vs[0]) for u in us]
            ring1 = [_flat(u, vs[1]) for u in reversed(us)]
            flat = Face(Polyline(*(ring0 + ring1), close=True))
        flat = _gh_from_local(pl, flat)
    else:
        raise ValueError(f"Unroll: a {gt} face is not developable here (plane, cylinder, cone)")

    def _uv_of(q):
        from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
        from OCP.BRep import BRep_Tool
        from OCP.gp import gp_Pnt
        pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(q.X, q.Y, q.Z), BRep_Tool.Surface_s(f.wrapped))
        u, v = pr.LowerDistanceParameters()
        return u, v

    def _map(q):
        if gt == GeomType.PLANE:
            return lay(q)
        u, v = _uv_of(q)
        return _gh_from_local(pl, _flat(u, v))

    pts = [_map(_gh_point(p)) for p in _gh_items(_points)] if _points is not None else None
    crvs = None
    if _curves is not None:
        crvs = []
        for c in _gh_items(_curves):
            w = _gh_curve(c)
            if w is None:
                continue
            if gt == GeomType.PLANE:
                crvs.append(lay(w))
            else:
                # unwrap u across the seam: a curve going round the cylinder
                # must not jump back across the whole sheet at u = 2π
                uvs, prev = [], None
                for i in range(97):
                    u, v = _uv_of(w.position_at(i / 96.0))
                    if prev is not None:
                        while u - prev > math.pi:
                            u -= 2 * math.pi
                        while prev - u > math.pi:
                            u += 2 * math.pi
                    prev = u
                    uvs.append((u, v))
                sm = [_gh_from_local(pl, _flat(u, v)) for u, v in uvs]
                crvs.append(Spline(*sm))
    return {"unrolled": flat, "unrolled_points": pts, "unrolled_curves": crvs}


def _gh_surface_morph(_geometry, _reference, _surface, _u=None, _v=None, _w=None):
    """Surface Morph: geometry placed in a REFERENCE BOX is carried onto a
    surface — the box's x/y become the surface's u/v (over the U/V domains,
    normalised), its z the distance along the surface normal (over W). Curves
    come back as splines through the morphed samples; faces and solids as a
    MESH (a B-Rep cannot be bent exactly — a decoration morphed onto a jar is
    triangles, and goes on through the mesh lane)."""
    f = _gh_face(_surface)
    if f is None or _geometry is None:
        return None
    ref = _gh_shapes(_reference)
    bb = _gh_bbox(ref[0] if ref else _geometry)
    U, V = _gh_domain(_u, (0.0, 1.0)), _gh_domain(_v, (0.0, 1.0))
    W = _gh_domain(_w, (0.0, 1.0))
    uv = _gh_face_uv(f)
    sx = max(bb.max.X - bb.min.X, 1e-12)
    sy = max(bb.max.Y - bb.min.Y, 1e-12)
    sz = max(bb.max.Z - bb.min.Z, 1e-12)

    def _m(q):
        a = (q.X - bb.min.X) / sx
        b = (q.Y - bb.min.Y) / sy
        c = (q.Z - bb.min.Z) / sz
        uu = U[0] + (U[1] - U[0]) * a
        vv = V[0] + (V[1] - V[0]) * b
        ww = W[0] + (W[1] - W[0]) * c
        pu = uv["u"][0] + (uv["u"][1] - uv["u"][0]) * uu
        pv = uv["v"][0] + (uv["v"][1] - uv["v"][0]) * vv
        from OCP.BRep import BRep_Tool
        from OCP.GeomLProp import GeomLProp_SLProps
        srf = BRep_Tool.Surface_s(f.wrapped)
        pr = GeomLProp_SLProps(srf, pu, pv, 1, 1e-6)
        p = pr.Value()
        n = pr.Normal() if pr.IsNormalDefined() else None
        base = Vector(p.X(), p.Y(), p.Z())
        if n is not None:
            nn = Vector(n.X(), n.Y(), n.Z())
            if f.wrapped.Orientation().name.endswith("REVERSED"):
                nn = -nn
            base = base + nn * ww
        return base

    out = []
    for g in _gh_items(_geometry):
        if isinstance(g, Vector):
            out.append(_m(g))
        elif isinstance(g, (Edge, Wire)):
            w = _gh_curve(g)
            out.append(Spline(*[_m(w.position_at(i / 96.0)) for i in range(97)]))
        else:
            m = _to_mesh(g, 0.05)
            import numpy as _np
            import trimesh as _tm
            V0 = _np.asarray(m.tm.vertices)
            V1 = _np.array([_m(Vector(*p)).to_tuple() for p in V0])
            out.append(Mesh(_tm.Trimesh(V1, _np.asarray(m.tm.faces), process=False)))
    return _gh_out(out)


# ===========================================================================
# Text (Squid's Font / Paragraph / Text Curves)
# ===========================================================================

def _gh_font_list():
    import subprocess
    try:
        out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return []
    fams = sorted({ln.split(",")[0].strip() for ln in out.splitlines() if ln.strip()})
    return fams


def _gh_font(_family=None, _size=10.0, _bold=False, _italic=False, _underline=False):
    return {"family": _family or "Liberation Sans", "size": _gh_num(_size, 10.0),
            "bold": _gh_bool(_bold), "italic": _gh_bool(_italic),
            "underline": _gh_bool(_underline)}


def _gh_paragraph(_halign=0, _valign=0, _rtl=False):
    return {"halign": int(_gh_num(_halign, 0)), "valign": int(_gh_num(_valign, 0)),
            "rtl": _gh_bool(_rtl)}


def _gh_text_curves(_plane, _text, _font=None, _paragraph=None):
    """Text Curves: the outlines of a text in a font, on a plane — every glyph's
    outer and inner loops as closed curves (Boundary Surfaces fills them back,
    holes included). Alignment from the Paragraph: h 0/1/2 = left/centre/right,
    v 0/1/2 = top/middle/bottom."""
    if _text is None or str(_text) == "":
        return None
    pl = _gh_plane(_plane)
    f = _font if isinstance(_font, dict) else _gh_font()
    p = _paragraph if isinstance(_paragraph, dict) else _gh_paragraph()
    style = (FontStyle.BOLDITALIC if f["bold"] and f["italic"] else
             FontStyle.BOLD if f["bold"] else FontStyle.ITALIC if f["italic"]
             else FontStyle.REGULAR)
    ha = [Align.MIN, Align.CENTER, Align.MAX][max(0, min(2, p["halign"]))]
    va = [Align.MAX, Align.CENTER, Align.MIN][max(0, min(2, p["valign"]))]
    sk = Text(str(_text), f["size"], font=f["family"], font_style=style, align=(ha, va))
    ws = []
    for fc in sk.faces():
        ws.append(fc.outer_wire())
        ws += list(fc.inner_wires())
    return [_gh_from_local(pl, w) for w in ws]


# ===========================================================================
# Export & bake (Pancake, eleFront)
# ===========================================================================

def _gh_step_export_option(_schema=None, *_rest):
    return {"format": "step", "schema": _schema}


def _gh_filepath_series(_pattern, _start=0, _qty=1):
    """Filepath Series: file names from a pattern ('{0}.stp' -> 0.stp, 1.stp…)."""
    pat = str(_pattern or "{0}")
    a = int(_gh_num(_start, 0))
    n = max(1, int(_gh_num(_qty, 1)))
    names = [pat.replace("{0}", str(a + i)) if "{0}" in pat else f"{pat}{a + i}"
             for i in range(n)]
    return {"filepath": names[0], "list_of_filepath": names, "counter_of_first_file": a}


def _gh_export_file(_geometry, _path, _node_id, _fmt=None):
    import os as _os
    s = _gh_shapes(_geometry)
    if not s or not _path:
        return None
    name = _os.path.basename(str(_path).replace("\\", "/")) or "export"
    ext = (_fmt or _os.path.splitext(name)[1].lstrip(".") or "step").lower()
    if ext in ("stp", "step"):
        ext = "step"
    base = _os.path.splitext(name)[0] + "." + ("step" if ext == "step" else ext)
    shape = s[0] if len(s) == 1 else Compound(s)
    out = _out(base, _node_id)
    if ext == "step":
        export_step(shape, out)
    elif ext == "stl":
        export_stl(shape, out)
    else:
        raise ValueError(f"export format {ext!r} is not supported (step, stl)")
    return out


def _gh_export_as(_geometry, _file, _overwrite=True, _options=None, _export=False,
                  _node_id=None, _attributes=None):
    """Export As (Pancake): when `export` is true, write the geometry to the
    project's exports/ under the file's NAME (a Windows path from the .gh keeps
    only its name). Format from the options (STEP Export Option) or the
    extension. Not exporting is not an error: ok = False."""
    if not _gh_bool(_export):
        return {"ok": False, "export_command": None}
    fmt = _options.get("format") if isinstance(_options, dict) else None
    p = _gh_export_file(_geometry, _file, _node_id, fmt)
    return {"ok": p is not None, "export_command": p}


def _gh_elefront_attributes(_name=None, _layer=None, _colour=None, _material=None, **_kw):
    """eleFront Attributes: the object attributes (name, layer, colour,
    material) as one value, read by Bake Objects / Export As."""
    return {"attributes": {"name": _name, "layer": _layer, "colour": _colour,
                           "material": _material},
            "name": _name, "layer": _layer, "display_colour": _colour,
            "material_name": _material}


def _gh_bake_objects(_geometry, _attributes=None, _bake=False, _node_id=None):
    """Bake Objects (eleFront): noodle has no Rhino document to bake into; the
    nearest thing is a file. When `bake` is true the geometry goes to exports/
    as STEP, named after the attributes' `name` (or 'bake')."""
    if not _gh_bool(_bake):
        return {"message": "not baked (bake is off)", "ghosts": None}
    a = _attributes.get("attributes", _attributes) if isinstance(_attributes, dict) else {}
    name = (a or {}).get("name") or "bake"
    p = _gh_export_file(_geometry, f"{name}.step", _node_id, "step")
    return {"message": f"baked to {p}", "ghosts": _geometry}
'''
