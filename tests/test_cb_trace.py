"""🔦 Traccia (cad_nodes/cb_trace.py + cb_trace_rt.py): which pieces of a
CodeBlock's result each #@param moves. Pure Python: the rewrite is plain
`ast`, and the runtime is driven with stand-in shapes (no build123d) — the
real thing is exercised in the worker by the trace route."""
import importlib.util
import math
from pathlib import Path

import pytest

from cad_nodes import cb_trace
from cad_nodes.transpiler import check_codeblock, parse_codeblock_outputs, parse_codeblock_params

ROOT = Path(__file__).resolve().parent.parent
RT = ROOT / "cad_nodes" / "cb_trace_rt.py"


def fresh_rt():
    """A new copy of the runtime (its registry is module state, like in a run)."""
    spec = importlib.util.spec_from_file_location("cbt_rt_test", RT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ── stand-in shapes: enough of the build123d surface the runtime looks at ──
class _W:
    def IsPartner(self, other):
        return False


class Shape:
    def __init__(self, name, cost=0.0):
        self.name, self.wrapped, self.location = name, _W(), None

    def faces(self):
        return []

    def edges(self):
        return [Shape(self.name + ".e")]

    def __add__(self, o):
        return Shape(f"({self.name}+{getattr(o, 'name', o)})")

    def __sub__(self, o):
        return Shape(f"({self.name}-{getattr(o, 'name', o)})")


class Loc:
    def __init__(self, *v):
        self.v = v

    def __mul__(self, s):
        return Shape(f"moved({s.name})")


def Box(a, b, c):
    return Shape(f"Box({a},{b},{c})")


def Cyl(r, h):
    return Shape(f"Cyl({r},{h})")


def fillet(edges, r):
    return Shape("filleted")


TINY = '''a = 3.0   #@param float min=1 max=10
b = 2.0   #@param float min=1 max=10
c = 4.0   #@param float min=1 max=10
n = 2     #@param int min=1 max=5
import math
#@out extra: solid
def blocco(x0, x1, y0, y1, z0, z1):
    return Pos((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2) * Box(x1 - x0, y1 - y0, z1 - z0)
base = Box(20, 20, 2)
x = Pos(-5, 0, 2) * Box(a, a, a)
y = Pos(c, 0, 2) * Cyl(b * math.cos(0.0), 5)
z = blocco(c, c + 3, -8, -5, 0, 4)
holes = []
for i in range(n):
    holes += [Pos(i, 0, 0) * Cyl(1, 1)]
body = base + x - y + z
for h in holes:
    body -= h
extra = Box(1, 1, 1)
result = body
'''


def run(code, rt=None):
    rt = rt or fresh_rt()
    ns = dict(vars(rt))
    ns.update(Pos=Loc, Box=Box, Cyl=Cyl, fillet=fillet)
    exec(compile(cb_trace.instrument(code), "<traced>", "exec"), ns)
    return rt, ns


def walk_params(rt, budget=0.1):
    """param → [(piece name, shapes?)] as the editor would light them."""
    out = {}
    for (i, _M, mp, _rung) in rt._cbt_walk(budget):
        n = rt.R["nodes"][i]
        name = n["obj"].name if hasattr(n["obj"], "name") else str(len(n["obj"]))
        for p in n["p"] | mp:
            out.setdefault(p, []).append((name, p in n["p"]))
    return out


# ── the rewrite ──
def test_the_traced_copy_compiles_as_a_block_and_keeps_its_declarations():
    traced = cb_trace.instrument(TINY)
    assert check_codeblock(traced) is None
    # same #@params (they become the function's arguments) and the same #@out
    assert [d["name"] for d in parse_codeblock_params(traced)] == ["a", "b", "c", "n"]
    assert [o["name"] for o in parse_codeblock_outputs(traced)] == ["extra"]
    # the numeric params are re-bound to numbers that remember them; calls and
    # + - & * go through the recorder, with the ORIGINAL line numbers
    assert "a = __cbt_param__(a, 'a')" in traced and "n = __cbt_param__(n, 'n')" in traced
    assert "__cbt_call__(9, Box, 20, 20, 2)" in traced
    assert "__cbt_bin__(18, 'sub', body, h, True)" in traced
    assert traced.rstrip().endswith("__cbt_done__(locals(), ('extra',))")


def test_frame_sensitive_builtins_are_not_wrapped():
    traced = cb_trace.instrument("x = locals()\ny = isinstance(1, int)\nresult = None\n")
    assert "__cbt_call__" not in traced


def test_a_syntax_error_is_the_blocks_own():
    with pytest.raises(SyntaxError):
        cb_trace.instrument("result = (\n")


# ── numbers that remember ──
def test_numbers_remember_their_params_through_arithmetic_and_math():
    rt = fresh_rt()
    a = rt.__cbt_param__(3.0, "a")
    b = rt.__cbt_param__(2, "b")
    v = rt.__cbt_call__(1, math.cos, a * 2 - b / 4)
    assert isinstance(v, float) and v._p == {"a", "b"}
    assert (-a)._p == {"a"} and abs(b - 5)._p == {"b"}
    assert rt.__cbt_call__(1, round, a)._p == {"a"}
    assert float(a) == 3.0 and int(b) == 2 and a == 3.0 and list(range(b)) == [0, 1]
    assert rt.__cbt_param__(True, "flag") is True           # bools stay bools


def test_plus_equals_on_a_list_keeps_the_same_list():
    rt = fresh_rt()
    xs = []
    ys = rt.__cbt_bin__(1, "add", xs, [1], True)
    assert ys is xs and xs == [1]


# ── the piece tree, walked as the editor lights it ──
def test_each_value_lights_the_pieces_it_makes():
    rt, ns = run(TINY)
    by = walk_params(rt)
    names = {p: sorted(n for n, _ in v) for p, v in by.items()}
    # the body is never a piece: it is split down to what went into it
    assert names["a"] == ["moved(Box(3.0,3.0,3.0))"]
    assert names["b"] == ["moved(Cyl(2.0,5))"]
    assert names["c"] == ["moved(Box(3.0,3,4))", "moved(Cyl(2.0,5))"]
    assert names["n"] == ["moved(Cyl(1,1))", "moved(Cyl(1,1))"]     # a count lights what it counts
    assert all(shapes for v in by.values() for _, shapes in v)
    assert rt.R["bools"] == 5 and rt.R["params"] == ["a", "b", "c", "n"]
    assert set(rt.R["out"]) == {"result", "extra"}


def test_a_piece_over_budget_steps_down_to_its_parts():
    code = '''a = 1.0  #@param float
p = Box(a, 1, 1) - Cyl(1, 1)
result = Box(9, 9, 9) - p
'''
    rt, _ = run(code)
    whole = [rt.R["nodes"][i]["obj"].name for (i, M, mp, r) in rt._cbt_walk(10.0)]
    assert "(Box(1.0,1,1)-Cyl(1,1))" in whole                      # cheap: the piece as it entered
    # pretend the piece is slow: it is drawn as its parts instead
    for n in rt.R["nodes"]:
        if n["obj"].name == "(Box(1.0,1,1)-Cyl(1,1))":
            n["cost"] = 1.0
    parts = [rt.R["nodes"][i]["obj"].name for (i, M, mp, r) in rt._cbt_walk(0.1)]
    assert "Box(1.0,1,1)" in parts and "Cyl(1,1)" in parts and "(Box(1.0,1,1)-Cyl(1,1))" not in parts


def test_a_fillet_on_picked_edges_is_a_feature_lit_by_its_radius():
    code = '''r = 2.0  #@param float
body = Box(5, 5, 5) + Box(1, 1, 9)
body = fillet(body.edges(), r)
result = body - Cyl(1, 1)
'''
    rt, _ = run(code)
    feats = [(rt.R["nodes"][i], mp) for (i, M, mp, rung) in rt._cbt_walk(0.1) if rung == "feature"]
    assert len(feats) == 1 and feats[0][0]["p"] == {"r"}
    # the boxes the fillet was made on are still pieces of the result
    names = [rt.R["nodes"][i]["obj"].name for (i, M, mp, rung) in rt._cbt_walk(0.1) if rung == "piece"]
    assert "Box(5,5,5)" in names and "Box(1,1,9)" in names


def test_a_feature_dearer_than_its_body_does_not_break_the_walk():
    # the fillet costs more than the box it rounds: the split must still read it
    rt, _ = run("r = 1.0  #@param float\nresult = fillet(Box(1, 1, 1).edges(), r)\n")
    feat = next(n for n in rt.R["nodes"] if n["feat"])
    feat["cost"] = 5.0
    assert [r for (*_, r) in rt._cbt_walk(0.1)].count("feature") == 1


def test_bookkeeping_never_breaks_the_users_code():
    rt = fresh_rt()

    def boom(*a):
        raise ValueError("the user's own error")
    with pytest.raises(ValueError, match="own error"):
        rt.__cbt_call__(1, boom, 1)
    rt.R["P"] = None                            # a broken registry: the call still answers
    assert rt.__cbt_call__(1, max, 1, 2) == 2


# ── the surfaces ──
def test_route_api_and_mcp_are_one_operation():
    server = (ROOT / "server.py").read_text()
    mcp = (ROOT / "mcp_server.py").read_text()
    assert '@app.post("/api/graph/{name}/codeblock/{node_id}/trace")' in server
    assert "off_loop(api.codeblock_trace" in server
    assert "api.codeblock_trace(store, gid, node)" in mcp


def test_the_editor_lights_on_hover_and_never_runs():
    html = (ROOT / "webui" / "nodes.html").read_text()
    i = html.index("// ── 🔦 Traccia")
    blk = html[i:html.index("window.traceCodeblock = traceCodeblock;")]
    assert "/trace`" in blk and "cbtHover" in html and "'🔦 Traccia valori'" in html
    # hovering and letting go draw only: no run, no Live, no save in the hover path
    hover = blk[blk.index("function cbtShow"):blk.index("function cbtCheckStale")]
    assert "runGraph" not in hover and "scheduleLive" not in hover and "persistToServer" not in hover


# ── step 3: replay — the lit pieces again, for another value, while dragging ──
class Pos2:
    """A Pos that writes its numbers into the moved shape's name."""
    def __init__(self, *v):
        self.v = tuple(float(x) for x in v)

    def __mul__(self, s):
        return Shape(f"at{self.v}({s.name})")


SUBS = []


class Body(Shape):
    def __sub__(self, o):
        SUBS.append(o)
        return Body(f"({self.name}-{o.name})")


def run_fn(code):
    """The block as the transpiler runs it: inside a function, so helpers read
    the params through closure cells (and loop variables too)."""
    rt = fresh_rt()
    ns = dict(vars(rt))
    ns.update(Pos=Pos2, Box=Box, Cyl=Cyl, Body=Body)
    body = cb_trace.instrument(code)
    src = "def _blk():\n" + "".join("    " + ln + "\n" for ln in body.splitlines()) + "_blk()\n"
    exec(compile(src, "<traced>", "exec"), ns)
    return rt


def keep_of(rt, budget=0.1):
    out = []
    for j, (i, S, mp, rung) in enumerate(rt._cbt_walk(budget)):
        mb = set()
        for (_T, _h, tp) in S:
            mb |= tp
        out.append({"key": f"cbt:{j}", "node": i, "S": S, "M0": rt._cbt_M(S), "rung": rung,
                    "params": rt.R["nodes"][i]["p"] | mp, "moves_by": mb})
    return out


def replay(rt, V, focus):
    rt._cbt_placed = lambda obj, M, curves: obj         # no build123d: the object itself
    prev, res = rt._cbt_replay(keep_of(rt), V, focus)
    return sorted(o.name for o in prev.values()), res


PEGS = '''k = 2.0   #@param float min=1 max=9
w = 50.0  #@param float min=10 max=90
def peg():
    return Pos(i * 10, 0, 0) * Box(k, k + 1, 1)
pegs = []
for i in range(3):
    pegs += [peg()]
body = Body("slab", 1.0)
body = body - Pos(w, 0, 0) * Box(w, 5, 5)
for g in pegs:
    body = body - g
result = body
'''


def test_a_replay_rebuilds_the_pieces_a_value_shapes_and_nothing_else():
    rt = run_fn(PEGS)
    SUBS.clear()
    names, res = replay(rt, {"k": 4.0}, "k")
    # each peg again with k = 4, at ITS OWN place: the helper reads k and the
    # loop's i through closure cells, put back as they were at each call
    assert names == ["at(0.0, 0.0, 0.0)(Box(4.0,5.0,1))", "at(10.0, 0.0, 0.0)(Box(4.0,5.0,1))",
                     "at(20.0, 0.0, 0.0)(Box(4.0,5.0,1))"]
    assert SUBS == []                                    # no boolean on the body ran
    assert res["approx"] == [] and not res["errors"]
    assert rt.R["vals"] == {"k": 2.0, "w": 50.0}
    # the value that is not dragged keeps its own replayed value too
    names, _ = replay(rt, {"k": 4.0, "w": 70.0}, "w")
    assert names == ["at(70.0, 0.0, 0.0)(Box(70.0,5,5))"]


def test_a_value_that_decides_a_branch_is_replayed_as_an_approximation():
    code = '''a = 3.0  #@param float
b = 2.0  #@param float
n = 2    #@param int
c = min(a, 9.0)
if b > 1:
    x = Box(b, 1, 1)
else:
    x = Cyl(b, 1)
ys = []
for i in range(n):
    ys += [Pos(i, 0, 0) * Box(c, 1, 1)]
body = Body("slab", 1.0) - x
for y in ys:
    body = body - y
result = body
'''
    rt = run_fn(code)
    assert rt.R["UNSAFE"] == {"b", "n"}        # min() chooses a value, not a branch
    names, res = replay(rt, {"b": 5.0}, "b")   # b > 1 still: the same branch, exact
    assert names == ["Box(5.0,1,1)"] and res["approx"] == []
    names, res = replay(rt, {"b": 0.5}, "b")   # b > 1 no more: the real run takes the else
    assert res["approx"] == ["b"] and res["flips"] == {"b": [5]}
    _, res = replay(rt, {"n": 3}, "n")          # one more piece than the trace counted
    assert res["approx"] == ["n"]
    _, res = replay(rt, {"n": 2}, "n")
    assert res["approx"] == []


def test_a_replay_runs_nothing_into_the_trace():
    rt = run_fn(PEGS)
    before = (len(rt.R["nodes"]), len(rt.R["KEEP"]), len(rt.R["RCP"]))
    replay(rt, {"k": 3.0}, "k")
    assert (len(rt.R["nodes"]), len(rt.R["KEEP"]), len(rt.R["RCP"])) == before
    assert rt.R["off"] is False


def test_the_replay_surfaces_are_one_operation():
    server = (ROOT / "server.py").read_text()
    assert '@app.post("/api/graph/{name}/codeblock/{node_id}/trace/replay")' in server
    assert "off_loop(api.codeblock_trace_replay" in server


def test_a_remembering_number_copies_and_pickles_as_a_number():
    import copy
    import pickle
    rt = fresh_rt()
    a = rt.__cbt_param__(3.0, "a")
    b = a * 2
    assert copy.deepcopy(b) is b and copy.copy(b) is b
    assert pickle.loads(pickle.dumps([b])) == [6.0]


class Picks(list):
    """A ShapeList: a list subclass, its own params empty, its items carry them."""


def test_a_fillet_on_picks_of_a_changed_shape_rounds_the_new_one():
    class Sk(Shape):
        def vertices(self):
            return Picks([Shape(self.name + ".v")])

    def Poly(*pts):
        return Sk(f"Poly{tuple(float(q) for q in pts)}")

    def fil(vs, r):
        return Shape(f"fil({vs[0].name},{float(r)})")
    code = '''h = 1.0  #@param float
f = Poly(h, 2)
f = fil(f.vertices(), 0.4)
result = Body("slab", 1.0) - f
'''
    rt = fresh_rt()
    ns = dict(vars(rt))
    ns.update(Poly=Poly, fil=fil, Body=Body)
    exec(compile(cb_trace.instrument(code), "<traced>", "exec"), ns)
    names, _ = replay(rt, {"h": 3.0}, "h")
    assert names == ["fil(Poly(3.0, 2.0).v,0.4)"]


class Curve:
    """A builder-mode stand-in: it takes its size by attribute, unseen."""


def make(c):
    return Shape(f"face({float(c.n)})")


def test_a_value_lost_inside_a_helper_is_found_again_by_calling_the_helper():
    # make(c) is blind to `a` (an attribute the trace cannot see), but the
    # helper prof(a) it came out of is not: the replay re-runs the helper
    code = '''a = 2.0  #@param float
def prof(v):
    c = Curve()
    c.n = v
    return make(c), 1
f, cf = prof(a)
result = Body("slab", 1.0) - f
'''
    rt = fresh_rt()
    ns = dict(vars(rt))
    ns.update(Curve=Curve, make=make, Body=Body)
    exec(compile(cb_trace.instrument(code), "<traced>", "exec"), ns)
    names, res = replay(rt, {"a": 3.0}, "a")
    assert names == ["face(3.0)"] and not any(it.get("old") for it in res["items"])


def test_a_value_nothing_can_follow_comes_back_flagged_old():
    code = '''a = 2.0  #@param float
c = Curve()
c.n = a
f = make(c)
g = Pos(a, 0, 0) * Box(1, 1, 1)
result = Body("slab", 1.0) - f - g
'''
    rt = fresh_rt()
    ns = dict(vars(rt))
    ns.update(Curve=Curve, make=make, Body=Body, Pos=Pos2, Box=Box)
    exec(compile(cb_trace.instrument(code), "<traced>", "exec"), ns)
    lit = [it for it in keep_of(rt) if "a" in it["params"]]
    assert len(lit) == 1                       # f never knew it depended on a
    names, _ = replay(rt, {"a": 3.0}, "a")
    assert names == ["at(3.0, 0.0, 0.0)(Box(1,1,1))"]


def test_a_helper_called_by_a_replayed_helper_reads_the_new_values_too():
    code = '''H = 10.0  #@param float
top = H * 2
y_o = lambda z: top * z / H
y_i = lambda z: y_o(z) - 1
result = Body("slab", 1.0) - Box(y_i(H), 1, 1)
'''
    rt = run_fn(code)
    names, _ = replay(rt, {"H": 20.0}, "H")
    assert names == ["Box(39.0,1,1)"]          # (2·20)·20/20 − 1, not the old 19


def test_the_trace_says_what_a_drag_of_each_value_costs(tmp_path):
    """⚡ in the editor: per value, how many pieces a drag REBUILDS (and their
    measured cost) and how many it only moves — only when the trace is parked."""
    import json
    import sys
    rt = run_fn(PEGS)
    rt._cbt_placed = lambda obj, M, curves: obj
    out = tmp_path / "t.json"
    try:
        rt._cbt_finish(0.1, str(out), tid="t-fast")
        d = json.loads(out.read_text())
        assert d["trace"] == "t-fast"
        assert d["fast"]["k"]["rebuilt"] == 3 and d["fast"]["k"]["moved"] == 0
        assert d["fast"]["w"]["rebuilt"] == 1
        assert all(v["rebuild_ms"] >= 0 for v in d["fast"].values())
        rt._cbt_finish(0.1, str(out))                   # nothing parked: no ⚡ anywhere
        assert json.loads(out.read_text())["fast"] == {}
    finally:
        sys.modules.pop("_noodle_cbt", None)


def test_a_replay_also_sends_the_pieces_of_other_changed_values():
    """⚡ Fast mode never re-runs on release, so a second slider must not make the
    first one's pieces vanish: `also` asks for theirs as well."""
    rt = run_fn(PEGS)
    rt._cbt_placed = lambda obj, M, curves: obj
    prev, _ = rt._cbt_replay(keep_of(rt), {"k": 4.0, "w": 70.0}, "w")
    assert sorted(o.name for o in prev.values()) == ["at(70.0, 0.0, 0.0)(Box(70.0,5,5))"]
    prev, _ = rt._cbt_replay(keep_of(rt), {"k": 4.0, "w": 70.0}, "w", also=["k"])
    assert len(prev) == 4 and "at(70.0, 0.0, 0.0)(Box(70.0,5,5))" in {o.name for o in prev.values()}
    # a name that did not change brings nothing
    prev, _ = rt._cbt_replay(keep_of(rt), {"w": 70.0}, "w", also=["k"])
    assert len(prev) == 1


def test_the_trace_route_can_trace_the_editor_snapshot():
    server = (ROOT / "server.py").read_text()
    assert 'snap = body if body and "nodes" in body else None' in server
