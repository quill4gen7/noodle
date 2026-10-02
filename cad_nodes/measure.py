"""
measure — geometry FACTS about node outputs, asked from inside the graph.

The questions an agent (or a person) keeps asking of a design and used to answer
with throw-away build123d scripts: how big is this part and is it ONE valid
solid (props), do these two parts collide and by how much (interference), how
far apart are they (distance), what does a cut at depth d look like (section),
is this point inside the material (probe). Each op takes node references, not
files, so the facts are always about the live graph.

Runs ONLY inside the execution worker (needs build123d), like slice_summary and
mesh_extractor: `executor.measure_graph` appends an epilogue to the transpiled
program that resolves each reference to the node's runtime value and calls
`run_queries`. The build123d-free half (reference parsing, validation) is
`parse_ref` / `refs_of`, importable anywhere.

References: "n5" (the node's first output), "n51.body" (a named output, e.g. a
CodeBlock `#@out`), "n51[3]" / "n51.parts[3]" (one item of a list value).
Every number is rounded (3 decimals) so the JSON stays compact.
"""

from __future__ import annotations

import itertools
import math
import re

_REF = re.compile(r"^(?P<node>[^.\[\]\s]+)(?:\.(?P<out>\w+))?(?:\[(?P<idx>-?\d+)\])?$")
OPS = ("props", "interference", "distance", "section", "probe", "summary")
_PAIR_MAX = 40          # items in a pairwise interference sweep (780 booleans)


# --- references (pure Python) ----------------------------------------------
def parse_ref(ref: str) -> dict:
    """'n51.body[2]' -> {"node": "n51", "out": "body", "idx": 2}."""
    m = _REF.match(str(ref or "").strip())
    if not m:
        raise ValueError(f"bad node reference {ref!r} (use n5, n5.out, n5[2])")
    idx = m.group("idx")
    return {"node": m.group("node"), "out": m.group("out"),
            "idx": int(idx) if idx is not None else None}


def refs_of(query: dict) -> list[str]:
    """Every node reference a query names (validated), in order."""
    op = query.get("op")
    if op not in OPS:
        raise ValueError(f"unknown op {op!r}; choose from {', '.join(OPS)}")
    if op in ("interference", "distance") and query.get("nodes"):
        refs = list(query["nodes"])
    elif op in ("interference", "distance") and query.get("a") and query.get("b"):
        refs = [query["a"], query["b"]]
    else:
        refs = [query.get("node") or query.get("a")]
    if not all(refs):
        raise ValueError(f"{op}: name the node(s) — 'node', 'a'+'b' or 'nodes'")
    for r in refs:
        parse_ref(r)
    return refs


def _base_ref(ref: str) -> str:
    """The value a reference reads before indexing: 'n51.body[2]' -> 'n51.body'."""
    p = parse_ref(ref)
    return p["node"] + (f".{p['out']}" if p["out"] else "")


# --- rounding ----------------------------------------------------------------
def _r(v, nd: int = 3):
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        v = round(v, nd)
        return 0.0 if v == 0 else v
    if isinstance(v, dict):
        return {k: _r(x, nd) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_r(x, nd) for x in v]
    return v


def _vec(p):
    return [p.X, p.Y, p.Z]


# --- shapes (worker only) ----------------------------------------------------
def _items(value) -> list:
    """Flatten a runtime value into its build123d Shapes (lists, ShapeLists,
    CodeBlock output dicts). Non-shapes (numbers, None) are dropped."""
    from build123d import Shape
    out = []
    if value is None:
        return out
    if isinstance(value, Shape):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)) or type(value).__name__ == "ShapeList":
        for v in value:
            out.extend(_items(v))
    return out


def _one(value, ref: str = "?"):
    """A single Shape for a value: itself, or a Compound of its items."""
    from build123d import Compound
    items = _items(value)
    if not items:
        raise ValueError(f"{ref}: no geometry (value is {type(value).__name__})")
    return items[0] if len(items) == 1 else Compound(children=items)


def _bbox(shape) -> dict:
    bb = shape.bounding_box()
    return {"min": _vec(bb.min), "max": _vec(bb.max),
            "size": [bb.max.X - bb.min.X, bb.max.Y - bb.min.Y, bb.max.Z - bb.min.Z]}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a fact we cannot compute is reported as None
        return default


def props(shape) -> dict:
    """bbox / volume / area / centre of mass / validity / topology counts.
    `shells` > `solids` or `solids` > 1 on what should be ONE printed part is
    the classic 'split across two shells' bug; `valid: false` is a broken BRep."""
    from build123d import CenterOf
    d = {"bbox": _bbox(shape),
         "volume": _safe(lambda: shape.volume),
         "area": _safe(lambda: shape.area),
         "center": _safe(lambda: _vec(shape.center(CenterOf.MASS))),
         "valid": _safe(lambda: bool(shape.is_valid)),
         "solids": _safe(lambda: len(shape.solids()), 0),
         "shells": _safe(lambda: len(shape.shells()), 0),
         "faces": _safe(lambda: len(shape.faces()), 0)}
    if not d["solids"]:
        d.pop("volume")
    return d


def _common(a, b):
    """(volume, bbox|None) of the overlap of a and b."""
    inter = a & b
    if isinstance(inter, list):
        inter = _one(inter) if inter else None
    vol = _safe(lambda: inter.volume, 0.0) if inter is not None else 0.0
    if not vol or vol < 1e-6:
        return 0.0, None
    return vol, _bbox(inter)


def interference(a, b) -> dict:
    """Overlap between two parts: the intersection volume (0 = no clash) and the
    bbox of the overlapping material (WHERE they collide)."""
    vol, bb = _common(a, b)
    out = {"volume": vol, "clash": vol > 1e-6}
    if bb:
        out["bbox"] = bb
    else:
        out["distance"] = _safe(lambda: a.distance_to(b))   # 0 = touching
    return out


def distance(a, b) -> dict:
    """Minimum distance between two shapes and the two closest points."""
    d, pa, pb = a.distance_to_with_closest_points(b)
    return {"distance": d, "at_a": _vec(pa), "at_b": _vec(pb)}


def _svg(faces, proj) -> str:
    """A compact SVG of section faces in in-plane coords (y flipped up)."""
    paths, xs, ys = [], [], []
    for f in faces:
        d = []
        for w in [f.outer_wire()] + list(f.inner_wires()):
            pts = []
            for e in w.edges():
                n = 1 if _gt(e) == "LINE" else 24
                seg = [proj(e.position_at(i / n)) for i in range(n + 1)]
                if pts:   # keep the chain continuous: flip an edge that runs backwards
                    last = pts[-1]
                    if (math.dist(last, seg[-1]) < math.dist(last, seg[0])):
                        seg.reverse()
                pts.extend(seg if not pts else seg[1:])
            if len(pts) < 2:
                continue
            xs += [p[0] for p in pts]
            ys += [p[1] for p in pts]
            d.append("M" + " L".join(f"{p[0]:.3g},{-p[1]:.3g}" for p in pts) + " Z")
        if d:
            paths.append(f'<path d="{" ".join(d)}"/>')
    if not paths:
        return ""
    x0, x1, y0, y1 = min(xs), max(xs), -max(ys), -min(ys)
    pad = 0.05 * max(x1 - x0, y1 - y0, 1e-3)
    vb = f"{x0 - pad:.3g} {y0 - pad:.3g} {x1 - x0 + 2 * pad:.3g} {y1 - y0 + 2 * pad:.3g}"
    sw = max(x1 - x0, y1 - y0) / 300
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}">'
            f'<g fill="#8ab4f8" fill-opacity="0.35" fill-rule="evenodd" '
            f'stroke="#1a73e8" stroke-width="{sw:.3g}">' + "".join(paths) + "</g></svg>")


def _gt(edge) -> str:
    g = edge.geom_type
    g = g() if callable(g) else g
    return getattr(g, "name", str(g)).upper()


def section_stats(shape, axis: str = "z", offset: float = 0.0,
                  svg: bool = False) -> dict:
    """ONE cut at axis=offset: how many regions and holes, their area,
    perimeter and in-plane bbox (+ an SVG when asked). The numeric companion of
    slice_summary.outline — use `outline: true` for the edge-by-edge listing."""
    from build123d import section

    from .slice_summary import _COORDS, _PLANE, _PROJ
    axis = str(axis).lower()
    if axis not in _PLANE:
        raise ValueError(f"axis must be x, y or z, not {axis!r}")
    proj = _PROJ[axis]
    sec = section(shape, section_by=_PLANE[axis](float(offset)))
    faces = [] if sec is None else list(sec.faces())
    regions = []
    xs, ys = [], []
    for f in faces:
        bb = f.bounding_box()
        lo, hi = proj(bb.min), proj(bb.max)
        xs += [lo[0], hi[0]]
        ys += [lo[1], hi[1]]
        regions.append({"area": f.area, "holes": len(f.inner_wires()),
                        "perimeter": f.outer_wire().length,
                        "bbox2d": [lo[0], lo[1], hi[0], hi[1]]})
    regions.sort(key=lambda r: -r["area"])
    out = {"axis": axis, "offset": float(offset), "coords": _COORDS[axis],
           "regions": len(regions), "holes": sum(r["holes"] for r in regions),
           "area": sum(r["area"] for r in regions), "detail": regions[:20]}
    if regions:
        out["bbox2d"] = [min(xs), min(ys), max(xs), max(ys)]
    if svg and faces:
        out["svg"] = _svg(faces, proj)
    return out


def probe(shape, points) -> list[dict]:
    """Is each point inside the material? state = in | on | out (per the OCCT
    solid classifier over every solid of the shape)."""
    from build123d import Vector
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN, TopAbs_ON
    solids = list(shape.solids()) or [shape]
    out = []
    for p in points:
        v = Vector(*p)
        state = "out"
        for s in solids:
            c = BRepClass3d_SolidClassifier(s.wrapped, v.to_pnt(), 1e-6)
            st = c.State()
            if st == TopAbs_IN:
                state = "in"
                break
            if st == TopAbs_ON:
                state = "on"
        out.append({"point": list(p), "state": state})
    return out


# --- dispatcher ----------------------------------------------------------------
def _resolve(vals: dict, ref: str, errors: dict):
    p = parse_ref(ref)
    base = _base_ref(ref)
    if base not in vals:
        raise ValueError(f"{ref}: unknown node/output (not a top-level node output)")
    v = vals[base]
    if v is None:
        err = (errors or {}).get(p["node"])
        raise ValueError(f"{ref}: no value" + (f" — node error: {err}" if err else ""))
    if p["idx"] is not None:
        items = v if isinstance(v, (list, tuple)) else _items(v)
        try:
            v = items[p["idx"]]
        except IndexError:
            raise ValueError(f"{ref}: index out of range (len {len(items)})") from None
    return v


def _labelled(vals, refs, errors) -> list[tuple[str, object]]:
    """(label, Shape) pairs. ONE reference to a list value expands into its
    items (n51[0], n51[1], …) so a pairwise sweep covers a whole assembly."""
    if len(refs) == 1:
        v = _resolve(vals, refs[0], errors)
        if isinstance(v, dict):
            return [(f"{refs[0]}.{k}", _one(x, k)) for k, x in v.items()
                    if _items(x)]
        if isinstance(v, (list, tuple)):
            return [(f"{refs[0]}[{i}]", _one(x)) for i, x in enumerate(v) if _items(x)]
        return [(refs[0], _one(v, refs[0]))]
    return [(r, _one(_resolve(vals, r, errors), r)) for r in refs]


def _pairwise(vals, refs, errors, fn, keep) -> dict:
    parts = _labelled(vals, refs, errors)
    if len(parts) > _PAIR_MAX:
        raise ValueError(f"{len(parts)} items: too many for a pairwise sweep "
                         f"(max {_PAIR_MAX}) — name the pairs")
    pairs = []
    for (la, a), (lb, b) in itertools.combinations(parts, 2):
        d = fn(a, b)
        if keep(d):
            pairs.append({"a": la, "b": lb, **d})
    return {"items": [lbl for lbl, _ in parts], "checked":
            len(parts) * (len(parts) - 1) // 2, "pairs": pairs}


def _one_query(vals: dict, q: dict, errors: dict):
    op = q.get("op")
    refs = refs_of(q)
    if op == "props":
        v = _resolve(vals, refs[0], errors)
        d = props(_one(v, refs[0]))
        if q.get("each") and isinstance(v, (list, tuple, dict)):
            d["each"] = [{"item": lbl, **props(s)}
                         for lbl, s in _labelled(vals, refs, errors)]
        return d
    if op == "interference":
        if len(refs) == 2 and not q.get("nodes"):
            a, b = (_one(_resolve(vals, r, errors), r) for r in refs)
            return interference(a, b)
        # a list of nodes (or ONE list-valued node): every pair; only clashes
        # are listed unless all=true.
        return _pairwise(vals, refs, errors, interference,
                         lambda d: q.get("all") or d["clash"])
    if op == "distance":
        if len(refs) == 2 and not q.get("nodes"):
            a, b = (_one(_resolve(vals, r, errors), r) for r in refs)
            return distance(a, b)
        return _pairwise(vals, refs, errors, distance, lambda d: True)
    shape = _one(_resolve(vals, refs[0], errors), refs[0])
    if op == "section":
        d = section_stats(shape, q.get("axis", "z"), float(q.get("offset", 0.0)),
                          bool(q.get("svg")))
        if q.get("outline"):
            from .slice_summary import outline
            d["outline"] = outline(shape, d["axis"], d["offset"]).get("text")
        return d
    if op == "probe":
        pts = q.get("points") or ([q["point"]] if q.get("point") else None)
        if not pts:
            raise ValueError("probe: give 'point': [x,y,z] or 'points': [[x,y,z], …]")
        return {"probes": probe(shape, pts)}
    if op == "summary":
        from .slice_summary import summarize
        n = max(2, min(int(q.get("n", 10)), 40))
        d = summarize(shape, n)
        return {k: d[k] for k in ("bbox", "size", "volume", "solids", "text") if k in d}
    raise ValueError(f"unknown op {op!r}")


def run_queries(vals: dict, queries: list, errors: dict | None = None) -> dict:
    """Answer every query against `vals` ({reference: runtime value}). One
    failing query never hides the others: it carries its own `error`."""
    results = []
    for q in queries:
        head = {k: q[k] for k in ("op", "node", "a", "b", "nodes") if k in q}
        try:
            results.append({**head, **_r(_one_query(vals, q, errors or {}))})
        except Exception as e:  # noqa: BLE001 — reported per query
            results.append({**head, "error": f"{type(e).__name__}: {e}"})
    return {"success": True, "results": results}
