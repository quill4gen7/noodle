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
    """GH's plane cast: a Plane as is, a point -> the XY plane through it,
    nothing -> world XY."""
    if _v is None:
        return Plane.XY
    if isinstance(_v, Plane):
        return _v
    if isinstance(_v, Location):
        return Plane(_v)
    p = _gh_point(_v)
    return Plane.XY.move(Location(p)) if p is not None else Plane.XY


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


def _gh_deconstruct_domain2(_d):
    if _d is None:
        return {"u_min": None, "u_max": None, "v_min": None, "v_max": None}
    if not isinstance(_d, dict):
        f = _gh_face(_d)
        if f is None:
            raise ValueError("Deconstruct Domain²: wire a 2D domain or a surface")
        _d = _gh_face_uv(f)
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
    return {"rectangle": pl.from_local_coords(w), "length": length}


def _gh_circle(_plane, _r):
    pl = _gh_plane(_plane)
    r = _gh_num(_r, 1.0)
    return pl.from_local_coords(Wire([Edge.make_circle(abs(r))]))


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


def _gh_offset_curve(_curve, _distance, _plane=None, _corners=1, _both=False):
    """Offset Curve. Rhino's sign: a positive distance offsets to the RIGHT of the
    curve's direction about the plane normal — outward on a counter-clockwise
    loop, inward on a clockwise one. OCCT's offset_2d offsets a CLOSED wire
    outward for a positive distance whatever its direction, so the sign is
    corrected by the loop's orientation; an OPEN wire goes LEFT/RIGHT by side.
    The curve is moved into the plane's local frame first so 'right' means the
    same thing as in Rhino."""
    w = _gh_curve(_curve)
    if w is None:
        return None
    d = _gh_num(_distance, 1.0)
    if abs(d) < 1e-12:
        return w
    pl = _gh_plane(_plane) if _plane is not None else _gh_curve_plane(w)
    kind = _GH_CORNERS.get(int(_gh_num(_corners, 1)), Kind.INTERSECTION)
    flat = pl.to_local_coords(w)
    out = []
    dists = [d, -d] if _both else [d]
    for dd in dists:
        if w.is_closed:
            ccw = _gh_signed_area(w, pl) > 0
            occ = dd if ccw else -dd
            r = flat.offset_2d(occ, kind=kind)
        else:
            side = Side.RIGHT if dd > 0 else Side.LEFT
            r = flat.offset_2d(abs(dd), kind=kind, side=side, closed=False)
        out.append(pl.from_local_coords(r))
    return out[0] if len(out) == 1 else out


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
        return pl.from_local_coords(Vector(l.X * sx, l.Y * sy, l.Z * sz))
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
    return pl.from_local_coords(_gh_wrap(r))


# --- surfaces & breps -----------------------------------------------------------------

def _gh_boundary_surfaces(_edges):
    """Boundary Surfaces: planar closed curves -> trimmed planar faces. A curve
    lying INSIDE another on the same plane becomes a hole in it (GH nests them),
    so a rectangle and its offset give a frame, not two plates."""
    ws = []
    for it in _gh_items(_edges):
        w = _gh_curve(it)
        if w is not None and w.is_closed:
            ws.append(w)
    if not ws:
        return None
    faces = []
    for w in ws:
        try:
            faces.append(Face(w))
        except Exception:
            pass
    faces.sort(key=lambda f: -f.area)
    used = [False] * len(faces)
    out = []
    for i, f in enumerate(faces):
        if used[i]:
            continue
        outer = f
        try:
            n0 = f.normal_at()
        except Exception:
            n0 = None
        for j in range(i + 1, len(faces)):
            if used[j]:
                continue
            g = faces[j]
            try:
                same = n0 is not None and abs(abs(n0.dot(g.normal_at())) - 1) < 1e-6 \
                    and abs((g.center() - f.center()).dot(n0)) < 1e-6
                if same and f.is_inside(g.center()) and (g - f).area < 1e-6 * g.area + 1e-9:
                    outer = outer - g
                    used[j] = True
            except Exception:
                pass
        out.append(outer.faces()[0] if hasattr(outer, "faces") and len(outer.faces()) == 1 else outer)
    return _gh_out(out)


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


def _gh_isotrim(_surface, _domain):
    """Isotrim: the part of a surface inside a UV domain (absolute parameters of
    THIS face as OCCT parameterises it — see _gh_face_uv)."""
    f = _gh_face(_surface)
    if f is None or _domain is None:
        return None
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    srf = BRep_Tool.Surface_s(f.wrapped)
    u0, u1 = sorted(_domain["u"])
    v0, v1 = sorted(_domain["v"])
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
            x = Face(_gh_curve(x))
        a = x.area
        c = x.center(CenterOf.MASS) if a > 0 else x.center()
        tot += a
        acc = acc + c * a
    return {"area": tot, "centroid": acc / tot if tot else None}


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
    return {"breps": _gh_out(rs),
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
    return {"curves": _gh_out(ws), "points": None}


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
    return _gh_out(list(r.faces()))


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
            return _gh_out(list(Wire.combine(es, tol=1e-4)))
        except Exception:
            return _gh_out(es)
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
        return _gh_out(list(Wire.combine(es, tol=1e-4)))
    except Exception:
        return _gh_out(es)


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
        bb = _gh_shapes(_box)[0].bounding_box()
        lo = _np.array([bb.min.X, bb.min.Y, bb.min.Z])
        hi = _np.array([bb.max.X, bb.max.Y, bb.max.Z])
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
'''
