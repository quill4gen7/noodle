"""
slice_summary — symbolic cross-section summary of a solid (PLAN_RETROENG, fase 1).

Slices a shape with stacks of parallel planes on X, Y and Z (like a slicer, but
on all three axes), compresses every section into exact symbolic text — circle /
rect / slot / poly fallback, each outer loop with its holes — dedups runs of
identical sections into intervals ("z=a..b: same section") and prepends a
bbox+volume checksum. The resulting text is the *perception format* the copilot
uses both to read a target part and to verify its own reconstruction
(re-slice + compare the two texts).

Imported ONLY inside the execution worker subprocess (needs build123d), like
mesh_extractor. The build123d-free entry point is `cad_nodes.api.slice_summary`.
"""

from __future__ import annotations

import math
from collections import Counter

from build123d import Compound, Plane, section

TOL = 1e-3


# --- formatting -------------------------------------------------------------
def _f(v: float) -> str:
    s = f"{v + 0.0:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _pt(xy) -> str:
    return f"({_f(xy[0])},{_f(xy[1])})"


# Section planes per axis + projection of a global point onto in-plane 2D coords.
_PROJ = {
    "z": lambda p: (p.X, p.Y),
    "x": lambda p: (p.Y, p.Z),
    "y": lambda p: (p.X, p.Z),
}
_PLANE = {
    "z": lambda pos: Plane(origin=(0, 0, pos), z_dir=(0, 0, 1)),
    "x": lambda pos: Plane(origin=(pos, 0, 0), z_dir=(1, 0, 0)),
    "y": lambda pos: Plane(origin=(0, pos, 0), z_dir=(0, 1, 0)),
}
_COORDS = {"z": "(x,y)", "x": "(y,z)", "y": "(x,z)"}


def _gt(edge) -> str:
    """Edge geom type name, tolerant of geom_type being property or method."""
    g = edge.geom_type
    g = g() if callable(g) else g
    return getattr(g, "name", str(g)).upper()


def _poly_points(edges, proj):
    """Unique projected endpoints of a set of edges."""
    pts = []
    for e in edges:
        for t in (0.0, 1.0):
            p = proj(e.position_at(t))
            if not any(abs(p[0] - q[0]) < TOL and abs(p[1] - q[1]) < TOL for q in pts):
                pts.append(p)
    return pts


def _edge_dir(edge, proj):
    a, b = proj(edge.position_at(0.0)), proj(edge.position_at(1.0))
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _wire_desc(wire, proj) -> str:
    """One closed loop -> symbolic text (exact from the B-Rep)."""
    edges = list(wire.edges())
    circs = [e for e in edges if _gt(e) == "CIRCLE"]
    lines = [e for e in edges if _gt(e) == "LINE"]

    # Full circle (possibly split into several arcs of the same circle).
    if circs and len(circs) == len(edges):
        r0, c0 = circs[0].radius, circs[0].arc_center
        if all(abs(e.radius - r0) < TOL and (e.arc_center - c0).length < TOL
               for e in circs):
            return f"circle r={_f(r0)} @{_pt(proj(c0))}"

    # Rectangle: 4 lines, vertices equidistant from centroid, sides in 2 pairs.
    if len(edges) == 4 and len(lines) == 4:
        pts = _poly_points(edges, proj)
        if len(pts) == 4:
            cx = sum(p[0] for p in pts) / 4
            cy = sum(p[1] for p in pts) / 4
            diag = [math.hypot(p[0] - cx, p[1] - cy) for p in pts]
            L = sorted(e.length for e in edges)
            if (max(diag) - min(diag) < 10 * TOL
                    and abs(L[0] - L[1]) < 10 * TOL and abs(L[2] - L[3]) < 10 * TOL):
                dirs = [_edge_dir(e, proj) for e in edges]
                if all(min(abs(dx), abs(dy)) < 1e-3 for dx, dy in dirs):
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    return (f"rect {_f(max(xs) - min(xs))}x{_f(max(ys) - min(ys))}"
                            f" @{_pt((cx, cy))}")
                e_long = max(edges, key=lambda e: e.length)
                dx, dy = _edge_dir(e_long, proj)
                ang = math.degrees(math.atan2(dy, dx)) % 180.0
                return (f"rect {_f(L[3])}x{_f(L[0])} @{_pt((cx, cy))}"
                        f" rot={_f(ang)}")

    # Rounded rect: 4 lines + 4 corner arcs of equal radius.
    if len(edges) == 8 and len(lines) == 4 and len(circs) == 4:
        r0 = circs[0].radius
        if all(abs(e.radius - r0) < TOL for e in circs):
            pts = [proj(e.position_at(t)) for e in edges for t in (0.0, 1.0)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            return (f"rrect {_f(max(xs) - min(xs))}x{_f(max(ys) - min(ys))}"
                    f" r={_f(r0)}"
                    f" @{_pt(((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2))}")

    # Slot: 2 parallel lines + 2 arcs of equal radius.
    if len(lines) == 2 and len(circs) == 2 and len(edges) == 4:
        if abs(circs[0].radius - circs[1].radius) < TOL:
            c0, c1 = proj(circs[0].arc_center), proj(circs[1].arc_center)
            c2c = math.hypot(c1[0] - c0[0], c1[1] - c0[1])
            mid = ((c0[0] + c1[0]) / 2, (c0[1] + c1[1]) / 2)
            ang = math.degrees(math.atan2(c1[1] - c0[1], c1[0] - c0[0])) % 180.0
            return (f"slot r={_f(circs[0].radius)} c2c={_f(c2c)}"
                    f" @{_pt(mid)} rot={_f(ang)}")

    # Fallback: edge-kind census + bbox. This is where the vision path helps.
    kinds = Counter(_gt(e) for e in edges)
    kind_s = ",".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    pts = [proj(e.position_at(t)) for e in edges for t in (0.0, 0.5, 1.0)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (f"poly({len(edges)}: {kind_s})"
            f" bbox {_f(max(xs) - min(xs))}x{_f(max(ys) - min(ys))}"
            f" @{_pt(((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2))}")


def _group(descs: list[str]) -> str:
    """'circle r=3 @(a)', 'circle r=3 @(b)' -> '2x circle r=3 @(a)@(b)'."""
    groups: dict[str, list[str]] = {}
    for d in descs:
        head, _, tail = d.partition(" @")
        groups.setdefault(head, []).append("@" + tail if tail else "")
    parts = []
    for head, tails in sorted(groups.items()):
        if len(tails) == 1:
            parts.append((head + " " + tails[0]).strip())
        else:
            parts.append(f"{len(tails)}x {head} " + "".join(sorted(tails)))
    return "; ".join(parts)


def _face_desc(face, proj) -> str:
    try:
        outer = _wire_desc(face.outer_wire(), proj)
        inner = [_wire_desc(w, proj) for w in face.inner_wires()]
    except Exception as e:  # noqa: BLE001 — never let one loop kill the summary
        return f"(face? {type(e).__name__})"
    if not inner:
        return outer
    return outer + " - holes: " + _group(inner)


def _section_desc(shape, plane, proj) -> str:
    try:
        sec = section(shape, section_by=plane)
    except Exception:
        return "(empty)"
    faces = [] if sec is None else list(sec.faces())
    if not faces:
        return "(empty)"
    return " ; ".join(sorted(_face_desc(f, proj) for f in faces))


def _stack(shape, axis: str, lo: float, hi: float, n: int) -> list[dict]:
    """n sections at bin midpoints in [lo, hi], runs of identical text merged."""
    proj, mkpl = _PROJ[axis], _PLANE[axis]
    step = (hi - lo) / n
    rows = [(lo + (i + 0.5) * step, _section_desc(shape, mkpl(lo + (i + 0.5) * step), proj))
            for i in range(n)]
    runs, i = [], 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][1] == rows[i][1]:
            j += 1
        runs.append({"from": round(rows[i][0], 2), "to": round(rows[j][0], 2),
                     "count": j - i + 1, "text": rows[i][1]})
        i = j + 1
    return runs


# ---------------------------------------------------------------------------
# STL / mesh path (fase 3): OCCT's section() segfaults on triangle-soup Faces,
# so meshes are sliced directly — triangle/plane intersection in numpy, chained
# into loops, then ARC-FITTED back to the same symbolic vocabulary (circle/
# rect/poly) so mesh and B-Rep summaries stay text-comparable.
# ---------------------------------------------------------------------------
_AXIS_I = {"x": 0, "y": 1, "z": 2}
_PROJ_I = {"z": (0, 1), "x": (1, 2), "y": (0, 2)}


def _load_stl(path: str):
    """Binary or ASCII STL -> (n,3,3) float64 triangle vertices."""
    import numpy as np
    with open(path, "rb") as f:
        data = f.read()
    is_ascii = data[:5].lower() == b"solid" and b"facet" in data[:1000]
    if is_ascii:
        import re
        nums = re.findall(rb"vertex\s+([\S]+)\s+([\S]+)\s+([\S]+)", data)
        v = np.array(nums, dtype=np.float64)
        return v.reshape(-1, 3, 3)
    n = int.from_bytes(data[80:84], "little")
    rec = np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")])
    arr = np.frombuffer(data, dtype=rec, count=n, offset=84)
    return arr["v"].reshape(-1, 3, 3).astype(np.float64)


def _mesh_volume(tris) -> float:
    import numpy as np
    return abs(float(np.einsum("ij,ij->i", tris[:, 0],
                               np.cross(tris[:, 1], tris[:, 2])).sum()) / 6.0)


def _mesh_section_loops(tris, axis: str, pos: float):
    """Slice the mesh with the plane axis=pos -> list of closed 2D loops
    (in-plane coords, ordered vertices, last != first)."""
    ai = _AXIS_I[axis]
    ui, vi = _PROJ_I[axis]
    d = tris[:, :, ai] - pos
    hit = (d.min(axis=1) < 0) & (d.max(axis=1) > 0)
    segs = []
    for T, dd in zip(tris[hit], d[hit], strict=True):
        pts = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            if dd[i] * dd[j] < 0:
                t = dd[i] / (dd[i] - dd[j])
                p = T[i] + t * (T[j] - T[i])
                pts.append((p[ui], p[vi]))
        if len(pts) == 2:
            segs.append(pts)
    def q(p):
        return (round(p[0], 3), round(p[1], 3))

    adj: dict = {}
    for a, b in segs:
        qa, qb = q(a), q(b)
        if qa == qb:
            continue
        adj.setdefault(qa, []).append(qb)
        adj.setdefault(qb, []).append(qa)
    loops, used = [], set()
    for start in adj:
        if start in used:
            continue
        loop, prev, cur = [start], None, start
        used.add(start)
        while True:
            nxt = next((p for p in adj.get(cur, ()) if p != prev and p not in used), None)
            if nxt is None:
                break
            loop.append(nxt)
            used.add(nxt)
            prev, cur = cur, nxt
        if len(loop) >= 3 and (start in adj.get(cur, ()) or cur == start):
            loops.append(loop)
    return loops


def _fit_circle(pts):
    """Kasa least-squares circle fit -> ((cx, cy), r, max_residual)."""
    import numpy as np
    P = np.asarray(pts, dtype=float)
    x, y = P[:, 0], P[:, 1]
    A = np.c_[2 * x, 2 * y, np.ones(len(P))]
    sol, *_ = np.linalg.lstsq(A, x * x + y * y, rcond=None)
    cx, cy, c = sol
    r = math.sqrt(max(c + cx * cx + cy * cy, 0.0))
    res = float(np.abs(np.hypot(x - cx, y - cy) - r).max()) if r else 1e9
    return (float(cx), float(cy)), r, res


def _collapse_collinear(pts, ang_tol_deg: float = 8.0):
    """Drop vertices whose turn angle is below tolerance -> true corners."""
    out = []
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        if math.degrees(math.acos(cosang)) >= ang_tol_deg:
            out.append(b)
    return out or list(pts)


def _max_turn_deg(pts) -> float:
    worst = 0.0
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        n1, n2 = math.hypot(*v1), math.hypot(*v2)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        worst = max(worst, math.degrees(math.acos(cosang)))
    return worst


def _turn_deg(a, b, c) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return 0.0
    cosang = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
    return math.degrees(math.acos(cosang))


def _line_dev(P, i, j) -> float:
    """Max distance of P[i..j] from the chord P[i]->P[j]."""
    ax, ay = P[i]
    bx, by = P[j]
    L = math.hypot(bx - ax, by - ay)
    if L < 1e-9:
        return 0.0
    worst = 0.0
    for k in range(i + 1, j):
        px, py = P[k]
        worst = max(worst, abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / L)
    return worst


def _fit_runs(pts, tol: float = 0.08):
    """Greedy line/arc decomposition of a closed polyline of raw mesh vertices
    -> [{'type': 'LINE'|'ARC', 'from', 'to', 'r'?, 'center'?}]. This is the
    arc-fitting core: chains of chords become one ARC, collinear chains one
    LINE, so mesh outlines speak the same vocabulary as B-Rep ones."""
    n = len(pts)
    if n < 3:
        return [{"type": "LINE", "from": pts[0], "to": pts[-1]}]
    # Start the walk at the sharpest corner so no run wraps the seam.
    turns = [_turn_deg(pts[i - 1], pts[i], pts[(i + 1) % n]) for i in range(n)]
    k = max(range(n), key=lambda i: turns[i])
    P = pts[k:] + pts[:k] + [pts[k]]
    segs, i, m = [], 0, len(P)
    while i < m - 1:
        j = i + 1
        while j + 1 < m and _line_dev(P, i, j + 1) < tol:
            j += 1
        line_j = j
        arc_j, arc = i + 1, None
        if i + 2 < m:
            jj = i + 2
            while jj < m:
                c, r, res = _fit_circle(P[i:jj + 1])
                if res < tol and 0.05 < r < 5000:
                    arc_j, arc = jj, (c, r)
                    jj += 1
                else:
                    break
        if arc is not None and arc_j > line_j + 1:
            segs.append({"type": "ARC", "from": P[i], "to": P[arc_j],
                         "r": arc[1], "center": arc[0]})
            i = arc_j
        else:
            segs.append({"type": "LINE", "from": P[i], "to": P[line_j]})
            i = line_j
    return segs


def _mesh_loop_desc(pts) -> str:
    """Arc-fit one polyline loop into the shared symbolic vocabulary."""
    (cx, cy), r, res = _fit_circle(pts)
    # Vertices of a regular N-gon also sit on a circle — demand smoothness too.
    if res < max(0.06, 0.005 * r) and len(pts) >= 12 and _max_turn_deg(pts) < 30:
        return f"circle r={_f(r)} @{_pt((cx, cy))}"
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    bbox_c = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
    segs = _fit_runs(pts)
    lines = [s for s in segs if s["type"] == "LINE"]
    arcs = [s for s in segs if s["type"] == "ARC"]

    def _mid(seg):
        return ((seg["from"][0] + seg["to"][0]) / 2,
                (seg["from"][1] + seg["to"][1]) / 2)

    if len(segs) == 4 and len(lines) == 4:
        corners = [s["from"] for s in segs]
        ccx = sum(p[0] for p in corners) / 4
        ccy = sum(p[1] for p in corners) / 4
        diag = [math.hypot(p[0] - ccx, p[1] - ccy) for p in corners]
        L = sorted(math.hypot(s["to"][0] - s["from"][0],
                              s["to"][1] - s["from"][1]) for s in segs)
        if (max(diag) - min(diag) < 0.2
                and abs(L[0] - L[1]) < 0.2 and abs(L[2] - L[3]) < 0.2):
            return (f"rect {_f(max(xs) - min(xs))}x{_f(max(ys) - min(ys))}"
                    f" @{_pt((ccx, ccy))}")
    if (len(segs) == 8 and len(lines) == 4 and len(arcs) == 4
            and max(a["r"] for a in arcs) - min(a["r"] for a in arcs) < 0.2):
        return (f"rrect {_f(max(xs) - min(xs))}x{_f(max(ys) - min(ys))}"
                f" r={_f(arcs[0]['r'])} @{_pt(bbox_c)}")
    if (len(segs) == 4 and len(lines) == 2 and len(arcs) == 2
            and abs(arcs[0]["r"] - arcs[1]["r"]) < 0.2):
        c0, c1 = arcs[0]["center"], arcs[1]["center"]
        c2c = math.hypot(c1[0] - c0[0], c1[1] - c0[1])
        mid = ((c0[0] + c1[0]) / 2, (c0[1] + c1[1]) / 2)
        ang = math.degrees(math.atan2(c1[1] - c0[1], c1[0] - c0[0])) % 180.0
        return (f"slot r={_f(arcs[0]['r'])} c2c={_f(c2c)}"
                f" @{_pt(mid)} rot={_f(ang)}")
    kind = f"{len(lines)} LINE" + (f",{len(arcs)} ARC" if arcs else "")
    return (f"poly(~{len(segs)}: {kind})"
            f" bbox {_f(max(xs) - min(xs))}x{_f(max(ys) - min(ys))}"
            f" @{_pt(bbox_c)}")


def _poly_area(pts) -> float:
    s = 0.0
    for i in range(len(pts)):
        a, b = pts[i - 1], pts[i]
        s += a[0] * b[1] - b[0] * a[1]
    return s / 2.0


def _point_in_poly(p, pts) -> bool:
    x, y = p
    inside = False
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i - 1], pts[i]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _mesh_section_desc(tris, axis: str, pos: float) -> str:
    loops = _mesh_section_loops(tris, axis, pos)
    if not loops:
        return "(empty)"
    # Nest: a loop inside another is that loop's hole (one level, like B-Rep).
    loops = sorted(loops, key=lambda lp: -abs(_poly_area(lp)))
    groups = []  # [outer_pts, [hole_pts...]]
    for lp in loops:
        c = (sum(p[0] for p in lp) / len(lp), sum(p[1] for p in lp) / len(lp))
        host = next((g for g in groups if _point_in_poly(c, g[0])), None)
        if host is not None:
            host[1].append(lp)
        else:
            groups.append([lp, []])
    parts = []
    for outer, holes in groups:
        txt = _mesh_loop_desc(outer)
        if holes:
            txt += " - holes: " + _group([_mesh_loop_desc(h) for h in holes])
        parts.append(txt)
    return " ; ".join(sorted(parts))


def _dedup_runs(rows, axis: str):
    runs, i = [], 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1][1] == rows[i][1]:
            j += 1
        runs.append({"from": round(rows[i][0], 2), "to": round(rows[j][0], 2),
                     "count": j - i + 1, "text": rows[i][1]})
        i = j + 1
    return runs


def summarize_stl(path: str, n_per_axis: int = 10) -> dict:
    """summarize(), mesh edition: same text format, sections arc-fitted."""
    tris = _load_stl(path)
    if not len(tris):
        return {"success": False, "error": "empty or unreadable STL"}
    lo = tris.reshape(-1, 3).min(axis=0)
    hi = tris.reshape(-1, 3).max(axis=0)
    size = hi - lo
    volume = round(_mesh_volume(tris), 2)
    lines = [
        "part: bbox x[{}..{}] y[{}..{}] z[{}..{}] size {}x{}x{}, volume {}, "
        "mesh ({} triangles; sections are arc-fitted)".format(
            _f(lo[0]), _f(hi[0]), _f(lo[1]), _f(hi[1]), _f(lo[2]), _f(hi[2]),
            _f(size[0]), _f(size[1]), _f(size[2]), volume, len(tris)),
    ]
    axes: dict = {}
    for axis in ("z", "x", "y"):
        ai = _AXIS_I[axis]
        a_lo, a_hi = float(lo[ai]), float(hi[ai])
        if a_hi - a_lo < 1e-6:
            lines.append(f"{axis.upper()} stack: (flat along {axis})")
            axes[axis] = []
            continue
        step = (a_hi - a_lo) / n_per_axis
        rows = [(a_lo + (i + 0.5) * step,
                 _mesh_section_desc(tris, axis, a_lo + (i + 0.5) * step))
                for i in range(n_per_axis)]
        runs = _dedup_runs(rows, axis)
        axes[axis] = runs
        lines.append(f"{axis.upper()} stack ({n_per_axis} slices, in-plane coords {_COORDS[axis]}):")
        for r in runs:
            span = (f"{axis}={_f(r['from'])}" if r["count"] == 1
                    else f"{axis}={_f(r['from'])}..{_f(r['to'])} ({r['count']}x)")
            lines.append(f"  {span}: {r['text']}")
    return {"success": True,
            "bbox": {"x": [round(float(lo[0]), 3), round(float(hi[0]), 3)],
                     "y": [round(float(lo[1]), 3), round(float(hi[1]), 3)],
                     "z": [round(float(lo[2]), 3), round(float(hi[2]), 3)]},
            "size": [round(float(s), 3) for s in size],
            "volume": volume, "solids": None,
            "axes": axes, "text": "\n".join(lines)}


def outline_stl(path: str, axis: str = "z", position: float = 0.0) -> dict:
    """outline(), mesh edition: fitted corners (or circle fit) per loop."""
    axis = str(axis).lower()
    if axis not in _AXIS_I:
        return {"success": False, "error": f"axis must be x, y or z, not {axis!r}"}
    tris = _load_stl(path)
    loops = _mesh_section_loops(tris, axis, float(position))
    lines = [f"exact section {axis}={_f(float(position))} (mesh, arc-fitted),"
             f" in-plane coords {_COORDS[axis]}: {len(loops)} loop(s)"]
    out_loops = []
    for lp in sorted(loops, key=lambda l: -abs(_poly_area(l))):
        desc = _mesh_loop_desc(lp)
        lines.append(f"  loop: {desc}")
        edges = []
        if not desc.startswith("circle"):
            for s in _fit_runs(lp)[:40]:
                e = {"type": s["type"],
                     "from": [round(s["from"][0], 3), round(s["from"][1], 3)],
                     "to": [round(s["to"][0], 3), round(s["to"][1], 3)]}
                txt = f"    {s['type']} {_pt(s['from'])}->{_pt(s['to'])}"
                if s["type"] == "ARC":
                    e["r"] = round(s["r"], 3)
                    e["center"] = [round(s["center"][0], 3),
                                   round(s["center"][1], 3)]
                    txt += f" r={_f(s['r'])} c={_pt(s['center'])}"
                edges.append(e)
                lines.append(txt)
        out_loops.append({"desc": desc, "edges": edges})
    if not loops:
        lines.append("  (empty)")
    return {"success": True, "axis": axis, "position": float(position),
            "loops": out_loops, "text": "\n".join(lines)}


def _norm_shape(shape):
    if isinstance(shape, (list, tuple)):
        items = [s for s in shape if s is not None]
        if not items:
            return None
        return items[0] if len(items) == 1 else Compound(children=list(items))
    return shape


def outline(shape, axis: str = "z", position: float = 0.0) -> dict:
    """The 'microscope': ONE exact section, edge by edge. Every loop (outer +
    holes) is listed with its edges' type, projected 2D endpoints, and radius/
    center for arcs — the detail view for when the symbolic summary is
    ambiguous. Complements `summarize` (the wide-angle view)."""
    if isinstance(shape, str):          # a file path -> the mesh pipeline
        return outline_stl(shape, axis, position)
    shape = _norm_shape(shape)
    if shape is None:
        return {"success": False,
                "error": "no shape to slice (the graph produced no result)"}
    axis = str(axis).lower()
    if axis not in _PLANE:
        return {"success": False, "error": f"axis must be x, y or z, not {axis!r}"}
    proj, mkpl = _PROJ[axis], _PLANE[axis]
    try:
        sec = section(shape, section_by=mkpl(float(position)))
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    faces = [] if sec is None else list(sec.faces())
    lines = [f"exact section {axis}={_f(float(position))},"
             f" in-plane coords {_COORDS[axis]}: {len(faces)} loop group(s)"]
    loops = []
    for f in faces:
        wires = [("outer", f.outer_wire())] + [("hole", w) for w in f.inner_wires()]
        for kind, w in wires:
            edges_out = []
            lines.append(f"  {kind}:")
            for e in w.edges():
                gt = _gt(e)
                a, b = proj(e.position_at(0.0)), proj(e.position_at(1.0))
                d = {"type": gt,
                     "from": [round(a[0], 3), round(a[1], 3)],
                     "to": [round(b[0], 3), round(b[1], 3)]}
                txt = f"    {gt} {_pt(a)}->{_pt(b)}"
                if gt == "CIRCLE":
                    try:
                        c = proj(e.arc_center)
                        d["r"] = round(e.radius, 3)
                        d["center"] = [round(c[0], 3), round(c[1], 3)]
                        txt += f" r={_f(e.radius)} c={_pt(c)}"
                    except Exception:
                        pass
                edges_out.append(d)
                lines.append(txt)
            loops.append({"kind": kind, "edges": edges_out})
    if not loops:
        lines.append("  (empty)")
    return {"success": True, "axis": axis, "position": float(position),
            "loops": loops, "text": "\n".join(lines)}


def summarize(shape, n_per_axis: int = 10) -> dict:
    """The one entry point: shape -> {'text': symbolic summary, 'axes': runs,
    bbox/volume checksum}. `shape` may be a single Shape, a list (fan-out), or
    an STL file path (mesh pipeline — OCCT sections segfault on mesh Faces)."""
    if isinstance(shape, str):          # a file path -> the mesh pipeline
        return summarize_stl(shape, n_per_axis)
    shape = _norm_shape(shape)
    if shape is None:
        return {"success": False,
                "error": "no shape to slice (the graph produced no result)"}

    from .mesh_extractor import _geom_bbox   # never the triangulation
    bb = _geom_bbox(shape)
    size = (bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z)
    try:
        volume = round(shape.volume, 2)
    except Exception:
        volume = None
    try:
        n_solids = len(shape.solids())
    except Exception:
        n_solids = None

    lines = [
        "part: bbox x[{}..{}] y[{}..{}] z[{}..{}] size {}x{}x{}, volume {}, solids {}".format(
            _f(bb.min.X), _f(bb.max.X), _f(bb.min.Y), _f(bb.max.Y),
            _f(bb.min.Z), _f(bb.max.Z), _f(size[0]), _f(size[1]), _f(size[2]),
            volume if volume is not None else "?",
            n_solids if n_solids is not None else "?"),
    ]
    axes: dict[str, list] = {}
    spans = {"z": (bb.min.Z, bb.max.Z), "x": (bb.min.X, bb.max.X),
             "y": (bb.min.Y, bb.max.Y)}
    for axis in ("z", "x", "y"):
        lo, hi = spans[axis]
        if hi - lo < 1e-6:
            lines.append(f"{axis.upper()} stack: (flat along {axis})")
            axes[axis] = []
            continue
        runs = _stack(shape, axis, lo, hi, n_per_axis)
        axes[axis] = runs
        lines.append(f"{axis.upper()} stack ({n_per_axis} slices, in-plane coords {_COORDS[axis]}):")
        for r in runs:
            span = (f"{axis}={_f(r['from'])}" if r["count"] == 1
                    else f"{axis}={_f(r['from'])}..{_f(r['to'])} ({r['count']}x)")
            lines.append(f"  {span}: {r['text']}")

    return {"success": True,
            "bbox": {"x": [round(bb.min.X, 3), round(bb.max.X, 3)],
                     "y": [round(bb.min.Y, 3), round(bb.max.Y, 3)],
                     "z": [round(bb.min.Z, 3), round(bb.max.Z, 3)]},
            "size": [round(s, 3) for s in size],
            "volume": volume, "solids": n_solids,
            "axes": axes, "text": "\n".join(lines)}
