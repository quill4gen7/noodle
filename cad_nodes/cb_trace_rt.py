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
import sys as _cbt_sys
import time as _cbt_time
import types as _cbt_types

_CBT_E = frozenset()
R = {"P": {}, "COST": {}, "LINE": {}, "CONT": {}, "SRC": {}, "NODE": {}, "KEEP": [], "BOOL": set(), "LEFT": {},
     "RCP": {}, "UNSAFE": set(), "BR": {}, "CMP": [], "CMPI": {}, "OVER": set(), "ln": None, "vals": {}, "off": False, "bi": 0, "file": None,
     "nodes": [], "bools": 0, "depth": 0, "out": None, "params": [], "t0": _cbt_time.perf_counter()}


# ── numbers that remember which #@param they came from ──
class _TF(float):
    pass


class _TI(int):
    pass


def _cbt_num(v, p, rcp=None):
    """v as a number that remembers p — and how to make it again (rcp), so a
    replay (_cbt_rp) can recompute it for another value of a param."""
    if not p or isinstance(v, bool):
        return v
    if isinstance(v, float):
        r = _TF(v)
    elif isinstance(v, int):
        r = _TI(v)
    else:
        return v
    r._p = p
    r._r = rcp
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


def _cbt_note(r, p, rcp=None):
    """A result of arithmetic: a number keeps p itself, anything else by id."""
    if isinstance(r, (int, float)) and not isinstance(r, bool):
        return _cbt_num(r, p, rcp)
    if p and r is not None and not isinstance(r, (bool, str, bytes)):
        _cbt_reg(r, p, None, None, None)
        _cbt_rcp(r, rcp)
    return r


def _cbt_arith(f):
    def fwd(a, b):
        return _cbt_note(f(_cbt_plain(a), _cbt_plain(b)), _cbt_pp(a) | _cbt_pp(b), ("op", f, a, b))

    def rev(a, b):
        return _cbt_note(f(_cbt_plain(b), _cbt_plain(a)), _cbt_pp(a) | _cbt_pp(b), ("op", f, b, a))
    return fwd, rev


for _n in ("add", "sub", "mul", "truediv", "floordiv", "mod", "pow"):
    _fw, _rv = _cbt_arith(getattr(_cbt_op, _n))
    for _c in (_TF, _TI):
        setattr(_c, f"__{_n}__", _fw)
        setattr(_c, f"__r{_n}__", _rv)
for _n in ("neg", "pos", "abs"):
    def _un(f):
        return lambda a: _cbt_num(f(_cbt_plain(a)), a._p, ("op1", f, a))
    for _c in (_TF, _TI):
        setattr(_c, f"__{_n}__", _un(getattr(_cbt_op, _n)))


def _cbt_branch(ps, frame, rec=None):
    """A value decided something in the block's OWN code (an if, a comparison,
    an index, a count). A replay keeps the branch the trace took, so each such
    decision is recorded with its outcome (`rec`): the replay re-decides the
    ones its values reach and calls itself approximate only where one FLIPS.
    Comparisons inside min/max/sorted, or inside a library, decide nothing of
    the block's shape."""
    if R["off"] or R["bi"] or not ps:
        return
    try:
        if frame is not None and R["file"] is not None and frame.f_code.co_filename != R["file"]:
            return
    except Exception:
        pass
    _cbt_decided(ps, rec)


def _cbt_decided(ps, rec):
    ln = R["ln"]                                # the block's own line (the last call seen)
    R["UNSAFE"] |= ps
    for q in ps:
        at = R["BR"].setdefault(q, [])
        if ln is not None and ln not in at and len(at) < 6:
            at.append(ln)
    if rec is None:
        return
    if len(R["CMP"]) >= 50000:                  # too many to re-decide: approximate outright
        R["OVER"] |= ps
        return
    R["CMP"].append(rec + (ln,))
    i = len(R["CMP"]) - 1
    for q in ps:
        R["CMPI"].setdefault(q, []).append(i)


def _cbt_cmp(f):
    def g(a, b):
        r = f(_cbt_plain(a), _cbt_plain(b))
        _cbt_branch(_cbt_pp(a) | _cbt_pp(b), _cbt_sys._getframe(1), ("cmp", f, a, b, r))
        return r
    return g


for _n in ("lt", "le", "gt", "ge", "eq", "ne"):
    for _c in (_TF, _TI):
        setattr(_c, f"__{_n}__", _cbt_cmp(getattr(_cbt_op, _n)))
_TF.__hash__ = float.__hash__
_TI.__hash__ = int.__hash__
# a number is immutable: copying it is itself (a block's deepcopy must not
# drag its recipe — closure cells — along), pickling it is the plain number
for _c, _b in ((_TF, float), (_TI, int)):
    _c.__copy__ = lambda self: self
    _c.__deepcopy__ = lambda self, memo: self
    _c.__reduce__ = (lambda b: lambda self: (b, (b(self),)))(_b)


def _cbt_bool(a):
    r = _cbt_plain(a) != 0
    _cbt_branch(a._p, _cbt_sys._getframe(1), ("bool", a, r))
    return r


def _cbt_index(a):
    r = int.__index__(a)
    _cbt_branch(a._p, _cbt_sys._getframe(1), ("idx", a, r))
    return r


_TF.__bool__ = _cbt_bool
_TI.__bool__ = _cbt_bool
_TI.__index__ = _cbt_index


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


def _cbt_rcp(x, rcp):
    """How x was made, for a replay: every call that handed it back, the
    innermost first (make_face(curve)), then the user functions it came out of
    (profilo(e)) — when the inner path is blind to a value, an outer one sees it."""
    if rcp is None or R["off"]:
        return
    lst = R["RCP"].get(id(x))
    if lst is None:
        R["RCP"][id(x)] = [rcp]
        R["KEEP"].append(x)       # alive while the trace lasts, or its id goes to another object
    elif len(lst) < 4 and not any(r is rcp for r in lst):
        lst.append(rcp)


def _cbt_reg(x, p, cost, line, cont):
    """Remember x (first sight wins its cost and contents; params accumulate)."""
    if R["off"]:
        return
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
    return c if c else [(_cbt_node(x), (), _CBT_E)]


# A placement is kept as its STEPS, outermost first — (T, how, params): the
# matrix the trace measured, how to measure it again, and the params it
# depends on — so a replay can recompute just the steps a value moves.
def _cbt_mm(A, B):
    return tuple(A or ()) + tuple(B or ())


def _cbt_M(S):
    """The steps' matrix (None = identity)."""
    M = None
    for (T, _how, _tp) in S or ():
        M = T if M is None else M @ T
    return M


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


def _cbt_snap(f, a=(), k=None):
    """The closure cells (or module globals) a user function reads, as they
    are NOW: a replay puts them back, so a helper called inside a loop sees
    that loop's values again — and the replayed values of the params. Also
    for the functions handed to the call (a filter_by's lambda)."""
    out = []
    for g in [f] + [x for x in list(a) + list((k or {}).values()) if isinstance(x, _cbt_types.FunctionType)]:
        out += _cbt_snap1(g)
    return tuple(out) or None


def _cbt_snap1(f):
    fn = getattr(f, "__func__", f)
    if not isinstance(fn, _cbt_types.FunctionType):
        return []
    out = []
    for c in fn.__closure__ or ():
        try:
            v = c.cell_contents
        except ValueError:
            continue
        if not (callable(v) or isinstance(v, _cbt_types.ModuleType) or _cbt_shapes_in(v)):
            out.append((c, v, None))
    g = fn.__globals__
    for n in fn.__code__.co_names:
        v = g.get(n)
        if isinstance(v, (_TF, _TI)):
            out.append((n, v, g))
    return out


def _cbt_after(line, f, a, k, r, dt, rf=None, snap=None):
    s = getattr(f, "__self__", None)
    if isinstance(s, (_cbt_types.ModuleType, type)):
        s = None
    ins = ([s] if s is not None else []) + list(a) + list(k.values())
    p = _CBT_E
    for x in ins:
        p = p | _cbt_pp(x)
    if snap:
        for (_c, v, _g) in snap:
            p = p | _cbt_pp(v)
    if r is None or isinstance(r, (bool, str, bytes)):
        return r
    rcp = ("call", rf or f, a, k, snap)
    if f is range and p and len(r) <= 100000:
        _cbt_decided(p, ("count", rcp, len(r)))   # a count: a replay keeps the trace's
        return _TRange(_cbt_num(i, p, ("item", rcp, j)) for j, i in enumerate(r))
    if isinstance(r, (int, float)):
        return _cbt_num(r, p | _cbt_pp(r), rcp) if p else r
    if p and any(r is x for x in ins):        # a method that changed its own object
        _cbt_decided(p, None)
        R["OVER"] |= p
    rs = _cbt_shapes_in(r)
    if not rs:
        if p:
            _cbt_reg(r, p, None, line, None)   # a Vector, a Location, a list of points…
            _cbt_rcp(r, rcp)
        return r
    if not any(r is x for x in ins):
        _cbt_rcp(r, rcp)
        if r is not rs[0]:                     # a list of shapes: each is an item of it
            for j, y in enumerate(r if isinstance(r, (list, tuple)) else ()):
                if _cbt_is_shape(y):
                    _cbt_rcp(y, ("item", rcp, j))
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
    step, tp = (), _CBT_E
    if name == "mirror":
        tp = flat
        step = ((_cbt_mirror(ins), ("mirror", tuple(ins)), tp),)
    elif len(srcs) == 1 and not sel and len(rs) == 1:
        T = _cbt_move(rs[0], srcs[0])
        if T is not None:
            tp = flat
            step = ((T, ("move", rs[0], srcs[0]), tp),)
    cont = []
    for y in srcs:
        cont += [(i, _cbt_mm(step, S), mp | tp) for (i, S, mp) in _cbt_entries(y)]
    if sel:
        sp = flat
        for y in sel:
            sp = sp | R["P"].get(id(y), _CBT_E)
        # replaying the picks rebuilds what they were picked from, not the fillet
        rc = sum(R["COST"].get(id(R["SRC"][id(y)]), 0.0) for y in _cbt_uniq(sel[:1]))
        R["nodes"].append({"obj": list(sel), "cost": dt, "p": sp, "line": line, "kids": [], "feat": True,
                           "bool": False, "left": None, "rcost": rc})
        cont.append((len(R["nodes"]) - 1, (), _CBT_E))
    for y in rs:
        _cbt_reg(y, p, cost, line, cont or None)
        if len(srcs) == 1 and id(srcs[0]) in R["LEFT"] and id(y) not in R["LEFT"]:
            R["LEFT"][id(y)] = R["LEFT"][id(srcs[0])]
    return r


class _TRange(list):
    """range(n) of a traced n: the loop's counter remembers n too, so the
    pieces a loop makes light up with the value that counts them."""


# builtins whose comparisons choose a value, not a branch of the block
_CBT_PURE = {min, max, sorted, sum, abs, round}


def __cbt_call__(_l, _f, /, *a, **k):
    if R["off"]:                               # a replay: just the call…
        C = R.get("ctx")
        if C is None:
            return _f(*a, **k)
        # …but a helper called from inside a replayed one (y_i calling y_o)
        # reads its closure too: the replayed values of what it closes over
        return _cbt_with(_cbt_snap(_f, a, k), C, lambda: _f(*a, **k))
    try:
        snap = _cbt_snap(_f, a, k)
    except Exception:
        snap = None
    R["ln"] = _l
    pure = _f in _CBT_PURE if isinstance(_f, _cbt_types.BuiltinFunctionType) else False
    R["depth"] += 1
    R["bi"] += pure
    t0 = _cbt_time.perf_counter()
    try:
        r = _f(*a, **k)
    finally:
        R["depth"] -= 1
        R["bi"] -= pure
    dt = _cbt_time.perf_counter() - t0
    try:
        return _cbt_after(_l, _f, a, k, r, dt, snap=snap)
    except Exception:
        return r


_CBT_OPS = {"add": _cbt_op.add, "sub": _cbt_op.sub, "and": _cbt_op.and_, "mul": _cbt_op.mul}
_CBT_IOPS = {"add": _cbt_op.iadd, "sub": _cbt_op.isub, "and": _cbt_op.iand, "mul": _cbt_op.imul}


def __cbt_bin__(_l, _o, a, b, _ip=False):
    f = (_CBT_IOPS if _ip else _CBT_OPS)[_o]
    if R["off"]:
        return f(a, b)
    R["ln"] = _l
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
                _cbt_rcp(r, ("call", _CBT_OPS[_o], (a, b), {}, None))
                R["BOOL"].add(id(r))
                left = _cbt_shapes_in(a)
                if _o in ("sub", "and") and len(left) == 1:
                    R["LEFT"][id(r)] = _cbt_node(left[0])   # what is cut from: the body
                return r
        if _o == "mul" and (_cbt_shapes_in(a) or _cbt_shapes_in(b)):
            return _cbt_after(_l, f, (a, b), {}, r, dt, rf=_CBT_OPS[_o])
        p = _cbt_pp(a) | _cbt_pp(b)
        if p and r is not a:
            _cbt_reg(r, p, None, _l, None)
            _cbt_rcp(r, ("call", _CBT_OPS[_o], (a, b), {}, None))
    except Exception:
        pass
    return r


def __cbt_ln__(line):
    """The block line a decision is taken on (the rewrite puts it before
    every comparison and if/while test)."""
    if not R["off"]:
        R["ln"] = line


def __cbt_param__(v, name):
    R["params"].append(name)
    if R["file"] is None:                      # the block's own code: where branches count
        try:
            R["file"] = _cbt_sys._getframe(1).f_code.co_filename
        except Exception:
            pass
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return v
    R["vals"][name] = v
    return _cbt_num(v, frozenset([name]), ("param", name))


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
            tasks.append(("split", _cbt_entries(y), (), _CBT_E, R["LEFT"].get(id(y))))
    while tasks and len(out) < maxn:
        kind, what, M, mp, left = tasks.pop()
        if kind == "split":
            ents = [(i, _cbt_mm(M, Si), mp | mpi) for (i, Si, mpi) in what]
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
                tasks.append(("body" if e is body else "piece", e, (), _CBT_E, None))
            continue
        i, Mi, mpi = what
        Mn = _cbt_M(Mi)
        key = (kind, i, None if Mn is None else tuple(round(float(v), 5) for v in Mn.ravel()))
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
                tasks.append(("piece", (j, _cbt_mm(Mi, Mj), mpi | mpj), (), _CBT_E, None))
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


# ── replay: the lit pieces again, for other values (step 3 — a slider dragging) ──
class _CbtCtx:
    """One replay: the changed params, a memo, and whether shapes are rebuilt
    (full) or kept as they were (frame: only where things are PUT is wanted —
    a move measured on the original shape is the same move)."""

    def __init__(self, V, full):
        self.V, self.keys, self.full, self.memo = dict(V), frozenset(V), full, {}
        self.hidden = 0       # recipes the value reached by a path the trace never saw


def _cbt_dep(x, C):
    return bool(_cbt_pp(x) & C.keys)


def _cbt_rp(x, C):
    """x as it would be with the params in C.V — recomputed from its recipe
    when it depends on one of them, else x itself."""
    t = type(x)
    if t is _TF or t is _TI:
        if not (x._p & C.keys):
            return x
        k = id(x)
        if k in C.memo:
            return C.memo[k][1]
        rc = getattr(x, "_r", None)
        if rc is None:
            v = _cbt_plain(x)
        elif rc[0] == "param":
            v = C.V.get(rc[1], _cbt_plain(x))
        elif rc[0] == "op":
            v = rc[1](_cbt_plain(_cbt_rp(rc[2], C)), _cbt_plain(_cbt_rp(rc[3], C)))
        elif rc[0] == "op1":
            v = rc[1](_cbt_plain(_cbt_rp(rc[2], C)))
        else:
            v = _cbt_plain(_cbt_eval(rc, C, x))
        C.memo[k] = (x, v)
        return v
    if x is None or t in (bool, int, float, str, bytes):
        return x
    if not C.full and _cbt_is_shape(x):
        return x
    k = id(x)
    if k in C.memo:
        return C.memo[k][1]
    rcs = R["RCP"].get(k)
    # a container's own params are empty (its items carry them: the
    # ShapeList of a .vertices()), hence _cbt_dep, which looks inside
    if rcs and ((R["P"].get(k, _CBT_E) & C.keys) or _cbt_dep(x, C)):
        v = _cbt_redo(x, rcs, C)
    elif isinstance(x, (list, tuple)) and _cbt_dep(x, C):
        v = (tuple if isinstance(x, tuple) else list)(_cbt_rp(y, C) for y in x)
        try:
            v = v if t in (list, tuple, _TRange) else t(v)     # a ShapeList stays one
        except Exception:
            pass
    elif t is dict and _cbt_dep(x, C):
        v = {n: _cbt_rp(y, C) for n, y in x.items()}
    else:
        return x
    C.memo[k] = (x, v)                          # x kept alive: its id stays its own
    return v


def _cbt_redo(x, rcs, C):
    """x again, by the first of its recipes the changed values reach."""
    for rc in rcs:
        v = _cbt_eval(rc, C, x)
        if v is not x:
            return v
    return x


def _cbt_eval(rc, C, x=None):
    """A recipe, run with replayed inputs. When none of them changed, the
    value reached x by a path the trace never saw (a curve drawn in builder
    mode, a number read back from a shape): x itself, counted in C.hidden."""
    if rc[0] == "item":
        r = _cbt_eval(rc[1], C, None)
        if r is None:
            return x
        return r[rc[2]]
    _k, f, a, k, snap = rc
    moved = False
    s = getattr(f, "__self__", None)
    if s is not None and not isinstance(s, (_cbt_types.ModuleType, type)):
        s2 = _cbt_rp(s, C)
        if s2 is not s:
            f, moved = getattr(s2, f.__name__), True
    a2 = [_cbt_rp(y, C) for y in a]
    k2 = {n: _cbt_rp(y, C) for n, y in k.items()}
    sv = [(c, v, g, _cbt_rp(v, C)) for (c, v, g) in snap or ()]
    moved = moved or any(y2 is not y for y, y2 in zip(a, a2)) or any(k2[n] is not k[n] for n in k) \
        or any(v2 is not v for (_c, v, _g, v2) in sv)
    if not moved:
        C.hidden += 1
        return x
    return _cbt_with(sv, C, lambda: f(*a2, **k2), done=True)


def _cbt_with(sv, C, call, done=False):
    """call() with closure cells / globals set to their replayed values."""
    if not done:
        sv = [(c, v, g, _cbt_rp(v, C)) for (c, v, g) in sv or ()]
        sv = [t for t in sv if t[3] is not t[1]]
    put = []
    try:
        for (c, v, g, v2) in sv:
            if isinstance(c, str):
                put.append((g, c, g.get(c)))
                g[c] = v2
            else:
                try:
                    old = c.cell_contents
                except ValueError:
                    continue
                put.append((c, None, old))
                c.cell_contents = v2
        return call()
    finally:
        for (c, n, old) in reversed(put):
            if n is None:
                c.cell_contents = old
            else:
                c[n] = old


def _cbt_flips(C, budget_s=0.25):
    """The recorded decisions the changed values reach, decided again:
    {param: [lines]} where one comes out differently (the replay then took
    the trace's branch, not the one the real run will take).

    C must be a FRAME context (shapes kept as they are): a decision on a
    number read back from a body (`len(body.solids())` in a cleanup loop)
    would otherwise rebuild the body for every comparison — on creepyfinger
    that filled RAM and swap and froze the machine (2026-10-21). And it has a
    time budget: what is left unchecked counts as a flip (approximate)."""
    out = {}
    t_end = _cbt_time.perf_counter() + budget_s
    for q in C.keys & R["OVER"]:
        out.setdefault(q, [])
    idx = set()
    for q in C.keys:
        idx.update(R["CMPI"].get(q, ()))
    for i in sorted(idx):
        rec = R["CMP"][i]
        kind, ln = rec[0], rec[-1]
        if _cbt_time.perf_counter() > t_end:
            for q in C.keys & set(R["CMPI"]):
                out.setdefault(q, [])
            break
        try:
            if kind == "cmp":
                now = rec[1](_cbt_plain(_cbt_rp(rec[2], C)), _cbt_plain(_cbt_rp(rec[3], C)))
            elif kind == "bool":
                now = _cbt_plain(_cbt_rp(rec[1], C)) != 0
            elif kind == "idx":
                now = int(_cbt_plain(_cbt_rp(rec[1], C)))
            else:                               # count: len(range(…)) again
                _k, f, a, k, _s = rec[1]
                now = len(f(*[_cbt_plain(_cbt_rp(y, C)) for y in a]))
        except Exception:
            now = None
        if now != rec[-2]:
            ops = rec[1:-2] if kind == "cmp" else ((rec[1][2],) if kind == "count" else (rec[1],))
            ps = _CBT_E
            for y in ops:
                ps = ps | _cbt_pp(y)
            for q in ps & C.keys:
                at = out.setdefault(q, [])
                if ln is not None and ln not in at and len(at) < 6:
                    at.append(ln)
    return out


def _cbt_steps_at(S, C):
    """The placement matrix of steps S with the params in C.V (frame mode)."""
    import numpy as np
    M = None
    for (T, how, tp) in S or ():
        if how is not None and (tp & C.keys):
            if how[0] == "mirror":
                T = _cbt_mirror([_cbt_rp(x, C) for x in how[1]])
            else:
                r, s = how[1], how[2]
                rcs = R["RCP"].get(id(r))
                if rcs:
                    T = _cbt_loc_matrix(_cbt_redo(r, rcs, C)) @ np.linalg.inv(_cbt_loc_matrix(s))
        M = T if M is None else M @ T
    return M


def _cbt_rel(M, M0):
    """M as a move from where the trace drew it (M0), row-major 16 floats."""
    import numpy as np
    A = np.eye(4) if M is None else M
    B = np.eye(4) if M0 is None else M0
    return [round(float(v), 9) for v in (A @ np.linalg.inv(B)).ravel()]


def _cbt_replay(items, V, focus=None, probe=(), heavy_s=1.5, wall_s=4.0):
    """The pieces `focus` lights, with the params in V (name → value). A piece
    the changed values SHAPE is rebuilt (its recipe, nothing downstream: no
    boolean on the body ever runs); a piece they only MOVE comes back as a
    matrix from where the trace drew it. `probe`: more values of `focus`, for
    which only those matrices are measured — the editor's derivative, which
    carries a moving piece at 60 fps between two answers."""
    t0 = _cbt_time.perf_counter()
    V = {n: v for n, v in V.items() if n in R["vals"]}
    lit = [it for it in items if (focus in it["params"] if focus else it["params"] & set(V))]
    keys = frozenset(V)
    # a feature's edges are picks of the shape they were picked from: rebuilt
    # as "item j of that shape's .edges()" (a count change can pick others)
    shaped = [it for it in lit if R["nodes"][it["node"]]["p"] & keys]
    # over budget the cheapest are rebuilt first; the rest come back moved
    # but in their old shape, flagged `old` (exact on release)
    cost = lambda it: R["nodes"][it["node"]].get("rcost", R["nodes"][it["node"]]["cost"])
    rebuild, acc = set(), 0.0
    for it in sorted(shaped, key=cost):
        acc += cost(it)
        if acc > heavy_s:
            break
        rebuild.add(it["key"])
    heavy = len(rebuild) < len(shaped)
    prev, out, errs, frames = {}, [], {}, {}
    old_lim = _cbt_sys.getrecursionlimit()
    R["off"] = True
    _cbt_sys.setrecursionlimit(max(old_lim, 20000))
    try:
        CF, CS = _CbtCtx(V, True), _CbtCtx(V, False)
        late = False
        for it in lit:
            R["ctx"] = CF
            n = R["nodes"][it["node"]]
            # a recipe can cost more than the trace measured (an outer helper
            # re-run): past the wall clock the rest come back moved, old shape
            late = late or _cbt_time.perf_counter() - t0 > wall_s
            try:
                R["ctx"] = CS
                M = _cbt_steps_at(it["S"], CS)
                R["ctx"] = CF
                if it["key"] in rebuild and not late:
                    h0 = CF.hidden
                    obj = _cbt_rp(n["obj"], CF)
                    if obj is n["obj"] and CF.hidden > h0:
                        # the value reaches it only through what the trace cannot
                        # see: moved, in its old shape — exact on release
                        out.append({"key": it["key"], "D": _cbt_rel(M, it["M0"]), "old": True})
                        continue
                    prev[it["key"]] = _cbt_placed(obj, M, it["rung"] in ("curve", "feature"))
                    out.append({"key": it["key"], "mesh": True})
                else:
                    out.append({"key": it["key"], "D": _cbt_rel(M, it["M0"]),
                                **({"old": True} if any(it is x for x in shaped) else {})})
            except Exception as e:
                m = type(e).__name__ + ": " + str(e)[:160]
                errs[m] = errs.get(m, 0) + 1
        for pv in probe or ():
            C = _CbtCtx({**V, focus: pv}, False)
            R["ctx"] = C
            for it in lit:
                if focus in it["moves_by"]:
                    try:
                        frames.setdefault(it["key"], []).append(_cbt_rel(_cbt_steps_at(it["S"], C), it["M0"]))
                    except Exception as e:
                        m = type(e).__name__ + ": " + str(e)[:160]
                        errs[m] = errs.get(m, 0) + 1
        CD = _CbtCtx(V, False)
        R["ctx"] = CD
        flips = _cbt_flips(CD)
    finally:
        R["off"] = False
        R["ctx"] = None
        _cbt_sys.setrecursionlimit(old_lim)
    return prev, {"items": out, "frames": frames, "heavy": heavy or late, "errors": errs,
                  # params whose replay took a branch the real run will not take
                  "approx": sorted(flips), "flips": flips,
                  "seconds": round(_cbt_time.perf_counter() - t0, 4)}


def _cbt_finish(budget, path, prefix="cbt:", tid=None):
    import json
    walk = _cbt_walk(budget)
    prev, items, errs, keep = {}, [], {}, []
    for k, (i, S, mp, rung) in enumerate(walk):
        n = R["nodes"][i]
        key = f"{prefix}{k}"
        M = _cbt_M(S)
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
        moves_by = set()
        for (_T, _how, tp) in S:
            moves_by |= tp
        keep.append({"key": key, "node": i, "S": S, "M0": M, "rung": rung,
                     "params": n["p"] | mp, "moves_by": moves_by})
    by = {}
    for it in items:
        for p in it["params"]:
            by.setdefault(p, []).append(it["key"])
    declared = list(dict.fromkeys(R["params"]))
    globals()["__previews__"] = prev
    live = False
    if tid:
        # parked in the worker process for the replays that follow (a module,
        # not __MEMO__: the memo store is an LRU and would evict it). Only the
        # newest trace is kept; a cold run's process dies with it.
        try:
            import types
            mod = _cbt_sys.modules.get("_noodle_cbt")
            if mod is None:
                mod = _cbt_sys.modules["_noodle_cbt"] = types.ModuleType("_noodle_cbt")
                mod.traces = {}
            while len(mod.traces) >= 1:          # one: a creepyfinger trace holds ~2 GB
                mod.traces.pop(next(iter(mod.traces)))
            # the wall clock follows the budget: a drag asks for a small one
            mod.traces[tid] = {"R": R, "replay": lambda V, focus=None, probe=(), heavy_s=1.5:
                               _cbt_replay(keep, V, focus, probe, heavy_s, max(0.6, 2.5 * heavy_s))}
            live = True
        except Exception:
            live = False
    # ⚡ per value, what a drag of it costs: the pieces it SHAPES are rebuilt
    # (their recipes, measured by the trace), the ones it only moves are matrices
    fast = {}
    if live:
        for p, ks in by.items():
            sh = [it for it in items if p in it["shapes"]]
            ms = sum(R["nodes"][it["node"]].get("rcost", R["nodes"][it["node"]]["cost"]) for it in sh)
            fast[p] = {"rebuild_ms": round(1000 * ms, 1), "rebuilt": len(sh), "moved": len(ks) - len(sh)}
    with open(path, "w") as f:
        json.dump({"items": items, "params": by, "declared": declared, "trace": tid if live else None,
                   "fast": fast,
                   "values": {n: float(v) for n, v in R["vals"].items()},
                   "approx": sorted(R["UNSAFE"]),
                   "branches": {q: R["BR"].get(q, []) for q in sorted(R["UNSAFE"])},
                   "silent": [p for p in declared if p not in by],
                   "stats": {"nodes": len(R["nodes"]), "booleans": R["bools"], "drawn": len(items),
                             "budget_ms": round(1000 * budget), "walked": len(walk), "unplaced": errs, "seconds": round(_cbt_time.perf_counter() - R["t0"], 3)}},
                  f)
