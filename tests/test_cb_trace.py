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
