"""Graph lint (cad_nodes/lint.py) — soft findings, pure Python."""

from cad_nodes.graph import Graph
from cad_nodes.lint import lint_graph

CODE = ("h = 12  #@param float min=4 max=30\n"
        "w = 5   #@param int\n"
        "#@out lid: solid\n"
        "#@out body: solid\n"
        "body = Box(w, w, h)\n"
        "result = {'lid': None}\n")


def _g(slider_params, cb_params=None, wire=True):
    nodes = [{"id": "s", "type": "NumberSlider", "params": slider_params, "title": "Alt"},
             {"id": "cb", "type": "CodeBlock",
              "params": {"code": CODE, **(cb_params or {})}}]
    conns = [{"id": "c", "from_node": "s", "from_socket": "result",
              "to_node": "cb", "to_socket": "h"}] if wire else []
    return Graph.from_dict({"name": "t", "nodes": nodes, "connections": conns})


def _codes(findings):
    return [(f["code"], f["level"]) for f in findings]


def test_clean_graph_has_no_findings():
    g = _g({"value": 12, "_ui": {"value": {"min": 4, "max": 30}}})
    assert lint_graph(g) == []


def test_slider_value_differs_is_info():
    g = _g({"value": 10, "_ui": {"value": {"min": 4, "max": 30}}})
    (f,) = lint_graph(g)
    assert (f["code"], f["level"], f["param"], f["slider"]) == \
        ("slider_mismatch", "info", "h", "s")
    assert "value 10 vs default 12" in f["message"] and "'Alt'" in f["message"]


def test_slider_bounds_differ_is_warning():
    g = _g({"value": 12, "_ui": {"value": {"min": 4, "max": 25}}})
    (f,) = lint_graph(g)
    assert f["level"] == "warning" and "max 25 vs 30" in f["message"]


def test_slider_outside_declared_range():
    # no _ui window -> the catalog bounds (0..100) apply
    g = _g({"value": 50})
    (f,) = lint_graph(g)
    assert f["level"] == "warning" and "OUTSIDE" in f["message"]
    assert f["slider_state"] == {"value": 50, "min": 0, "max": 100}


def test_cb_overrides_hidden_dead_and_stale():
    g = _g({"value": 12, "_ui": {"value": {"min": 4, "max": 30}}},
           {"_cb": {"w": 8, "h": 20, "gone": 1}})
    by = {f.get("param"): f for f in lint_graph(g) if f["code"] == "cb_override"}
    assert by["w"]["level"] == "warning" and "runs as 8 but the code says 5" in by["w"]["message"]
    assert by["h"].get("dead") and by["h"]["level"] == "info"      # wired socket wins
    assert by["gone"].get("stale")
    # an override equal to the default is not hidden
    g = _g({"value": 12, "_ui": {"value": {"min": 4, "max": 30}}}, {"_cb": {"w": 5}})
    assert lint_graph(g) == []


def test_syntax_and_unassigned_out():
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "a", "type": "CodeBlock", "params": {"code": "x = (1,\n"}},
        {"id": "b", "type": "CodeBlock",
         "params": {"code": "#@out ghost: solid\n#@out lid\nresult = {'lid': 1}\n"}}],
        "connections": []})
    f = lint_graph(g)
    assert ("codeblock_syntax", "error") in _codes(f)
    syn = next(x for x in f if x["code"] == "codeblock_syntax")
    assert syn["node"] == "a" and syn["line"] == 1
    un = [x for x in f if x["code"] == "cb_out_unassigned"]
    assert [x["output"] for x in un] == ["ghost"]           # 'lid' is a dict key
