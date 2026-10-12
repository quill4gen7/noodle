"""Geometry cached inside a .gh, and how it becomes noodle geometry.

Grasshopper keeps a copy of every Rhino-referenced object in the definition as
`ON_Data`. Unwrapped, that is:

    raw DEFLATE
      -> a .NET BinaryFormatter record of a RhinoCommon object
         (`Rhino.Geometry.PolyCurve`, `…Brep`, `…Mesh`…), ISerializable with the
         members version / archive3dm / opennurbs / data
      -> `data` is an openNURBS archive of the object.

Those four members are exactly what `rhino3dm.CommonObject.Decode` takes, so
unwrapping is pure Python here and only the last step needs rhino3dm (MIT,
McNeel). It is optional: without it the importer still reads the graph, and
says the geometry could not be decoded.

Curves come out EXACT — degree, control points, weights, knots — and
`curve_code()` writes a CodeBlock that rebuilds them as OCCT B-splines, not as a
polyline through samples.
"""
from __future__ import annotations

import re
import struct
import zlib
from typing import Any


def unwrap(blob: bytes) -> tuple[str | None, dict | None]:
    """ON_Data bytes -> (RhinoCommon class name, rhino3dm Decode dict)."""
    d = zlib.decompressobj(-15).decompress(blob)
    m = re.search(rb"Rhino\.Geometry\.(\w+)", d)
    cls = m.group(1).decode() if m else None
    j = d.find(b"\x04data")
    if j < 0:
        return cls, None
    # After the member names: 4 BinaryTypeEnum bytes, 4 primitive type bytes
    # (Int32 x3, Byte), the library id, then the three Int32 values.
    p = j + 5 + 4 + 4 + 4
    version, archive3dm, opennurbs = struct.unpack_from("<iii", d, p)
    p += 12
    if d[p] == 0x09:                 # MemberReference -> the array follows
        p += 5
    if d[p] != 0x0F:                 # ArraySinglePrimitive
        raise ValueError(f"unexpected BinaryFormatter record 0x{d[p]:02x}")
    n = struct.unpack_from("<i", d, p + 5)[0]
    p += 10                          # record, object id, length, primitive type
    return cls, {"version": version, "archive3dm": archive3dm,
                 "opennurbs": opennurbs, "data": d[p:p + n]}


def have_rhino3dm() -> bool:
    try:
        import rhino3dm  # noqa: F401
        return True
    except Exception:
        return False


def decode(blob: bytes) -> dict | None:
    """ON_Data -> a plain record: {"kind": "curve", "segments": [...]},
    {"kind": "mesh", ...}, {"kind": "brep", "mesh": ...} — or None when it
    cannot be decoded (no rhino3dm, unknown class)."""
    import base64
    import rhino3dm as r
    cls, dd = unwrap(blob)
    if dd is None:
        return None
    obj = r.CommonObject.Decode({**dd, "data": base64.b64encode(dd["data"]).decode()})
    if obj is None:
        return None
    if isinstance(obj, r.Curve):
        segs = ([obj.SegmentCurve(i) for i in range(obj.SegmentCount)]
                if isinstance(obj, r.PolyCurve) else [obj])
        return {"kind": "curve", "class": cls, "closed": bool(obj.IsClosed),
                "segments": [_nurbs(s) for s in segs]}
    if isinstance(obj, r.Mesh):
        return {"kind": "mesh", "class": cls, **_mesh(obj)}
    if isinstance(obj, r.Brep):
        meshes = [f.GetMesh(r.MeshType.Any) for f in obj.Faces]
        meshes = [m for m in meshes if m is not None]
        rec = {"kind": "brep", "class": cls, "solid": bool(obj.IsSolid),
               "faces": len(obj.Faces)}
        if meshes:
            # The render meshes Rhino cached per face; rhino3dm cannot
            # tessellate a Brep itself, so this is all we get.
            vs, fs = [], []
            for m in meshes:
                mm = _mesh(m)
                off = len(vs)
                vs += mm["vertices"]
                fs += [[i + off for i in f] for f in mm["faces"]]
            rec["mesh"] = {"vertices": vs, "faces": fs}
        return rec
    return {"kind": "other", "class": cls}


def _nurbs(c) -> dict:
    n = c.ToNurbsCurve()
    pts = [n.Points[i] for i in range(len(n.Points))]
    return {"degree": n.Degree, "periodic": bool(n.IsPeriodic),
            "points": [[p.X, p.Y, p.Z] for p in pts],
            "weights": [p.W for p in pts] if n.IsRational else None,
            "knots": [n.Knots[i] for i in range(len(n.Knots))]}


def _mesh(m) -> dict:
    vs = [[v.X, v.Y, v.Z] for v in (m.Vertices[i] for i in range(len(m.Vertices)))]
    fs = []
    for i in range(len(m.Faces)):
        f = m.Faces[i]
        a, b, c, d = f
        fs.append([a, b, c])
        if c != d:
            fs.append([a, c, d])
    return {"vertices": vs, "faces": fs}


def occ_knots(rhino_knots: list[float], degree: int) -> tuple[list[float], list[int]]:
    """Rhino stores n+degree-1 knots; OCCT wants the full n+degree+1 vector as
    unique values + multiplicities. Add one knot at each end (repeating the end
    value, as a clamped curve has) and run-length encode."""
    full = [rhino_knots[0]] + list(rhino_knots) + [rhino_knots[-1]]
    values, mults = [], []
    for k in full:
        if values and abs(k - values[-1]) < 1e-12:
            mults[-1] += 1
        else:
            values.append(k)
            mults.append(1)
    return values, mults


def _fmt(x: float) -> str:
    return f"{x:.10g}"


def curve_code(curves: list[dict], label: str = "") -> str:
    """CodeBlock source that rebuilds `curves` (decode() records of kind
    "curve") exactly: one OCCT B-spline per segment, the segments of a
    PolyCurve joined into one Wire. One curve -> that curve; several -> a list.
    """
    rows = []
    for c in curves:
        segs = []
        for s in c["segments"]:
            vals, mults = occ_knots(s["knots"], s["degree"])
            pts = ", ".join("(" + ", ".join(_fmt(v) for v in p) + ")" for p in s["points"])
            w = ("[" + ", ".join(_fmt(v) for v in s["weights"]) + "]") if s["weights"] else "None"
            segs.append(f"        ({s['degree']}, [{pts}], {w}, "
                        f"[{', '.join(_fmt(v) for v in vals)}], {mults}),")
        rows.append("    [\n" + "\n".join(segs) + "\n    ],")
    head = f"# Rhino geometry cached in the .gh{(' — ' + label) if label else ''}: exact B-splines.\n"
    return head + _CURVE_TEMPLATE.replace("@CURVES@", "\n".join(rows))


_CURVE_TEMPLATE = '''from OCP.Geom import Geom_BSplineCurve
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TColStd import TColStd_Array1OfReal, TColStd_Array1OfInteger
from OCP.gp import gp_Pnt
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge

def _bspline(deg, pts, w, knots, mults):
    P = TColgp_Array1OfPnt(1, len(pts))
    for i, p in enumerate(pts, 1):
        P.SetValue(i, gp_Pnt(*p))
    K = TColStd_Array1OfReal(1, len(knots))
    M = TColStd_Array1OfInteger(1, len(mults))
    for i, (k, m) in enumerate(zip(knots, mults), 1):
        K.SetValue(i, k)
        M.SetValue(i, m)
    if w:
        W = TColStd_Array1OfReal(1, len(w))
        for i, x in enumerate(w, 1):
            W.SetValue(i, x)
        g = Geom_BSplineCurve(P, W, K, M, deg)
    else:
        g = Geom_BSplineCurve(P, K, M, deg)
    return Edge(BRepBuilderAPI_MakeEdge(g).Edge())

CURVES = [
@CURVES@
]
def _wire(edges):
    # a Rhino PolyCurve may leave gaps OCCT's 1e-7 does not close: sew within
    # 1e-3, and if it still falls apart keep the pieces rather than nothing
    try:
        return Wire(edges)
    except Exception:
        ws = Wire.combine(edges, tol=1e-3)
        return ws[0] if len(ws) == 1 else list(ws)

shapes = []
for segs in CURVES:
    edges = [_bspline(*s) for s in segs]
    shapes.append(edges[0] if len(edges) == 1 else _wire(edges))
result = shapes[0] if len(shapes) == 1 else shapes
'''
