"""Geometry facts (cad_nodes/measure.py + executor.measure_graph).

Reference parsing and query validation are pure Python. The geometry itself
needs build123d and is skipped where it is not installed (CI's pure job).
"""

import tempfile
from pathlib import Path

import pytest

from cad_nodes.graph import Graph
from cad_nodes.measure import _r, parse_ref, refs_of


def test_parse_ref():
    assert parse_ref("n5") == {"node": "n5", "out": None, "idx": None}
    assert parse_ref("n51.body") == {"node": "n51", "out": "body", "idx": None}
    assert parse_ref("n51[3]") == {"node": "n51", "out": None, "idx": 3}
    assert parse_ref("n51.parts[-1]")["idx"] == -1
    for bad in ("", "n5.", "a b", "n5[x]"):
        with pytest.raises(ValueError):
            parse_ref(bad)


def test_refs_of_validates_shape_of_query():
    assert refs_of({"op": "props", "node": "n1"}) == ["n1"]
    assert refs_of({"op": "interference", "a": "n1", "b": "n2"}) == ["n1", "n2"]
    assert refs_of({"op": "distance", "nodes": ["n1", "n2", "n3"]}) == ["n1", "n2", "n3"]
    with pytest.raises(ValueError):
        refs_of({"op": "props"})
    with pytest.raises(ValueError):
        refs_of({"op": "explode", "node": "n1"})


def test_rounding_is_compact():
    assert _r({"v": 1.23456789, "l": [0.00001, -0.0000001], "n": float("nan")}) == \
        {"v": 1.235, "l": [0.0, 0.0], "n": None}


def test_ref_exprs_resolution():
    from cad_nodes.executor import _ref_exprs
    from cad_nodes.transpiler import Transpiler
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "cb", "type": "CodeBlock",
         "params": {"code": "#@out a: solid\na = Box(1,1,1)\nresult = [a]"}},
        {"id": "b", "type": "Box", "params": {}},
        {"id": "e", "type": "ExportSTL", "params": {}}], "connections": []})
    t = Transpiler(g, memo=True)
    t.run()
    ex = _ref_exprs(g, t, ["cb", "cb.a", "b", "b.result"])
    assert ex["cb.a"] == f"globals().get({t.out_var_of[('cb', 'a')]!r})"
    assert ex["cb"] == f"globals().get({t.out_var_of[('cb', 'result')]!r})"
    assert ex["b"] == ex["b.result"] == f"globals().get({t.var_of['b']!r})"
    for bad in ("zz", "cb.nope", "e"):
        with pytest.raises(ValueError):
            _ref_exprs(g, t, [bad])


# --- geometry (needs build123d) ---------------------------------------------
def _parts():
    b = pytest.importorskip("build123d")
    a = b.Box(10, 10, 10)
    c = b.Pos(8, 0, 0) * b.Box(10, 10, 10)
    far = b.Pos(30, 0, 0) * b.Box(2, 2, 2)
    return b, a, c, far


def test_props_and_split_detection():
    b, a, c, far = _parts()
    from cad_nodes.measure import run_queries
    two = b.Compound(children=[a, far])            # one "part" in two pieces
    res = run_queries({"p": a, "two": two}, [
        {"op": "props", "node": "p"}, {"op": "props", "node": "two"}])["results"]
    assert res[0]["volume"] == 1000.0 and res[0]["solids"] == 1 and res[0]["valid"]
    assert res[0]["bbox"]["size"] == [10.0, 10.0, 10.0]
    assert res[1]["solids"] == 2 and res[1]["shells"] == 2


def test_interference_distance_probe_section():
    b, a, c, far = _parts()
    from cad_nodes.measure import run_queries
    vals = {"a": a, "c": c, "far": far, "asm": [a, c, far]}
    res = run_queries(vals, [
        {"op": "interference", "a": "a", "b": "c"},
        {"op": "interference", "a": "a", "b": "far"},
        {"op": "interference", "node": "asm"},
        {"op": "distance", "a": "a", "b": "far"},
        {"op": "probe", "node": "a", "points": [[0, 0, 0], [5, 0, 0], [9, 0, 0]]},
        {"op": "section", "node": "a", "axis": "z", "offset": 0, "svg": True},
        {"op": "props", "node": "asm[2]"},
        {"op": "props", "node": "asm[9]"},
    ])["results"]
    assert res[0]["clash"] and res[0]["volume"] == 200.0
    assert res[0]["bbox"]["size"] == [2.0, 10.0, 10.0]
    assert not res[1]["clash"] and res[1]["distance"] == 24.0
    assert res[2]["checked"] == 3 and len(res[2]["pairs"]) == 1
    assert (res[2]["pairs"][0]["a"], res[2]["pairs"][0]["b"]) == ("asm[0]", "asm[1]")
    assert res[3]["distance"] == 24.0 and res[3]["at_b"][0] == 29.0
    assert [p["state"] for p in res[4]["probes"]] == ["in", "on", "out"]
    assert res[5]["regions"] == 1 and res[5]["area"] == 100.0
    assert res[5]["svg"].startswith("<svg")
    assert res[6]["volume"] == 8.0
    assert "index out of range" in res[7]["error"]          # fails alone


def test_measure_graph_end_to_end(monkeypatch):
    pytest.importorskip("build123d")
    from cad_nodes import executor
    monkeypatch.setattr(executor, "_warm_enabled", False)
    code = ("#@out a: solid\n#@out b: solid\n"
            "a = Box(10, 10, 10)\nb = Pos(8, 0, 0) * Box(10, 10, 10)\n")
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "cb", "type": "CodeBlock", "params": {"code": code}},
        {"id": "boom", "type": "CodeBlock", "params": {"code": "result = 1/0"}}],
        "connections": []})
    wd = Path(tempfile.mkdtemp())
    r = executor.measure_graph(g, wd, [
        {"op": "interference", "a": "cb.a", "b": "cb.b"},
        {"op": "props", "node": "ghost"},
        {"op": "props", "node": "boom"},
    ])
    assert r["success"]
    res = r["results"]
    assert res[0]["volume"] == 200.0
    assert res[1]["node"] == "ghost" and "no node" in res[1]["error"]
    assert "ZeroDivisionError" in res[2]["error"]           # the node's own error
    assert "boom" in r["node_errors"]
    # slice_summary can target one node's output now
    s = executor.slice_summary_graph(g, wd, 3, node="cb.b")
    assert s["success"] and s["bbox"]["x"] == [3.0, 13.0]
