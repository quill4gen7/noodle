"""🔦 Traccia — the RUNTIME of a CodeBlock trace (see cb_trace.py).

This file is not imported by the engine: its SOURCE is injected at the top of
the trace run's script (executor.codeblock_trace_run), like the sections
markers are. It is also a plain module, importable on its own, so the tests
can drive it with stand-in shapes — nothing here imports build123d at load.

The block runs from a rewritten copy in which every call is
``__cbt_call__(line, f, *args)`` and every ``+ - & *`` is
``__cbt_bin__(line, op, a, b)``. From that the trace keeps, per object:

- P     the #@param names it depends on. Numbers carry them themselves
        (_TF / _TI: a float / int that remembers), every other object by id.
- COST  seconds to build it from scratch (its call + what its inputs cost).
- CONT  what it is made of: [(node, M, mp)] — a node of the piece tree, the
        4x4 matrix carrying that node's frame into this object's, and the
        params that matrix depends on (a Pos(x…) that moved it).

A boolean turns each operand into a NODE (the piece as it enters the
boolean) and the result contains those nodes. A move (Pos/Rot * shape,
.moved…, mirror) carries the contents with a matrix. A fillet / chamfer on
picked edges adds a FEATURE node: the edges, with the radius's params.
_cbt_finish walks the tree from the block's outputs and draws each node
whole when it costs ≤ budget, else its parts, else its curves.

Nothing here may break the user's code: every bookkeeping step is wrapped,
and a failure there only loses that step's trace.
"""
import operator as _cbt_op
import time as _cbt_time
import types as _cbt_types

_CBT_E = frozenset()
R = {"P": {}, "COST": {}, "LINE": {}, "CONT": {}, "SRC": {}, "NODE": {}, "KEEP": [], "BOOL": set(), "LEFT": {},
     "nodes": [], "bools": 0, "depth": 0, "out": None, "params": [], "t0": _cbt_time.perf_counter()}


# ── numbers that remember which #@param they came from ──
class _TF(float):
    pass


class _TI(int):
    pass


def _cbt_num(v, p):
    if not p or isinstance(v, bool):
        return v
    if isinstance(v, float):
        r = _TF(v)
    elif isinstance(v, int):
        r = _TI(v)
    else:
        return v
    r._p = p
    return r


def _cbt_plain(x):
    t = type(x)
    if t is _TF:
        return float(x)
    if t is _TI:
        return int(x)
    return x


def _cbt_pp(x, depth=0):
    """The params x depends on."""
    if isinstance(x, (_TF, _TI)):
        return x._p
    if x is None or isinstance(x, (bool, int, float, str, bytes)):
        return _CBT_E
    p = R["P"].get(id(x), _CBT_E)
    if depth < 2 and isinstance(x, (list, tuple, set, frozenset)):
        for i, y in enumerate(x):
            if i >= 256:
                break
            p = p | _cbt_pp(y, depth + 1)
    elif depth < 2 and isinstance(x, dict):
        for i, y in enumerate(x.values()):
            if i >= 256:
                break
            p = p | _cbt_pp(y, depth + 1)
    return p


def _cbt_note(r, p):
    """A result of arithmetic: a number keeps p itself, anything else by id."""
    if isinstance(r, (int, float)) and not isinstance(r, bool):
        return _cbt_num(r, p)
    if p and r is not None and not isinstance(r, (bool, str, bytes)):
        _cbt_reg(r, p, None, None, None)
    return r


def _cbt_arith(f):
    def fwd(a, b):
        return _cbt_note(f(_cbt_plain(a), _cbt_plain(b)), _cbt_pp(a) | _cbt_pp(b))

    def rev(a, b):
        return _cbt_note(f(_cbt_plain(b), _cbt_plain(a)), _cbt_pp(a) | _cbt_pp(b))
    return fwd, rev


for _n in ("add", "sub", "mul", "truediv", "floordiv", "mod", "pow"):
    _fw, _rv = _cbt_arith(getattr(_cbt_op, _n))
    for _c in (_TF, _TI):
        setattr(_c, f"__{_n}__", _fw)
        setattr(_c, f"__r{_n}__", _rv)
for _n in ("neg", "pos", "abs"):
    def _un(f):
        return lambda a: _cbt_num(f(_cbt_plain(a)), a._p)
    for _c in (_TF, _TI):
        setattr(_c, f"__{_n}__", _un(getattr(_cbt_op, _n)))


# ── shapes ──
def _cbt_is_shape(x):
    if getattr(x, "_noodle_mesh", False):
        return True
    return hasattr(x, "wrapped") and hasattr(type(x), "faces") and hasattr(x, "location")


def _cbt_shapes_in(x):
    if _cbt_is_shape(x):
        return [x]
    if isinstance(x, (list, tuple)):
        return [y for y in x[:4000] if _cbt_is_shape(y)]
    return []


def _cbt_reg(x, p, cost, line, cont):
    """Remember x (first sight wins its cost and contents; params accumulate)."""
    k = id(x)
    if k in R["P"]:
        R["P"][k] = R["P"][k] | (p or _CBT_E)
        if line is not None and R["depth"] == 0:
            R["LINE"][k] = line                # the call site in the block's own flow
        return
    R["P"][k] = p or _CBT_E
    R["KEEP"].append(x)                        # ids must stay unique while the run lasts
    if cost is not None:
        R["COST"][k] = cost
    if line is not None:
        R["LINE"][k] = line
    if cont:
        R["CONT"][k] = cont


def _cbt_node(x):
    """x as a node of the piece tree (once per object)."""
    k = id(x)
    i = R["NODE"].get(k)
    if i is None:
        if k not in R["P"]:
            _cbt_reg(x, _CBT_E, 0.0, None, None)
        R["nodes"].append({"obj": x, "cost": R["COST"].get(k, 0.0), "p": R["P"].get(k, _CBT_E),
                           "line": R["LINE"].get(k), "kids": list(R["CONT"].get(k, ())), "feat": False,
                           "bool": k in R["BOOL"], "left": R["LEFT"].get(k)})
        i = len(R["nodes"]) - 1
        R["NODE"][k] = i
    return i


def _cbt_entries(x):
    c = R["CONT"].get(id(x))
    return c if c else [(_cbt_node(x), None, _CBT_E)]


def _cbt_mm(A, B):
    if A is None:
        return B
    if B is None:
        return A
    return A @ B


def _cbt_loc_matrix(shape):
    import numpy as np
    T = shape.location.wrapped.Transformation()
    M = np.eye(4)
    for i in range(3):
        for j in range(4):
            M[i, j] = T.Value(i + 1, j + 1)
    return M


def _cbt_move(r, s):
    """The matrix that took s to r, when r is s moved (same TShape): else None."""
    try:
        if not r.wrapped.IsPartner(s.wrapped):
            return None
        import numpy as np
        return _cbt_loc_matrix(r) @ np.linalg.inv(_cbt_loc_matrix(s))
    except Exception:
        return None


def _cbt_mirror(ins):
    import numpy as np
    pl = next((x for x in ins if hasattr(x, "z_dir") and hasattr(x, "origin")), None)
    if pl is None:
        n, o = np.array([0.0, 1.0, 0.0]), np.zeros(3)   # build123d's default: Plane.XZ
    else:
        n = np.array([pl.z_dir.X, pl.z_dir.Y, pl.z_dir.Z], float)
        o = np.array([pl.origin.X, pl.origin.Y, pl.origin.Z], float)
    n = n / (np.linalg.norm(n) or 1.0)
    M = np.eye(4)
    M[:3, :3] -= 2 * np.outer(n, n)
    M[:3, 3] = 2 * float(o @ n) * n
    return M


# sub-shape queries: their results are a SELECTION of the shape asked, not a part of it
_CBT_SELECT = {"edges", "edge", "faces", "face", "vertices", "vertex", "wires", "wire",
               "group_by", "sort_by", "filter_by", "filter_by_position", "sort_by_distance",
               "__getitem__", "first", "last"}


def _cbt_uniq(xs):
    seen, out = set(), []
    for x in xs:
        if id(x) not in seen:
            seen.add(id(x))
            out.append(x)
    return out


def _cbt_after(line, f, a, k, r, dt):
    s = getattr(f, "__self__", None)
    if isinstance(s, (_cbt_types.ModuleType, type)):
        s = None
    ins = ([s] if s is not None else []) + list(a) + list(k.values())
    p = _CBT_E
    for x in ins:
        p = p | _cbt_pp(x)
    if r is None or isinstance(r, (bool, str, bytes)):
        return r
    if f is range and p and len(r) <= 100000:
        return _TRange(_cbt_num(i, p) for i in r)
    if isinstance(r, (int, float)):
        return _cbt_num(r, p | _cbt_pp(r)) if p else r
    rs = _cbt_shapes_in(r)
    if not rs:
        if p:
            _cbt_reg(r, p, None, line, None)   # a Vector, a Location, a list of points…
        return r
    name = getattr(f, "__name__", "") or ""
    if s is not None and name in _CBT_SELECT and (_cbt_is_shape(s) or isinstance(s, (list, tuple))):
        # picked sub-shapes remember the shape they were picked FROM
        srcs = [R["SRC"].get(id(y), y) for y in _cbt_shapes_in(s)]
        src = srcs[0] if srcs else s
        for y in rs:
            if id(y) not in R["P"]:
                R["SRC"][id(y)] = src
                _cbt_reg(y, p, 0.0, line, None)
        return r
    shp = [y for x in ins for y in _cbt_shapes_in(x)]
    sel = [y for y in shp if id(y) in R["SRC"]]
    srcs = _cbt_uniq([y for y in shp if id(y) not in R["SRC"]] + [R["SRC"][id(y)] for y in sel])
    srcs = [y for y in srcs if not any(y is z for z in rs)]      # a method returning self
    cost = dt + sum(R["COST"].get(id(y), 0.0) for y in srcs)
    flat = _CBT_E
    for x in ins:
        if not _cbt_shapes_in(x):
            flat = flat | _cbt_pp(x)
    T, tp = None, _CBT_E
    if name == "mirror":
        T, tp = _cbt_mirror(ins), flat
    elif len(srcs) == 1 and not sel and len(rs) == 1:
        T = _cbt_move(rs[0], srcs[0])
        tp = flat if T is not None else _CBT_E
    cont = []
    for y in srcs:
        cont += [(i, _cbt_mm(T, M), mp | tp) for (i, M, mp) in _cbt_entries(y)]
    if sel:
        sp = flat
        for y in sel:
            sp = sp | R["P"].get(id(y), _CBT_E)
        R["nodes"].append({"obj": list(sel), "cost": dt, "p": sp, "line": line, "kids": [], "feat": True,
                           "bool": False, "left": None})
        cont.append((len(R["nodes"]) - 1, None, _CBT_E))
    for y in rs:
        _cbt_reg(y, p, cost, line, cont or None)
        if len(srcs) == 1 and id(srcs[0]) in R["LEFT"] and id(y) not in R["LEFT"]:
            R["LEFT"][id(y)] = R["LEFT"][id(srcs[0])]
    return r


class _TRange(list):
    """range(n) of a traced n: the loop's counter remembers n too, so the
    pieces a loop makes light up with the value that counts them."""


def __cbt_call__(_l, _f, /, *a, **k):
    R["depth"] += 1
    t0 = _cbt_time.perf_counter()
    try:
        r = _f(*a, **k)
    finally:
        R["depth"] -= 1
    dt = _cbt_time.perf_counter() - t0
    try:
        return _cbt_after(_l, _f, a, k, r, dt)
    except Exception:
        return r


_CBT_OPS = {"add": _cbt_op.add, "sub": _cbt_op.sub, "and": _cbt_op.and_, "mul": _cbt_op.mul}
_CBT_IOPS = {"add": _cbt_op.iadd, "sub": _cbt_op.isub, "and": _cbt_op.iand, "mul": _cbt_op.imul}


def __cbt_bin__(_l, _o, a, b, _ip=False):
    f = (_CBT_IOPS if _ip else _CBT_OPS)[_o]
    R["depth"] += 1
    t0 = _cbt_time.perf_counter()
    try:
        r = f(a, b)
    finally:
        R["depth"] -= 1
    dt = _cbt_time.perf_counter() - t0
    try:
        if r is None or isinstance(r, (int, float, bool, str, bytes)):
            return r
        if _o in ("add", "sub", "and") and _cbt_is_shape(r):
            ops = [y for x in (a, b) for y in _cbt_shapes_in(x)]
            ops = [y for y in ops if y is not r]
            if ops:
                R["bools"] += 1
                cont = [(_cbt_node(y), None, _CBT_E) for y in ops]
                cost = dt + sum(R["COST"].get(id(y), 0.0) for y in ops)
                _cbt_reg(r, _cbt_pp(a) | _cbt_pp(b), cost, _l, cont)
                R["BOOL"].add(id(r))
                left = _cbt_shapes_in(a)
                if _o in ("sub", "and") and len(left) == 1:
                    R["LEFT"][id(r)] = _cbt_node(left[0])   # what is cut from: the body
                return r
        if _o == "mul" and (_cbt_shapes_in(a) or _cbt_shapes_in(b)):
            return _cbt_after(_l, f, (a, b), {}, r, dt)
        p = _cbt_pp(a) | _cbt_pp(b)
        if p:
            _cbt_reg(r, p, None, _l, None)
    except Exception:
        pass
    return r


def __cbt_param__(v, name):
    R["params"].append(name)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    return _cbt_num(v, frozenset([name]))


def __cbt_done__(loc, outs=()):
    R["out"] = {n: loc[n] for n in ("result",) + tuple(outs) if n in loc}


# ── the end of the run: what to draw, per value ──
def _cbt_walk(budget, maxn=400):
    """[(node, M, mp, rung)] to draw, from the block's outputs down.

    At a boolean the dearer operand is the BODY being built up: it is never
    drawn, it is split again. The other operands are the PIECES: each drawn
    whole when it rebuilds within `budget`, else its parts (the same rule,
    one level down), and a piece with no parts over budget gives its curves.
    A fillet / chamfer's picked edges are a 'feature'."""
    out, seen = [], set()
    tasks = []
    for v in reversed(list((R["out"] or {}).values())):
        for y in reversed(_cbt_shapes_in(v)):
            tasks.append(("split", _cbt_entries(y), None, _CBT_E, R["LEFT"].get(id(y))))
    while tasks and len(out) < maxn:
        kind, what, M, mp, left = tasks.pop()
        if kind == "split":
            ents = [(i, _cbt_mm(M, Mi), mp | mpi) for (i, Mi, mpi) in what]
            if not ents:
                continue
            # the body: what was cut from (left of - and &), else the dearest
            # operand — but only a body that is itself built by booleans; a
            # plain shape on either side is a piece like the others
            body = next((e for e in ents if e[0] == left), None)
            if body is None:
                top = max(ents, key=lambda e: R["nodes"][e[0]]["cost"])
                body = top if R["nodes"][top[0]]["bool"] else None
            for e in reversed(ents):
                tasks.append(("body" if e is body else "piece", e, None, _CBT_E, None))
            continue
        i, Mi, mpi = what
        key = (kind, i, None if Mi is None else tuple(round(float(v), 5) for v in Mi.ravel()))
        if key in seen:
            continue
        seen.add(key)
        n = R["nodes"][i]
        if n["feat"]:
            out.append((i, Mi, mpi, "feature"))
        elif kind == "body" and n["kids"]:
            tasks.append(("split", n["kids"], Mi, mpi, n["left"]))
        elif n["cost"] <= budget or not n["kids"]:
            out.append((i, Mi, mpi, "piece" if n["cost"] <= budget else "curve"))
        else:                                   # a piece over budget: its parts
            for (j, Mj, mpj) in reversed(n["kids"]):
                tasks.append(("piece", (j, _cbt_mm(Mi, Mj), mpi | mpj), None, _CBT_E, None))
    return out


def _cbt_placed(obj, M, curves):
    """The node's geometry in the block's output frame (edges only for curves)."""
    from build123d import Compound
    shapes = obj if isinstance(obj, list) else [obj]
    if curves:
        edges = []
        for s in shapes:
            try:
                edges += list(s.edges())
            except Exception:
                pass
        if not edges:                           # the last rung: points (a 2D fillet's vertices)
            import numpy as np
            from build123d import Vector
            P = np.array([[c.X, c.Y, c.Z] for s in shapes for c in (v.center() for v in s.vertices())], float)
            if M is not None and len(P):
                P = P @ M[:3, :3].T + M[:3, 3]
            return [Vector(*map(float, q)) for q in P]
        shape = Compound(edges)
    else:
        shape = shapes[0] if len(shapes) == 1 else Compound(shapes)
    if M is None:
        return shape
    # a rigid move or a mirror, applied as such: the rotation snapped to the
    # nearest orthogonal matrix (products of inverses drift by ~1e-12, and
    # OCCT refuses anything not orthogonal to its own tolerance)
    import numpy as np
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf
    U, _, Vt = np.linalg.svd(M[:3, :3])
    Rm = U @ Vt
    t = gp_Trsf()
    t.SetValues(*(float(v) for row in np.hstack([Rm, M[:3, 3:4]]) for v in row))
    out = BRepBuilderAPI_Transform(shape.wrapped, t, True).Shape()
    return Compound(out) if not isinstance(shape, Compound) else Compound(out)


def _cbt_finish(budget, path, prefix="cbt:"):
    import json
    walk = _cbt_walk(budget)
    prev, items, errs = {}, [], {}
    for k, (i, M, mp, rung) in enumerate(walk):
        n = R["nodes"][i]
        key = f"{prefix}{k}"
        try:
            prev[key] = _cbt_placed(n["obj"], M, rung in ("curve", "feature"))
        except Exception as e:                  # counted and named: a silent drop hid a whole block once
            errs[type(e).__name__ + ": " + str(e)[:160]] = errs.get(type(e).__name__ + ": " + str(e)[:160], 0) + 1
            continue
        obj = n["obj"]
        items.append({"key": key, "node": i, "rung": rung, "line": n["line"],
                      "cost_ms": round(1000 * n["cost"], 2), "params": sorted(n["p"] | mp),
                      # what the value does to it: makes it (its size, its own parts) or only
                      # carries it somewhere (a Pos / Rot / gira applied to what contains it)
                      "shapes": sorted(n["p"]), "moves": sorted(mp - n["p"]),
                      "kind": type(obj[0] if isinstance(obj, list) and obj else obj).__name__})
    by = {}
    for it in items:
        for p in it["params"]:
            by.setdefault(p, []).append(it["key"])
    declared = list(dict.fromkeys(R["params"]))
    globals()["__previews__"] = prev
    with open(path, "w") as f:
        json.dump({"items": items, "params": by, "declared": declared,
                   "silent": [p for p in declared if p not in by],
                   "stats": {"nodes": len(R["nodes"]), "booleans": R["bools"], "drawn": len(items),
                             "budget_ms": round(1000 * budget), "walked": len(walk), "unplaced": errs, "seconds": round(_cbt_time.perf_counter() - R["t0"], 3)}},
                  f)
