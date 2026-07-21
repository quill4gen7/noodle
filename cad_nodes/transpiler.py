"""
Transpiler: Graph -> build123d Python source.

Primary path is *algebra mode*: a flat sequence of assignments
`__out_N = <code_template>` in topological order, with placeholders replaced by
upstream variables (inputs) and formatted parameter values.

Group nodes (BuildPart/BuildSketch) emit nested `with ... as __ctx_N:` blocks;
their children are emitted with the *builder* template, indented inside the
block.

The generated module exposes `__result__` (the shape to preview) and
`__panels__` (a dict of Panel-node values for inspection).
"""

from __future__ import annotations

import hashlib
import re

from . import catalog
from .graph import Graph
from .toposort import toposort

_PLACEHOLDER = re.compile(r"\{(\w+)(![rs])?\}")

# Invisible sentinels used only by the instrumented transpile (run(emit_map=True)).
# A parameter value that lands in the generated source is wrapped as
#   <A><span-id><SEP><literal><B>
# so that after the whole program is built we can scan the FINAL text, measure
# each value's (row, col) — immune to indentation / group nesting / fan-out
# wrapping — strip the sentinels, and emit a param<->code source map. These are
# NUL-class control chars that never occur in real Python source.
_SP_A = "\x00"
_SP_SEP = "\x1f"
_SP_B = "\x01"
_SP_RE = re.compile(_SP_A + r"(\d+)" + _SP_SEP + r"([^" + _SP_B + r"]*)" + _SP_B)

# A `#@param` annotation on a CodeBlock declaration line, e.g.
#   teeth = 12   #@param int min=6 max=40
#   mode  = "a"  #@param select=a,b,c
_CB_PARAM = re.compile(r"#@param\b(.*)$")


def _parse_annot(annot: str) -> dict:
    """Parse the `#@param ...` tail into {type?, min?, max?, step?, options?}."""
    spec: dict = {}
    for tok in annot.strip().split():
        if "=" in tok:
            key, _, val = tok.partition("=")
            key, val = key.strip(), val.strip()
            if key in ("min", "max", "step"):
                try:
                    spec[key] = float(val)
                except ValueError:
                    pass
            elif key in ("select", "options"):
                opts = [o for o in val.split(",") if o]
                if opts:
                    spec["options"] = opts
                    spec.setdefault("type", "select")
        elif tok in ("int", "float", "bool", "str", "select"):
            spec["type"] = tok
    return spec


def _parse_cb_line(line: str) -> dict | None:
    """A single `name = <literal>  #@param ...` declaration -> a param schema,
    or None. Uses `ast.literal_eval` on the RHS literal only — never `exec`."""
    import ast
    m = _CB_PARAM.search(line)
    if not m:
        return None
    code_part = line[: m.start()]
    if "=" not in code_part:
        return None
    name, _, rhs = code_part.partition("=")
    name, rhs = name.strip(), rhs.strip()
    if not name.isidentifier():
        return None
    try:
        default = ast.literal_eval(rhs)
    except Exception:
        return None
    spec = _parse_annot(m.group(1))
    kind = spec.get("type")
    if kind is None:
        if isinstance(default, bool):
            kind = "bool"
        elif isinstance(default, int):
            kind = "int"
        elif isinstance(default, float):
            kind = "float"
        else:
            kind = "str"
    return {"name": name, "type": kind, "default": default,
            "min": spec.get("min"), "max": spec.get("max"),
            "step": spec.get("step"), "options": spec.get("options")}


def parse_codeblock_params(code: str) -> list[dict]:
    """Every `#@param` declaration in a CodeBlock's source, in order."""
    out, seen = [], set()
    for line in (code or "").splitlines():
        decl = _parse_cb_line(line)
        if decl and decl["name"] not in seen:
            seen.add(decl["name"])
            out.append(decl)
    return out


def _cb_literal(value, kind: str) -> str:
    """Render a CodeBlock param override value as a Python source literal."""
    try:
        if kind == "bool":
            return "True" if value else "False"
        if kind == "int":
            return str(int(value))
        if kind == "float":
            return repr(float(value))
    except (TypeError, ValueError):
        pass
    return repr(value)


def _annot(node) -> str:
    """Trailing marker so a runtime traceback line maps back to its node.

    Parsed by the executor (see `_NODE_MARK`); kept as a Python comment so it
    never affects execution.
    """
    return f"  # @node:{node.id} ({node.type})"

PREAMBLE = """\
import copy as _copy
import json as _json
import math
import os
import random
from time import perf_counter as _perf

from build123d import *

# --- runtime helpers (injected by the transpiler) ---
__panels__ = {}
__previews__ = {}
__errors__ = {}
__timings__ = {}     # per-node wall-clock seconds, keyed by node id
__hashes__ = {}      # node id -> content key (memo mode; drives the mesh cache)
__cached__ = {}      # node id -> True when the memo store served it (no re-run)

# Persistent memo store. The warm worker injects __MEMO__ (a plain dict that
# survives across runs) into the script globals; a cold subprocess has none, so
# every _memo_get misses and the graph executes exactly as before. Entries are
# content-addressed (see Transpiler._memo_plan), so a param edit re-runs ONLY
# the dirty subtree — everything upstream/sideways is restored from the cache.
try:
    __MEMO__
except NameError:
    __MEMO__ = None

_MEMO_CAP = 256      # LRU entries (node outputs + preview meshes + views)

# Live progress. The executor points __PROGRESS_PATH__ at a per-run NDJSON file
# (see executor.build_script) and the editor tails it WHILE the run is still going,
# so nodes light up as they execute instead of being replayed afterwards. It is the
# only channel that works on BOTH paths: the warm worker redirects stdout into a
# buffer during exec, and the cold subprocess has no pipe home at all. No path set
# (the /ui code view, tests) = every _ev is a no-op.
try:
    __PROGRESS_PATH__
except NameError:
    __PROGRESS_PATH__ = None

_PROG_F = None


def _ev(_kind, _nid, _dt=None, _cached=False, _err=None):
    global _PROG_F
    if not __PROGRESS_PATH__:
        return
    try:
        if _PROG_F is None:
            _PROG_F = open(__PROGRESS_PATH__, "a")
        _e = {"k": _kind, "n": _nid}
        if _dt is not None:
            _e["t"] = round(_dt, 4)
        if _cached:
            _e["c"] = True
        if _err:
            _e["x"] = True
        _PROG_F.write(_json.dumps(_e) + "\\n")
        _PROG_F.flush()      # the tailer reads this file while we're still running
    except Exception:
        pass                 # progress is a nicety: never let it break a run


def _memo_get(_k):
    if __MEMO__ is None:
        return None
    _v = __MEMO__.get(_k)
    if _v is not None:
        __MEMO__.pop(_k, None)   # LRU touch (dict keeps insertion order)
        __MEMO__[_k] = _v
    return _v


def _memo_put(_k, _v):
    if __MEMO__ is None:
        return
    while len(__MEMO__) >= _MEMO_CAP:
        __MEMO__.pop(next(iter(__MEMO__)))
    __MEMO__[_k] = _v


def _out(_path):
    \"\"\"Resolve an Export node's path into the project's exports/ folder.
    The worker runs with cwd = the project dir, so exports land in
    projects/<name>/exports/. The path is sandboxed to a basename (any
    directory components / traversal are stripped) so an Export node can
    never write outside its own project. Feeds the global file library.\"\"\"
    _name = os.path.basename(str(_path).strip()) or "output"
    os.makedirs("exports", exist_ok=True)
    return os.path.join("exports", _name)


def _panel(_id, _value, _text="", _mode="friendly"):
    \"\"\"Dual-mode Panel. When a value is wired it DISPLAYS it (pass-through);
    when nothing is wired it is a data SOURCE that parses its own text into
    value(s) on the wire. Multi-line text -> a list (fans out downstream).\"\"\"
    if _value is not None:
        __panels__[_id] = _value
        return _value
    _parsed = _panel_parse(_text, _mode)
    __panels__[_id] = _parsed
    return _parsed


def _probe(_id, _value):
    \"\"\"Typed container / legend: record the value for the inspector (Panels tab)
    and pass it through UNCHANGED. One per wire type — colours the wire and lets
    you label/inspect what flows, without altering the data. (Cast/transform role
    comes later — see PLAN_DATA_PROTOCOL.md.)\"\"\"
    __panels__[_id] = _value
    return _value


def _panel_one(_line, _mode):
    \"\"\"Parse a single line into a value per the chosen syntax.\"\"\"
    import json as _json
    _s = _line.strip()
    if _mode == "build123d":
        return eval(_s, globals())              # build123d expression (arbitrary)
    if _mode == "json":
        return _json.loads(_s)
    # friendly: JSON scalar/array first, else "x, y, z" numbers -> Vector, else str
    try:
        return _json.loads(_s)
    except Exception:
        pass
    import re as _re
    _parts = [p for p in _re.split(r"[,\\s]+", _s.strip("()[] ")) if p]
    try:
        _nums = [float(p) for p in _parts]
    except ValueError:
        return _s
    if len(_nums) == 3:
        return Vector(*_nums)
    if len(_nums) == 2:
        return Vector(_nums[0], _nums[1], 0)
    if len(_nums) == 1:
        return _nums[0]
    return _nums


def _panel_parse(_text, _mode="friendly"):
    \"\"\"Text -> value(s). One item per non-empty line; >1 line -> a list.\"\"\"
    if not _text or not str(_text).strip():
        return None
    if _mode == "json":
        import json as _json
        try:
            return _json.loads(_text)           # whole block as one JSON document
        except Exception:
            pass
    _lines = [ln for ln in str(_text).splitlines() if ln.strip()]
    if not _lines:
        return None
    _vals = []
    for _ln in _lines:
        try:
            _vals.append(_panel_one(_ln, _mode))
        except Exception as _e:
            _vals.append("⚠ " + str(_e))
    return _vals[0] if len(_vals) == 1 else _vals


def _subshape_fingerprint(_s, _kind):
    \"\"\"A pick's identity beyond position: (anchor, size, orient) — mirrors the
    layout the picker stores in `sig`. size = edge length / face area; orient =
    edge tangent / face normal (unit). Vertices carry only an anchor.\"\"\"
    try:
        if _kind == "edge":
            _m, _d = _s @ 0.5, _s % 0.5
            _o = _d if _d.length == 0 else _d.normalized()
            return ((_m.X, _m.Y, _m.Z), float(_s.length), (_o.X, _o.Y, _o.Z))
        if _kind == "face":
            _c = _s.center()
            _n = _s.normal_at(_c)
            _o = _n if _n.length == 0 else _n.normalized()
            return ((_c.X, _c.Y, _c.Z), float(_s.area), (_o.X, _o.Y, _o.Z))
        if _kind == "shape":
            # a WHOLE object picked from a list: bbox centre + a size metric
            # (volume, else area, else length) — universal across solids/faces/curves.
            _bb = _s.bounding_box()
            _c = (_bb.min + _bb.max) * 0.5
            _sz = 0.0
            for _attr in ("volume", "area", "length"):
                try:
                    _v = float(getattr(_s, _attr))
                    if _v:
                        _sz = _v
                        break
                except Exception:
                    pass
            return ((_c.X, _c.Y, _c.Z), _sz, None)
        return ((_s.X, _s.Y, _s.Z), None, None)
    except Exception:
        return None


def _select_subshapes(_shape, _kind, _indices, _sigs, _nid=None):
    \"\"\"Resolve a picked sub-shape set against a (possibly recomputed) shape.

    Matches each stored pick by a FINGERPRINT — position + size (length/area) +
    orientation (tangent/normal), not position alone — with a confidence gate:
    if the best candidate is clearly not the same feature (moved far AND changed
    size/orientation), the pick is left UNRESOLVED and a warning is recorded in
    __errors__[_nid] ("re-pick") instead of silently grabbing the nearest shape.
    Survives OCC re-ordering and honest parameter tweaks; flags real breakage.
    Falls back to raw indices when no signatures were stored.\"\"\"
    if _shape is None:
        return ShapeList([])
    if _kind == "shape":
        # Pick WHOLE objects from a list: the pickable units are the list items
        # themselves (a Compound is exploded into its children), NOT decomposed.
        if _is_seq(_shape):
            subs = [s for s in _flatten(list(_shape)) if s is not None]
        else:
            try:
                _ch = list(_shape.children) if getattr(_shape, "children", None) else []
            except Exception:
                _ch = []
            subs = _ch or [_shape]
    else:
        # Multi-piece input (a list / ShapeList of solids — e.g. from a Geometry
        # container or a fanned upstream): merge into ONE Compound so faces/edges/
        # vertices enumerate across ALL pieces, matching the picker's enumeration
        # (mesh_extractor._as_shape does the same). Without this, `.faces()` on a
        # bare list is missing and every pick reads as "stale".
        if _is_seq(_shape):
            _pieces = [s for s in _flatten(list(_shape)) if s is not None]
            if not _pieces:
                return ShapeList([])
            try:
                _shape = Compound(children=_pieces)
            except Exception:
                _shape = _pieces[0]
        _get = {"edge": getattr(_shape, "edges", None),
                "face": getattr(_shape, "faces", None),
                "vertex": getattr(_shape, "vertices", None)}.get(_kind)
        subs = list(_get()) if _get else []
    fps = [_subshape_fingerprint(s, _kind) for s in subs]

    # part scale, to judge "moved far" relative to the model (not absolute mm)
    _xs = [fp[0][0] for fp in fps if fp] or [0.0]
    _ys = [fp[0][1] for fp in fps if fp] or [0.0]
    _zs = [fp[0][2] for fp in fps if fp] or [0.0]
    _diag = max(((max(_xs) - min(_xs)) ** 2 + (max(_ys) - min(_ys)) ** 2 +
                 (max(_zs) - min(_zs)) ** 2) ** 0.5, 1e-6)
    _THRESH = 0.45                        # accept a match below this combined cost
    #                                       (margin so honest tweaks don't false-flag)

    def _cost(fp, want):
        (ca, cs, co) = fp
        wa = (want[0], want[1], want[2])
        dp = ((ca[0] - wa[0]) ** 2 + (ca[1] - wa[1]) ** 2 + (ca[2] - wa[2]) ** 2) ** 0.5 / _diag
        ss = 0.0
        if cs is not None and len(want) >= 4 and want[3]:
            ss = abs(cs - want[3]) / max(abs(want[3]), 1e-6)
        oo = 0.0
        if co is not None and len(want) >= 7:
            _wl = (want[4] ** 2 + want[5] ** 2 + want[6] ** 2) ** 0.5 or 1.0
            _dot = (co[0] * want[4] + co[1] * want[5] + co[2] * want[6]) / _wl
            oo = 1.0 - min(abs(_dot), 1.0)         # tangent/normal sign is ambiguous
        return dp * 1.0 + min(ss, 1.0) * 0.6 + oo * 0.5

    chosen, used, _unresolved = [], set(), 0
    for want in (_sigs or []):
        best, best_c = None, None
        for j, fp in enumerate(fps):
            if j in used or fp is None:
                continue
            c = _cost(fp, want)
            if best_c is None or c < best_c:
                best_c, best = c, j
        if best is not None and best_c is not None and best_c <= _THRESH:
            used.add(best)
            chosen.append(subs[best])
        else:
            _unresolved += 1
    if _unresolved and _nid is not None:
        __errors__[_nid] = (f"selection stale: {_unresolved} of {len(_sigs)} picked "
                            f"{_kind}(s) no longer match (geometry changed) — re-pick")
    if not _sigs:
        for idx in (_indices or []):
            if 0 <= idx < len(subs) and idx not in used:
                used.add(idx)
                chosen.append(subs[idx])
    return ShapeList(chosen)


def _faces_by_normal(_shape, _axis="Z", _sign="+", _tol=0.1):
    \"\"\"PREDICATE selector: faces whose outward normal aligns with a world axis.
    +Z is 'the top face(s)', -Z the bottom, 'both' either. Re-evaluated against
    the CURRENT geometry, so it survives parameter changes that move the face —
    unlike a picked (positional) selection. Returns a ShapeList (fed to a face op
    or a modifier).\"\"\"
    if _shape is None:
        return ShapeList([])
    _faces = _shape.faces() if hasattr(_shape, "faces") else ShapeList([])
    _ax = {"X": Vector(1, 0, 0), "Y": Vector(0, 1, 0), "Z": Vector(0, 0, 1)}.get(_axis, Vector(0, 0, 1))
    _out = []
    for _f in _faces:
        try:
            _d = _f.normal_at().dot(_ax)
        except Exception:
            continue
        if (_sign == "+" and _d > 1 - _tol) or \
           (_sign == "-" and _d < -(1 - _tol)) or \
           (_sign == "both" and abs(_d) > 1 - _tol):
            _out.append(_f)
    return ShapeList(_out)


def _subs(_shape, _kind):
    \"\"\"The edge/face/vertex sub-shapes of a shape as a ShapeList (empty if none).\"\"\"
    if _shape is None:
        return ShapeList([])
    _g = {"edge": getattr(_shape, "edges", None),
          "face": getattr(_shape, "faces", None),
          "vertex": getattr(_shape, "vertices", None)}.get(_kind)
    try:
        return _g() if _g else ShapeList([])
    except Exception:
        return ShapeList([])


def _edges_by_type(_shape, _type="circle"):
    \"\"\"PREDICATE: edges of a given geometry type — 'circle' picks every hole/round.\"\"\"
    _gt = {"circle": GeomType.CIRCLE, "line": GeomType.LINE,
           "ellipse": GeomType.ELLIPSE, "spline": GeomType.BSPLINE}.get(_type, GeomType.CIRCLE)
    try:
        return _subs(_shape, "edge").filter_by(_gt)
    except Exception:
        return ShapeList([])


def _faces_by_type(_shape, _type="plane"):
    \"\"\"PREDICATE: faces of a given surface type (plane/cylinder/sphere/cone/torus).\"\"\"
    _gt = {"plane": GeomType.PLANE, "cylinder": GeomType.CYLINDER,
           "sphere": GeomType.SPHERE, "cone": GeomType.CONE,
           "torus": GeomType.TORUS}.get(_type, GeomType.PLANE)
    try:
        return _subs(_shape, "face").filter_by(_gt)
    except Exception:
        return ShapeList([])


def _by_size(_shape, _kind="face", _metric="area", _pick="largest", _n=1):
    \"\"\"PREDICATE: the N biggest/smallest sub-shapes by a metric (area/length/radius).\"\"\"
    _s = _subs(_shape, _kind)
    if not _s:
        return ShapeList([])
    _sb = {"area": SortBy.AREA, "length": SortBy.LENGTH,
           "radius": SortBy.RADIUS, "volume": SortBy.VOLUME}.get(_metric, SortBy.AREA)
    try:
        _sorted = _s.sort_by(_sb)
    except Exception:
        return ShapeList([])
    _n = max(1, int(_n))
    _low = _pick in ("smallest", "shortest", "min")
    return ShapeList(list(_sorted[:_n] if _low else _sorted[-_n:]))


def _by_position(_shape, _kind="face", _axis="Z", _pick="max", _n=1):
    \"\"\"PREDICATE: the N extreme sub-shapes along an axis — 'the topmost face',
    'the leftmost edges'. Re-derived from position, so stable under param tweaks.\"\"\"
    _s = _subs(_shape, _kind)
    if not _s:
        return ShapeList([])
    _ax = {"X": Axis.X, "Y": Axis.Y, "Z": Axis.Z}.get(_axis, Axis.Z)
    try:
        _sorted = _s.sort_by(_ax)
    except Exception:
        return ShapeList([])
    _n = max(1, int(_n))
    _low = _pick in ("min", "smallest", "first")
    return ShapeList(list(_sorted[:_n] if _low else _sorted[-_n:]))


def _combine_sel(_a, _b, _mode="or"):
    \"\"\"Boolean-combine two selections: or (union), and (intersection),
    subtract (in A, not in B). Sub-shapes are deduped by identity hash.\"\"\"
    _a = list(_a or [])
    _b = list(_b or [])
    if _mode == "or":
        _seen, _out = set(), []
        for _x in _a + _b:
            _h = hash(_x)
            if _h not in _seen:
                _seen.add(_h)
                _out.append(_x)
        return ShapeList(_out)
    _bs = {hash(_x) for _x in _b}
    if _mode == "and":
        return ShapeList([_x for _x in _a if hash(_x) in _bs])
    return ShapeList([_x for _x in _a if hash(_x) not in _bs])


def _origin_points(_o):
    \"\"\"Normalise a vector / vertex / tuple / list / ShapeList into [Vector, ...].\"\"\"
    if _o is None:
        return []
    items = list(_o) if isinstance(_o, (list, tuple)) else [_o]
    out = []
    for it in items:
        if it is None:
            continue
        if hasattr(it, "X") and hasattr(it, "Y") and hasattr(it, "Z"):
            out.append(Vector(it.X, it.Y, it.Z))
        elif isinstance(it, (list, tuple)) and len(it) >= 3:
            out.append(Vector(it[0], it[1], it[2]))
        elif hasattr(it, "center"):
            c = it.center()
            out.append(Vector(c.X, c.Y, c.Z))
    return out


def _deconstruct(_s):
    \"\"\"Explode anything into a flat list of points (Vectors) — the one coherent
    point extractor: a solid/surface/curve -> ALL its vertices; a plane/frame ->
    its origin; a selection / list -> each item's points; a vertex/point/tuple ->
    itself. Output is a list, so downstream fans out.\"\"\"
    if _s is None:
        return []
    out = []
    for it in (_s if _is_seq(_s) else [_s]):
        if it is None:
            continue
        if hasattr(it, "X") and hasattr(it, "Y") and hasattr(it, "Z"):
            out.append(Vector(it.X, it.Y, it.Z))           # already a point / vertex
            continue
        if isinstance(it, (list, tuple)) and len(it) >= 3 and not hasattr(it, "vertices"):
            out.append(Vector(it[0], it[1], it[2]))        # a raw (x, y, z)
            continue
        if hasattr(it, "origin") and not hasattr(it, "vertices"):
            o = it.origin                                  # a plane / frame -> origin
            out.append(Vector(o.X, o.Y, o.Z))
            continue
        verts = None
        try:
            verts = list(it.vertices())                    # a shape -> its vertices
        except Exception:
            verts = None
        if verts:
            for v in verts:
                c = v.center() if hasattr(v, "center") else v
                out.append(Vector(c.X, c.Y, c.Z))
        elif hasattr(it, "center"):
            c = it.center()
            out.append(Vector(c.X, c.Y, c.Z))
    return out


def _explode(_s, _kind):
    \"\"\"Explode shape(s) into their sub-shapes of a given kind: 'edge' -> the
    constituent Edges (curves), 'face' -> the constituent Faces (surfaces). The
    companion to _deconstruct (points). Accepts a single shape or a list /
    selection; returns a flat list, so downstream fans out (one run per edge /
    face). A bare edge/face is passed through unchanged.\"\"\"
    if _s is None:
        return []
    out = []
    for it in (_s if _is_seq(_s) else [_s]):
        if it is None:
            continue
        subs = None
        try:
            subs = list(getattr(it, _kind + "s")())        # .edges() / .faces()
        except Exception:
            subs = None
        if subs:
            out.extend(subs)
    return out


def _at(_shape, _origin):
    \"\"\"Place _shape at _origin. One point -> a moved copy; many points -> a
    Compound with a copy at each (so a Select Vertex can seed N primitives).\"\"\"
    if _shape is None or _origin is None:
        return _shape
    pts = _origin_points(_origin)
    if not pts:
        return _shape
    if _is_mesh(_shape):
        # A mesh has no .moved()/Location and cannot go in a Compound — it is a
        # matrix on the vertex array, exactly as in _mesh_matrix. Same contract:
        # one point moves it, many give a copy at each (concatenated into one).
        copies = [_shape.transformed(_mesh_matrix("move", x=p.X, y=p.Y, z=p.Z))
                  for p in pts]
        return copies[0] if len(copies) == 1 else _mesh_concat(copies)
    if len(pts) == 1:
        return _shape.moved(Pos(pts[0].X, pts[0].Y, pts[0].Z))
    return Compound(children=[_shape.moved(Pos(p.X, p.Y, p.Z)) for p in pts])


def _pushpull(_part, _faces, _amount):
    \"\"\"Push/pull selected faces along their normal: positive grows a boss,
    negative carves a pocket.\"\"\"
    if _part is None or not _faces or not _amount:
        return _part
    prism = extrude(_faces, amount=_amount)
    return (_part + prism) if _amount > 0 else (_part - prism)


def _shell(_part, _thickness):
    \"\"\"Give a part a wall of thickness _thickness.
    - Solid / Compound-of-solids: hollow it out, leaving the top (+Z) face open.
    - Shell / Face (an open surface, e.g. a non-solid Loft): thicken the surface
      into a solid wall — there is no enclosed volume to hollow, so 'openings'
      does not apply (offset() rejects a bare Shell, which used to raise
      'Unsupported type ... Shell').\"\"\"
    if _part is None or not _thickness:
        return _part
    solids = list(_part.solids()) if isinstance(_part, Compound) else \\
             ([_part] if isinstance(_part, Solid) else [])
    if solids:
        top = _part.faces().sort_by(Axis.Z)[-1]
        return offset(_part, amount=-_thickness, openings=top)
    # open surface: thicken it into a solid wall
    surf = _part if isinstance(_part, (Shell, Face)) else _face(_part)
    return _thicken(surf, _thickness)


def _thicken(_surf, _thickness):
    \"\"\"`Solid.thicken` with the two ways it fails made legible.

    build123d ends its thicken with a blind `TopoDS.Solid(...)` cast, but OCCT's
    BRepOffset hands back a SHELL whenever it could not close the wall — so the
    user sees `Standard_TypeMismatch: TopoDS::Solid`, which says nothing. And the
    shell it returns is not salvageable in general: measured on an icosahedron
    minus one face, sewing it back gives volume 483.49 that neither BRepCheck nor
    a tessellation accepts (not watertight, faces missing from the triangulation).

    Sharp dihedral angles between many facets are BRepOffset's weak spot — of the
    platonic solids minus a face, the tetra/cube/dodeca thicken fine and the
    octa/icosa do not, at any tolerance, join mode or thickness (all swept). The
    broken wall is deliberately NOT returned: it renders like a part and is not
    one. Say what happened and what to do instead.

    TRAP, paid for: it thickens a COPY. BRepOffset registers its modifications on
    the input faces' TShapes, so a FAILED thicken poisons them for whatever else
    still holds them — measured: Polyhedron -> Join(all but one face) -> Shell,
    and the sibling Shell By Faces that hollowed the same polyhedron through the
    left-out face then returned volume 4728 on a part of 2536, invalid, instead
    of 432. Same family as the `_reanchor` trap: OCCT hands out shared topology
    and a node must not scribble on what it did not build.\"\"\"
    try:
        _out = Solid.thicken(_copy.deepcopy(_surf), _thickness)
    except Exception as _exc:
        raise ValueError(
            "Shell could not thicken this open surface into a wall — OCCT's "
            "offset choked on it (%s), as it does on sharp angles between many "
            "facets (a polyhedron with a face removed is the classic case). "
            "Hollow the CLOSED solid instead and choose the openings there: "
            "part -> a face selector -> Shell By Faces. That is a hollowing "
            "rather than an offset of a loose surface, and it is exact."
            % type(_exc).__name__)
    if not _out.is_valid:
        raise ValueError(
            "Shell thickened this open surface into an INVALID wall (%d faces, "
            "volume %.2f) — it would render like a part without being one. Use "
            "Shell By Faces on the closed solid instead."
            % (len(_out.faces()), _out.volume))
    return _out


def _shell_faces(_part, _faces, _thickness):
    \"\"\"Hollow a solid to a wall of _thickness, leaving the SELECTED faces open.
    Like _shell but the openings come from a face selector instead of the +Z
    face — pair with FacesByNormal / FacesByType / CombineSelection. An empty
    selection makes a fully closed hollow shell.

    It hollows a CLOSED SOLID — that is the whole operation. Fed an open surface
    (a Join of faces, a Section, a non-solid Loft) `offset()` refuses, and this
    used to swallow the exception and hand BACK THE INPUT: no error, no hollow,
    a node that quietly did nothing. Both cases now say so.\"\"\"
    if _part is None or not _thickness:
        return _part
    _solid = bool(list(_part.solids())) if hasattr(_part, "solids") else False
    if not _solid:
        raise ValueError(
            "Shell By Faces hollows a closed solid, and it got an open surface "
            "(%s). Remove the faces AFTER hollowing, not before: hollow the "
            "closed part here and pick the openings with the `faces` selector. "
            "To give a loose surface a wall thickness instead, use Shell."
            % type(_part).__name__)
    _op = list(_faces) if _faces else []
    try:
        return offset(_part, amount=-_thickness, openings=_op)
    except Exception as _exc:
        raise ValueError(
            "Shell By Faces could not hollow this part to %g (OCCT: %s). A wall "
            "thicker than the part's thinnest feature is the usual cause — try "
            "a smaller thickness." % (_thickness, type(_exc).__name__))


def _bbox_solid(_shape):
    \"\"\"The axis-aligned bounding box of _shape as an actual solid Box (not the
    raw BoundBox data object), seated at the box centre. A flat input (a 2D
    sketch / face — e.g. Text) has a zero-thickness side; that side is given a
    tiny proportional thickness so the result is still a valid, renderable solid
    (a thin slab) instead of failing on a degenerate dimension.\"\"\"
    if _shape is None:
        return None
    bb = _shape.bounding_box()
    sz, c = bb.size, bb.center()
    eps = max(sz.X, sz.Y, sz.Z, 1.0) * 1e-3
    return Box(max(sz.X, eps), max(sz.Y, eps), max(sz.Z, eps)).moved(Pos(c.X, c.Y, c.Z))


def _bbox_plane(_shape, _plane, _t=0.5):
    \"\"\"A reusable Plane, parallel to the chosen base plane (XY/XZ/YZ), centred
    on _shape's bounding box and slid along its own normal to the _t position
    in [0,1] (0 = min side, 0.5 = centre, 1 = max side). This is a first-class
    Plane on the wire — feed it to Section, BuildSketch, or anything that takes
    a plane. Works for any geometry, since the position comes from its box.\"\"\"
    pl = _plane if isinstance(_plane, Plane) else Plane(_plane)
    if _shape is None:
        return pl
    try:
        bb = _shape.bounding_box()
        n = pl.z_dir
        lo, hi = Vector(bb.min), Vector(bb.max)
        c = (lo + hi) * 0.5
        target = lo.dot(n) + (hi.dot(n) - lo.dot(n)) * float(_t)
        pl = Plane(origin=(c.X, c.Y, c.Z), x_dir=pl.x_dir, z_dir=pl.z_dir)
        pl = pl.offset(target - pl.origin.dot(n))
    except Exception:
        pass  # degenerate / empty shape: fall back to the base plane
    return pl


def _section(_shape, _plane=None):
    \"\"\"Planar cross-section of _shape, cut by the Plane wired into _plane
    (e.g. from a Bounding Plane node, which carries both orientation and
    position). Defaults to Plane.XY through the global origin when nothing is
    wired.\"\"\"
    if _shape is None:
        return None
    pl = _plane if isinstance(_plane, Plane) else Plane.XY
    return section(_shape, section_by=pl)


# ===========================================================================
# The mesh lane (PLAN_MESH_LANE.md)
#
# build123d/OCCT cannot model meshes — `import_stl` yields a Face with only a
# triangulation and no surface (booleans on it are refused outright), and
# `Mesher.read` sews every triangle into a planar B-Rep face: 300s to open a
# 147k-triangle part, 81s per boolean. So triangles get their own lane, with
# trimesh as the engine (MIT — noodle stays MIT; see THIRD_PARTY_NOTICES.md).
# ===========================================================================
_TRIMESH = None


def _tm():
    \"\"\"Import trimesh lazily — a graph with no mesh node must not pay for it.\"\"\"
    global _TRIMESH
    if _TRIMESH is None:
        import trimesh as _t
        _TRIMESH = _t
    return _TRIMESH


class Mesh:
    \"\"\"A triangle mesh flowing on a `mesh` wire.

    Wraps a trimesh.Trimesh so the engine stays an implementation detail (it is
    swappable) and so the transforms and the preview extractor can dispatch on a
    named type instead of duck-typing a third-party class.\"\"\"

    # Duck-type marker. mesh_extractor runs as an imported module and cannot see
    # this class (it is defined in the generated script's globals), so it sniffs
    # for this attribute rather than using isinstance.
    _noodle_mesh = True
    # `_noodle_anim` is the Drop timeline riding on a result. It has to be a slot:
    # a build123d Shape takes any attribute, so the B-Rep lane carried the plan for
    # free and nobody noticed that on the mesh lane the assignment was hitting the
    # __slots__ wall and being swallowed by _drop's try/except — a mesh dropped or
    # collided simply never replayed in the browser, silently.
    # `_noodle_extra` rides the same way: the moving `container` bodies, which are
    # NOT outputs of the node but must still be drawn (and animated) in its preview.
    __slots__ = ("tm", "_noodle_anim", "_noodle_extra", "_noodle_owner")

    def __init__(self, tm):
        self.tm = tm

    @property
    def volume(self):
        try:
            return float(self.tm.volume)
        except Exception:
            return 0.0

    @property
    def area(self):
        try:
            return float(self.tm.area)
        except Exception:
            return 0.0

    @property
    def n_tris(self):
        return int(len(self.tm.faces))

    @property
    def watertight(self):
        try:
            return bool(self.tm.is_watertight)
        except Exception:
            return False

    def arrays(self):
        \"\"\"(vertices, triangles) as plain nested lists — what the viewer wants.\"\"\"
        return self.tm.vertices.tolist(), self.tm.faces.tolist()

    def transformed(self, _m):
        \"\"\"Apply a 4x4 matrix to the vertices — this is what a transform IS on a
        mesh, and it is why Move/Rotate/Scale stay ONE node across both lanes.\"\"\"
        t = self.tm.copy()
        t.apply_transform(_m)
        return Mesh(t)

    def __repr__(self):
        return "Mesh(%d tris, watertight=%s)" % (self.n_tris, self.watertight)


def _is_mesh(_x):
    return getattr(_x, "_noodle_mesh", False)


def _mesh_concat(_items):
    ms = [m for m in _items if m is not None]
    if not ms:
        return None
    if len(ms) == 1:
        return ms[0]
    return Mesh(_tm().util.concatenate([m.tm for m in ms]))


def _to_mesh(_x, _tol=None):
    \"\"\"Cast a B-Rep shape onto the mesh lane (the solid->mesh / surface->mesh cast
    in casts.py — applied automatically at the wire boundary). Tessellates with
    build123d, then hands the triangles to trimesh.\"\"\"
    if _x is None or _is_mesh(_x):
        return _x
    if isinstance(_x, (list, tuple)):
        return _mesh_concat([_to_mesh(i, _tol) for i in _x])
    if _tol is None:                       # scale the deflection to the part
        try:
            bb = _x.bounding_box(optimal=False)
            diag = (bb.size.X ** 2 + bb.size.Y ** 2 + bb.size.Z ** 2) ** 0.5
            _tol = max(diag * 0.005, 0.01)
        except Exception:
            _tol = 0.1
    verts, tris = _x.tessellate(float(_tol), 0.3)
    return Mesh(_tm().Trimesh(vertices=[[v.X, v.Y, v.Z] for v in verts],
                              faces=[list(t) for t in tris], process=True))


def _as_mesh(_x):
    \"\"\"Coerce whatever landed on a mesh input to a Mesh: a Mesh passes through, a
    B-Rep shape tessellates, a path loads.\"\"\"
    if _x is None or _is_mesh(_x):
        return _x
    if isinstance(_x, str):
        return _mesh_load(_x)
    return _to_mesh(_x)


def _mesh_load(_path):
    \"\"\"Load an STL/OBJ/PLY/3MF as a Mesh — 0.16s on a 147k-triangle part, where
    build123d's Mesher takes 300s for the same file.\"\"\"
    if not _path:
        return None
    return Mesh(_tm().load(str(_path), force="mesh"))


def _mesh_fix(_mesh, _min_body=16, _fill_holes=True):
    \"\"\"Repair a mesh into something a boolean engine will accept: merge duplicate
    vertices, drop duplicate and degenerate faces, drop stray shards (bodies under
    `_min_body` triangles), fill holes, fix normals.

    This is the node that finds what OCCT cannot even see. The raccordo STL's only
    real defect was ONE stray triangle plus a duplicate face — no holes at all —
    and OCCT reported it `is_valid=True` (PLAN_MESH_LANE.md §0).\"\"\"
    _mesh = _as_mesh(_mesh)
    if _mesh is None:
        return None
    tm = _mesh.tm.copy()
    tm.merge_vertices()
    tm.update_faces(tm.unique_faces())
    tm.update_faces(tm.nondegenerate_faces())
    try:
        bodies = list(tm.split(only_watertight=False))
    except Exception:
        bodies = []
    if len(bodies) > 1:
        keep = [b for b in bodies if len(b.faces) >= int(_min_body)]
        if keep:
            tm = keep[0] if len(keep) == 1 else _tm().util.concatenate(keep)
    if _fill_holes and not tm.is_watertight:
        try:
            _tm().repair.fill_holes(tm)
        except Exception:
            pass
    try:
        _tm().repair.fix_normals(tm)
    except Exception:
        pass
    return Mesh(tm)


def _mesh_inspect(_mesh):
    \"\"\"A text report of a mesh's health — the mesh lane's perception tool (the
    sibling of slice_summary for the retroeng flow). Wire it into a Panel.\"\"\"
    _mesh = _as_mesh(_mesh)
    if _mesh is None:
        return "no mesh"
    tm = _mesh.tm
    try:
        bodies = len(tm.split(only_watertight=False))
    except Exception:
        bodies = 1
    try:
        import numpy as _np
        e = tm.edges_sorted
        _u, _c = _np.unique(e, axis=0, return_counts=True)
        boundary = int((_c == 1).sum())
        nonmanifold = int((_c > 2).sum())
    except Exception:
        boundary = nonmanifold = -1
    lines = [
        "triangles   : %d" % len(tm.faces),
        "vertices    : %d" % len(tm.vertices),
        "watertight  : %s" % bool(tm.is_watertight),
        "bodies      : %d" % bodies,
        "boundary edges     : %d" % boundary,
        "non-manifold edges : %d" % nonmanifold,
        "euler       : %s" % tm.euler_number,
        "volume      : %.3f" % _mesh.volume,
        "area        : %.3f" % _mesh.area,
    ]
    return "\\n".join(lines)


def _mesh_export(_mesh, _path):
    \"\"\"Write a Mesh to STL/OBJ/PLY/3MF (the extension picks the format).\"\"\"
    _mesh = _as_mesh(_mesh)
    if _mesh is None or not _path:
        return None
    _mesh.tm.export(str(_path))
    return _mesh


_MANIFOLD = None


def _mf():
    \"\"\"Import manifold3d lazily (Apache-2.0) — the boolean/simplify engine.\"\"\"
    global _MANIFOLD
    if _MANIFOLD is None:
        import manifold3d as _m
        _MANIFOLD = _m
    return _MANIFOLD


def _to_manifold(_x, _who="this operation"):
    \"\"\"Mesh -> manifold3d.Manifold, with the one error message that matters.

    manifold3d only operates on a closed volume: hand it an open or non-manifold
    mesh and it returns an EMPTY manifold whose status() says why. trimesh's own
    wrapper surfaces that as "Not all meshes are volumes!", which tells the user
    nothing actionable — so we translate it into what to actually do.\"\"\"
    import numpy as _np
    m = _as_mesh(_x)
    if m is None:
        return None
    mf = _mf()
    man = mf.Manifold(mf.Mesh(
        vert_properties=_np.asarray(m.tm.vertices, dtype=_np.float32),
        tri_verts=_np.asarray(m.tm.faces, dtype=_np.uint32)))
    if man.is_empty() and m.n_tris:
        raise ValueError(
            "%s needs a closed (watertight) mesh — manifold3d reports %s. "
            "Put a Mesh Fix node in front of it." % (_who, man.status()))
    return man


def _from_manifold(_man):
    \"\"\"manifold3d.Manifold -> Mesh.

    process=False is load-bearing: manifold3d already returns a welded, valid
    manifold, and trimesh's default processing re-merges its vertices — which
    silently BREAKS it. (Measured: simplify at 1.5mm came back non-watertight
    with process=True and watertight with process=False. Same triangles.)\"\"\"
    if _man is None or _man.is_empty():
        return None
    msh = _man.to_mesh()
    return Mesh(_tm().Trimesh(vertices=msh.vert_properties[:, :3].astype(float),
                              faces=msh.tri_verts, process=False))


def _mesh_bool(_mode, *_items):
    \"\"\"Boolean on the mesh lane (manifold3d): 0.107s on the 147k-triangle raccordo,
    where the same cut through OCCT's B-Rep takes 81s.\"\"\"
    ms = [_as_mesh(i) for i in _flatten(list(_items))]
    ms = [m for m in ms if m is not None]
    if not ms:
        return None
    who = "Mesh " + _mode
    mans = [_to_manifold(m, who) for m in ms]
    if len(mans) == 1:
        return _from_manifold(mans[0])
    mf = _mf()
    op = {"union": mf.OpType.Add, "subtract": mf.OpType.Subtract,
          "intersect": mf.OpType.Intersect}[_mode]
    return _from_manifold(mf.Manifold.batch_boolean(mans, op))


# --- threads (§12d) --------------------------------------------------------
# build123d 0.11 ships NO thread primitive (they live in bd_warehouse, which is
# not a dependency), so the geometry is ours — and it belongs on the MESH lane.
# Measured on a real ISO 68-1 profile: the helical sweep is fast on either lane
# (~0.03s), but fusing the rib to its core through OCCT costs 2-8s and gets it
# WRONG WITHOUT RAISING — M6x1 came back as the bare core (the thread silently
# dropped, volume 227.9 = the cylinder alone) and M20x2.5 came back with volume
# ZERO. Letting the section abut the core instead of overlapping it does not
# even build: StdFail_NotDone, "BRep_API: command not done". manifold3d does the
# same union in 0.01s, watertight, major diameter exact to 4 decimals. Same
# family of trap as the rest of PLAN_MESH_LANE.md. See PLAN_THREADS.md.
_IN_MM = 25.4


def _unified_sizes(_rows):
    \"\"\"Inch families tabulated the way they are actually quoted — nominal
    diameter in inches, threads per inch — and converted ONCE, here, instead of
    by hand into a table nobody can proofread.\"\"\"
    return {n: (d * _IN_MM, _IN_MM / t) for n, (d, t) in _rows.items()}


_THREAD_SIZES = {
    "ISO metric": {                                  # coarse pitch; fine via `pitch`
        "M1.6": (1.6, 0.35), "M2": (2.0, 0.4), "M2.5": (2.5, 0.45),
        "M3": (3.0, 0.5), "M3.5": (3.5, 0.6), "M4": (4.0, 0.7),
        "M5": (5.0, 0.8), "M6": (6.0, 1.0), "M8": (8.0, 1.25),
        "M10": (10.0, 1.5), "M12": (12.0, 1.75), "M14": (14.0, 2.0),
        "M16": (16.0, 2.0), "M18": (18.0, 2.5), "M20": (20.0, 2.5),
        "M22": (22.0, 2.5), "M24": (24.0, 3.0), "M27": (27.0, 3.0),
        "M30": (30.0, 3.5), "M33": (33.0, 3.5), "M36": (36.0, 4.0),
    },
    "Trapezoidal": {                                 # ISO 2904 lead screws
        "Tr8x1.5": (8.0, 1.5), "Tr10x2": (10.0, 2.0), "Tr12x3": (12.0, 3.0),
        "Tr14x3": (14.0, 3.0), "Tr16x4": (16.0, 4.0), "Tr18x4": (18.0, 4.0),
        "Tr20x4": (20.0, 4.0), "Tr22x5": (22.0, 5.0), "Tr24x5": (24.0, 5.0),
        "Tr28x5": (28.0, 5.0), "Tr30x6": (30.0, 6.0), "Tr32x6": (32.0, 6.0),
        "Tr36x6": (36.0, 6.0), "Tr40x7": (40.0, 7.0), "Tr44x7": (44.0, 7.0),
        "Tr48x8": (48.0, 8.0), "Tr50x8": (50.0, 8.0),
    },
    "UNC": _unified_sizes({
        "#4-40 UNC": (0.112, 40), "#6-32 UNC": (0.138, 32),
        "#8-32 UNC": (0.164, 32), "#10-24 UNC": (0.190, 24),
        "1/4-20 UNC": (0.250, 20), "5/16-18 UNC": (0.3125, 18),
        "3/8-16 UNC": (0.375, 16), "7/16-14 UNC": (0.4375, 14),
        "1/2-13 UNC": (0.500, 13), "5/8-11 UNC": (0.625, 11),
        "3/4-10 UNC": (0.750, 10), "1-8 UNC": (1.000, 8),
    }),
    "UNF": _unified_sizes({
        "#4-48 UNF": (0.112, 48), "#6-40 UNF": (0.138, 40),
        "#8-36 UNF": (0.164, 36), "#10-32 UNF": (0.190, 32),
        "1/4-28 UNF": (0.250, 28), "5/16-24 UNF": (0.3125, 24),
        "3/8-24 UNF": (0.375, 24), "7/16-20 UNF": (0.4375, 20),
        "1/2-20 UNF": (0.500, 20), "5/8-18 UNF": (0.625, 18),
        "3/4-16 UNF": (0.750, 16), "1-12 UNF": (1.000, 12),
    }),
    "ACME": _unified_sizes({
        "1/4-16 ACME": (0.250, 16), "5/16-14 ACME": (0.3125, 14),
        "3/8-12 ACME": (0.375, 12), "1/2-10 ACME": (0.500, 10),
        "5/8-8 ACME": (0.625, 8), "3/4-6 ACME": (0.750, 6),
        "1-5 ACME": (1.000, 5), "1.1/4-5 ACME": (1.250, 5),
        "1.1/2-4 ACME": (1.500, 4),
    }),
    "NPT": _unified_sizes({                          # pipe OD at the gauge plane
        "1/8 NPT": (0.405, 27), "1/4 NPT": (0.540, 18), "3/8 NPT": (0.675, 18),
        "1/2 NPT": (0.840, 14), "3/4 NPT": (1.050, 14), "1 NPT": (1.315, 11.5),
        "1.1/4 NPT": (1.660, 11.5), "1.1/2 NPT": (1.900, 11.5),
        "2 NPT": (2.375, 11.5),
    }),
}

# family -> (half of the included flank angle, crest flat / pitch, radial taper).
# Every family is the SAME trapezoid with different numbers, which is why one
# section builder covers all of them.
_THREAD_FAMILY = {
    "ISO metric": (30.0, 0.125, 0.0),
    "UNC": (30.0, 0.125, 0.0),
    "UNF": (30.0, 0.125, 0.0),
    "Trapezoidal": (15.0, 0.366, 0.0),
    "ACME": (14.5, 0.3707, 0.0),
    "NPT": (30.0, 0.0381, 1.0 / 32.0),     # 1:16 on the diameter = 1:32 on radius
}


def _thread_spec(_size, _profile="ISO metric", _diameter=0.0, _pitch=0.0):
    \"\"\"Resolve a size name to (major diameter mm, pitch mm, family).

    The size list is flat and the family is read OFF the name ("M6", "Tr20x4",
    "1/4-20 UNC") rather than selected separately: a param's options cannot
    depend on another param's value, and one searchable list beats two combos
    that can disagree. `diameter`/`pitch` override whatever the table said —
    that is how a fine pitch is asked for (M8 + pitch 1.0) and how "custom"
    works at all.\"\"\"
    fam, d, p = None, 0.0, 0.0
    for family, table in _THREAD_SIZES.items():
        if _size in table:
            d, p = table[_size]
            fam = family
            break
    if fam is None:                                  # "custom"
        fam = _profile if _profile in _THREAD_FAMILY else "ISO metric"
    if _diameter:
        d = float(_diameter)
    if _pitch:
        p = float(_pitch)
    if d <= 0 or p <= 0:
        raise ValueError(
            "Thread: size 'custom' needs both `diameter` and `pitch` set — pick "
            "a standard size instead, or say how big the custom thread is.")
    return d, p, fam


def _thread_section(_fam, _p, _internal):
    \"\"\"One pitch of the axial profile: (thread depth, crest flat, half width at
    the root). Male and female are NOT two generators — on the 60 degree
    families they are the same trapezoid truncated at a different depth
    (17H/24 external, giving the ISO minor d3 = d - 1.2269p; 15H/24 internal,
    giving D1 = d - 1.0825p). That difference IS male vs female.\"\"\"
    half, crest_f, _taper = _THREAD_FAMILY[_fam]
    if _fam in ("ISO metric", "UNC", "UNF"):
        h = (15.0 if _internal else 17.0) / 24.0 * (math.sqrt(3) / 2 * _p)
    elif _fam == "Trapezoidal":                      # ISO 2904 clearance ac
        h = 0.5 * _p + (0.15 if _p <= 5 else (0.25 if _p <= 12 else 0.5))
    elif _fam == "ACME":
        h = 0.5 * _p + 0.254                         # 0.010" general purpose
    else:                                            # NPT
        h = 0.8 * _p
    c = crest_f * _p
    hw = c / 2.0 + h * math.tan(math.radians(half))
    if 2.0 * hw >= _p:
        raise ValueError(
            "Thread: a %s profile of pitch %.3fmm has no room left between "
            "turns — the flanks meet before the root does. Use a coarser pitch."
            % (_fam, _p))
    return h, c, hw


def _thread(_size="M6", _length=10.0, _internal=False, _clearance=0.3,
            _starts=1, _lefthand=False, _lead_in=True, _segments=64,
            _profile="ISO metric", _diameter=0.0, _pitch=0.0, _shape=None,
            _at_pt=None):
    \"\"\"A threaded rod (external) or the tap that cuts a nut (internal), as a Mesh.

    The female thread is not modelled directly: the tool IS the male thread
    grown by the clearance, and subtracting it from a body drills the hole and
    cuts the thread in one go — exactly what a tap does. Verified by measuring
    the boolean interference of a mating pair (M6x1): the profiles are tangent
    by construction at clearance 0 and free from 0.1mm up (residual ~0.01mm^3,
    which is manifold3d's numerical noise, not contact).

    The placement socket is `at`, NOT `origin`, and that is deliberate. The
    emitter wraps an `origin` socket around the node's WHOLE result (_at is
    applied outside the template), which with `shape` wired would displace the
    finished assembly — so a tapped hole could never be put anywhere but the
    axis. Taking the point here instead places the THREAD inside the part,
    before the boolean, which is the only useful reading. It also means a LIST
    of points drills a whole pattern of tapped holes in one node.\"\"\"
    import numpy as np
    d, p, fam = _thread_spec(_size, _profile, _diameter, _pitch)
    h, c, hw = _thread_section(fam, p, _internal)
    taper = _THREAD_FAMILY[fam][2]
    L = float(_length)
    if L <= 0:
        raise ValueError("Thread: `length` must be positive.")
    n_st = max(1, int(_starts))
    lead = p * n_st                                  # axial advance per turn
    # `clearance` loosens THIS thread: the male shrinks, the female tool grows.
    # Set it on ONE half of a mating pair — applying it to both doubles the gap.
    crest = d / 2.0 + (0.5 if _internal else -0.5) * float(_clearance)
    root = crest - h
    if root <= 0.0:
        raise ValueError(
            "Thread: %s of pitch %.3fmm cuts deeper (%.3fmm) than the %.3fmm "
            "radius — the thread would swallow its own axis." % (fam, p, h, crest))
    base = root - 0.15 * p          # reach INTO the core: the union needs real
                                    # overlap, never a tangency (OCCT's failure)

    # The section, one pitch tall. Consecutive turns of a rib abut exactly at
    # +-p/2, which is what lets them fuse into a continuous helical ridge.
    S = np.array([(base, -p / 2), (root, -p / 2), (root, -hw), (crest, -c / 2),
                  (crest, c / 2), (root, hw), (root, p / 2), (base, p / 2)])
    n = len(S)
    turns = L / lead + 1.0
    K = max(8, int(_segments * turns)) + 1
    t = np.linspace(0.0, turns, K)
    Z = (lead * t - p / 2.0)[:, None] + S[:, 1][None, :]
    R = S[:, 0][None, :] + taper * Z                 # NPT opens away from the axis

    # Connectivity is the same for every start, so build it once.
    kk = np.arange(K - 1)[:, None]
    jj = np.arange(n)[None, :]
    a0 = (kk * n + jj).ravel()
    b0 = (kk * n + (jj + 1) % n).ravel()
    side = np.concatenate([np.stack([a0, b0, b0 + n], 1),
                           np.stack([a0, b0 + n, a0 + n], 1)])
    last = (K - 1) * n
    caps = []
    for j in range(1, n - 1):                        # the section is convex: fan
        caps.append([0, j + 1, j])
        caps.append([last, last + j, last + j + 1])
    # Reversed: built inward-facing, and manifold3d reads an inverted winding as
    # NEGATIVE volume and SUBTRACTS the rib — silently, and still watertight.
    F = np.concatenate([side, np.array(caps, dtype=side.dtype)])[:, ::-1]

    mf = _mf()
    seg = max(24, int(_segments))
    # n starts = n identical ribs rotated by 2pi/n. No axial offset: rotating a
    # helix of lead n*p by 2pi/n already shifts it by exactly one pitch.
    mans = []
    for s in range(n_st):
        a = (2 * math.pi * t * (-1.0 if _lefthand else 1.0))[:, None] \
            + 2 * math.pi * s / n_st
        V = np.stack([R * np.cos(a), R * np.sin(a), Z], -1).reshape(-1, 3)
        mans.append(mf.Manifold(mf.Mesh(V.astype(np.float32),
                                        F.astype(np.uint32))))
    core = mf.Manifold.cylinder(L + 2 * p, max(root - taper * p, 1e-3),
                                max(root + taper * (L + p), 1e-3), seg,
                                False).translate([0, 0, -p])
    body = mf.Manifold.batch_boolean([core] + mans, mf.OpType.Add)
    span = 4 * (crest + taper * L) + 4
    body = body ^ mf.Manifold.cube([span, span, L], True).translate([0, 0, L / 2])

    if _lead_in and L > 3 * h:
        # A thread that starts as a knife edge is unprintable and will not
        # catch. The male gets its ends chamfered away, the female tool gets
        # them FLARED so the mouth of the hole guides the bolt in.
        def _rc(z):
            return crest + taper * z + 0.001
        if _internal:
            body = mf.Manifold.batch_boolean([
                body,
                mf.Manifold.cylinder(h, _rc(0) + h, _rc(h), seg, False),
                mf.Manifold.cylinder(h, _rc(L - h), _rc(L) + h, seg,
                                     False).translate([0, 0, L - h]),
            ], mf.OpType.Add)
        else:
            body = body ^ mf.Manifold.batch_boolean([
                mf.Manifold.cylinder(h, max(_rc(0) - h, 1e-3), _rc(h), seg, False),
                mf.Manifold.cylinder(L - 2 * h, _rc(h), _rc(L - h), seg,
                                     False).translate([0, 0, h]),
                mf.Manifold.cylinder(h, _rc(L - h), max(_rc(L) - h, 1e-3), seg,
                                     False).translate([0, 0, L - h]),
            ], mf.OpType.Add)

    out = _from_manifold(body)
    if _at_pt is not None:
        out = _at(out, _at_pt)              # place the thread, THEN cut with it
    if _shape is None:
        return out
    # The boolean nobody should have to get right by hand: a male thread ADDS to
    # the part, a female tool CUTS from it.
    return _mesh_bool("subtract" if _internal else "union", _shape, out)


def _voronoi3d(_points, _body=None, _scale=0.9):
    \"\"\"3D Voronoi cells as closed convex Mesh bodies, clipped to `body`.

    Sites are mirrored across the SIX planes of the domain box (the body's bbox
    if wired, else the points' extent) so every kept cell is finite — the 3D
    analog of _voronoi2d's trick. Each finite cell is its Voronoi vertices,
    shrunk toward the centroid by `scale`, hulled straight into a manifold
    (Manifold.hull_points — a Voronoi cell is convex by construction, the hull
    IS the cell), then intersected with the body. Mesh Subtract the shrunk
    cells from the body for a voronoi lattice. Measured: 60 sites clipped to a
    5k-triangle sphere in under 0.1s.\"\"\"
    import numpy as np
    from scipy.spatial import Voronoi
    pts = _origin_points(_points)
    if len(pts) < 2:
        return []
    if len(pts) > 2000:
        raise ValueError("Voronoi 3D: %d points — cap is 2000 (each one is a "
                         "solid cell and a boolean)." % len(pts))
    P = np.array([(p.X, p.Y, p.Z) for p in pts], dtype=float)
    body = _as_mesh(_body)
    if body is not None:
        lo, hi = body.tm.bounds
    else:
        lo, hi = P.min(axis=0), P.max(axis=0)
    pad = []
    for (x, y, z) in P:
        pad += [(2 * lo[0] - x, y, z), (2 * hi[0] - x, y, z),
                (x, 2 * lo[1] - y, z), (x, 2 * hi[1] - y, z),
                (x, y, 2 * lo[2] - z), (x, y, 2 * hi[2] - z)]
    try:
        vor = Voronoi(np.vstack([P, pad]))
    except Exception:
        raise ValueError("Voronoi 3D needs points spread in a VOLUME (>= 2, not "
                         "all coplanar) — wire a solid into Populate's region to "
                         "scatter them inside a body.")
    mf = _mf()
    s = float(_scale)
    man_body = _to_manifold(body, "Voronoi 3D") if body is not None else None
    out = []
    for i in range(len(P)):                        # original sites only
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        v = vor.vertices[reg]
        c = v.mean(axis=0)
        cell = mf.Manifold.hull_points((c + s * (v - c)).astype(np.float32))
        if man_body is not None:
            cell = cell ^ man_body
        m = _from_manifold(cell)
        if m is not None:
            out.append(m)
    return out


_PLATONIC_FACES = (4, 6, 8, 12, 20)


def _poly_snap(_kind, _n):
    \"\"\"Round a requested face count to one this family can actually build, and
    return (faces, k) — k being the base polygon's side count where there is one.

    There is no polyhedron for every (family, face count) pair, so the slider
    SNAPS rather than failing: a prism has k+2 faces, an antiprism 2k+2, a
    bipyramid 2k, and a triangulated hull of V vertices has exactly 2V-4.\"\"\"
    n = max(4, int(_n))
    if _kind == "platonic":
        return min(_PLATONIC_FACES, key=lambda f: (abs(f - n), f)), 0
    if _kind == "prism":
        k = max(3, n - 2)
        return k + 2, k
    if _kind == "antiprism":
        k = max(3, (n - 2) // 2)
        return 2 * k + 2, k
    if _kind == "bipyramid":
        k = max(3, n // 2)
        return 2 * k, k
    return max(4, n - n % 2), 0            # sphere: F = 2V - 4, so F is even


def _poly_ring(_k, _z, _twist=0.0):
    \"\"\"A regular _k-gon lying ON the unit sphere at height _z (hence radius
    sqrt(1-z^2)), turned by _twist of one edge.\"\"\"
    import numpy as np
    r = max(0.0, 1.0 - _z * _z) ** 0.5
    a = 2 * np.pi * (np.arange(_k) + _twist) / _k
    return np.stack([r * np.cos(a), r * np.sin(a), np.full(_k, float(_z))], axis=1)


def _poly_verts(_kind, _faces):
    \"\"\"Vertices of the requested polyhedron, ALL ON THE UNIT SPHERE.

    That constraint is what makes "the most volume for N faces" a well-posed
    question here. Unconstrained it is unbounded, and the classical isoperimetric
    version — most volume per unit SURFACE — is an OPEN problem past a handful of
    face counts, whose answers are not the shapes anyone guesses: the best
    5-faced solid is a triangular prism, not a square pyramid, and the best
    8-faced one is not the regular octahedron. So this node answers the question
    it can answer exactly: the largest member of the chosen family inscribed in a
    sphere of radius `size`. Prism and antiprism have one free proportion, found
    by scanning the hull volume; the platonics, the bipyramid and the hull of
    Fibonacci points leave nothing to choose.\"\"\"
    import numpy as np
    from scipy.spatial import ConvexHull
    n, k = _poly_snap(_kind, _faces)
    if _kind == "platonic":
        phi = (1 + 5 ** 0.5) / 2
        if n == 4:
            V = [(1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1)]
        elif n == 8:
            V = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
        elif n == 20:
            V = []
            for s in (-1, 1):
                for t in (-1, 1):
                    V += [(0, s, t * phi), (s, t * phi, 0), (t * phi, 0, s)]
        else:                                  # cube, and the dodecahedron on it
            V = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
            if n == 12:
                for s in (-1, 1):
                    for t in (-1, 1):
                        V += [(0, s / phi, t * phi), (s / phi, t * phi, 0),
                              (t * phi, 0, s / phi)]
        P = np.array(V, dtype=float)
        return P / np.linalg.norm(P, axis=1)[:, None]
    if _kind == "bipyramid":       # every vertex on the sphere leaves no freedom
        return np.vstack([_poly_ring(k, 0.0), [(0, 0, 1.0), (0, 0, -1.0)]])
    if _kind in ("prism", "antiprism"):
        tw = 0.5 if _kind == "antiprism" else 0.0

        def vol_at(z):
            P = np.vstack([_poly_ring(k, z), _poly_ring(k, -z, tw)])
            return float(ConvexHull(P).volume), P

        # Coarse sweep, then refine around the winner: the grid alone leaves ~0.02%
        # on the table (it stops at z=0.57 where the hexagonal prism's optimum is
        # 1/sqrt(3)=0.5774, and makes the triangular antiprism miss being exactly a
        # regular octahedron). Two passes cost nothing and land on the real optimum.
        lo, hi = 0.05, 0.95
        best = None
        for _ in range(3):
            zs = np.linspace(lo, hi, 46)
            vals = [vol_at(z) for z in zs]
            i = max(range(len(zs)), key=lambda j: vals[j][0])
            best = vals[i][1]
            step = zs[1] - zs[0]
            lo, hi = max(0.01, zs[i] - step), min(0.99, zs[i] + step)
        return best
    nv = (n + 4) // 2                          # triangulated hull: F = 2V - 4
    i = np.arange(nv) + 0.5                    # Fibonacci sphere: near-uniform
    z = 1 - 2 * i / nv
    th = np.pi * (1 + 5 ** 0.5) * i
    r = np.sqrt(np.maximum(0.0, 1 - z * z))
    return np.stack([r * np.cos(th), r * np.sin(th), z], axis=1)


def _poly_solid(_V):
    \"\"\"Sew a convex point cloud into a real B-Rep solid: hull it, MERGE the
    coplanar triangles scipy returns into one polygonal face each, order that
    face's vertices around its own normal, then Shell -> Solid. The merge is what
    makes a cube six faces instead of twelve triangles — and what makes the face
    count the node advertises the face count you actually get.\"\"\"
    import numpy as np
    from scipy.spatial import ConvexHull
    h = ConvexHull(_V)
    facets = {}
    for i, eq in enumerate(h.equations):
        facets.setdefault(tuple(np.round(eq, 6)), []).append(i)
    faces = []
    for eq, tris in facets.items():
        P = _V[sorted({int(v) for t in tris for v in h.simplices[t]})]
        c = P.mean(axis=0)
        u = P[0] - c
        u = u / np.linalg.norm(u)
        w = np.cross(np.array(eq[:3], dtype=float), u)
        P = P[np.argsort(np.arctan2((P - c) @ w, (P - c) @ u))]
        faces.append(Face(Wire.make_polygon([Vector(*p) for p in P], close=True)))
    return Solid(Shell(faces))


def _polyhedron(_kind="platonic", _faces=6, _size=10.0, _as_mesh=False):
    \"\"\"The Polyhedron node. ONE vertex set feeds both lanes — a hull is a hull, so
    the B-Rep lane sews it (the default, exact planar faces you can Fillet) and the
    `mesh output` toggle hands the same points to manifold3d instead. Positioning
    is NOT done here: the emitter wraps any node with an `origin` socket in _at()
    on its own, and doing it twice would double the translation.\"\"\"
    import numpy as np
    V = _poly_verts(_kind, _faces) * float(_size)
    if _as_mesh:
        return _from_manifold(_mf().Manifold.hull_points(V.astype(np.float32)))
    return _poly_solid(V)


def _mesh_simplify(_mesh, _tolerance=0.05, _max_error=5.0):
    \"\"\"Simplify to a BOUNDED geometric deviation (manifold3d): no surface moves
    further than `tolerance`. On the raccordo: 0.07s, 147k -> 35k triangles, volume
    off by -0.10%, still watertight.

    Tolerance-driven rather than count-driven on purpose — in CAD you know how much
    error you can accept, not how many triangles you want. (The MIT count-driven
    decimators were measured and rejected: they break the part. PLAN_MESH_LANE.md §5.)

    The sharp edge, and why this node checks its own work: a tolerance at or above
    the part's feature size doesn't simplify it, it DESTROYS it — and quietly. The
    raccordo is a thin-walled shell whose mean wall (volume/area) is 1.11mm, and
    simplify tracks that exactly: 1 body and -0.2% volume at 0.5mm, still 1 body at
    1.0mm, then it TEARS — 2 bodies at 1.2mm, 3 at 1.5mm, -22% volume at 3mm. You
    cannot move a surface further than the wall is thick and still have a wall.

    So we verify the outcome rather than trust the setting: if simplify moved the
    volume more than `_max_error` percent, or broke the part into more pieces than it
    started with, that is a failure and it is reported as one. `decompose()` makes the
    piece count essentially free (~0.02s).\"\"\"
    src = _as_mesh(_mesh)
    man = _to_manifold(src, "Mesh Simplify")
    if man is None:
        return None
    simple = man.simplify(float(_tolerance))
    out = _from_manifold(simple)
    if out is None:
        return None
    v0, v1 = man.volume(), simple.volume()
    err = abs(v1 - v0) / abs(v0) * 100.0 if v0 else 0.0
    bodies0, bodies1 = len(man.decompose()), len(simple.decompose())
    if err > float(_max_error) or bodies1 > bodies0:
        wall = abs(v0) / src.area if src.area else 0.0
        raise ValueError(
            "Mesh Simplify: tolerance %gmm is too coarse for this part — %s. Its mean "
            "wall is %.2fmm (volume/area), and a tolerance near or above that collapses "
            "it. Lower the tolerance%s."
            % (float(_tolerance),
               ("it tore the part into %d pieces" % bodies1) if bodies1 > bodies0
               else ("it moved the volume by %.1f%% (limit %.1f%%)" % (err, float(_max_error))),
               wall,
               "" if bodies1 > bodies0 else " or raise `max error`"))
    return out


def _mesh_to_solid(_mesh, _max_tris=20000):
    \"\"\"The ONE bridge back to the B-Rep lane: sew every triangle into a planar face,
    then fix the shell into a solid.

    It is a node and not a cast because it is brutally expensive — ~300s on a
    147k-triangle part, and every downstream B-Rep boolean then costs ~80s on the
    147k-FACE solid it produces. The guard refuses loudly instead of hanging the app
    for five minutes; simplify first.\"\"\"
    m = _as_mesh(_mesh)
    if m is None:
        return None
    if m.n_tris > int(_max_tris):
        raise ValueError(
            "MeshToSolid refuses %d triangles (limit %d): sewing that many into a "
            "B-Rep takes minutes, and every boolean after it tens of seconds. Put a "
            "Mesh Simplify in front of it." % (m.n_tris, int(_max_tris)))
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_Sewing, BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace)
    from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Solid
    from OCP.TopoDS import TopoDS
    from OCP.gp import gp_Pnt
    verts = m.tm.vertices
    sew = BRepBuilderAPI_Sewing(1e-6)
    for tri in m.tm.faces:
        pts = [gp_Pnt(float(verts[i][0]), float(verts[i][1]), float(verts[i][2]))
               for i in tri]
        poly = BRepBuilderAPI_MakePolygon(pts[0], pts[1], pts[2], True)
        if not poly.IsDone():
            continue
        face = BRepBuilderAPI_MakeFace(poly.Wire())
        if face.IsDone():
            sew.Add(face.Face())
    sew.Perform()
    fixer = ShapeFix_Shape(ShapeFix_Solid().SolidFromShell(
        TopoDS.Shell_s(sew.SewedShape())))
    fixer.Perform()
    return Solid(fixer.Shape())


# --- print physics (PLAN_PRINT_PHYSICS.md) ---------------------------------
# An FDM part is ANISOTROPIC: the bond between layers is roughly a third to two
# thirds of the strength within a layer, depending on material and temperature. So
# orientation is not a convenience — it decides where the part breaks. Everything
# below measures a mesh SITTING ON THE BED (z=0), and it is a heuristic, not an FEA:
# it captures the dominant failure mode (the part splits at the glued interface with
# the least area) and says nothing about stress concentration around a hole.


def _sections(_mesh, _n=48):
    \"\"\"Cross-section area up Z. manifold3d slices in ~0.01s for 80 sections, so
    scoring a hundred candidate orientations is free.\"\"\"
    import numpy as _np
    tm = _mesh.tm
    z0, z1 = float(tm.bounds[0][2]), float(tm.bounds[1][2])
    if z1 - z0 < 1e-6:
        return _np.array([]), _np.array([])
    try:
        man = _to_manifold(_mesh, "the print check")
    except Exception:
        return _np.array([]), _np.array([])   # not watertight: no honest section area
    zs = _np.linspace(z0 + (z1 - z0) * 0.02, z1 - (z1 - z0) * 0.02, int(_n))
    return _np.array([man.slice(float(z)).area() for z in zs]), zs


def _print_metrics(_mesh, _angle=45.0, _layer=0.2, _nsec=48):
    \"\"\"Measure one orientation. The mesh must already sit on the bed.

    The faces ON THE BED are excluded from the overhang: a flat base points straight
    down too, and counting it as an overhang — the classic way to get this wrong —
    makes a part that needs no support at all look like the worst option there is.\"\"\"
    import numpy as _np
    tm = _mesh.tm
    n, a = tm.face_normals, tm.area_faces
    zc = tm.triangles[:, :, 2].mean(axis=1)
    z0 = float(tm.bounds[0][2])
    crit = math.cos(math.radians(float(_angle)))
    down = n[:, 2] < -crit                      # steeper than the printer can bridge
    bed = down & (zc < z0 + max(float(_layer), 0.05) * 2.0)
    over = down & ~bed
    areas, zs = _sections(_mesh, _nsec)
    i = int(_np.argmin(areas)) if len(areas) else -1
    return {
        "support_area": float(a[over].sum()),
        # what the support COSTS is not its footprint but how far it must reach down
        "support_vol": float((a[over] * (zc[over] - z0)).sum()),
        "bed_area": float(a[bed].sum()),
        "height": float(tm.bounds[1][2] - z0),
        "weak_area": float(areas[i]) if i >= 0 else 0.0,
        "weak_z": float(zs[i]) if i >= 0 else 0.0,
        "sectioned": i >= 0,
        "over_mask": over,
        "n_over": int(over.sum()),
    }


def _support_body(_mesh, _angle=45.0, _layer=0.2, _clearance=0.2):
    \"\"\"The support material itself, as a body you can look at — not an estimate of it.

    Under every overhanging triangle, drop a prism to the bed. Union the lot, subtract the
    part (and the part shifted down by `clearance`, which is what carves the gap the
    support must leave under the face, or it welds itself on). What is left IS the support:
    its volume is grams, and you can preview it.

    `area x height above the bed` — the cheap proxy the search falls back on — is not this
    number and cannot be: it counts the column under an overhang even where the part
    itself is already sitting in the way. On a sphere of r=20 on the bed this returns
    1.63 cm3 against 1.73 cm3 worked out with a pencil (the rest is tessellation and the
    clearance gap). It costs 0.58s on a 20k-triangle mesh, so it is affordable per node
    and NOT free inside a search over every pose — hence the guard in _orient_plan.\"\"\"
    import numpy as _np
    m = _as_mesh(_mesh)
    if m is None:
        return None
    tm = m.tm
    met = _print_metrics(m, _angle, _layer, _nsec=2)
    over = _np.where(met["over_mask"])[0]
    z0 = float(tm.bounds[0][2])
    if not len(over):
        return None
    mf = _mf()
    prisms = []
    for fi in over:
        tri = tm.triangles[fi]
        if float(tm.area_faces[fi]) < 1e-9 or float(tri[:, 2].max()) - z0 < 0.01:
            continue                     # a degenerate face, or one already on the bed
        v = _np.array([[p[0], p[1], p[2]] for p in tri]
                      + [[p[0], p[1], z0] for p in tri], dtype=_np.float32)
        # the face's own normal points DOWN (it is an overhang), so the prism's outward
        # normal there points UP: the top triangle is the face, wound the other way.
        f = _np.array([[0, 2, 1], [3, 4, 5],
                       [0, 1, 4], [0, 4, 3],
                       [1, 2, 5], [1, 5, 4],
                       [2, 0, 3], [2, 3, 5]], dtype=_np.uint32)
        prisms.append(mf.Manifold(mf.Mesh(v, f)))
    if not prisms:
        return None
    col = mf.Manifold.batch_boolean(prisms, mf.OpType.Add)
    part = _to_manifold(m, "the support volume")
    blocker = part + part.translate((0.0, 0.0, -float(_clearance)))
    return _from_manifold(col - blocker)


def _support_report(_mesh, _angle=45.0, _layer=0.2, _clearance=0.2, _density=1.24):
    \"\"\"The support body, in the units that hurt — and NOT pretending they are grams on the
    spool. This is the ENVELOPE: a slicer fills it with a 10-20% lattice, so the plastic
    that actually goes through the nozzle is a fraction of it. Quoting the solid mass as
    the print cost would overstate it five-fold, and a number that flatters itself is
    worse than no number.\"\"\"
    s = _support_body(_mesh, _angle, _layer, _clearance)
    if s is None:
        return None, "support     : none at all — it prints as it stands"
    cm3 = s.volume / 1000.0
    return s, ("support     : %.2f cm3 of envelope  (%.1f g of PLA if it were solid; a "
               "slicer fills it\\n              sparse, so scale by your support density — "
               "and none of that touches\\n              the hour you will spend picking "
               "it off)" % (cm3, cm3 * float(_density)))


def _bed_drop(_shape, _center=True, _clearance=0.0):
    \"\"\"Sit a shape on the bed: its lowest point goes to z=0. Works on both lanes —
    it measures on the mesh and moves the original, so a solid stays a solid.

    It measures on the TESSELLATION rather than Shape.bounding_box() on purpose: the
    fast OCCT box is oversized (which is why the live view tags its bbox `approx`),
    and a part dropped by an oversized box hovers above the bed by up to 1% of its
    size — invisible on screen, and a failed first layer.\"\"\"
    if _shape is None:
        return None
    m = _as_mesh(_shape)
    if m is None:
        return _shape
    b = m.tm.bounds
    dz = -float(b[0][2]) + float(_clearance)
    dx = dy = 0.0
    if _center:
        dx = -float(b[0][0] + b[1][0]) / 2.0
        dy = -float(b[0][1] + b[1][1]) / 2.0
    return _move(_shape, None, dx, dy, dz)


# Coefficient of restitution on a hard bed, per material. Fixed values, and
# honest about being a caricature: real restitution depends on both bodies,
# the impact speed and the geometry. What matters here is the CONTRAST —
# rubber keeps bouncing, lead lands with one dead thud.
_DROP_E = {"plastic": 0.55, "rubber": 0.85, "steel": 0.65, "wood": 0.45,
           "lead": 0.08, "clay": 0.0}


def _drop_segs(_e):
    \"\"\"Normalised bounce segments (duration, up-speed): the first fall lasts 1
    time unit and lands at speed 2 (g=2, h0=1), then a geometric series of
    parabolas, each keeping _e of the impact speed, until the apex sinks below
    0.1% of the drop. Everything scales: real seconds are these times t0.\"\"\"
    e = max(0.0, min(float(_e), 0.95))
    segs = [(1.0, None)]
    v, apex = 2.0, 1.0
    for _ in range(60):
        v *= e
        apex *= e * e
        if apex < 1e-3:
            break
        segs.append((v, v))            # duration 2v/g = v when g=2
    return segs


def _drop_height(_segs, _tnorm):
    \"\"\"Height (in h0=1 units) at normalised time _tnorm along the bounce.\"\"\"
    tau = _tnorm
    for d, up in _segs:
        if tau <= d:
            if up is None:
                return max(1.0 - tau * tau, 0.0)
            return max(up * tau - tau * tau, 0.0)
        tau -= d
    return 0.0


def _rodrigues(_e, _ang):
    import numpy as _np
    K = _np.array([[0.0, -_e[2], _e[1]], [_e[2], 0.0, -_e[0]], [-_e[1], _e[0], 0.0]])
    return _np.eye(3) + math.sin(_ang) * K + (1.0 - math.cos(_ang)) * (K @ K)


def _support_pick(_C2, _com2, _eps):
    \"\"\"The stability geometry shared by the bed cascade and the pile tips:
    given the 2D shadow of the contact points and the com's shadow, return
    (None, None) when the com is INSIDE the support (at rest), else the
    closest boundary point q and, when q lies in an edge's interior, that
    edge — the pivot the part will tip about.\"\"\"
    import numpy as _np
    C2 = _C2
    if len(C2) >= 3:
        try:
            from scipy.spatial import ConvexHull as _CH2
            poly = C2[_CH2(C2).vertices]               # ccw
        except Exception:
            poly = None                                # collinear contacts
        if poly is not None:
            if all((poly[(i + 1) % len(poly)][0] - poly[i][0]) * (_com2[1] - poly[i][1])
                   - (poly[(i + 1) % len(poly)][1] - poly[i][1]) * (_com2[0] - poly[i][0])
                   >= -_eps for i in range(len(poly))):
                return None, None                      # com inside: at rest
            C2 = poly
    if len(C2) == 1:
        return C2[0], None
    n2, best = len(C2), None
    for i in range(n2 if n2 > 2 else 1):
        a, b = C2[i], C2[(i + 1) % n2]
        ed = b - a
        L2 = float(ed @ ed)
        tp = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, float((_com2 - a) @ ed) / L2))
        cp = a + tp * ed
        d2 = float((_com2 - cp) @ (_com2 - cp))
        if best is None or d2 < best[0]:
            best = (d2, cp, a, b, tp)
    _, q, a, b, tp = best
    return q, ((a, b) if 1e-7 < tp < 1.0 - 1e-7 else None)


def _pivot_axes(_q, _seg, _com, _p3, _eps):
    \"\"\"Candidate tip axes (horizontal, through _p3), sense chosen so gravity
    does the tipping. Balanced ties return both senses (edge) or the compass
    (point) — the caller's energy guard decides which, if any, is a real
    descent.\"\"\"
    import numpy as _np
    u = _com[:2] - _q
    if _seg is not None:
        e3 = _np.array([_seg[1][0] - _seg[0][0], _seg[1][1] - _seg[0][1], 0.0])
        e3 /= _np.linalg.norm(e3)
        if float(u @ u) > _eps * _eps:
            return [e3] if _np.cross(e3, _com - _p3)[2] < 0.0 else [-e3]
        return [e3, -e3]
    if float(u @ u) > _eps * _eps:
        d3 = _np.array([u[0], u[1], 0.0]) / math.sqrt(float(u @ u))
        return [_np.cross(_np.array([0.0, 0.0, 1.0]), d3)]
    return [_np.array([1.0, 0.0, 0.0]), _np.array([-1.0, 0.0, 0.0]),
            _np.array([0.0, 1.0, 0.0]), _np.array([0.0, -1.0, 0.0])]


def _settle_plan(_pts, _com, _max_steps=40):
    \"\"\"The topple cascade: quasi-static rigid settling on the bed (z=0), played
    out on the convex hull. Returns (steps, settled) — each step is (pivot,
    axis, angle_deg, seconds) in bed coordinates, replayed in order (a partial
    last step is a scrub position).

    The mechanics: a resting body is stable iff its centre of mass projects
    inside the support polygon — the same test OrientForPrint uses to ENUMERATE
    the stable poses; this walks the PATH between them. If the com is outside,
    gravity tips the body about the nearest support edge (or corner), it rolls
    onto the next hull facet, and the loop repeats. Two rules carry all the
    honesty: every step must strictly LOWER the centre of mass (the energy
    guard — a tessellated sphere \"toppling\" facet to facet releases nothing
    and is declared at rest, where a cube balanced on an edge drops its centre
    by 20% and goes over), and perfectly balanced ties pick a deterministic
    side (a real part would be tipped by the first draught; a graph must give
    the same answer twice).\"\"\"
    import numpy as _np
    g = 9810.0
    pts = _np.asarray(_pts, dtype=float).copy()
    com = _np.asarray(_com, dtype=float).copy()
    scale = float(_np.linalg.norm(pts.max(0) - pts.min(0))) or 1.0
    eps_c = 1e-5 * scale               # this close to the bed = touching
    eps_t = 1e-6 * scale               # com-over-support tie tolerance

    def _first_touch(p, e):
        # smallest positive rotation about the horizontal axis (p, e) at which
        # some hull vertex reaches the bed: z(phi) = A cos(phi) + B sin(phi)
        r = pts - p
        A = r[:, 2]
        Bz = _np.cross(_np.broadcast_to(e, r.shape), r)[:, 2]
        best = None
        for a, b in zip(A, Bz):
            if a * a + b * b < (1e-9 * scale) ** 2:
                continue               # on the axis: it IS the contact
            phi = math.atan2(-a, b) % math.pi
            if phi < 1e-6:
                phi += math.pi
            if best is None or phi < best:
                best = phi
        return best

    steps = []
    for _ in range(int(_max_steps)):
        pts[:, 2] -= pts[:, 2].min()
        com2 = com[:2]
        C2 = pts[pts[:, 2] < eps_c][:, :2]
        if len(C2) == 0:
            return steps, False
        q, seg = _support_pick(C2, com2, eps_t)
        if q is None:
            return steps, True                         # com inside: at rest
        p3 = _np.array([q[0], q[1], 0.0])
        took = False
        for e3 in _pivot_axes(q, seg, com, p3, eps_t):
            th = _first_touch(p3, e3)
            if th is None or th > math.pi - 1e-6:
                continue
            R = _rodrigues(e3, th)
            ncom = R @ (com - p3) + p3
            npts = (R @ (pts - p3).T).T + p3
            if com[2] - (ncom[2] - npts[:, 2].min()) <= max(1e-4 * scale, 1e-7):
                continue                               # energy guard: no release, no
            rc = com - p3                              # topple (rolling, or uphill)
            rperp = rc - float(rc @ e3) * e3
            tau = 2.0 * math.sqrt(max(float(_np.linalg.norm(rperp)), 1e-3) * th / g)
            steps.append((tuple(p3), tuple(e3), math.degrees(th), tau))
            pts, com = npts, ncom
            took = True
            break
        if not took:
            return steps, True                         # balanced / rolling: at rest
    return steps, False                                # cap hit: report, don't loop


def _bed_frame(_plane):
    # (origin, basis) of the bed: columns of B are its x/y/z dirs, world frame
    import numpy as _np
    if isinstance(_plane, Plane):
        o = _np.array(tuple(_plane.origin), dtype=float)
        B = _np.column_stack([_np.array(tuple(d), dtype=float)
                              for d in (_plane.x_dir, _plane.y_dir, _plane.z_dir)])
        return o, B
    return _np.zeros(3), _np.eye(3)


def _drop_apply(_shape, _B, _o, _ops):
    \"\"\"Seat _shape at one moment of its journey: _ops is the ordered event
    prefix — ("t", dz) translations along the bed normal and ("r", p, ax, deg)
    rotations in bed coordinates — applied in sequence on either lane.\"\"\"
    import numpy as _np
    n = _B[:, 2]
    if _is_mesh(_shape):
        M = _np.eye(4)
        for op in _ops:
            if op[0] == "t":
                Tm = _np.eye(4)
                Tm[:3, 3] = op[1] * n
                M = Tm @ M
            elif op[0] == "t3":
                Tm = _np.eye(4)
                Tm[:3, 3] = _B @ _np.asarray(op[1])
                M = Tm @ M
            else:
                M = _tm().transformations.rotation_matrix(
                    math.radians(op[3]), _B @ _np.asarray(op[2]),
                    _B @ _np.asarray(op[1]) + _o) @ M
        return _shape.transformed(M)
    s = _shape
    moved = False
    for op in _ops:
        if op[0] == "t":
            s = _move(s, None, float(op[1] * n[0]), float(op[1] * n[1]), float(op[1] * n[2]))
        elif op[0] == "t3":
            v = _B @ _np.asarray(op[1])
            s = _move(s, None, float(v[0]), float(v[1]), float(v[2]))
        else:
            pw = _B @ _np.asarray(op[1]) + _o
            aw = _B @ _np.asarray(op[2])
            s = s.rotate(Axis((pw[0], pw[1], pw[2]), (aw[0], aw[1], aw[2])), op[3])
        moved = True
    if not moved:
        s = _move(s, None, 0.0, 0.0, 0.0)              # a COPY even at rest (tags ride results)
    return s


def _drop(_shape, _plane=None, _t=1.0, _material="plastic", _settle=True,
          _collide=False, _container=None, _grip=1.0, _motion=None, _owner_ids=None,
          _shape_ids=None):
    \"\"\"A real fall onto the plane, scrubbed by _t: 0 = where the part is now,
    1 = at rest. The part falls, BOUNCES (each impact keeps _DROP_E of its
    speed), and — with `settle` — TOPPLES: once the bounces die, the quasi-
    static cascade of _settle_plan tips it about its support edges until its
    centre of mass sits over the contact polygon. With `collide` and several
    shapes wired into the SAME node they fall as ONE SCENE instead of a fan
    (_drop_collide): sequentially, each onto the bed or onto the parts already
    down. A `container` is an immovable collider they land IN — a bowl, a tray —
    kept concave rather than hulled, and it turns scene mode on by itself (there
    is no other way to honour it, and one part falling into a bowl is the point).
    With a `motion` plan wired in, that container stops being furniture: it tilts,
    shakes or spins on the same timeline, and the parts answer to it through
    contact alone (_container_motion / _motion_driver).
    Works on both lanes: it measures on the mesh and transforms the ORIGINAL, so
    a solid stays a solid. A part starting under the plane surfaces linearly —
    it cannot fall.\"\"\"
    if _container is not None or (_collide and isinstance(_shape, (list, tuple))):
        shapes = list(_shape) if isinstance(_shape, (list, tuple)) else [_shape]
        out = _drop_collide(shapes, _plane, _t, _material, _settle, _container,
                            _grip, _motion, _owner_ids, _shape_ids)
        return out if isinstance(_shape, (list, tuple)) else (out[0] if out else None)
    if _shape is None:
        return None
    m = _as_mesh(_shape)
    if m is None:
        return _shape
    import numpy as _np
    o, B = _bed_frame(_plane)
    V = (m.tm.vertices - o) @ B                        # bed coordinates
    h0 = float(V[:, 2].min())
    t = max(0.0, min(float(_t), 1.0))
    g = 9810.0
    steps = []
    if _settle:
        tm_ = m.tm
        try:
            cw = _np.asarray(tm_.center_mass, dtype=float)
            if not (tm_.is_watertight and float(tm_.volume) > 1e-9):
                raise ValueError                       # open patch: mass centre is a lie
        except Exception:
            cw = _np.asarray(tm_.bounds, dtype=float).mean(axis=0)
        com = (cw - o) @ B
        com[2] -= h0
        try:
            from scipy.spatial import ConvexHull as _CH
            hp = V[_CH(V, qhull_options="QJ").vertices].copy()
        except Exception:
            hp = V.copy()
        hp[:, 2] -= h0
        steps, _ok = _settle_plan(hp, com)
    segs = _drop_segs(_DROP_E.get(str(_material), 0.55))
    tot_n = sum(d for d, _ in segs)
    t0 = math.sqrt(2.0 * abs(h0) / g)
    Tb = t0 * (tot_n if h0 > 0.0 else 1.0)             # bounce (or surfacing) seconds
    Ts = sum(s[3] for s in steps)                      # topple seconds
    T = Tb + Ts
    if T <= 1e-12:
        return _shape                                  # already at rest, already stable
    n = B[:, 2]
    # The whole journey, as plain data riding on the result (mesh_extractor lifts
    # it into the preview entry as `anim`): the editor replays it in the browser
    # at 60fps while the slider drags — same math, world coordinates, plus the t
    # THIS preview was baked at, so the browser can move relative to it.
    plan = {"t": float(t), "T": float(T), "Tb": float(Tb), "h0": float(h0),
            "tot_n": float(tot_n),
            "n": [float(n[0]), float(n[1]), float(n[2])],
            "segs": [[float(d), None if up is None else float(up)]
                     for d, up in segs],
            "steps": [{"p": [float(x) for x in (B @ _np.asarray(p) + o)],
                       "ax": [float(x) for x in (B @ _np.asarray(ax))],
                       "deg": float(deg), "du": float(du)}
                      for (p, ax, deg, du) in steps]}

    def _tag(_res):
        try:
            _res._noodle_anim = plan
        except Exception:
            pass
        return _res

    tau = t * T
    if tau < Tb or not steps:                          # still in the air
        f = tau / Tb if Tb > 0.0 else 1.0
        h = h0 * (1.0 - f) if h0 <= 0.0 else h0 * _drop_height(segs, f * tot_n)
        return _tag(_drop_apply(_shape, B, o, [("t", float(h - h0))]))
    ops = [("t", float(-h0))]
    left = tau - Tb                                    # landed: replay the topples
    for (p, ax, deg, du) in steps:
        if left >= du - 1e-12:
            ops.append(("r", p, ax, deg))
            left -= du
        else:
            fr = max(0.0, left / du)
            ops.append(("r", p, ax, deg * fr * fr))    # ease-in: a topple starts slow
            break
    return _tag(_drop_apply(_shape, B, o, ops))


def _quat_mat(_q):
    # 3x3 from a pybullet quaternion (x, y, z, w)
    import numpy as _np
    x, y, z, w = float(_q[0]), float(_q[1]), float(_q[2]), float(_q[3])
    return _np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _quat_slerp(_qa, _qb, _f):
    import numpy as _np
    qa = _np.asarray(_qa, dtype=float)
    qb = _np.asarray(_qb, dtype=float)
    d = float(qa @ qb)
    if d < 0.0:
        qb, d = -qb, -d
    if d > 0.9995:
        q = qa + _f * (qb - qa)
        return q / _np.linalg.norm(q)
    th = math.acos(max(-1.0, min(1.0, d)))
    return (math.sin((1 - _f) * th) * qa + math.sin(_f * th) * qb) / math.sin(th)


def _mat_quat(_R):
    \"\"\"3x3 rotation -> pybullet quaternion (x, y, z, w). Shepperd's branch: pick
    the largest diagonal term so the divisor is never near zero.\"\"\"
    import numpy as _np
    R = _np.asarray(_R, dtype=float)
    tr = R[0, 0] + R[1, 1] + R[2, 2]
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        return [(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s,
                (R[1, 0] - R[0, 1]) / s, 0.25 * s]
    i = int(_np.argmax([R[0, 0], R[1, 1], R[2, 2]]))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(max(1.0 + R[i, i] - R[j, j] - R[k, k], 1e-30)) * 2.0
    q = [0.0, 0.0, 0.0, (R[k, j] - R[j, k]) / s]
    q[i], q[j], q[k] = 0.25 * s, (R[j, i] + R[i, j]) / s, (R[k, i] + R[i, k]) / s
    return q


def _container_motion(_offset=None, _x=0.0, _y=0.0, _z=0.0,
                      _rx=0.0, _ry=0.0, _rz=0.0, _pivot=None,
                      _cycles=0.0, _duration=1.0, _delay=0.0, _easing="smooth"):
    \"\"\"A prescribed motion for a Drop `container`: the bowl/tray/crate stops being
    furniture and becomes the thing that DOES something — tilt it and it pours,
    shake it and the pile packs, spin it and the contents lag behind.

    It is deliberately generic rather than a menu of presets: one translation, one
    rotation, and `cycles`, which chooses between the two shapes of motion anyone
    actually wants. cycles = 0 is a RAMP — go there once and stay (tilt, pour,
    tip a crate over). cycles > 0 is an OSCILLATION about the start pose (shake,
    stir, vibrate, tap). Every preset falls out of that: pour = rotate 110 deg,
    cycles 0; shake = move 10mm, cycles 8; centrifuge = rotate z 720.

    This is NOT gravity and NOT a fall — it is a motion you dictate, and the parts
    inside respond to it through contact and friction alone. Returns a plain plan
    dict; `_motion_driver` turns it into the pose(t) the solver drives.\"\"\"
    def _v3(v, fb):
        if v is None:
            return [float(fb[0]), float(fb[1]), float(fb[2])]
        if hasattr(v, "X"):
            return [float(v.X), float(v.Y), float(v.Z)]
        try:
            s = list(v)[:3]
            return [float(s[0]), float(s[1]), float(s[2])]
        except Exception:
            return [float(fb[0]), float(fb[1]), float(fb[2])]
    return {"move": _v3(_offset, (_x, _y, _z)),
            "rot": [float(_rx), float(_ry), float(_rz)],
            "pivot": None if _pivot is None else _v3(_pivot, (0.0, 0.0, 0.0)),
            "cycles": max(0.0, float(_cycles)),
            "duration": max(1e-3, float(_duration)),
            "delay": max(0.0, float(_delay)),
            "easing": str(_easing)}


def _motion_driver(_plan, _B, _o, _pivot_bed):
    \"\"\"Turn a motion plan into `pose(tau) -> (position, quaternion)` for the static
    rig, in BED coordinates (where the colliders live).

    The user dictates the motion in WORLD xyz — that is how they think about a
    tray — so both the displacement and the rotation axes are carried into the bed
    frame here: R_bed = B^T R_world B, and a rotation about pivot p becomes the
    base position p - R p (bullet poses a body as x -> R x + pos, so that is the
    only way to make it turn about anything other than the bed origin).\"\"\"
    import numpy as _np
    if _plan is None:
        return None, 0.0
    mv = _np.asarray(_plan["move"], dtype=float)
    rot = _np.asarray(_plan["rot"], dtype=float)
    dur, dly = float(_plan["duration"]), float(_plan["delay"])
    cyc, ease = float(_plan["cycles"]), str(_plan.get("easing", "smooth"))
    p_b = (_np.asarray(_plan["pivot"], dtype=float) - _o) @ _B \
        if _plan.get("pivot") is not None else _np.asarray(_pivot_bed, dtype=float)
    d_b = mv @ _B                                      # world displacement -> bed
    end = dly + dur

    def _phase(tau):
        u = (float(tau) - dly) / dur
        if u <= 0.0:
            return 0.0
        if cyc > 0.0:
            # An oscillation ABOUT the start pose, and it must return to it: past
            # the end the rig is parked where it began, not frozen mid-swing.
            return math.sin(2.0 * math.pi * cyc * min(u, 1.0)) if u <= 1.0 else 0.0
        u = min(u, 1.0)
        return u * u * (3.0 - 2.0 * u) if ease == "smooth" else u

    def pose(tau):
        s = _phase(tau)
        a = _np.radians(rot * s)
        ca, sa = _np.cos(a), _np.sin(a)
        Rx = _np.array([[1, 0, 0], [0, ca[0], -sa[0]], [0, sa[0], ca[0]]])
        Ry = _np.array([[ca[1], 0, sa[1]], [0, 1, 0], [-sa[1], 0, ca[1]]])
        Rz = _np.array([[ca[2], -sa[2], 0], [sa[2], ca[2], 0], [0, 0, 1]])
        R_b = _B.T @ (Rz @ Ry @ Rx) @ _B               # world euler -> bed frame
        return p_b - R_b @ p_b + s * d_b, _mat_quat(R_b)

    return pose, end


def _animate(_shape, _motion=None, _t=1.0, _hold=0.0):
    \"\"\"A prescribed motion with NO physics: the shape simply GOES where the plan
    says, on a timeline you scrub. A lid unscrewing off a jar, a drawer sliding
    out, a hinge swinging, a lathe chuck spinning, a part lifted clear of an
    assembly to show how it comes apart.

    It is the SAME motion vocabulary that drives a Drop's container
    (`_container_motion` / `_motion_driver`), so one Motion node can tilt a jar
    and unscrew its cap on one clock. The difference is that nothing here is
    simulated and nothing answers back: no solver, no contact, no friction, no
    cost — and equally, no falling, no collision. Screw motions fall straight
    out of the shared plan because translation and rotation run on ONE phase:
    move z 12 + rotate z 720, cycles 0, and the cap rises as it turns.

    Baked to keyframes rather than left analytic on purpose: the browser already
    replays a "keys" plan at 60fps (sceneBodyPose — the collide-scene machinery),
    so scrubbing costs no engine run and no frontend format was invented.\"\"\"
    if _shape is None or _motion is None:
        return _shape
    import numpy as _np
    t = max(0.0, min(float(_t), 1.0))
    B, o = _np.eye(3), _np.zeros(3)
    # Rotation is about the part's OWN centre unless the plan names a pivot, and
    # that centre is measured on the tessellation for the same reason PlaceOnBed
    # is: the fast OCCT bounding box is oversized, and an off-centre axis is
    # exactly what an unscrewing lid cannot afford.
    m = _as_mesh(_shape)
    if m is None:
        return _shape
    bb = _np.asarray(m.tm.bounds, dtype=float)
    pose, end = _motion_driver(_motion, B, o, 0.5 * (bb[0] + bb[1]))
    if pose is None or end <= 1e-9:
        return _shape
    # `hold` pads the timeline AFTER the motion is over, and its only job is to
    # let one slider drive this node and something else — a Drop, another
    # Animate — on ONE clock. The wire from a slider must go STRAIGHT into `t`
    # for the live 60fps replay to see it (applyDropTargets follows direct links
    # only), so rescaling t through a Remap in between costs you the scrub.
    # Padding the shorter timeline instead keeps the wire direct. Past `end` the
    # phase already parks — at the destination for a ramp, at the start for an
    # oscillation — so holding is free and exact.
    T = float(end) + max(0.0, float(_hold))
    cyc = float(_motion.get("cycles", 0.0) or 0.0)
    n = int(max(T * 60.0, cyc * 24.0, 2.0))            # >=24 samples per cycle
    n = min(n, 2000)
    times = [T * i / n for i in range(n + 1)]
    poss, quats = [], []
    for tau in times:
        p_i, q_i = pose(tau)
        poss.append([float(x) for x in p_i])
        quats.append([float(x) for x in q_i])
    plan = {"kind": "keys", "t": float(t), "T": T,
            "c0": [0.0, 0.0, 0.0], "times": times, "pos": poss, "quat": quats}
    p_t, q_t = pose(t * T)
    ax, ang = _axis_angle(q_t)
    ops = []
    if ang > 1e-9:
        ops.append(("r", (0.0, 0.0, 0.0), tuple(ax), math.degrees(ang)))
    ops.append(("t3", tuple(float(x) for x in p_t)))
    res = _drop_apply(_shape, B, o, ops)
    try:
        res._noodle_anim = plan
    except Exception:
        pass
    return res


def _dyn_sim(_hulls, _coms, _e, _t_max=8.0, _statics=(), _grip=1.0,
             _pose=None, _drive_until=0.0):
    \"\"\"The rigid-body simulation behind collide: pybullet, DIRECT mode, fixed
    timestep (deterministic for a given scene on a given build), every part a
    CONVEX HULL of its mesh. All parts fall TOGETHER — they hit each other in
    the air, push each other over, tumble, stack — and the run is recorded as
    60Hz keyframes per body until everything sleeps.

    _statics are the immovable colliders (the `container` socket): mass 0, and
    — the whole point — NOT hulled. A static body may be a concave triangle
    soup (GEOM_FORCE_CONCAVE_TRIMESH), so a bowl keeps its cavity and actually
    cradles what falls in instead of being a dome that sheds it. Bullet only
    allows that for static bodies, which is exactly the trade this socket makes:
    the thing that holds is exact, the things that fall are hulls.

    `_pose(tau) -> (position, quaternion)` MOVES that rig (the ContainerMotion
    node): tilt, shake, spin. Two things are load-bearing and both were measured.
    resetBasePositionAndOrientation alone does NOT carry the contents — it
    teleports the body, so the contact exists with zero relative velocity, the
    friction solver has nothing to transmit and the tray slides out from under
    the part (a box on a tray translated 50mm rode along 1.2% of the way, i.e.
    not at all). Pairing it with resetBaseVelocity every step is what makes the
    contact real: 99.3% carried. And the obvious alternative — a real mass with a
    JOINT_FIXED constraint driven by changeConstraint — carries just as well
    (99.9%) and is still WRONG here, because mass > 0 forbids
    GEOM_FORCE_CONCAVE_TRIMESH: it would hull the bowl and throw away the cavity,
    which is the only reason the container socket exists.

    Units are millimetres DIRECTLY (no down-scaling): pybullet's collision
    margin is a fixed absolute value, so working in mm makes it a sub-micron
    gap instead of the ~1mm it becomes when a 20mm cube is shrunk to 0.02m.
    Tunnelling risk is scale-invariant (speed x dt / thickness), so nothing is
    lost — and continuous collision (a swept sphere per body) covers the fast,
    thin cases anyway. Mass is the hull VOLUME (so ratios are physical: a heavy
    part settles a light one, not the reverse). Returns (times, [pos per body],
    [quat per body]).\"\"\"
    import numpy as _np
    import pybullet as _pb
    from scipy.spatial import ConvexHull as _CH
    cl = _pb.connect(_pb.DIRECT)
    try:
        # restitutionVelocityThreshold in mm/s: BELOW it a contact is inelastic.
        # Bullet's default is 0.2 m/s = 200mm/s; the 1mm/s I first used makes
        # every real contact elastic, so the pile jitters forever instead of
        # settling. With this + rolling/spinning friction + angular damping a
        # three-box scene sleeps in ~0.6s where before it ran the full 8s.
        _pb.setPhysicsEngineParameter(fixedTimeStep=1.0 / 240.0,
                                      numSolverIterations=80,
                                      restitutionVelocityThreshold=100.0,
                                      deterministicOverlappingPairs=1,
                                      physicsClientId=cl)
        _pb.setGravity(0.0, 0.0, -9810.0, physicsClientId=cl)
        plane = _pb.createCollisionShape(_pb.GEOM_PLANE, physicsClientId=cl)
        pbody = _pb.createMultiBody(0.0, plane, physicsClientId=cl)
        _pb.changeDynamics(pbody, -1, restitution=1.0, lateralFriction=0.8 * _grip,
                           physicsClientId=cl)
        sids = []
        for (sv, sf, _src, _own) in _statics:
            scs = _pb.createCollisionShape(
                _pb.GEOM_MESH, vertices=_np.asarray(sv, dtype=float).tolist(),
                indices=[int(i) for i in _np.asarray(sf).reshape(-1)],
                flags=_pb.GEOM_FORCE_CONCAVE_TRIMESH, physicsClientId=cl)
            sid = _pb.createMultiBody(0.0, scs, basePosition=[0.0, 0.0, 0.0],
                                      physicsClientId=cl)
            # A little grippier and deader than the bed: a part that lands in a
            # bowl should stop there, not skate around the cavity for 8 seconds.
            # Scaled by grip, and that scaling is load-bearing: at 0.9 a sloped
            # static face GRABS a ball and throws it sideways instead of letting
            # it slide off. Measured on the Galton board — 0.9 gives two lumps
            # against the walls, 0.3 gives the bell.
            _pb.changeDynamics(sid, -1, restitution=0.2, lateralFriction=0.9 * _grip,
                               rollingFriction=0.05 * _grip, spinningFriction=0.05 * _grip,
                               physicsClientId=cl)
            sids.append(sid)
        if _pose is not None and sids:
            p0, q0 = _pose(0.0)
            for sid in sids:
                _pb.resetBasePositionAndOrientation(
                    sid, [float(x) for x in p0], [float(x) for x in q0],
                    physicsClientId=cl)
        ids = []
        for hv, c0 in zip(_hulls, _coms):
            local = _np.asarray(hv, dtype=float) - c0
            cs = _pb.createCollisionShape(_pb.GEOM_MESH, vertices=local.tolist(),
                                          physicsClientId=cl)
            try:
                vol = float(_CH(local).volume)
            except Exception:
                vol = float(_np.prod(local.max(0) - local.min(0)))
            r = float(_np.linalg.norm(local, axis=1).min())    # inscribed-ish radius
            bid = _pb.createMultiBody(baseMass=max(vol, 1.0),
                                      baseCollisionShapeIndex=cs,
                                      basePosition=[float(x) for x in c0],
                                      physicsClientId=cl)
            _pb.changeDynamics(bid, -1, restitution=float(_e), lateralFriction=0.6 * _grip,
                               rollingFriction=0.06 * _grip, spinningFriction=0.06 * _grip,
                               linearDamping=0.02, angularDamping=0.25,
                               ccdSweptSphereRadius=max(r * 0.4, 0.1),
                               physicsClientId=cl)
            ids.append(bid)
        times, poss, quats = [], [[] for _ in ids], [[] for _ in ids]
        spos, squat = [], []
        still = 0
        dt = 1.0 / 240.0
        steps = int(240 * _t_max)
        for k in range(steps):
            tau = k * dt
            if _pose is not None and sids:
                p_a, q_a = _pose(tau)
                p_b2, q_b2 = _pose(tau + dt)           # forward difference = the
                lin = [(float(b) - float(a)) / dt      # velocity the solver needs
                       for a, b in zip(p_a, p_b2)]
                ang = _ang_vel(q_a, q_b2, dt)
                for sid in sids:
                    _pb.resetBasePositionAndOrientation(
                        sid, [float(x) for x in p_a], [float(x) for x in q_a],
                        physicsClientId=cl)
                    _pb.resetBaseVelocity(sid, linearVelocity=lin,
                                          angularVelocity=ang, physicsClientId=cl)
            _pb.stepSimulation(physicsClientId=cl)
            if k % 4:
                continue                               # record at 60Hz (scrub lerps)
            times.append((k + 1) / 240.0)
            if _pose is not None:
                p_r, q_r = _pose(tau + dt)
                spos.append([float(x) for x in p_r])
                squat.append([float(x) for x in q_r])
            calm = True
            # A driven rig must never let the scene "settle": a tray that tilts
            # slowly would otherwise put everything to sleep BEFORE the tilt even
            # begins, and the pile would ride along frozen.
            if _pose is not None and tau <= _drive_until:
                calm = False
            for j, bid in enumerate(ids):
                pos, q = _pb.getBasePositionAndOrientation(bid, physicsClientId=cl)
                poss[j].append([float(p) for p in pos])
                quats[j].append([float(x) for x in q])
                v, w = _pb.getBaseVelocity(bid, physicsClientId=cl)
                if (sum(x * x for x in v) > 4.0 or sum(x * x for x in w) > 0.01):
                    calm = False
            still = still + 1 if calm else 0
            if still >= 30:
                break                                  # half a second of stillness (60Hz)
        return times, poss, quats, spos, squat
    finally:
        _pb.disconnect(physicsClientId=cl)


def _ang_vel(_qa, _qb, _dt):
    \"\"\"Angular velocity carrying quaternion _qa to _qb in _dt: the axis-angle of
    the relative rotation dq = qb * qa^-1 (shortest arc), divided by dt. Taken
    EXACTLY rather than as the usual 2*dq.xyz/dt small-angle reading — that one
    drifts 1% by 30 degrees a step, and a spin node is entitled to ask for a
    whole turn in a fraction of a second.\"\"\"
    ax, ay, az, aw = [float(v) for v in _qa]
    bx, by, bz, bw = [float(v) for v in _qb]
    # qb * conj(qa)
    dx = bw * -ax + bx * aw + by * -az - bz * -ay
    dy = bw * -ay - bx * -az + by * aw + bz * -ax
    dz = bw * -az + bx * -ay - by * -ax + bz * aw
    dw = bw * aw - bx * -ax - by * -ay - bz * -az
    if dw < 0.0:                                       # shortest arc
        dx, dy, dz, dw = -dx, -dy, -dz, -dw
    s = math.sqrt(dx * dx + dy * dy + dz * dz)
    if s < 1e-12:
        return [0.0, 0.0, 0.0]
    k = 2.0 * math.atan2(s, dw) / (s * _dt)            # angle / dt, along the axis
    return [dx * k, dy * k, dz * k]


def _keys_pose(_times, _pos, _quat, _tau):
    \"\"\"Interpolated (position, quaternion) at _tau seconds: lerp + slerp between
    the two bracketing keyframes.\"\"\"
    import numpy as _np
    if not _times:
        return None, None
    if _tau <= _times[0]:
        return _np.asarray(_pos[0], dtype=float), _np.asarray(_quat[0], dtype=float)
    if _tau >= _times[-1]:
        return _np.asarray(_pos[-1], dtype=float), _np.asarray(_quat[-1], dtype=float)
    import bisect
    i = bisect.bisect_right(_times, _tau)
    t0, t1 = _times[i - 1], _times[i]
    f = (_tau - t0) / (t1 - t0) if t1 > t0 else 0.0
    p = (1 - f) * _np.asarray(_pos[i - 1], dtype=float) \
        + f * _np.asarray(_pos[i], dtype=float)
    return p, _quat_slerp(_quat[i - 1], _quat[i], f)


def _static_colliders(_container, _o, _B, _owner_ids=None):
    \"\"\"The `container` input as pybullet-ready triangle soups in bed coordinates:
    (vertices, faces, source shape, owner node id) per body, NOT hulled — that is
    the whole point
    of the socket. Accepts one shape or several wired into it. The source shape
    rides along because a MOVING container has to be drawn as well as simulated,
    and the preview wants the original (either lane), not the soup.\"\"\"
    import numpy as _np
    out = []
    if _container is None:
        return out
    # Flatten as deep as it goes: wiring an ArrayLinear (or an array of arrays —
    # a peg grid is exactly that) into `container` hands us nested lists, and the
    # alternative is making the user thread ListFlatten through every branch.
    def _flat(v, out):
        if isinstance(v, (list, tuple)):
            for it in v:
                _flat(it, out)
        elif v is not None:
            out.append(v)
        return out

    # Owner ids are per TOP-LEVEL container input, so flatten each separately and
    # carry its id down: it is what lets the viewer give the bowl its own finish
    # instead of the falling parts' (CLAUDE.md §5d-bis).
    tops = list(_container) if isinstance(_container, (list, tuple)) else [_container]
    ids = list(_owner_ids or [])
    for k, top in enumerate(tops):
        owner = ids[k] if k < len(ids) else (ids[0] if len(ids) == 1 else None)
        for c in _flat(top, []):
            if c is None:
                continue
            m = _as_mesh(c)
            if m is None or len(m.tm.faces) == 0:
                continue
            V = (_np.asarray(m.tm.vertices, dtype=float) - _o) @ _B
            out.append((V, _np.asarray(m.tm.faces, dtype=int), c, owner))
    return out


def _drop_collide(_shapes, _plane=None, _t=1.0, _material="plastic", _settle=True,
                  _container=None, _grip=1.0, _motion=None, _owner_ids=None,
                  _shape_ids=None):
    \"\"\"The multi-body drop, done with real dynamics: every shape wired into the
    node becomes a rigid body (its convex hull) in ONE pybullet scene, and they
    all fall TOGETHER — colliding in the air, pushing each other, tumbling,
    stacking — until everything sleeps. The run is recorded as keyframes; the
    timeline scrubs them, and each returned shape carries its own keyframe plan
    (`_noodle_anim` kind "keys") so the editor can replay the whole scene in
    the browser. Declared limits: hulls, not the true meshes (a bowl will not
    cradle a ball); rest poses carry the solver's contact margin (a fraction of
    a mm), not CAD exactness; deterministic for a given scene on a given build,
    but chaotic in the physical sense — move a part a hair and the pile lands
    differently. That is not a bug, that is what falling IS.\"\"\"
    import numpy as _np
    o, B = _bed_frame(_plane)
    e = _DROP_E.get(str(_material), 0.55)
    t = max(0.0, min(float(_t), 1.0))
    bodies = []
    for i, s in enumerate(_shapes):
        m = _as_mesh(s) if s is not None else None
        if m is None:
            bodies.append({"i": i, "shape": s, "hull": None})
            continue
        Vl = (m.tm.vertices - o) @ B
        try:
            from scipy.spatial import ConvexHull as _CH
            hv = Vl[_CH(Vl, qhull_options="QJ").vertices]
        except Exception:
            hv = Vl
        bodies.append({"i": i, "shape": s, "hull": hv,
                       "c0": hv.mean(axis=0)})
    live = [b for b in bodies if b["hull"] is not None]
    results = [bb["shape"] for bb in bodies]           # non-meshables pass through
    if not live:
        return results
    statics = _static_colliders(_container, o, B, _owner_ids)
    pose, drive_until = None, 0.0
    if _motion is not None and statics:
        allV = _np.vstack([sv for (sv, _sf, _s, _o2) in statics])
        pose, drive_until = _motion_driver(
            _motion, B, o, 0.5 * (allV.min(axis=0) + allV.max(axis=0)))
    times, poss, quats, spos, squat = _dyn_sim(
        [b["hull"] for b in live], [b["c0"] for b in live], e,
        _t_max=max(8.0, drive_until + 2.0), _statics=statics,
        _grip=float(_grip), _pose=pose, _drive_until=drive_until)
    if not times:
        return results
    T = times[-1]
    tau = t * T
    for j, b in enumerate(live):
        p_t, q_t = _keys_pose(times, poss[j], quats[j], tau)
        if p_t is None:
            continue
        R = _quat_mat(q_t)                             # bed-frame pose of the body
        ax, ang = _axis_angle(q_t)
        c0 = b["c0"]
        ops = []
        if ang > 1e-9:
            ops.append(("r", tuple(c0), tuple(ax), math.degrees(ang)))
        ops.append(("t3", tuple(p_t - c0)))
        res = _drop_apply(b["shape"], B, o, ops)
        plan = {"kind": "keys", "t": float(t), "T": float(T),
                "c0": [float(x) for x in (B @ c0 + o)],
                "times": [float(x) for x in times],
                "pos": [[float(x) for x in (B @ _np.asarray(pp) + o)]
                        for pp in poss[j]],
                "quat": [[float(x) for x in qq] for qq in quats[j]]}
        try:
            res._noodle_anim = plan
            # Which node drew THIS falling body. Several shapes wired into one
            # Drop usually come from several nodes (five bolts, five Moves), so
            # they can be styled apart exactly like the container. Only a single
            # node fanning out into many pieces genuinely shares an owner.
            if _shape_ids:
                _k = b["i"]
                res._noodle_owner = _shape_ids[_k] if _k < len(_shape_ids) else None
        except Exception:
            pass
        results[b["i"]] = res
    if pose is not None and statics and live:
        # A moving container has to be SEEN moving, or the pile looks haunted. It
        # is not an output of this node and must not become one, so it rides the
        # result as `_noodle_extra`: mesh_extractor turns those into extra bodies
        # of the same Scene preview, each with its own keyframe track, and the
        # browser replays them with the machinery the parts already use.
        p_s, q_s = pose(tau)
        ax_s, ang_s = _axis_angle(q_s)
        ops_s = []
        if ang_s > 1e-9:
            ops_s.append(("r", (0.0, 0.0, 0.0), tuple(ax_s), math.degrees(ang_s)))
        ops_s.append(("t3", tuple(p_s)))
        plan_s = {"kind": "keys", "t": float(t), "T": float(T),
                  "c0": [float(x) for x in o],
                  "times": [float(x) for x in times],
                  "pos": [[float(x) for x in (B @ _np.asarray(pp) + o)]
                          for pp in spos],
                  "quat": [[float(x) for x in qq] for qq in squat]}
        extras = []
        for (_sv, _sf, src, owner) in statics:
            try:
                g = _drop_apply(src, B, o, ops_s)
                g._noodle_anim = plan_s
                # Whose node drew this? The viewer resolves colour AND finish per
                # body from it, so a glass jar can pour steel bolts (§5d-bis).
                g._noodle_owner = owner
                extras.append(g)
            except Exception:
                pass
        if extras:
            try:
                results[live[0]["i"]]._noodle_extra = extras
            except Exception:
                pass
    return results


def _axis_angle(_q):
    # (unit axis, angle rad) from a pybullet quaternion (x, y, z, w)
    import numpy as _np
    v = _np.asarray(_q[:3], dtype=float)
    n = float(_np.linalg.norm(v))
    if n < 1e-12:
        return _np.array([0.0, 0.0, 1.0]), 0.0
    return v / n, 2.0 * math.atan2(n, float(_q[3]))


def _overhang_faces(_mesh, _angle=45.0, _layer=0.2):
    \"\"\"Just the faces that will need support, as a mesh of their own — so the viewer
    gives them their own colour and you SEE them on the part. Open by construction:
    it is a patch, not a body, and MeshInspect will say so.\"\"\"
    m = _as_mesh(_mesh)
    if m is None:
        return None
    met = _print_metrics(m, _angle, _layer, _nsec=2)
    if met["n_over"] == 0:
        return None                              # nothing overhangs: an empty preview
    return Mesh(m.tm.submesh([met["over_mask"]], append=True, repair=False))


def _print_check(_mesh, _angle=45.0, _layer=0.2, _clearance=0.2):
    \"\"\"The print report: how tall, how much support, how well it sticks — and WHERE
    IT WILL BREAK. Wire it into a Panel.\"\"\"
    m = _as_mesh(_mesh)
    if m is None:
        return "no mesh"
    met = _print_metrics(m, _angle, _layer)
    layers = int(met["height"] / max(float(_layer), 0.01)) + 1
    out = [
        "height      : %.1f mm  (%d layers at %.2f mm)" % (met["height"], layers, _layer),
        "bed contact : %.1f mm2" % met["bed_area"],
    ]
    try:                                 # the real thing: prisms to the bed, minus the part
        _, line = _support_report(m, _angle, _layer, _clearance)
        out.append(line)
    except Exception as _e:              # not watertight -> no boolean -> the proxy, said so
        out.append("support     : %.0f mm2 of face overhangs past %.0f deg (area only — "
                   "the true volume\\n              needs a closed mesh: %s)"
                   % (met["support_area"], _angle, _e))
    if met["sectioned"]:
        out += [
            "",
            "WEAK PLANE  : %.1f mm2 at z = %.1f mm" % (met["weak_area"], met["weak_z"]),
            "              That is the smallest glued area in the part, and a printed",
            "              part comes apart at a layer line before it breaks anywhere",
            "              else: across that plane the load is carried by layer",
            "              adhesion alone (roughly a third to two thirds of the",
            "              strength the same material has within a layer). Turn the",
            "              part so the load runs ALONG the layers, not through them.",
        ]
    else:
        out += ["", "WEAK PLANE  : not watertight — no honest cross-section to measure.",
                "              Put a Mesh Fix in front of this."]
    return "\\n".join(out)


def _rest_planes(_mesh):
    \"\"\"The faces the part could actually rest on: every face of its convex hull whose
    polygon contains the centre of mass, projected down. That is what "stable" means —
    let go of it and it stays. (trimesh has compute_stable_poses, but it wants networkx
    and shapely, which are not in the image; the convex hull is scipy, which is.)\"\"\"
    import numpy as _np
    tm = _mesh.tm
    hull = tm.convex_hull
    com = _np.asarray(tm.center_mass, dtype=float)
    # Cluster the hull's faces by normal with a TOLERANCE, not by a rounded key: one
    # flat side of the part is many triangles whose normals agree to 6 decimals and
    # not to 3, and a rounded key splits them into two identical "poses".
    groups = []                                  # [(normal, [face indices])]
    for fi, nrm in enumerate(hull.face_normals):
        for g in groups:
            if float(_np.dot(g[0], nrm)) > 0.999:
                g[1].append(fi)
                break
        else:
            groups.append((_np.asarray(nrm, dtype=float), [fi]))
    seen = {tuple(g[0]): g[1] for g in groups}
    planes = []
    for key, faces in seen.items():
        nrm = _np.asarray(key, dtype=float)
        nrm /= (_np.linalg.norm(nrm) or 1.0)
        pts = hull.vertices[_np.unique(hull.faces[faces].ravel())]
        # is the centre of mass over the footprint? Build a 2D frame on the plane and
        # ask whether the projected COM is inside the convex hull of the projected face.
        u = _np.cross(nrm, [0.0, 0.0, 1.0])
        if _np.linalg.norm(u) < 1e-6:
            u = _np.cross(nrm, [0.0, 1.0, 0.0])
        u /= (_np.linalg.norm(u) or 1.0)
        v = _np.cross(nrm, u)
        p2 = _np.column_stack([pts @ u, pts @ v])
        c2 = _np.array([com @ u, com @ v])
        try:
            from scipy.spatial import ConvexHull as _CH
            eq = _CH(p2).equations                # A x + b <= 0 inside
            inside = bool(_np.all(eq[:, :2] @ c2 + eq[:, 2] <= 1e-6))
        except Exception:
            inside = True                         # degenerate footprint: keep it, and let
        if inside:                                # the score decide
            planes.append(nrm)
    return planes


def _orient_plan(_mesh, _load=None, _w_strength=1.0, _w_support=1.0, _w_speed=0.3,
                 _angle=45.0, _layer=0.2, _exact_below=25000, _max_cand=60):
    \"\"\"Try every stable resting pose, score it, keep the best.

    STRENGTH is the term worth reading twice. With a `load` wired in (the direction the
    part will be pulled), the score is |load . Z| after the rotation: you want the load
    lying IN the layer plane, not crossing it, because crossing it is the printer's
    weakest direction by a factor of two or three. With no load declared, the proxy is
    the largest weak plane — maximise the smallest glued cross-section.\"\"\"
    import numpy as _np
    m = _as_mesh(_mesh)
    if m is None:
        return {"mesh": None, "report": "no mesh"}
    tmesh = _tm()
    planes = _rest_planes(m)[: int(_max_cand)]
    load = None
    if _load is not None:
        v = _np.array([_load.X, _load.Y, _load.Z] if hasattr(_load, "X")
                      else list(_load)[:3], dtype=float)
        nv = _np.linalg.norm(v)
        load = v / nv if nv > 1e-9 else None
    # The real support volume is a boolean per pose (~0.6s on a 20k-triangle part): fine
    # for a handful of poses on a modest part, not for a big mesh. Above the budget the
    # search falls back to the `area x height` proxy — and the report says which it used,
    # because a number whose provenance is a secret is worse than no number.
    #
    # It is all-or-nothing on purpose. Scoring one pose by real volume and the next by a
    # proxy would rank two different quantities against each other and call it a decision.
    def _score_all(exact):
        out = []
        for nrm in planes:
            R = tmesh.geometry.align_vectors(nrm, [0.0, 0.0, -1.0])  # that face -> the bed
            dropped = _bed_drop(m.transformed(R), True, 0.0)
            met = _print_metrics(dropped, _angle, _layer)
            if exact:
                body = _support_body(dropped, _angle, _layer)        # may raise: not closed
                met["support_vol"] = float(body.volume) if body is not None else 0.0
            met["mesh"] = dropped
            met["load_z"] = abs(float(R[:3, :3] @ load @ _np.array([0, 0, 1.0]))) \
                if load is not None else None
            out.append(met)
        return out

    exact = m.n_tris <= int(_exact_below)
    try:
        cands = _score_all(exact)
    except Exception:
        exact = False
        cands = _score_all(False)
    if not cands:
        return {"mesh": m, "report": "no stable resting pose found"}
    mx = lambda k: max(max(c[k] for c in cands), 1e-9)            # noqa: E731
    hi_w, hi_s, hi_h, hi_b = mx("weak_area"), mx("support_vol"), mx("height"), mx("bed_area")
    for c in cands:
        strength = (c["load_z"] if c["load_z"] is not None
                    else 1.0 - c["weak_area"] / hi_w)             # 0 = strong
        c["score"] = (float(_w_strength) * strength
                      + float(_w_support) * (c["support_vol"] / hi_s
                                             + 0.4 * (1.0 - c["bed_area"] / hi_b))
                      + float(_w_speed) * (c["height"] / hi_h))
        c["strength"] = strength
    cands.sort(key=lambda c: c["score"])
    best = cands[0]
    head = ("load declared: strength = how much of it crosses the layers (0 is best)"
            if load is not None else
            "no load declared: strength = the smallest glued cross-section (bigger is better)")
    sup_unit = "cm3" if exact else "mm2xmm"
    rows = ["%-4s %-8s %-9s %-10s %-8s %s" % ("", "score", "strength",
                                              "support " + sup_unit, "height", "weak plane")]
    for i, c in enumerate(cands[:5]):
        sup = ("%.2f" % (c["support_vol"] / 1000.0)) if exact else ("%.0f" % c["support_vol"])
        rows.append("%-4s %-8.3f %-9s %-10s %-8.1f %.0f mm2 @ z=%.1f%s" % (
            "->" if i == 0 else " %d" % (i + 1), c["score"],
            ("%.2f" % c["load_z"]) if c["load_z"] is not None else "%.0f mm2" % c["weak_area"],
            sup, c["height"], c["weak_area"], c["weak_z"],
            "   <- chosen" if i == 0 else ""))
    prov = ("support is the REAL volume: prisms under every overhang, down to the bed, "
            "minus the part" if exact else
            "support is the area x height PROXY — the part is over `exact below` triangles, "
            "and\\na boolean per pose would cost more than the answer is worth. Wire a "
            "Support Volume\\nnode onto the winner for the true number.")
    report = "\\n".join(
        ["%d stable poses tried, %s" % (len(cands), head), ""] + rows
        + ["", prov, "",
           "Weights: strength %.1f, support %.1f, speed %.1f — they are a taste, not"
           % (_w_strength, _w_support, _w_speed),
           "a law. The measurements above are the part; the ranking is your priorities."])
    return {"mesh": best["mesh"], "report": report}


def _mesh_matrix(_kind, **_kw):
    \"\"\"Build the 4x4 for a transform on the mesh lane. A Move/Rotate/Scale/Mirror
    on triangles is exactly a matrix on the vertex array — the SAME operation the
    B-Rep lane expresses as a Location, which is why one node serves both.\"\"\"
    import numpy as _np
    m = _np.eye(4)
    if _kind == "move":
        m[:3, 3] = [_kw["x"], _kw["y"], _kw["z"]]
    elif _kind == "rotate":
        d = _kw["axis"].direction
        p = _kw.get("point")
        m = _tm().transformations.rotation_matrix(
            math.radians(float(_kw["angle"])), [d.X, d.Y, d.Z],
            [p.X, p.Y, p.Z] if p is not None else [0, 0, 0])
    elif _kind == "scale":
        m[0, 0], m[1, 1], m[2, 2] = _kw["x"], _kw["y"], _kw["z"]
    elif _kind == "mirror":
        p = _kw["plane"]
        o, n = p.origin, p.z_dir
        m = _tm().transformations.reflection_matrix([o.X, o.Y, o.Z], [n.X, n.Y, n.Z])
    return m


def _pivot_of(_x):
    \"\"\"The point a shape turns about when no explicit pivot is wired: the bbox
    CENTRE, measured on the tessellation (the fast OCCT box is oversized, same
    reason _bed_drop measures on triangles), aggregated over a list — the union
    box of everything, so a group turns rigidly about one shared point. Planes
    pivot on their origin; point-likes are their own pivot.\"\"\"
    los, his = [], []
    for it in _flatten([_x]):
        if it is None:
            continue
        if isinstance(it, Plane):
            q = it.origin
            los.append((q.X, q.Y, q.Z)); his.append((q.X, q.Y, q.Z))
            continue
        q = _as_point(it)
        if q is not None:
            los.append((q.X, q.Y, q.Z)); his.append((q.X, q.Y, q.Z))
            continue
        try:
            b = _as_mesh(it).tm.bounds
            los.append(tuple(b[0])); his.append(tuple(b[1]))
        except Exception:
            continue
    if not los:
        return None
    lo = [min(v[i] for v in los) for i in range(3)]
    hi = [max(v[i] for v in his) for i in range(3)]
    return Vector((lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, (lo[2] + hi[2]) / 2.0)


def _rotate(_obj, _axis, _angle, _pivot=None, _about="world"):
    \"\"\"Rotate any spatial object — Shape, Plane OR Mesh — by _angle degrees.
    About what: the wired _pivot point when present; else _about picks it —
    "world" is the global axis (the old behaviour), "part"/"group" the bbox
    centre of what came in (for a fanned list, "group" receives the collective
    centre hoisted by the emitter, so the ensemble turns as one rigid body).
    Uses Location algebra (Pos * Rot * Pos⁻¹ * obj) so it is polymorphic: a
    plane rotates just like a solid; a mesh takes the equivalent 4x4.\"\"\"
    if _obj is None:
        return None
    p = _as_point(_pivot) if _pivot is not None else None
    if p is None and _about in ("part", "group"):
        p = _pivot_of(_obj)
    if _is_mesh(_obj):
        return _obj.transformed(_mesh_matrix("rotate", axis=_axis, angle=_angle, point=p))
    d = _axis.direction
    r = Rot(d.X * _angle, d.Y * _angle, d.Z * _angle)
    if p is None:
        return r * _obj
    return Pos(p.X, p.Y, p.Z) * (r * (Pos(-p.X, -p.Y, -p.Z) * _obj))


def _scale(_shape, _factor=1.0, _x=1.0, _y=1.0, _z=1.0):
    \"\"\"Scale a shape. `factor` is the uniform multiplier; `x/y/z` add per-axis
    factors on top (all 1.0 -> uniform). by=(factor*x, factor*y, factor*z).\"\"\"
    if _shape is None:
        return None
    f = float(_factor)
    if _is_mesh(_shape):
        return _shape.transformed(_mesh_matrix(
            "scale", x=f * float(_x), y=f * float(_y), z=f * float(_z)))
    return scale(_shape, by=(f * float(_x), f * float(_y), f * float(_z)))


def _mirror(_shape, _plane, _copy=False):
    \"\"\"Mirror a shape across a plane. With `copy`, keep the original too so the
    result is symmetric (original + reflection).\"\"\"
    if _shape is None:
        return None
    if _is_mesh(_shape):
        m = _shape.transformed(_mesh_matrix("mirror", plane=_plane))
        return _mesh_concat([_shape, m]) if _copy else m
    m = mirror(_shape, about=_plane)
    if not _copy:
        return m
    try:
        return _shape + m
    except Exception:
        return Compound(children=[_shape, m])


def _al(_centered, _n=3):
    \"\"\"An align tuple for a primitive: CENTER on every axis when centred, else MIN
    (corner / base at the origin).\"\"\"
    a = Align.CENTER if _centered else Align.MIN
    return tuple([a] * int(_n))


_FANOUT_MAX = 2000  # guard: an absurd list (e.g. a runaway Range count) must
                    # surface as a per-node error, not freeze/crash the worker.


def _is_seq(_v):
    \"\"\"A value the engine should iterate over for fan-out. Lists/tuples and
    build123d ShapeLists count; a single Vector/Shape/Plane does NOT (a Vector
    is itself iterable, so test for the list-like containers explicitly).\"\"\"
    return isinstance(_v, (list, tuple, ShapeList))


def _fanout(_fn, _kw):
    \"\"\"Grasshopper-style data matching. _kw maps each item-access input name to
    its value. Any value that is a sequence makes the node run once per item;
    scalars are broadcast to every run; shorter lists reuse their last item
    (longest-list match). Returns a single value when nothing was a list, else a
    list of results.\"\"\"
    cols = {k: list(v) for k, v in _kw.items() if _is_seq(v)}
    if not cols:
        return _fn(**_kw)
    n = max((len(v) for v in cols.values()), default=0)
    if n > _FANOUT_MAX:
        raise ValueError(
            f"fan-out of {n} copies exceeds the {_FANOUT_MAX} limit "
            f"(check the list feeding this node, e.g. a Range count)")
    out = []
    for i in range(n):
        call = {}
        for k, v in _kw.items():
            if k in cols:
                seq = cols[k]
                call[k] = seq[i] if i < len(seq) else (seq[-1] if seq else None)
            else:
                call[k] = v
        out.append(_fn(**call))
    return out


def _flatten(_x):
    out = []
    for it in (_x or []):
        out.extend(_flatten(it)) if _is_seq(it) else out.append(it)
    return out


def _union_atoms(_s):
    \"\"\"The atomic pieces of a shape to feed a union: its solids (3D), else its
    faces (2D), else the shape itself. Decomposing a Compound this way lets a
    single multi-piece input actually FUSE internally — e.g. a Text whose glyphs
    a variable font split into overlapping fragments (a bar + an arc) merge back
    into one letter, while genuinely separate letters stay separate.\"\"\"
    for _attr in ("solids", "faces"):
        try:
            _lst = list(getattr(_s, _attr)())
            if _lst:
                return _lst
        except Exception:
            pass
    return [_s]


def _coplanar_check(_faces):
    \"\"\"(ok, why) — are these faces all planar and on the SAME plane?

    A union of faces is a REGION MERGE and OCCT only does it in a common plane.
    Hand it faces on different planes (or a curved one) and '+' returns them
    side by side in a Compound: no error, no sewing, just a loose bag that looks
    fused in the viewport. `_union` uses this to refuse instead — that is Join's
    job, not Union's.\"\"\"
    ref = None
    for f in _faces:
        try:
            if f.geom_type != GeomType.PLANE:
                return False, "one of them is curved (%s)" % (f.geom_type,)
            n = Vector(f.normal_at()); c = Vector(f.center())
        except Exception:
            return True, ""                      # can't tell — don't block
        if ref is None:
            ref = (n, n.dot(c))
            continue
        rn, rd = ref
        if abs(abs(n.dot(rn)) - 1.0) > 1e-6:
            return False, "they face different directions"
        if abs(rn.dot(c) - rd) > 1e-4:
            return False, "they lie on parallel but distinct planes"
    return True, ""


def _union(*_items):
    \"\"\"Fuse any number of shapes into ONE, dimension-agnostic (build123d '+').
    Flattens nested lists, drops None; decomposes each shape into its atomic
    pieces (solids/faces) so overlapping fragments inside a single Compound fuse
    too — nothing -> None. Works for 2D faces/sketches (-> a merged region) and
    3D solids (-> one part). Union feeds its whole `shapes` collector in as one
    arg (a list or single value); BooleanMulti spreads several — both flatten.

    Union is a BOOLEAN: it fuses overlapping material in a common space. It does
    NOT sew — faces that merely touch along an edge are Join's business, and
    asking Union for that used to return a loose Compound that never said so.\"\"\"
    parts = [p for p in _flatten(list(_items)) if p is not None]
    if not parts:
        return None
    atoms = [a for p in parts for a in _union_atoms(p)]
    if not atoms:
        return None
    if len(atoms) > 1 and not any(isinstance(a, Solid) for a in atoms):
        faces = [a for a in atoms if isinstance(a, Face)]
        if len(faces) == len(atoms):
            ok, why = _coplanar_check(faces)
            if not ok:
                raise ValueError(
                    "Union fuses coplanar regions and closed solids — these %d "
                    "faces cannot be fused (%s). Union is a boolean, not a sew: "
                    "use the Join node to sew touching surfaces into a shell "
                    "(and a closed shell into a solid)." % (len(faces), why))
    out = atoms[0]
    for p in atoms[1:]:
        out = out + p
    return out


def _join(_items, _tol=1e-6, _solid=True):
    \"\"\"Sew touching surfaces into ONE shell / chain touching curves into ONE wire.

    The complement of `_union`: a boolean fuses OVERLAPPING material, this stitches
    pieces that merely SHARE A BORDER — which OCCT does with a different operation
    entirely (BRepBuilderAPI_Sewing, reached via Face.sew_faces/Shell, and
    Wire.combine for edges). Six box faces become a closed shell and then a real
    Solid; five stay a shell (an open surface — Solid() would happily build an
    invalid one out of it, measured volume 800 on a 1000 box, so the closed check
    is load-bearing).

    Faces win over edges when both are present: an input that has faces is a
    surface, and its edges are that surface's border, not a separate curve.
    Pieces that do NOT touch are an ERROR, not a silent Compound — that is the
    whole point of the node.\"\"\"
    parts = [p for p in _flatten([_items]) if p is not None]
    if not parts:
        return None
    if any(_is_mesh(p) for p in parts):
        raise ValueError(
            "Join works on B-Rep curves and surfaces; a mesh reached it. "
            "Use Mesh Union (mesh lane) or Mesh To Solid first.")
    faces, edges = [], []
    for p in parts:
        try:
            fl = list(p.faces())
        except Exception:
            fl = []
        if fl:
            faces.extend(fl)
            continue
        try:
            el = list(p.edges())
        except Exception:
            el = []
        if not el:
            raise ValueError(
                "Join got something with no faces and no edges (%s)."
                % type(p).__name__)
        edges.extend(el)

    if faces:
        if len(faces) == 1:
            return faces[0]
        groups = Face.sew_faces(faces)
        if len(groups) > 1:
            raise ValueError(
                "Join: these surfaces do not touch — they sew into %d separate "
                "pieces (%s faces). Joined surfaces must SHARE their border "
                "edges; check the pieces really meet (a Split/Section leaves "
                "matching edges, two independent primitives usually do not)."
                % (len(groups), "+".join(str(len(g)) for g in groups)))
        shell = Shell(list(groups[0]))
        if _solid and shell.wrapped is not None and shell.wrapped.Closed():
            try:
                solid = Solid(shell)
                if solid.is_valid:
                    return solid
            except Exception:
                pass
        return shell

    wires = Wire.combine(edges, tol=_tol)
    if len(wires) > 1:
        raise ValueError(
            "Join: these curves do not touch — they chain into %d separate "
            "wires. Raise `tolerance` if the endpoints are close but not "
            "coincident." % (len(wires),))
    return wires[0] if wires else None


def _reanchor(_target, _items, _verify=True):
    \"\"\"Point picked sub-shapes at the shape they must actually operate on.

    build123d's algebra-mode fillet()/chamfer() take NO target argument: with no
    builder context they read `_items[0].topo_parent`. That back-link survives
    booleans still naming the PRE-boolean operand — so filleting a corner of a
    Union ran on that operand alone and silently returned it, dropping
    everything the Union added (measured: a 24000mm3 fused pair came back as
    15940, and a 6402mm2 fused sketch as 3512). The node knows the real target:
    it is the shape wired into `part`. Anchor the picks to it.

    Mutating topo_parent in place is deliberate — chamfer()'s 2D branch matches
    `v in object_list` by TShape identity, so handing it copies would match
    nothing. `_verify` checks the picks really belong to the target (O(1) per
    pick, hash/eq are TShape-based) so a mismatched wire keeps its old
    behaviour instead of failing deep inside OCCT; callers that took the items
    off the target themselves pass _verify=False and skip the set build.\"\"\"
    if _target is None:
        return _items
    _own, _kinds = {}, {"Vertex": "vertices", "Edge": "edges", "Face": "faces"}
    for _it in _items:
        _attr = _kinds.get(type(_it).__name__)
        if _attr is None:
            continue
        if _verify:
            if _attr not in _own:
                try:
                    _own[_attr] = set(getattr(_target, _attr)())
                except Exception:
                    _own[_attr] = set()
            if _it not in _own[_attr]:
                continue
        try:
            _it.topo_parent = _target
        except Exception:
            pass
    return _items


def _round(_part, _items, _mode="fillet", _size=1.0):
    \"\"\"Round (fillet) or bevel (chamfer) a set of sub-shapes — edges (3D) or
    vertices (2D corners). One node, `_mode` picks the operation: build123d uses
    radius= for fillet and length= for chamfer. `_part` is the shape the picks
    belong to; without it build123d guesses (see _reanchor) and a part built by
    a boolean comes back truncated.\"\"\"
    _sel = [_s for _s in _flatten([_items]) if _s is not None]
    if not _sel:
        raise ValueError(
            "nothing selected to fillet/chamfer — the picks no longer match the "
            "geometry, re-pick them in the Select node")
    _reanchor(_part, _sel)
    if _mode == "chamfer":
        return chamfer(_sel, length=_size)
    return fillet(_sel, radius=_size)


def _round_corners(_shape, _mode="fillet", _size=1.0):
    \"\"\"The 2D corner round/bevel: fill the input to a face ONCE and work on that
    face's own vertices. One _face() call, not two — the picks must belong to
    the very face being modified for _reanchor (and build123d's 2D branch) to
    match them.\"\"\"
    _f = _face(_shape)
    if _f is None:
        return None
    return _round(_f, list(_f.vertices()), _mode, _size)


def _round_all(_part, _mode="fillet", _size=1.0):
    \"\"\"Fillet/chamfer ALL edges of a part in one operation, but RESILIENT: if the
    requested size is too large for the geometry (very common with extruded text —
    thin letter strokes, and letters like 'i'/'do' are several disjoint solids),
    the size is shrunk until it fits so the operation SUCCEEDS instead of failing
    wholesale on the thinnest part. The common case (size fits) runs at full size
    on the FIRST try with no extra cost. A geometric shrink-retry is used rather
    than build123d's max_fillet(), which is an expensive iterative search (seconds
    on text) — a few cheap fillet attempts converge close to the max far faster.\"\"\"
    if _part is None:
        return None
    try:
        _edges = _part.edges()
        _reanchor(_part, _edges, _verify=False)   # they came off _part: no check
    except Exception:
        _edges = _part                       # already a sub-shape list
    _op = ((lambda _z: chamfer(_edges, length=_z)) if _mode == "chamfer"
           else (lambda _z: fillet(_edges, radius=_z)))
    _z = float(_size)
    for _ in range(7):                       # requested size first, then shrink to fit
        if _z <= 1e-4:
            break
        try:
            return _op(_z)
        except Exception:
            _z *= 0.6                        # ~converges toward the geometric max
    return _part                             # give up: leave the part unrounded


def _as_point(_p):
    # a Vector for a point-like value (Vector, Vertex, (x,y,z) tuple), else None
    if isinstance(_p, Vector):
        return _p
    if isinstance(_p, (tuple, list)) and 2 <= len(_p) and all(
            isinstance(_v, (int, float)) for _v in _p[:3]):
        return Vector(float(_p[0]), float(_p[1]), float(_p[2]) if len(_p) > 2 else 0.0)
    if type(_p).__name__ == "Vertex":
        try:
            return Vector(_p.X, _p.Y, _p.Z)
        except Exception:
            return None
    return None


def _shape_center_weight(_s):
    # (centre Vector, weight) for one shape; weight = volume else area else length
    # else 1. build123d .center(MASS): a straight edge's mass centre is its midpoint,
    # a circle edge's is the circle centre, a face's is its area centroid.
    try:
        _c = _s.center(CenterOf.MASS)
    except Exception:
        try:
            _bb = _s.bounding_box()
            _c = (_bb.min + _bb.max) * 0.5
        except Exception:
            return (None, 0.0)
    _w = 0.0
    for _a in ("volume", "area", "length"):
        try:
            _v = float(getattr(_s, _a))
            if _v:
                _w = _v
                break
        except Exception:
            pass
    return (_c, _w or 1.0)


def _center_of(_x):
    # Universal centre, aggregating whatever is wired in:
    #   closed solids -> mass-weighted centre of mass (volume via _volume_of)
    #   faces -> area-weighted centroid; curves -> length-weighted centroid
    #   (a straight line -> its midpoint, a circle -> its centre)
    #   a point cloud (points / vertices) -> the mean point
    # Mixed sets: measure-weighted mean of each piece's own centre. Returns a Vector.
    _items = [p for p in _flatten([_x]) if p is not None]
    if not _items:
        return Vector(0, 0, 0)
    _pts = [_as_point(p) for p in _items]
    _shapes = [p for p, q in zip(_items, _pts) if q is None]
    _points = [q for q in _pts if q is not None]
    if not _shapes:                       # pure point cloud -> mean point
        _acc = Vector(0, 0, 0)
        for q in _points:
            _acc = _acc + q
        return _acc / (len(_points) or 1)
    _tot = 0.0
    _acc = Vector(0, 0, 0)
    for _sh in _shapes:
        _c, _w = _shape_center_weight(_sh)
        if _c is None:
            continue
        _acc = _acc + _c * _w
        _tot += _w
    for q in _points:                     # loose points join as unit-weight samples
        _acc = _acc + q
        _tot += 1.0
    return _acc / _tot if _tot else Vector(0, 0, 0)


def _volume_of(_x):
    # total volume of everything closed wired in (0 for open curves / faces / points)
    _tot = 0.0
    for p in _flatten([_x]):
        if p is None:
            continue
        try:
            _tot += float(p.volume)
        except Exception:
            pass
    return _tot


def _slice(_lst, _start, _stop, _step):
    _lst = list(_lst or [])
    stop = None if _stop == 0 else _stop
    return _lst[_start:stop:(_step or 1)]


def _sort(_items, _by="X"):
    \"\"\"Sort a list. build123d shapes use ShapeList ordering (by position along
    an axis, or by a metric); points sort by component; anything else by value.\"\"\"
    items = list(_items or [])
    if not items:
        return items
    head = items[0]
    if hasattr(head, "wrapped") or hasattr(head, "volume") or hasattr(head, "center"):
        axis = {"X": Axis.X, "Y": Axis.Y, "Z": Axis.Z}.get(_by)
        metric = {"length": SortBy.LENGTH, "area": SortBy.AREA,
                  "volume": SortBy.VOLUME, "radius": SortBy.RADIUS}.get(_by)
        try:
            if axis is not None:
                return ShapeList(items).sort_by(axis)
            if metric is not None:
                return ShapeList(items).sort_by(metric)
        except Exception:
            pass
    if hasattr(head, "X") and hasattr(head, "Y") and hasattr(head, "Z"):
        comp = {"X": "X", "Y": "Y", "Z": "Z"}.get(_by, "X")
        return sorted(items, key=lambda v: getattr(v, comp))
    try:
        return sorted(items)
    except Exception:
        return items


def _move(_shape, _offset, _x, _y, _z):
    \"\"\"Translate a shape/plane/mesh. By the wired `offset` vector when present
    (item-access: a list of vectors scatters the shape to each), else by the
    x/y/z widgets.\"\"\"
    if _shape is None:
        return None
    if _offset is None:
        v = Vector(_x, _y, _z)
    elif hasattr(_offset, "X"):
        v = _offset
    else:
        v = Vector(*list(_offset)[:3])
    if _is_mesh(_shape):
        return _shape.transformed(_mesh_matrix("move", x=v.X, y=v.Y, z=v.Z))
    return Pos(v.X, v.Y, v.Z) * _shape


def _pt(_v):
    \"\"\"Coerce one vector/vertex/tuple to a Vector (first item if given many);
    None passes through. Lets curve nodes accept any point-like input.\"\"\"
    pts = _origin_points(_v)
    return pts[0] if pts else None


def _curve_points(_items):
    \"\"\"Flatten curve point inputs into [Vector, ...]. Accepts several wired
    points AND/OR list(s) of points, so Polyline/Spline take either shape.\"\"\"
    return _origin_points(_flatten(_items))


def _curve_draw(_points, _mode="polyline", _closed=False, _plane=None):
    \"\"\"Build a curve from local (u, v) points authored with the on-canvas
    ✎ Draw tool (Curve on Plane node), then re-seat it onto `_plane` (Plane.XY/
    XZ/YZ by default, or any wired Plane — dynamic frames included). Mirrors
    _to_plane: the curve is built flat in local XY, then `plane * curve`.\"\"\"
    pts = [(float(p[0]), float(p[1]), 0.0)
           for p in (_points or []) if p and len(p) >= 2]
    if len(pts) < 2:
        return None
    if _mode == "spline":
        crv = Spline(*pts, periodic=bool(_closed))
    else:
        crv = Polyline(*pts, close=bool(_closed))
    pl = _plane if isinstance(_plane, Plane) else Plane.XY
    return pl * crv


def _trace_curves(_contours, _scale=1.0, _imgh=None):
    \"\"\"Rebuild wires from a TraceImage node's FROZEN artifact (baked in by its
    ✎ edit-mode: rembg + magic-wand + pen + 2-point scale). Each contour is
    {pts:[[x,y],...] in PIXELS, closed, hole}. Pixels are scaled by _scale
    (mm/pixel) and Y is flipped (image Y grows downward, CAD Y upward) so the
    part comes out upright, not mirrored. The flip uses the IMAGE height _imgh
    when known — a stable origin (image bottom-left → mm (0,0)) so a RefImage of
    the same picture aligns 1:1 with the traced curves; it falls back to the
    contours' own max Y for older artifacts. Closed contours become closed Wires
    — they fill into faces via _face like any 2D primitive (hole-flagged loops
    are inner boundaries, nested downstream by Make Face). Deterministic: reads
    no image at run time. Returns a Wire (single) or a ShapeList of Wires.\"\"\"
    _cs = [c for c in (_contours or [])
           if c and len(c.get("pts") or []) >= 2]
    if not _cs:
        return None
    _all_y = [p[1] for c in _cs for p in c["pts"] if p and len(p) >= 2]
    _h = float(_imgh) if _imgh else (max(_all_y) if _all_y else 0.0)
    _wires = []
    for c in _cs:
        pts = [(float(p[0]) * _scale, (_h - float(p[1])) * _scale, 0.0)
               for p in c["pts"] if p and len(p) >= 2]
        # a trailing point equal to the first would zero-length the closing seg
        if len(pts) >= 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) < 2:
            continue
        try:
            _wires.append(Polyline(*pts, close=bool(c.get("closed", True))))
        except Exception:
            continue
    if not _wires:
        return None
    return _wires[0] if len(_wires) == 1 else ShapeList(_wires)


def _outline(_s):
    \"\"\"The boundary of a 2D primitive as a closed curve (Wire). A filled
    sketch/face is reduced to its outer wire(s) so primitives read as curves;
    feed a Surface (Make Face) node to fill it back into a face. Anything that is
    not face-like (already a curve) passes straight through.\"\"\"
    if _s is None:
        return None
    try:
        ws = list(_s.wires()) if hasattr(_s, "wires") else []
    except Exception:
        ws = []
    if len(ws) == 1:
        return ws[0]
    if ws:
        return ShapeList(ws)
    return _s


def _face(_s):
    \"\"\"Coerce a shape to a face/sketch. A closed curve (Wire/Edge) is filled with
    make_face; anything already face-like passes through. Used where an op needs a
    face but may receive a curve primitive.\"\"\"
    if _s is None:
        return None
    try:
        if hasattr(_s, "faces") and len(_s.faces()) > 0:
            return _s
    except Exception:
        pass
    try:
        return make_face(_s)
    except Exception:
        return _s


def _classify(_it):
    \"\"\"Coarse geometric kind of a runtime value: 'solid' | 'surface' | 'curve' |
    'point' | 'other'. Drives the container filter/transform gate (_gate).\"\"\"
    if _it is None:
        return "other"
    if hasattr(_it, "X") and hasattr(_it, "Y") and hasattr(_it, "Z"):
        return "point"
    if isinstance(_it, (list, tuple)) and len(_it) >= 3 and not hasattr(_it, "vertices"):
        return "point"
    if hasattr(_it, "origin") and hasattr(_it, "z_dir"):
        return "plane"                       # a Plane / frame (no faces/edges)
    def _cnt(name):
        try:
            return len(getattr(_it, name)())
        except Exception:
            return 0
    if _cnt("solids") > 0:
        return "solid"
    if _cnt("faces") > 0:
        return "surface"
    if _cnt("edges") > 0 or _cnt("wires") > 0:
        return "curve"
    if _cnt("vertices") > 0:
        return "point"
    return "other"


def _is_planar(_f):
    \"\"\"True if a face is planar (the Surface transform keeps only these).\"\"\"
    try:
        return "PLANE" in str(_f.geom_type).upper()
    except Exception:
        return True


def _polyline_through(_pts):
    \"\"\"Join >=2 points into one open polyline (the curve transform of points).\"\"\"
    vs = _deconstruct(_pts)
    if len(vs) < 2:
        return None
    try:
        return Polyline(*[(v.X, v.Y, v.Z) for v in vs])
    except Exception:
        return None


def _gate(_id, _v, _kind, _mode):
    \"\"\"Container filter/transform gate — the typed transformer. `_kind` is the
    container's OWN type ('surface' | 'curve'); `_mode` is 'filter' | 'transform'.
      - filter:    keep only inputs already of that kind.
      - transform: also coerce/extract everything compatible into it —
          surface: fill closed curves (_face); a solid's PLANAR faces.
          curve:   a surface's outer outline; a solid's edges (wireframe);
                   >=2 points joined into one polyline.
    Always returns a flat LIST (so downstream fans out) and records it for the
    Panels tab (_probe). Single source of truth for the container transformer.\"\"\"
    items = [it for it in _flatten([_v]) if it is not None]
    out = []
    if _kind == "surface":
        for it in items:
            k = _classify(it)
            if k == "surface":
                try:
                    out.extend(it.faces())
                except Exception:
                    out.append(it)
            elif _mode == "transform" and k == "curve":
                f = _face(it)
                if f is not None:
                    out.append(f)
            elif _mode == "transform" and k == "solid":
                try:
                    out.extend([f for f in it.faces() if _is_planar(f)])
                except Exception:
                    pass
    elif _kind == "curve":
        pts = [it for it in items if _classify(it) == "point"]
        if _mode == "transform" and len(pts) >= 2:
            pl = _polyline_through(pts)
            if pl is not None:
                out.append(pl)
        for it in items:
            k = _classify(it)
            if k == "curve":
                try:
                    ws = list(it.wires())
                    out.extend(ws if ws else [it])
                except Exception:
                    out.append(it)
            elif _mode == "transform" and k == "surface":
                try:
                    for f in it.faces():
                        w = f.outer_wire() if hasattr(f, "outer_wire") else None
                        out.append(w if w is not None else f)
                except Exception:
                    out.append(it)
            elif _mode == "transform" and k == "solid":
                try:
                    out.extend(it.edges())
                except Exception:
                    pass
    elif _kind == "point":
        # filter: keep only points; transform: explode anything into its points
        # (vertices of shapes, a plane's origin, …) — same rule as Deconstruct.
        for it in items:
            if _mode == "transform" or _classify(it) == "point":
                out.extend(_deconstruct(it))
    elif _kind == "solid":
        # filter: keep solids (a compound stays whole); transform: explode a
        # compound into its individual solids.
        for it in items:
            if _classify(it) != "solid":
                continue
            if _mode == "transform":
                try:
                    out.extend(it.solids())
                except Exception:
                    out.append(it)
            else:
                out.append(it)
    elif _kind == "plane":
        # filter: keep planes/frames; transform: read the Plane of each PLANAR
        # face of a surface or solid.
        for it in items:
            k = _classify(it)
            if k == "plane":
                out.append(it)
            elif _mode == "transform" and k in ("surface", "solid"):
                try:
                    out.extend([Plane(f) for f in it.faces() if _is_planar(f)])
                except Exception:
                    pass
    elif _kind == "selection":
        # A selection is ONE ShapeList consumed whole (Fillet/Chamfer/…), so this
        # returns a single ShapeList, not a fan-out list. filter: keep loose
        # sub-shapes (edges/faces/vertices); transform: also pull a solid's
        # faces/edges/vertices in. Assemble a pickable selection programmatically.
        sel = []
        for it in items:
            k = _classify(it)
            if k in ("curve", "surface", "point"):
                sel.append(it)
            elif _mode == "transform" and k == "solid":
                try:
                    sel.extend(it.faces()); sel.extend(it.edges()); sel.extend(it.vertices())
                except Exception:
                    pass
        res = ShapeList(sel)
        _probe(_id, res)
        return res
    _probe(_id, out)
    return out


def _as_curve(_c):
    \"\"\"Coerce an input to a 1D curve (Edge/Wire) exposing location_at. Passes a
    curve through; otherwise takes the first edge of a shape.\"\"\"
    if _c is None:
        return None
    if hasattr(_c, "location_at"):
        return _c
    try:
        es = list(_c.edges())
        return es[0] if es else None
    except Exception:
        return None


def _curve_frames(_curve, _count=8):
    \"\"\"N evenly spaced frames (Planes) along a curve; each frame's local Z axis
    is the curve tangent, so a 2D profile seated on it (ToPlane) sits perpendicular
    to the curve — ready to Loft. Returns a list of Planes.\"\"\"
    c = _as_curve(_curve)
    if c is None:
        return []
    n = max(int(_count), 1)
    out = []
    for i in range(n):
        t = i / (n - 1) if n > 1 else 0.5
        try:
            out.append(Plane(c.location_at(t)))
        except Exception:
            pass
    return out


def _eval_frame(_curve, _t=0.0):
    \"\"\"The frame (Plane) at parameter t in [0,1] on a curve (Z = tangent).\"\"\"
    c = _as_curve(_curve)
    if c is None:
        return None
    try:
        return Plane(c.location_at(min(max(float(_t), 0.0), 1.0)))
    except Exception:
        return None


def _plane_origin(_p):
    \"\"\"The origin point (Vector) of a Plane/Location; a point passes through.\"\"\"
    if _p is None:
        return None
    if hasattr(_p, "origin"):
        o = _p.origin
        return Vector(o.X, o.Y, o.Z)
    return _pt(_p)


def _curve_endpoints(_curve):
    \"\"\"[start, end] points of a curve as a 2-item list of Vectors.\"\"\"
    c = _as_curve(_curve)
    if c is None:
        return []
    try:
        return [Vector(c @ 0.0), Vector(c @ 1.0)]
    except Exception:
        return []


def _curve_length(_curve):
    c = _as_curve(_curve)
    try:
        return float(c.length) if c is not None else 0.0
    except Exception:
        return 0.0


def _divide_domain(_dom, _count=10):
    \"\"\"N values evenly spanning a domain [min, max], endpoints inclusive.\"\"\"
    d = list(_dom) if _is_seq(_dom) else [0.0, 1.0]
    a = float(d[0]) if d else 0.0
    b = float(d[-1]) if len(d) > 1 else a + 1.0
    n = max(int(_count), 1)
    if n == 1:
        return [(a + b) / 2.0]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def _bounds(_lst):
    \"\"\"The [min, max] domain spanning a flat list of numbers.\"\"\"
    nums = [float(x) for x in _flatten(_lst) if isinstance(x, (int, float))]
    return [min(nums), max(nums)] if nums else [0.0, 1.0]


def _remap(_v, _src, _tgt, _smin, _smax, _tmin, _tmax):
    \"\"\"Linearly remap _v from a source range to a target range. A [min,max]
    domain wired into _src/_tgt overrides the scalar fallbacks.\"\"\"
    a, b = (_src[0], _src[1]) if _is_seq(_src) and len(_src) >= 2 else (_smin, _smax)
    c, d = (_tgt[0], _tgt[1]) if _is_seq(_tgt) and len(_tgt) >= 2 else (_tmin, _tmax)
    a, b, c, d = float(a), float(b), float(c), float(d)
    if b == a:
        return c
    return c + (float(_v) - a) / (b - a) * (d - c)


def _section_wire(_s):
    \"\"\"The outline Wire of a loft/extrude section (a face/sketch -> its outer wire;
    a curve passes through). Used for open-surface (un-capped) lofts.\"\"\"
    if _s is None:
        return None
    if isinstance(_s, Wire):
        return _s
    try:
        ws = list(_s.wires())
        if ws:
            return ws[0]
    except Exception:
        pass
    try:
        es = list(_s.edges())
        if es:
            return Wire(es)
    except Exception:
        pass
    return None


def _wrap_shape(_s):
    \"\"\"Wrap a raw OCP TopoDS_Shape into the matching build123d class.\"\"\"
    if _s is None:
        return None
    from OCP.TopAbs import TopAbs_ShapeEnum as _T
    cls = {_T.TopAbs_SOLID: Solid, _T.TopAbs_SHELL: Shell, _T.TopAbs_FACE: Face,
           _T.TopAbs_COMPOUND: Compound, _T.TopAbs_COMPSOLID: Compound,
           _T.TopAbs_WIRE: Wire, _T.TopAbs_EDGE: Edge}.get(_s.ShapeType())
    return cls(_s) if cls else None


def _wire_normal(_w):
    \"\"\"Unit normal of a planar wire (via its filled face); falls back to +Z.\"\"\"
    try:
        n = make_face(_w).faces()[0].normal_at()
        return Vector(n.X, n.Y, n.Z)
    except Exception:
        return Vector(0, 0, 1)


def _loft(_sections, _ruled=False, _solid=True, _smoothing=False,
          _continuity="C2", _parametrization="chord", _max_degree=8,
          _start=None, _end=None):
    \"\"\"Loft (skin) through an ordered list of sections, exposing the full OCC
    ThruSections control set. Sections may be several wired sketches/faces/curves
    AND/OR a single list (e.g. ToPlane over Divide Curve). Settings:
      ruled           - straight skin between sections (vs a smooth surface).
      solid           - cap the ends into a solid (vs an open shell of the outlines).
      smoothing       - use the approximating algorithm instead of exact
                        interpolation (smooth path); pair with continuity/max_degree.
      continuity      - C0|C1|C2|G1|G2 of the lofted surface (smoothing).
      parametrization - uniform|chord|centripetal: how sections are spaced in the
                        smooth interpolation (changes the bulge between sections).
      max_degree      - cap on the result surface's B-spline U-degree.
      _start / _end   - optional points: the loft starts/ends at a tip (a cone cap).
    Falls back to build123d's high-level loft if the builder fails.\"\"\"
    secs = [s for s in _flatten([_sections]) if s is not None]
    n_caps = (1 if _start is not None else 0) + (1 if _end is not None else 0)
    if len(secs) < 1 or len(secs) + n_caps < 2:
        return None
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.GeomAbs import GeomAbs_Shape as _G
    from OCP.Approx import Approx_ParametrizationType as _P
    _cont = {"C0": _G.GeomAbs_C0, "C1": _G.GeomAbs_C1, "C2": _G.GeomAbs_C2,
             "G1": _G.GeomAbs_G1, "G2": _G.GeomAbs_G2}.get(_continuity, _G.GeomAbs_C2)
    _par = {"uniform": _P.Approx_IsoParametric, "chord": _P.Approx_ChordLength,
            "centripetal": _P.Approx_Centripetal}.get(_parametrization, _P.Approx_ChordLength)
    def _vtx(_v):
        return Vertex(_v.X, _v.Y, _v.Z) if hasattr(_v, "X") else Vertex(*_v)
    try:
        b = BRepOffsetAPI_ThruSections(bool(_solid), bool(_ruled), 1e-6)
        b.SetSmoothing(bool(_smoothing))
        b.SetContinuity(_cont)
        b.SetParType(_par)
        if _max_degree:
            b.SetMaxDegree(int(_max_degree))
        b.CheckCompatibility(True)          # reparametrize wires -> no smooth-loft twist
        if _start is not None:
            b.AddVertex(_vtx(_start).wrapped)
        n = 0
        for s in secs:
            w = _section_wire(s)
            if w is not None:
                b.AddWire(w.wrapped)
                n += 1
        if _end is not None:
            b.AddVertex(_vtx(_end).wrapped)
        if n < 1:
            return None
        b.Build()
        out = _wrap_shape(b.Shape())
        if out is not None:
            return out
    except Exception:
        pass
    if _solid:                              # fallback: high-level loft of the faces
        faces = [f for f in (_face(s) for s in secs) if f is not None]
        if len(faces) >= 2:
            return loft(faces, ruled=bool(_ruled))
    return None


def _extrude(_profile, _amount, _taper=0.0, _both=False, _solid=True):
    \"\"\"Extrude a 2D profile along its normal. With `solid` (default) the profile is
    filled into a face and the result is a solid; with `solid` off the profile's
    outline is extruded into an open surface (a wall / ribbon; `taper` ignored).\"\"\"
    if _profile is None:
        return None
    if _solid:
        prof = _face(_profile)
        return extrude(prof, amount=_amount, taper=_taper, both=_both) if prof is not None else None
    w = _section_wire(_profile)
    if w is None:
        return None
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec
    nrm = _wire_normal(w)
    a = float(_amount)
    src = w
    if _both:
        src = w.moved(Pos(-nrm.X * a, -nrm.Y * a, -nrm.Z * a))
        a *= 2.0
    return _wrap_shape(BRepPrimAPI_MakePrism(src.wrapped, gp_Vec(nrm.X * a, nrm.Y * a, nrm.Z * a)).Shape())


def _revolve(_profile, _axis, _angle=360, _solid=True):
    \"\"\"Revolve a 2D profile around an in-plane axis. With `solid` (default) the
    profile is filled into a face for a solid of revolution; with `solid` off the
    outline is revolved into an open surface.\"\"\"
    if _profile is None:
        return None
    if _solid:
        prof = _face(_profile)
        return revolve(prof, axis=_axis, revolution_arc=_angle) if prof is not None else None
    w = _section_wire(_profile)
    if w is None:
        return None
    import math
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
    ax = _axis.wrapped if hasattr(_axis, "wrapped") else _axis
    return _wrap_shape(BRepPrimAPI_MakeRevol(w.wrapped, ax, math.radians(float(_angle))).Shape())


def _sweep(_section, _path, _frenet=False, _solid=True):
    \"\"\"Sweep a profile along a path curve. The profile is seated on the path's
    start frame (perpendicular to the curve) so a flat XY sketch sweeps cleanly.
    With `solid` (default) the profile is a face and the sweep is a solid; with
    `solid` off the outline is swept into an open surface (a tube wall).\"\"\"
    if _section is None or _path is None:
        return None
    p = _as_curve(_path)
    if _solid:
        sec = _to_plane(_face(_section), _eval_frame(p, 0.0))
        return sweep(sec, path=p, is_frenet=bool(_frenet))
    sec = _to_plane(_section_wire(_section), _eval_frame(p, 0.0))
    if sec is None:
        return None
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe
    spine = p if isinstance(p, Wire) else Wire([p])
    return _wrap_shape(BRepOffsetAPI_MakePipe(spine.wrapped, sec.wrapped).Shape())


def _to_plane(_shape, _plane):
    \"\"\"Re-seat a 2D profile onto a plane/frame: its local XY comes to lie in the
    plane (Plane * shape). Used to place loft/sweep sections on curve frames.\"\"\"
    if _shape is None or _plane is None:
        return _shape
    pl = _plane if isinstance(_plane, Plane) else Plane(_plane)
    return pl * _shape


def _as_plane(_s):
    \"\"\"Coerce a planar surface (Face / Sketch) into the Plane it lies in — origin
    at the face centre, z along its normal — so a picked flat face can drive any
    plane input (Section, Text-on-plane, …). Passes a Plane / frame through.\"\"\"
    if _s is None or isinstance(_s, Plane):
        return _s
    try:
        _f = _s if isinstance(_s, Face) else _s.faces()[0]
        return Plane(_f)
    except Exception:
        return _s


def _font(_name):
    \"\"\"Text() font kwargs for a chosen font name. A custom uploaded font (in the
    shared _fonts library) resolves to font_path=<file> so it renders WITHOUT
    being installed system-wide; any other name stays a font= family (fontconfig).\"\"\"
    try:
        from cad_nodes.fonts import resolve_font
        return resolve_font(_name)
    except Exception:
        return {"font": _name} if _name else {"font": "Arial"}


def _domain2d(_region, _w, _h, _pts=None):
    \"\"\"(x0, y0, x1, y1) of a 2D domain: a region face's bounding box if given,
    else the points' extent, else a 0..w x 0..h box.\"\"\"
    if _region is not None:
        try:
            bb = _region.bounding_box()
            return bb.min.X, bb.min.Y, bb.max.X, bb.max.Y
        except Exception:
            pass
    if _pts is not None and len(_pts):
        import numpy as np
        P = np.asarray(_pts, dtype=float)
        return P[:, 0].min(), P[:, 1].min(), P[:, 0].max(), P[:, 1].max()
    return 0.0, 0.0, float(_w), float(_h)


def _winding_inside(_V, _F, _P, _chunk=128):
    \"\"\"Point-in-mesh by the generalized winding number (van Oosterom-Strackee),
    pure numpy — trimesh.contains needs rtree, which is not in the image. For a
    closed mesh the winding of an inside point is ~1, outside ~0; the 0.25
    threshold is tolerant of near-surface points. Chunked over points to bound
    memory (chunk x tris temporaries).\"\"\"
    import numpy as np
    V = np.asarray(_V, dtype=float)
    F = np.asarray(_F)
    P = np.asarray(_P, dtype=float)
    T = V[F]                                       # (tris, 3 corners, xyz)
    out = np.zeros(len(P), dtype=bool)
    for s in range(0, len(P), int(_chunk)):
        p = P[s:s + int(_chunk)]
        a = T[None, :, 0, :] - p[:, None, :]
        b = T[None, :, 1, :] - p[:, None, :]
        c = T[None, :, 2, :] - p[:, None, :]
        la = np.linalg.norm(a, axis=2)
        lb = np.linalg.norm(b, axis=2)
        lc = np.linalg.norm(c, axis=2)
        det = np.einsum("kmi,kmi->km", a, np.cross(b, c))
        den = (la * lb * lc + np.einsum("kmi,kmi->km", a, b) * lc
               + np.einsum("kmi,kmi->km", b, c) * la
               + np.einsum("kmi,kmi->km", a, c) * lb)
        w = np.arctan2(det, den).sum(axis=1) / (2.0 * np.pi)
        out[s:s + int(_chunk)] = np.abs(w) > 0.25
    return out


def _populate_volume(_m, _n, _rng):
    \"\"\"Rejection-sample _n points INSIDE a watertight mesh: uniform candidates in
    the bbox, kept by winding number. The inside test runs on a simplified proxy
    when the mesh is heavy — it is only an oracle, so a plain manifold simplify
    with no verification is fine (unlike MeshSimplify, whose result is the part).\"\"\"
    import numpy as np
    tm0 = _as_mesh(_m).tm
    proxy = tm0
    if len(tm0.faces) > 4000:
        try:
            diag = float(np.linalg.norm(tm0.bounds[1] - tm0.bounds[0]))
            proxy = _from_manifold(
                _to_manifold(Mesh(tm0), "Populate").simplify(diag * 0.005)).tm
        except Exception:
            pass
    lo, hi = tm0.bounds
    V, F = np.asarray(proxy.vertices, dtype=float), np.asarray(proxy.faces)
    got, have = [], 0
    for _round in range(24):
        cand = _rng.uniform(lo, hi, (max(2 * int(_n), 64), 3))
        keep = cand[_winding_inside(V, F, cand)]
        got.append(keep)
        have += len(keep)
        if have >= _n:
            break
    P = np.vstack(got) if got else np.zeros((0, 3))
    if len(P) < _n:
        raise ValueError(
            "Populate filled only %d of %d points — the body occupies too little "
            "of its bounding box, or the mesh is not closed (run Mesh Fix)."
            % (len(P), int(_n)))
    return [Vector(float(x), float(y), float(z)) for x, y, z in P[:int(_n)]]


def _populate_on_surface(_m, _n, _seed):
    \"\"\"Area-uniform random points ON a surface (curved faces, shells, meshes):
    trimesh sample_surface over the tessellation. Uniform BY AREA — random UV
    through position_at would crowd points where the parametrization compresses.\"\"\"
    pts, _fi = _tm().sample.sample_surface(_as_mesh(_m).tm, int(_n),
                                           seed=int(_seed))
    return [Vector(float(x), float(y), float(z)) for x, y, z in pts]


def _populate(_count=40, _seed=1, _width=100.0, _height=100.0, _region=None):
    \"\"\"Universal scatter, deterministic per `seed` — dispatches on what `region`
    IS (the socket is raw, so the value arrives untouched):
      nothing        -> the legacy 0..width x 0..height box at z=0
      curve          -> ALONG it, uniform by arc length (1D)
      planar XY face -> INSIDE the region — really inside, not its bbox (2D)
      curved face    -> ON the surface, uniform by area (2.5D)
      solid / watertight mesh -> INSIDE the volume (3D)
    Dispatch is on topology (solids/faces/edges), not duck-typing position_at —
    Edge and Face both have one.\"\"\"
    import numpy as np
    rng = np.random.RandomState(int(_seed))
    n = max(1, int(_count))
    r = _region
    if r is None:                                  # legacy path — UNCHANGED
        xs = rng.uniform(0.0, float(_width), n)
        ys = rng.uniform(0.0, float(_height), n)
        return [Vector(float(x), float(y), 0.0) for x, y in zip(xs, ys)]
    if _is_mesh(r):
        return (_populate_volume(r, n, rng) if r.watertight
                else _populate_on_surface(r, n, _seed))
    solids = list(r.solids()) if hasattr(r, "solids") else []
    faces = list(r.faces()) if hasattr(r, "faces") else []
    edges = list(r.edges()) if hasattr(r, "edges") else []
    if solids:                                     # 3D: inside the volume
        return _populate_volume(_to_mesh(r), n, rng)
    if not faces and edges:
        # A CLOSED curve is a region BOUNDARY — the legacy idiom: Rectangle /
        # Circle wired as `region` used to arrive filled via the _face cast,
        # which the raw socket now skips. Fill it and treat it as the face it
        # meant. An OPEN curve scatters ALONG itself (the 1D branch below).
        filled = _face(r)
        ff = list(filled.faces()) if hasattr(filled, "faces") else []
        if ff:
            r, faces = filled, ff
    if faces:
        f = faces[0]
        flat_xy = False
        if len(faces) == 1:
            try:
                flat_xy = f.is_planar and abs(f.normal_at(f.center()).Z) > 0.99
            except Exception:
                flat_xy = False
        if flat_xy:
            # 2D: draw in the bbox with the SAME two rng.uniform calls as the
            # old node (a Rectangle rejects nothing -> byte-identical points,
            # e.g. the voronoi vase), then keep only what is really inside.
            bb = r.bounding_box()
            z = (bb.min.Z + bb.max.Z) / 2.0
            out = []
            for _round in range(24):
                xs = rng.uniform(bb.min.X, bb.max.X, n)
                ys = rng.uniform(bb.min.Y, bb.max.Y, n)
                out += [Vector(float(x), float(y), z) for x, y in zip(xs, ys)
                        if f.is_inside((float(x), float(y), z))]
                if len(out) >= n:
                    break
            if not out:
                raise ValueError("Populate: no point landed inside the region "
                                 "face — is it degenerate?")
            return out[:n]
        return _populate_on_surface(_to_mesh(r), n, _seed)   # 2.5D: ON it
    if edges:                                      # 1D: uniform by arc length
        L = [float(e.length) for e in edges]
        tot = sum(L)
        cum = np.cumsum([0.0] + L)
        u = rng.uniform(0.0, tot, n)
        idx = np.minimum(np.searchsorted(cum, u, side="right") - 1,
                         len(edges) - 1)
        out = []
        for ui, i in zip(u, idx):
            t = (ui - cum[int(i)]) / (L[int(i)] or 1.0)
            p = edges[int(i)].position_at(min(max(float(t), 0.0), 1.0))
            out.append(Vector(p.X, p.Y, p.Z))
        return out
    x0, y0, x1, y1 = _domain2d(r, _width, _height)  # fallback: old bbox path
    xs = rng.uniform(x0, x1, n)
    ys = rng.uniform(y0, y1, n)
    return [Vector(float(x), float(y), 0.0) for x, y in zip(xs, ys)]


def _voronoi2d(_points, _boundary=None, _scale=0.9):
    \"\"\"Polygonal Voronoi cells from a point set, clipped to the `boundary`
    rectangle (its bounding box). `scale` shrinks each cell toward its centre to
    leave a frame between cells (1.0 = cells touch / share edges). Sites are
    mirrored across the boundary so the kept cells are finite and clipped.\"\"\"
    import numpy as np
    from scipy.spatial import Voronoi
    pts = _origin_points(_points)
    if len(pts) < 2:
        return ShapeList([])
    P = np.array([(p.X, p.Y) for p in pts], dtype=float)
    x0, y0, x1, y1 = _domain2d(_boundary, 100.0, 100.0, P)
    pad = []
    for (x, y) in P:
        pad += [(2 * x0 - x, y), (2 * x1 - x, y), (x, 2 * y0 - y), (x, 2 * y1 - y)]
    vor = Voronoi(np.vstack([P, pad]))
    s = float(_scale)
    faces = []
    for i in range(len(P)):                        # original sites only
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        poly = vor.vertices[reg]
        cx, cy = poly[:, 0].mean(), poly[:, 1].mean()
        vs = [Vector(cx + s * (x - cx), cy + s * (y - cy), 0) for x, y in poly]
        try:
            faces.append(make_face(Polyline(*vs, close=True)))
        except Exception:
            pass
    return ShapeList(faces)


def _map_to_surface(_shapes, _surface, _boundary=None, _width=100.0, _height=100.0):
    \"\"\"Lay flat shapes onto a surface: each shape's centroid maps from the 2D
    domain (the `boundary` rectangle, or 0..width x 0..height) to the surface's
    UV, and the shape is re-seated on the tangent plane there. Extrude the result
    (along the normal) to cut/boss radially.\"\"\"
    if _surface is None or not _shapes:
        return ShapeList([])
    faces = list(_surface.faces()) if hasattr(_surface, "faces") else [_surface]
    if not faces:
        return ShapeList([])
    face = max(faces, key=lambda f: getattr(f, "area", 0.0))
    x0, y0, x1, y1 = _domain2d(_boundary, _width, _height)
    dx = (x1 - x0) or 1.0; dy = (y1 - y0) or 1.0
    items = _shapes if isinstance(_shapes, (list, tuple, ShapeList)) else [_shapes]
    out = []
    for sh in items:
        if sh is None:
            continue
        try:
            c = sh.center()
            u = min(max((c.X - x0) / dx, 0.0), 1.0)
            v = min(max((c.Y - y0) / dy, 0.0), 1.0)
            P = face.position_at(u, v)
            tpl = Plane(origin=P, z_dir=face.normal_at(P))
            out.append(tpl.location * (Pos(-c.X, -c.Y, 0) * sh))
        except Exception:
            pass
    return ShapeList(out)


def _divide_surface(_shape, _u=6, _v=6):
    \"\"\"Sample a u x v grid of points on a surface. Uses the largest face of the
    input shape and build123d's parametric position_at(u, v) (u, v in 0..1).\"\"\"
    if _shape is None:
        return []
    faces = list(_shape.faces()) if hasattr(_shape, "faces") else [_shape]
    if not faces:
        return []
    face = max(faces, key=lambda f: getattr(f, "area", 0.0))
    nu, nv = max(int(_u), 1), max(int(_v), 1)
    out = []
    for i in range(nu):
        for j in range(nv):
            u = i / (nu - 1) if nu > 1 else 0.5
            v = j / (nv - 1) if nv > 1 else 0.5
            try:
                out.append(face.position_at(u, v))
            except Exception:
                pass
    return out


def _gm_pts(_pts):
    \"\"\"Normalise GraphMapper control points to [(x, y, hi, ho), ...] where hi/ho
    are absolute in-/out-handle coords (or None). Accepts plain [x, y] pairs or
    {p, hi, ho} anchor dicts; sorts by x (a function curve is single-valued).\"\"\"
    _out = []
    for _a in (_pts or []):
        if isinstance(_a, dict):
            _p, _hi, _ho = _a.get("p"), _a.get("hi"), _a.get("ho")
        else:
            _p, _hi, _ho = _a, None, None
        if not _p:
            continue
        _out.append((float(_p[0]), float(_p[1]),
                     tuple(_hi) if _hi else None, tuple(_ho) if _ho else None))
    _out.sort(key=lambda _r: _r[0])
    return _out


def _gm_bezier(_x, _pts):
    \"\"\"y at x along a chained cubic Bezier (anchors _pts = (x, y, hi, ho)). Finds
    the segment bracketing x, then solves X(s)=x by bisection (the curve is
    x-monotonic for a function) and returns Y(s). Missing handles fall back to the
    1/3 and 2/3 chord points (a straight segment).\"\"\"
    for _i in range(len(_pts) - 1):
        _x0, _y0, _, _ho = _pts[_i]
        _x1, _y1, _hi, _ = _pts[_i + 1]
        if not (_x0 <= _x <= _x1):
            continue
        _c0 = _ho if _ho else (_x0 + (_x1 - _x0) / 3.0, _y0 + (_y1 - _y0) / 3.0)
        _c1 = _hi if _hi else (_x0 + 2 * (_x1 - _x0) / 3.0, _y0 + 2 * (_y1 - _y0) / 3.0)
        _lo, _hs = 0.0, 1.0
        for _ in range(48):
            _s = (_lo + _hs) / 2.0
            _mt = 1 - _s
            _xs = (_mt**3 * _x0 + 3 * _mt**2 * _s * _c0[0]
                   + 3 * _mt * _s**2 * _c1[0] + _s**3 * _x1)
            if _xs < _x:
                _lo = _s
            else:
                _hs = _s
        _s = (_lo + _hs) / 2.0
        _mt = 1 - _s
        return (_mt**3 * _y0 + 3 * _mt**2 * _s * _c0[1]
                + 3 * _mt * _s**2 * _c1[1] + _s**3 * _y1)
    return _pts[-1][1] if _x >= _pts[-1][0] else _pts[0][1]


def _graphmap(_t, _pts, _mode="smooth"):
    \"\"\"Evaluate an editable function curve f(x)->y at parameter(s) _t along x.
    _pts are control points ([x, y] or {p, hi, ho}); modes: linear | smooth
    (monotone cubic, no overshoot) | bezier (per-segment cubic with tangent
    handles). Scalar in -> scalar out, list in -> list out (so it fans out).\"\"\"
    import numpy as _np
    _p = _gm_pts(_pts)
    _scalar = not isinstance(_t, (list, tuple))
    _ts = _np.atleast_1d(_np.asarray(_t, dtype=float))
    if len(_p) < 2:
        _ys = _np.full_like(_ts, _p[0][1] if _p else 0.0)
        return float(_ys[0]) if _scalar else _ys.tolist()
    _xs = _np.array([_q[0] for _q in _p])
    _ys = _np.array([_q[1] for _q in _p])
    _tc = _np.clip(_ts, _xs[0], _xs[-1])
    if _mode == "linear":
        _o = _np.interp(_tc, _xs, _ys)
    elif _mode == "bezier":
        _o = _np.array([_gm_bezier(float(_v), _p) for _v in _tc])
    else:  # smooth: monotone cubic interpolation (no overshoot)
        try:
            from scipy.interpolate import PchipInterpolator as _P
            _o = _P(_xs, _ys)(_tc)
        except Exception:
            _o = _np.interp(_tc, _xs, _ys)
    return float(_o[0]) if _scalar else _o.tolist()


def _array_polar(_shape, _count=6, _angle=360.0, _axis=None):
    \"\"\"Repeat a shape around a global axis -> list. A full 360 spaces `count`
    items evenly (no overlap at the seam); a partial angle includes both
    endpoints. Rotation is about the axis through the origin — Move the shape
    off-axis first for the classic bolt-circle.\"\"\"
    if _shape is None:
        return None
    _n = max(1, int(_count))
    _a = float(_angle)
    _ax = _axis if _axis is not None else Axis.Z
    if abs(_a) >= 360.0:
        _step = _a / _n
    else:
        _step = _a / (_n - 1) if _n > 1 else 0.0
    _d = _ax.direction
    return [Rot(_d.X * _step * _i, _d.Y * _step * _i, _d.Z * _step * _i) * _shape
            for _i in range(_n)]


def _align(_shape, _ref=None, _target=None):
    \"\"\"Translate a shape so `ref` (default: its bounding-box centre) lands on
    `target` (default: the global origin). Unwired = centre at origin.\"\"\"
    if _shape is None:
        return None
    _r = _pt(_ref)
    if _r is None:
        _bb = _shape.bounding_box()
        _r = (_bb.min + _bb.max) / 2
    _t = _pt(_target)
    if _t is None:
        _t = Vector(0, 0, 0)
    _v = _t - _r
    return Pos(_v.X, _v.Y, _v.Z) * _shape


def _split_tool(_t):
    \"\"\"Coerce whatever is wired into Split's `plane` socket into a cutting tool
    build123d's split() accepts: a Plane, a Face, or a Shell. A SOLID cuts by its
    skin (the shell of its faces) — so a sphere trims a box along the sphere.\"\"\"
    if _t is None:
        return Plane.XY
    if isinstance(_t, Plane):
        return _t
    if isinstance(_t, Location):
        return Plane(_t)
    if isinstance(_t, Shell):
        return _t
    _fs = list(_t.faces()) if hasattr(_t, "faces") else []
    if not _fs:
        return Plane.XY
    if len(_fs) == 1 and not (hasattr(_t, "solids") and _t.solids()):
        return _fs[0]
    return Shell(_fs)


def _on_tool(_p, _tool, _tol=1e-6):
    \"\"\"True when point _p lies ON the cutting tool — i.e. the face it came from
    is one the cut CREATED, not a face of the original shape.\"\"\"
    try:
        if isinstance(_tool, Plane):
            return abs(_tool.to_local_coords(Vector(_p)).Z) <= _tol
        return Vertex(_p.X, _p.Y, _p.Z).distance_to(_tool) <= _tol
    except Exception:
        return False


def _open_cut(_r, _tool):
    \"\"\"Drop the faces the cut created, leaving the shape OPEN where it was cut.
    Everything else survives, sewn back into a Shell (one per piece).\"\"\"
    if _r is None:
        return None
    _pieces = list(_r.solids()) or [_r]
    _out = []
    for _p in _pieces:
        _keep = [_f for _f in _p.faces() if not _on_tool(_f.center(), _tool)]
        if not _keep:
            continue
        try:
            _out.append(Shell(_keep))
        except Exception:
            _out.append(Compound(_keep))
    if not _out:
        return None
    return _out[0] if len(_out) == 1 else Compound(_out)


def _split(_shape, _plane=None, _keep=None, _solid=True):
    \"\"\"Split a shape by a plane, a surface or another solid, keeping the
    requested side(s). Defaults to the XY plane / Keep.TOP. With _solid False the
    cut is left OPEN — the faces the tool created are dropped and what comes back
    is a shell, not a capped solid.\"\"\"
    if _shape is None:
        return None
    _tool = _split_tool(_plane)
    _r = split(_shape, bisect_by=_tool,
               keep=_keep if _keep is not None else Keep.TOP)
    return _r if _solid else _open_cut(_r, _tool)


def _plane_normal(_origin=None, _normal=None):
    \"\"\"A Plane from an origin point and a normal (z) direction.\"\"\"
    _o = _pt(_origin)
    _n = _pt(_normal)
    return Plane(origin=(_o if _o is not None else Vector(0, 0, 0)),
                 z_dir=(_n if _n is not None else Vector(0, 0, 1)))


def _plane_offset(_plane, _dist=0.0):
    \"\"\"Slide a plane along its own normal by `dist`.\"\"\"
    _pl = _plane if isinstance(_plane, Plane) else Plane.XY
    return _pl.offset(float(_dist))


def _star(_outer=10.0, _inner=5.0, _points=5):
    \"\"\"A star outline: 2*points vertices alternating between the outer and
    inner radius, first spike pointing +Y.\"\"\"
    _n = max(2, int(_points))
    _pts = []
    for _i in range(2 * _n):
        _r = float(_outer) if _i % 2 == 0 else float(_inner)
        _a = math.pi * _i / _n + math.pi / 2
        _pts.append((_r * math.cos(_a), _r * math.sin(_a)))
    return _outline(Polygon(*_pts))


def _repeat(_v, _n=2):
    \"\"\"Repeat a value (or a whole list, concatenated) n times -> list.\"\"\"
    _items = list(_v) if _is_seq(_v) else [_v]
    return _items * max(0, int(_n))


def _shift(_lst, _offset=1, _wrap=True):
    \"\"\"Cyclically shift a list by `offset` (wrap), or drop from one end.\"\"\"
    _items = list(_lst) if _is_seq(_lst) else [_lst]
    if not _items:
        return _items
    _o = int(_offset)
    if _wrap:
        _o %= len(_items)
        return _items[_o:] + _items[:_o]
    return _items[_o:] if _o >= 0 else _items[:_o]


def _dispatch(_lst, _pattern=True, _invert=False):
    \"\"\"Keep the items where the (cycled) boolean pattern is True — the GH
    Dispatch/Cull idiom. `invert` keeps the False side instead.\"\"\"
    _items = list(_lst) if _is_seq(_lst) else [_lst]
    _pat = [bool(_p) for _p in (_pattern if _is_seq(_pattern) else [_pattern])]
    if not _pat:
        _pat = [True]
    return [_x for _i, _x in enumerate(_items)
            if _pat[_i % len(_pat)] != bool(_invert)]


def _unique(_lst):
    \"\"\"Drop duplicate items, keeping first occurrence (hashables by value,
    shapes by identity).\"\"\"
    _seen, _out = set(), []
    for _x in (list(_lst) if _is_seq(_lst) else [_lst]):
        _k = _x if isinstance(_x, (int, float, str, bool, tuple)) else id(_x)
        if _k not in _seen:
            _seen.add(_k)
            _out.append(_x)
    return _out


def _randlist(_count=10, _lo=0.0, _hi=1.0, _seed=1):
    \"\"\"`count` uniform random numbers in [lo, hi], deterministic per seed.\"\"\"
    _rng = random.Random(int(_seed))
    return [_rng.uniform(float(_lo), float(_hi)) for _ in range(max(0, int(_count)))]


def _export_3mf(_shape, _path):
    \"\"\"Write the shape to a 3MF file (via build123d's Mesher).\"\"\"
    _m = Mesher()
    _m.add_shape(_shape)
    _m.write(_path)
    return _shape


def _export_2d(_shape, _path, _fmt="svg"):
    \"\"\"Write a 2D shape (sketch/curve, or a Section of a solid) to SVG or DXF.
    The drawing is the XY projection — seat the geometry with ToPlane/Section
    first.\"\"\"
    _exp = ExportSVG() if _fmt == "svg" else ExportDXF()
    _exp.add_shape(_shape)
    _exp.write(_path)
    return _shape
"""

# Output wire types that yield a drawable preview (mesh for solids/sketches,
# polylines for curves, dots for points). Mirrors the mesh_extractor render paths.
_PREVIEWABLE = {catalog.WIRE_SOLID, catalog.WIRE_SURFACE, catalog.WIRE_CURVE,
                catalog.WIRE_VECTOR, catalog.WIRE_MESH}

# Pick-based selectors. Their output wire is `selection`/`data`, but at run time
# they hold sub-shapes the viewer can draw, so the eye works on them (opt-in
# only — see _previewed). Same list the emit dispatcher routes to _emit_select.
_SELECT_TYPES = ("SelectEdge", "SelectFace", "SelectVertex", "SelectShape")

# Node types whose output is always a Python list at runtime. Feeding one of
# these into an item-access input makes the consumer fan out. (A fanned node is
# added to this set dynamically as the graph is walked, so lists propagate.)
_LIST_PRODUCERS = {
    "ArrayLinear", "ArrayPolar", "ListCreate", "ListRange", "ListSeries", "ListRepeat",
    "ListSlice", "ListReverse", "ListSort", "ListFlatten", "Concat",
    "ListShift", "ListFilter", "ListUnique", "Random",
    "Voronoi2D", "Voronoi3D", "DivideSurface", "PopulateGeometry", "MapToSurface",
    "DivideCurve", "CurveEndpoints", "Deconstruct",
    "DeconstructEdges", "DeconstructFaces",
    "Surface", "Curve", "Point",   # gated containers always emit a list (filter/transform)
    "Geometry", "Plane",
    # Selectors: their `selection` output is consumed whole (list_access inputs),
    # while their geometry output (edges/faces/points) fans out — so they produce
    # a list. Pick-based Select* are marked in _emit_select; these run _emit_simple.
    "FacesByNormal", "FacesByType", "FacesByArea",
    "EdgesByType", "EdgesByLength", "SubshapesByPosition", "CombineSelection",
    "Series", "DivideDomain",
    "Input",    # source-mode multi-line text -> a list
    "Display",  # pass-through preserves list-ness
}


def format_param(pdef: catalog.Param, value) -> str:
    """Render a parameter value as a Python source literal/expression."""
    if value is None:
        value = pdef.default
    if pdef.type == "select":
        return pdef.code_map.get(value, repr(value))
    if pdef.raw:
        return str(value)
    if pdef.type == "bool":
        return "True" if value else "False"
    if pdef.type == "str":
        return repr(value)
    if pdef.type == "int":
        return str(int(value))
    return repr(float(value))


def _substitute(template: str, values: dict) -> str:
    """Replace {name} / {name!r} placeholders using `values`. Unknown -> None."""
    def repl(m: re.Match) -> str:
        key, conv = m.group(1), m.group(2)
        val = values.get(key, "None")
        if conv == "!r":
            return repr(val)
        return str(val)

    return _PLACEHOLDER.sub(repl, template)


# --- memo cache (transpile(memo=True) — the execute path only) ---------------
# A node's memo KEY is a content hash of its generated statement(s) with every
# variable name replaced by the KEY of the node that produced it, so keys are
# stable under var renumbering (adding an unrelated node never invalidates the
# cache) and change exactly when the node's own params/code or ANY upstream
# node changes. Keys address a store that the warm worker keeps alive across
# runs (__MEMO__), so a param edit re-executes only the dirty subtree.
_MEMO_VAR = re.compile(r"__(?:out|codeblock|ctx)_\d+(?:_vol)?\b")
# Deterministic but with a display/export/warning side effect: keyable (lineage
# stays cacheable downstream) yet re-executed every run so the side effect
# happens (_select_subshapes records "selection stale" re-pick warnings).
_MEMO_SIDEFX = ("_panel(", "_probe(", "_gate(", "_out(", "export_", "_export",
                "_select_subshapes(")
# Non-deterministic / reads external state: no key, poisons downstream too.
_MEMO_NONDET = ("import_", "open(", "random.")


class Transpiler:
    def __init__(self, graph: Graph, memo: bool = False):
        self.graph = graph
        self._memo = memo
        # var name -> memo key of the node output it carries (memo mode).
        self._var_key: dict[str, str] = {}
        # node id -> memo key (memo mode) — emitted as __hashes__ for the
        # persistent preview-mesh / view cache in mesh_extractor.
        self.key_of: dict[str, str] = {}
        self.var_of: dict[str, str] = {}
        # (node_id, output-socket name) -> var, for the rare node that emits
        # DISTINCT expressions per output (e.g. CenterOfMass: center + volume).
        self.out_var_of: dict[tuple, str] = {}
        self._counter = 0
        # Source-map instrumentation (off unless run(emit_map=True)).
        self._emit_map = False
        self._span_meta: dict[int, dict] = {}
        # node ids whose output is a list at runtime (static producers + any node
        # that gets fanned out). Filled as nodes are emitted in topological order.
        self._produces_list: set[str] = set()
        # A node is an "extremity" unless its geometry is passed onward to a node
        # that *continues* the geometry (feeds a geometry-class input AND itself
        # produces geometry). Feeding a Panel/inspector (data input) or an Export
        # (no output) doesn't count — those nodes stay visible by default.
        by_id = {n.id: n for n in graph.nodes}
        self._consumed: set[str] = set()
        for c in graph.connections:
            dst = by_id.get(c.to_node)
            if dst is None:
                continue
            ddef = catalog.get(dst.type)
            inp = ddef.input(c.to_socket)
            if inp is None or inp.wire_type not in _PREVIEWABLE:
                continue  # fed into a data/inspector input
            if ddef.outputs and ddef.outputs[0].wire_type in _PREVIEWABLE:
                self._consumed.add(c.from_node)  # geometry genuinely continues

    def _previewed(self, node, ndef: catalog.NodeDef) -> bool:
        """Whether this node draws in the viewport. Per-node eye:
        True/False force it; None (auto) shows only terminal geometry nodes
        (those whose output isn't consumed downstream)."""
        eye = getattr(node, "preview", None)
        previewable = (node.type == "CodeBlock" or
                       (ndef.outputs and ndef.outputs[0].wire_type in _PREVIEWABLE))
        if not previewable:
            # A selector's runtime value IS drawable — a ShapeList of picked
            # edges/faces/vertices — its declared first output just happens to be
            # a `selection` (or `data`) wire, which says how it may be WIRED, not
            # what it holds. Honour an explicit eye on it, but never auto-draw:
            # a real graph is full of wired selectors and highlighting them all
            # unasked would bury the part under its own picks.
            return eye is True and node.type in _SELECT_TYPES
        if eye is True:
            return True
        if eye is False:
            return False
        return node.id not in self._consumed  # auto: terminal only

    def _new_var(self, node_id: str, prefix: str = "__out_") -> str:
        self._counter += 1
        var = f"{prefix}{self._counter}"
        self.var_of[node_id] = var
        return var

    def _src_expr(self, fn: str, fs: str) -> str:
        """Expression for an upstream node's OUTPUT SOCKET. Defaults to the node's
        single var; a node that emits per-socket vars (e.g. CenterOfMass: center /
        volume) registers them in out_var_of so each output wire resolves distinctly."""
        return self.out_var_of.get((fn, fs), self.var_of.get(fn, "None"))

    def _input_values(self, node_id: str, ndef: catalog.NodeDef) -> dict[str, str]:
        """Map each input socket -> the source variable expression."""
        feeds = self.graph.inputs_of(node_id)
        out: dict[str, str] = {}
        for sock in ndef.inputs:
            srcs = feeds.get(sock.name, [])
            vars_ = [self._src_expr(fn, fs) for (fn, fs) in srcs]
            if sock.multiple:
                out[sock.name] = ", ".join(vars_)
            else:
                out[sock.name] = vars_[0] if vars_ else "None"
        return out

    def _merge_inputs(self, node_id: str, ndef: catalog.NodeDef,
                      values: dict[str, str]) -> dict[str, str]:
        """Overlay wired inputs onto param values, honouring params-as-inputs.

        An input socket that shares a param's name overrides the widget when
        WIRED and falls back to it when not (§5b). `_input_values` cannot say
        that — it reports "None" for every unwired socket — so merging it blindly
        wipes the widget's value. `_emit_simple` has always had the rule inline;
        the group path did not, and so emitted `Box(None, None, None)` for every
        CHILD of a BuildPart: a group child silently lost all of its parameters
        and the group failed with a constructor TypeError. Both paths merge here
        now, so the rule lives in exactly one place.
        """
        for name, expr in self._input_values(node_id, ndef).items():
            if expr == "None" and ndef.param(name) is not None:
                continue                    # nothing wired: keep the widget value
            values[name] = expr
        return values

    def _param_values(self, node, ndef: catalog.NodeDef) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in ndef.params:
            formatted = format_param(p, node.params.get(p.name, p.default))
            out[p.name] = self._wrap_param(node, p, formatted)
        return out

    def _register_span(self, meta: dict, literal: str) -> str:
        """Wrap a literal in source-map sentinels and remember its metadata."""
        sid = len(self._span_meta)
        self._span_meta[sid] = meta
        return f"{_SP_A}{sid}{_SP_SEP}{literal}{_SP_B}"

    def _wrap_param(self, node, pdef: catalog.Param, formatted: str) -> str:
        """Tag a built-in param's emitted literal so it becomes an editable span.
        Skipped for raw/code params (whole-expression, not a single literal) and
        when not building a map. A wired params-as-input overwrites this value
        before substitution, so its sentinel never reaches the final text — which
        is exactly why wired params get no editable span (the literal isn't there)."""
        if not self._emit_map or pdef.raw or pdef.type not in (
                "float", "int", "bool", "str", "select"):
            return formatted
        return self._register_span({
            "node_id": node.id, "node_type": node.type, "param": pdef.name,
            "kind": pdef.type, "label": pdef.label or pdef.name,
            "value": node.params.get(pdef.name, pdef.default),
            "min": pdef.min, "max": pdef.max, "step": pdef.step,
            "options": list(pdef.options) if pdef.options else None,
        }, formatted)

    @staticmethod
    def _body_outputs(body: list[str]) -> list[str]:
        """Ordered unique top-level assignment targets in a node's body."""
        outs: list[str] = []
        for bl in body:
            m = re.match(r"(__\w+) = ", bl)
            if m and m.group(1) not in outs:
                outs.append(m.group(1))
        return outs

    def _memo_plan(self, node, body: list[str], key_src: str):
        """(key, wrapped) for this node. key=None -> not cacheable (poisons
        downstream); wrapped=False with a key -> lineage stays cacheable but the
        body re-runs every time (display/export side effects)."""
        outs = self._body_outputs(body)
        # Hash the body WITHOUT preview lines, so toggling a node's eye reuses
        # the cached shape (and the mesh cache keeps its key).
        core = [bl for bl in body if not bl.startswith("__previews__[")]
        text = "\n".join(core) + "\n" + key_src
        if any(m in text for m in _MEMO_NONDET):
            return None, False
        own = {v: f"@o{i}" for i, v in enumerate(outs)}
        poisoned = False

        def sub(m: re.Match) -> str:
            nonlocal poisoned
            v = m.group(0)
            if v in own:
                return own[v]
            # Node-LOCAL tokens that are not assignment targets: a CodeBlock's
            # function name (its body is hashed via key_src) and a group's
            # builder ctx. Never carry upstream values -> stable placeholders.
            if v.startswith("__codeblock_"):
                return "@fn"
            if v.startswith("__ctx_"):
                return "@ctx"
            k = self._var_key.get(v)
            if k is None:           # upstream not cacheable -> neither are we
                poisoned = True
                return "@x"
            return k

        norm = _MEMO_VAR.sub(sub, text)
        if poisoned:
            return None, False
        key = hashlib.sha1(norm.encode()).hexdigest()[:16]
        for i, v in enumerate(outs):
            self._var_key[v] = f"{key}#{i}"
        self.key_of[node.id] = key
        wrapped = bool(outs) and not any(m in text for m in _MEMO_SIDEFX)
        return key, wrapped

    def _guard(self, lines: list[str], body: list[str], node,
               key_src: str = "", sub_ids: list[str] | None = None) -> None:
        """Wrap a node's statement(s) in try/except so one node's runtime error
        is recorded in __errors__ and doesn't abort the rest of the workflow.
        In memo mode, also wrap the body in a cache lookup: on a hit the node's
        output vars are restored from the persistent store and the body is
        skipped entirely (preview assignments still run), and the node brackets
        itself in _ev() progress events so the editor can light it up live."""
        key = wrapped = None
        if self._memo:
            key, wrapped = self._memo_plan(node, body, key_src)
        lines.append("try:")
        lines.append("    _t0 = _perf()")
        if self._memo:
            lines.append(f"    _ev('s', {node.id!r})")
        if wrapped:
            outs = self._body_outputs(body)
            tail = [bl for bl in body if bl.startswith("__previews__[")]
            core = [bl for bl in body if not bl.startswith("__previews__[")]
            tup = ", ".join(outs) + ("," if len(outs) == 1 else "")
            lines.append(f"    _m = _memo_get({key!r})")
            lines.append("    if _m is None:")
            for bl in core:
                if bl:
                    lines.append("        " + bl)
            lines.append(f"        _memo_put({key!r}, ({tup}))")
            lines.append("    else:")
            lines.append(f"        ({tup}) = _m")
            lines.append(f"        __cached__[{node.id!r}] = True")
            # The body was skipped, so anything nested in it (a group's children)
            # never got to speak for itself. Report it as what it is: cached.
            for sid in (sub_ids or []):
                lines.append(f"        __cached__[{sid!r}] = True")
                lines.append(f"        __timings__[{sid!r}] = 0.0")
                lines.append(f"        _ev('s', {sid!r})")
                lines.append(f"        _ev('e', {sid!r}, 0.0, True)")
            for bl in tail:
                lines.append("    " + bl)
        else:
            for bl in body:
                if bl:
                    lines.append("    " + bl)
        lines.append(f"    __timings__[{node.id!r}] = _perf() - _t0")
        if self._memo:
            lines.append(
                f"    _ev('e', {node.id!r}, __timings__[{node.id!r}], "
                f"__cached__.get({node.id!r}, False))"
            )
        lines.append("except Exception as _e:")
        lines.append(f"    __timings__[{node.id!r}] = _perf() - _t0")
        if self._memo:
            lines.append(
                f"    _ev('e', {node.id!r}, __timings__[{node.id!r}], False, True)"
            )
        var = self.var_of.get(node.id)
        if var:
            lines.append(f"    {var} = None")
        lines.append(f"    __errors__[{node.id!r}] = f\"{{type(_e).__name__}}: {{_e}}\"")

    def _emit_bypass(self, node, lines: list[str]) -> None:
        """Bypassed node: skip its operation and pass an upstream value straight
        through to its output (Grasshopper/ComfyUI semantics). Prefer an input
        whose wire type matches the first output; else the first connected input.
        A node with no output simply disappears from the program."""
        ndef = catalog.get(node.type)
        if not ndef.outputs:
            return
        var = self._new_var(node.id)
        out_wt = ndef.outputs[0].wire_type
        inputs = self._input_values(node.id, ndef)
        chosen = None
        for sock in ndef.inputs:
            v = inputs.get(sock.name, "None")
            if not v or v == "None":
                continue
            v = v.split(",")[0].strip()  # passthrough takes the first feed
            if sock.wire_type == out_wt:
                chosen = v
                break
            if chosen is None:
                chosen = v
        lines.append(f"{var} = {chosen or 'None'}{_annot(node)}  # bypassed")
        # A bypass is a pure alias: it carries the SAME value as its source, so
        # in memo mode its var inherits the upstream key (lineage continues).
        if self._memo and chosen in self._var_key:
            self._var_key[var] = self._var_key[chosen]
        # Bypassing a node must not silently un-draw it: it still carries a value
        # (its upstream's), so it still answers to the eye. Unguarded, like the
        # alias itself — there is no operation here that could throw.
        if self._previewed(node, ndef):
            lines.append(f"__previews__[{node.id!r}] = {var}")

    def _emit_select(self, node, lines: list[str]) -> None:
        """Sub-shape selector (SelectEdge/Face/Vertex): resolve the picked set
        against the upstream shape at run time via the injected helper."""
        ndef = catalog.get(node.type)
        var = self._new_var(node.id)
        in_name = ndef.inputs[0].name if ndef.inputs else "geometry"
        src = self._input_values(node.id, ndef).get(in_name, "None")
        sel = node.params.get("selection") or {}
        default_kind = {"SelectFace": "face", "SelectVertex": "vertex",
                        "SelectShape": "shape"}.get(node.type, "edge")
        kind = sel.get("kind", default_kind)
        indices = sel.get("indices", []) or []
        sigs = sel.get("sigs", []) or []
        body = [f"{var} = _select_subshapes({src}, {kind!r}, {indices!r}, {sigs!r}, {node.id!r}){_annot(node)}"]
        # The resolved var is a ShapeList of sub-shapes. Its `selection` output is
        # consumed WHOLE (Fillet/… inputs are list_access); its geometry output
        # (edges/faces/points) fans out downstream, so mark it a list-producer.
        self._produces_list.add(node.id)
        # With the eye on, DRAW the picked set — edges as polylines, faces as a
        # mesh, vertices as dots. Seeing what you picked is the whole point of a
        # picker, and until this hook existed the eye on a selector did nothing.
        if self._previewed(node, ndef):
            body.append(f"__previews__[{node.id!r}] = {var}")
        self._guard(lines, body, node)

    def _emit_vectorize(self, node, lines: list[str]) -> None:
        """TraceImage: rebuild wires from the contour artifact FROZEN into the
        node by its ✎ edit-mode. Mirrors _emit_select — the contours + mm/pixel
        scale are inlined as literals and resolved by the injected helper, so no
        image is read at run time and the graph re-runs from fixed data."""
        var = self._new_var(node.id)
        trace = node.params.get("trace") or {}
        contours = trace.get("contours", []) or []
        scale = trace.get("scale", 1.0) or 1.0
        imgh = trace.get("imgH")
        body = [f"{var} = _trace_curves({contours!r}, {scale!r}, {imgh!r}){_annot(node)}"]
        if self._previewed(node, catalog.get(node.type)):
            body.append(f"__previews__[{node.id!r}] = {var}")
        self._guard(lines, body, node)

    def _emit_center(self, node, lines: list[str]) -> None:
        """CenterOfMass: a universal centre + a volume from one shape input, via the
        injected helpers. Special-cased because a node's two outputs otherwise share
        one var — here `center` (a vector) and `volume` (a number) need distinct
        expressions, registered per output socket in out_var_of."""
        ndef = catalog.get(node.type)
        var = self._new_var(node.id)                 # var_of[node] = center (the default)
        src = self._input_values(node.id, ndef).get("shape", "None")
        volvar = var + "_vol"
        lines.append(f"{volvar} = None")             # defined even if the guarded body throws
        body = [f"{var} = _center_of({src}){_annot(node)}",
                f"{volvar} = _volume_of({src})"]
        self.out_var_of[(node.id, "center")] = var
        self.out_var_of[(node.id, "volume")] = volvar
        if self._previewed(node, ndef):          # the centre, drawn as a dot
            body.append(f"__previews__[{node.id!r}] = {var}")
        self._guard(lines, body, node)

    def _emit_orient(self, node, lines: list[str]) -> None:
        """OrientForPrint: one search, two outputs — the oriented mesh and the table
        that says why it won. Same shape as _emit_center: the node's two output
        sockets need distinct expressions, so they are registered in out_var_of. The
        plan is computed ONCE and both outputs read it (scoring 60 poses means 60
        slicings; doing it twice because a Panel is wired in would be daft)."""
        ndef = catalog.get(node.type)
        var = self._new_var(node.id)                 # var_of[node] = the mesh
        vals = self._input_values(node.id, ndef)
        p = self._param_values(node, ndef)          # keeps the editable-literal spans
        args = ", ".join([vals.get("mesh", "None"), vals.get("load", "None"),
                          p["strength"], p["supports"], p["speed"],
                          p["angle"], p["layer"], p["exact_below"]])
        plan, rep = var + "_plan", var + "_rep"
        lines.append(f"{rep} = None")                # defined even if the body throws
        body = [f"{plan} = _orient_plan({args}){_annot(node)}",
                f"{var} = {plan}['mesh']",
                f"{rep} = {plan}['report']"]
        self.out_var_of[(node.id, "result")] = var
        self.out_var_of[(node.id, "report")] = rep
        if self._previewed(node, ndef):          # the ORIENTED mesh, not the input
            body.append(f"__previews__[{node.id!r}] = {var}")
        self._guard(lines, body, node)

    def _emit_codeblock(self, node, lines: list[str]) -> None:
        """A CodeBlock transpiles like TWO connected nodes: a params node and a
        code node. Declared `#@param`s become the function's named ARGUMENTS — so
        the body stays pure user code (the `#@param` declaration lines are dropped
        from it) and every param value appears exactly once, at the call site, as
        an editable literal. The code body itself is exposed as an editable `code`
        input (a span on the function name)."""
        ndef = catalog.get(node.type)
        var = self._new_var(node.id)
        feeds = self.graph.inputs_of(node.id)
        inputs = self._input_values(node.id, ndef)        # the static in_0..in_5
        user_code = node.params.get("code", "result = None")
        overrides = node.params.get("_cb") or {}
        declared = parse_codeblock_params(user_code)
        names = [d["name"] for d in declared]
        fn = f"__codeblock_{self._counter}"

        # Resolve each param's call-site argument. A same-named wired socket drives
        # it (params-as-inputs) and fans the whole block out if it carries a list
        # (Range -> CodeBlock.teeth => one result per value); otherwise it is the
        # effective value (override, else the in-code default) as an editable span.
        fan: dict[str, str] = {}          # name -> list expr fed to _fanout
        call_arg: dict[str, str] = {}     # name -> expr passed as <name>=... at the call
        for d in declared:
            name = d["name"]
            srcs = feeds.get(name, [])
            vars_ = [self._src_expr(fid, fs) for (fid, fs) in srcs]
            if vars_:
                maybe_list = len(vars_) >= 2 or any(
                    fid in self._produces_list for (fid, _fs) in srcs)
                if maybe_list:
                    fan[name] = f"[{', '.join(vars_)}]" if len(vars_) >= 2 else vars_[0]
                    call_arg[name] = name        # bound by the _fanout lambda
                else:
                    call_arg[name] = vars_[0]
            else:
                value = overrides.get(name, d["default"])
                literal = _cb_literal(value, d["type"])
                if self._emit_map:
                    literal = self._register_span({
                        "node_id": node.id, "node_type": node.type,
                        "param": "_cb." + name, "kind": d["type"], "label": name,
                        "value": value, "min": d["min"], "max": d["max"],
                        "step": d["step"], "options": d["options"],
                    }, literal)
                call_arg[name] = literal

        # def <fn>(in_0..in_5, <params...>):  — the function name doubles as the
        # editable `code` span (click it in the code view to edit the whole body).
        fn_tok = fn
        if self._emit_map:
            fn_tok = self._register_span({
                "node_id": node.id, "node_type": node.type, "param": "code",
                "kind": "code", "label": "code", "value": user_code,
                "min": None, "max": None, "step": None, "options": None,
            }, fn)
        sig = [s.name for s in ndef.inputs] + names
        lines.append(f"def {fn_tok}({', '.join(sig)}):")
        emitted = False
        for raw in user_code.splitlines() or ["result = None"]:
            if _parse_cb_line(raw) is not None:
                continue                 # a #@param declaration -> now an argument
            lines.append("    " + raw)
            emitted = True
        if not emitted:
            lines.append("    pass")
        lines.append("    return result")

        in_kw = ", ".join(f"{s.name}={inputs.get(s.name, 'None')}" for s in ndef.inputs)
        param_kw = "".join(f", {n}={call_arg[n]}" for n in names)
        if fan:
            lam = f"lambda {', '.join(fan)}: {fn}({in_kw}{param_kw})"
            kw = "{" + ", ".join(f"{n!r}: {v}" for n, v in fan.items()) + "}"
            call = f"_fanout({lam}, {kw})"
            self._produces_list.add(node.id)   # fanned -> output is a list
        else:
            call = f"{fn}({in_kw}{param_kw})"

        body = [f"{var} = {call}{_annot(node)}"]
        if self._previewed(node, ndef):
            body.append(f"__previews__[{node.id!r}] = {var}")
        # The def lines live outside the guarded body: hash the user code too,
        # so editing the block's code invalidates its cache entry.
        self._guard(lines, body, node, key_src=user_code)

    def _cast(self, src, sock, var: str) -> str:
        """Auto-apply a boundary cast when the effective upstream type needs one to
        match this input's type (e.g. a curve into a sketch input -> _face(var)).
        Skipped for `raw` sockets (the node coerces by itself) and identity edges.
        The registry of casts lives in cad_nodes/casts.py."""
        if getattr(sock, "raw", False):
            return var
        eff = self.graph.effective_output_type(src[0], src[1])
        helper = catalog.cast_helper(eff, sock.wire_type)
        return f"{helper}({var})" if helper else var

    def _emit_simple(self, node, lines: list[str]) -> None:
        ndef = catalog.get(node.type)
        feeds = self.graph.inputs_of(node.id)

        # Classify each input. Item-access inputs that may carry a list at run
        # time (>=2 connections, or fed by a list-producing node) FAN OUT: they
        # become lambda parameters and _fanout maps the node over the items.
        subs = self._param_values(node, ndef)
        subs["node_id"] = node.id
        fan: dict[str, str] = {}  # input name -> value expr passed to _fanout
        for sock in ndef.inputs:
            srcs = feeds.get(sock.name, [])
            vars_ = [self._src_expr(fn, fs) for (fn, fs) in srcs]
            if sock.multiple:                       # collector: whole list
                subs[sock.name] = ", ".join(vars_)
            elif sock.list_access:                  # consumes the list as-is
                subs[sock.name] = vars_[0] if vars_ else "None"
            elif not vars_:
                # Unconnected: fall back to a same-named param (params-as-inputs —
                # wire the socket to override/drive the widget), else None.
                if ndef.param(sock.name) is None:
                    subs[sock.name] = "None"
            else:
                maybe_list = len(vars_) >= 2 or any(
                    fn in self._produces_list for (fn, _fs) in srcs)
                if maybe_list:
                    fan[sock.name] = f"[{', '.join(vars_)}]" if len(vars_) >= 2 else vars_[0]
                    subs[sock.name] = sock.name     # bound by the lambda
                else:
                    subs[sock.name] = self._cast(srcs[0], sock, vars_[0])

        # Drop with `collide`: the shapes wired into the node are ONE scene, not
        # a fan — hand the whole list to the runtime (which stacks them against
        # each other) and mark the output a list so downstream still fans.
        # A wired `container` (the immovable bowl/tray) means the same thing: the
        # parts fall INTO one thing, so they are one scene whatever the toggle says.
        if node.type == "Drop":
            if "container" in fan:                  # several statics = one rig
                subs["container"] = fan.pop("container")
            # Which node DREW the container. Only the emitter knows — the runtime
            # sees a shape, not a graph — and the viewer needs it to give the bowl
            # its own colour and finish instead of the falling parts' (§5d-bis).
            subs["container_ids"] = repr([fn for (fn, _fs)
                                          in feeds.get("container", [])])
            subs["shape_ids"] = repr([fn for (fn, _fs) in feeds.get("shape", [])])
            scene = (subs.get("collide") == "True"
                     or subs.get("container") not in (None, "None"))
            if scene and "shape" in fan:
                subs["shape"] = fan.pop("shape")
                self._produces_list.add(node.id)

        # `about="group"` while the shape input fans out: every item must pivot
        # about the ONE collective centre, or each piece spins about itself —
        # hoist the centre out of the per-item lambda. Unfanned there is nothing
        # to do: _rotate sees the whole value and its bbox IS the group box.
        hoist = None
        if (subs.get("about") == "'group'" and "shape" in fan
                and subs.get("pivot") in (None, "None")):
            self._counter += 1
            pv = f"__pivot_{self._counter}"
            hoist = f"{pv} = _pivot_of({fan['shape']})"
            subs["pivot"] = pv

        template = ndef.code_template.get("algebra")
        if template is None:
            raise ValueError(f"Node {node.type} has no algebra template")
        expr = _substitute(template, subs)

        # Optional `origin` input positions the result; it is item-access too, so
        # it may be a lambda-bound name (fanned) or a direct value.
        origin = subs.get("origin")
        if origin and origin != "None":
            expr = f"_at({expr}, {origin})"

        if fan:
            lam = f"lambda {', '.join(fan)}: {expr}"
            kw = "{" + ", ".join(f"{n!r}: {v}" for n, v in fan.items()) + "}"
            expr = f"_fanout({lam}, {kw})"
            self._produces_list.add(node.id)        # fanned -> output is a list
        elif node.type in _LIST_PRODUCERS:
            self._produces_list.add(node.id)

        if ndef.outputs:
            var = self._new_var(node.id)
            body = ([hoist] if hoist else []) + [f"{var} = {expr}{_annot(node)}"]
            if self._previewed(node, ndef):
                body.append(f"__previews__[{node.id!r}] = {var}")
            self._guard(lines, body, node)
        else:  # export / sink statement
            self._guard(lines, [f"{expr}{_annot(node)}"], node)

    def _emit_group(self, node, lines: list[str], indent: str = "") -> None:
        ndef = catalog.get(node.type)
        self._counter += 1
        ctx = f"__ctx_{self._counter}"
        var = f"__out_{self._counter}"
        self.var_of[node.id] = var
        values = self._merge_inputs(node.id, ndef, self._param_values(node, ndef))
        values["ctx"] = ctx
        header = _substitute(ndef.code_template["builder"], values)

        # Build the block at relative indent; _guard shifts it under try/except.
        body = [header + _annot(node)]
        children = self.graph.children_of(node.id)
        child_order = toposort([c.id for c in children],
                               [(c.from_node, c.to_node) for c in self.graph.connections
                                if c.from_node in {x.id for x in children}
                                and c.to_node in {x.id for x in children}])
        for cid in child_order:
            child = self.graph.node(cid)
            cdef = catalog.get(child.type)
            cvals = self._merge_inputs(child.id, cdef, self._param_values(child, cdef))
            tmpl = cdef.code_template.get("builder") or cdef.code_template.get("algebra", "")
            stmt = _substitute(tmpl, cvals) + _annot(child)
            if not self._memo:
                body.append("    " + stmt)
                continue
            # A child is substituted INLINE into the parent's `with` block, so it
            # cannot be _guard()ed on its own — and for a long time that meant it
            # reported nothing at all: a BuildPart of twenty nodes lit ONE glow and
            # the twenty stayed dark for the whole run, however long they took.
            # They bracket themselves instead.
            #
            # The inner try/except only REPORTS: it re-raises, so a failing child
            # still fails its group exactly as before. Without it the exception
            # would unwind straight past the child's 's' event to the group's
            # handler, leaving that node breathing amber to the end of the run —
            # the same lie that an un-closed span always tells.
            body.append(f"    _ev('s', {cid!r})")
            body.append("    _tc = _perf()")
            body.append("    try:")
            body.append("        " + stmt)
            body.append("    except Exception:")
            body.append(f"        __timings__[{cid!r}] = _perf() - _tc")
            body.append(f"        _ev('e', {cid!r}, __timings__[{cid!r}], False, True)")
            body.append("        raise")
            body.append(f"    __timings__[{cid!r}] = _perf() - _tc")
            body.append(f"    _ev('e', {cid!r}, __timings__[{cid!r}], False)")
        if not children:
            body.append("    pass")

        attr = {"part": "part", "sketch": "sketch", "line": "line"}.get(ndef.group_kind, "part")
        body.append(f"{var} = {ctx}.{attr}{_annot(node)}")
        if self._previewed(node, ndef):
            body.append(f"__previews__[{node.id!r}] = {var}")
        # On a cache hit the whole block is skipped, children included — so tell
        # _guard who they are and it reports them cached too. Otherwise a group's
        # children would light up on a cold run and go dark on every warm one,
        # which looks exactly like the glow failing at random.
        self._guard(lines, body, node, sub_ids=list(child_order))

    def _pick_result(self, order: list[str]) -> str | None:
        # A connection into a pure sink (a node with no outputs — every Export
        # node) does NOT "consume" the shape: the upstream node is still the
        # meaningful end of the chain. Otherwise wiring geometry into an Export
        # node hides it from the preview/quick-export, which then falls back to
        # some dangling node (e.g. an unconnected Import). See feedback
        # 20260707-231423 ("export step non va piu").
        def _has_outputs(nid: str) -> bool:
            try:
                return bool(catalog.get(self.graph.node(nid).type).outputs)
            except Exception:
                return True
        used_as_source = {c.from_node for c in self.graph.connections
                          if _has_outputs(c.to_node)}
        geometry_like = {catalog.WIRE_SOLID, catalog.WIRE_SURFACE, catalog.WIRE_MESH}
        candidates = []
        for nid in order:
            if nid not in self.var_of:
                continue
            ndef = catalog.get(self.graph.node(nid).type)
            if ndef.outputs and ndef.outputs[0].wire_type in geometry_like:
                candidates.append(nid)
        sinks = [n for n in candidates if n not in used_as_source]
        chosen = (sinks or candidates)
        if chosen:
            return self.var_of[chosen[-1]]
        # fallback: the last variable actually produced (skip nodes that emit no
        # var — Note annotations, export sinks — which can sit last in topo order)
        for nid in reversed(order):
            if nid in self.var_of:
                return self.var_of[nid]
        return None

    def _extract_spans(self, text: str) -> tuple[str, list[dict]]:
        """Strip the source-map sentinels from `text`, returning the clean source
        and a list of param spans with their (row, col0, col1) in that clean text.
        Rows/cols are 0-based to match Ace editor coordinates."""
        clean: list[str] = []
        spans: list[dict] = []
        row = col = i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == _SP_A:
                m = _SP_RE.match(text, i)
                if m:
                    sid, literal = int(m.group(1)), m.group(2)
                    clean.append(literal)
                    meta = dict(self._span_meta.get(sid, {}))
                    meta.update(row=row, col0=col, col1=col + len(literal))
                    spans.append(meta)
                    col += len(literal)
                    i = m.end()
                    continue
            clean.append(ch)
            if ch == "\n":
                row += 1
                col = 0
            else:
                col += 1
            i += 1
        return "".join(clean), spans

    def run(self, emit_map: bool = False):
        """Transpile the graph. With emit_map, return (source, param_spans) where
        each span maps a code location back to a node param for inline editing."""
        self._emit_map = emit_map
        warnings = self.graph.validate()
        order = toposort([n.id for n in self.graph.nodes], self.graph.edges())

        body: list[str] = []
        for nid in order:
            node = self.graph.node(nid)
            if node.parent is not None:
                continue  # emitted inside its group
            if node.type in ("Note", "RefImage"):
                continue  # editor-only (annotation / viewport reference) — never emitted
            if getattr(node, "bypassed", False):
                self._emit_bypass(node, body)
                continue
            ndef = catalog.get(node.type)
            if ndef.is_group:
                self._emit_group(node, body)
            elif node.type == "CodeBlock":
                self._emit_codeblock(node, body)
            elif node.type in _SELECT_TYPES:
                self._emit_select(node, body)
            elif node.type == "TraceImage":
                self._emit_vectorize(node, body)
            elif node.type == "CenterOfMass":
                self._emit_center(node, body)
            elif node.type == "OrientForPrint":
                self._emit_orient(node, body)
            else:
                self._emit_simple(node, body)

        result_var = self._pick_result(order)

        out = [PREAMBLE, "", "# --- nodes ---"]
        if warnings:
            out.append("# warnings: " + "; ".join(warnings))
        out.extend(body)
        out.append("")
        out.append("# --- result for preview ---")
        out.append(f"__result__ = {result_var}" if result_var else "__result__ = None")
        if self._memo and self.key_of:
            hashes = dict(self.key_of)
            result_nid = next((nid for nid, v in self.var_of.items()
                               if v == result_var), None)
            if result_nid in hashes:
                hashes["__result__"] = hashes[result_nid]
            out.append("# node content keys -> persistent mesh/view cache")
            out.append(f"__hashes__.update({hashes!r})")
        text = "\n".join(out) + "\n"
        if emit_map:
            return self._extract_spans(text)
        return text


def transpile(graph: Graph, memo: bool = False) -> str:
    """Convenience: transpile a Graph to build123d source. memo=True (the
    execute path) wraps each cacheable node in a persistent-store lookup so a
    warm worker re-runs only what changed; the default output stays clean for
    the /ui code view."""
    return Transpiler(graph, memo=memo).run()


def transpile_with_map(graph: Graph) -> tuple[str, list[dict]]:
    """Transpile + a param<->code source map (see Transpiler._extract_spans)."""
    return Transpiler(graph).run(emit_map=True)
