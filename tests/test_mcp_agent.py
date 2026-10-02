"""
The MCP surface an agent actually drives: lean execute by default, validated
set_param by id or title, the compact catalog, and the agent-editing section
(get_graph / edit_code / apply_ops / validate). Tool functions are called
directly — FastMCP's decorator returns them unchanged.
"""

import json

import pytest

pytest.importorskip("mcp")

import mcp_server as M  # noqa: E402
from cad_nodes import api  # noqa: E402
from cad_nodes.store import GraphStore  # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = GraphStore(tmp_path)
    monkeypatch.setattr(M, "STORE", s)
    api.create_graph(s, "g")
    return s


def test_execute_is_lean_by_default(store, monkeypatch):
    api.add_node(store, "g", "Box", title="Body")
    seen = {}

    def fake(graph, workdir, timeout=120):
        seen["w"] = graph.node("box_1").params.get("width")
        return {"success": True, "code": "x" * 300_000, "stdout": "",
                "view": {"success": True, "volume": 1000.0000001,
                         "mesh": {"vertices": [[0, 0, 0]] * 1000},
                         "previews": {"box_1": {"kind": "Solid", "mesh": {}}}}}
    monkeypatch.setattr(api, "execute_graph", fake)
    out = M.cad_execute("g")
    assert "code" not in out and "mesh" not in out["view"]
    assert out["view"]["volume"] == 1000.0
    assert len(json.dumps(out)) < 500
    assert "code" in M.cad_execute("g", include_code=True)
    M.cad_execute("g", overrides={"Body": {"width": 12}})
    assert seen["w"] == 12
    assert "width" not in store.load("g").node("box_1").params   # not saved


def test_set_param_validates_and_takes_a_title(store):
    api.add_node(store, "g", "Box", title="Body")
    assert M.cad_set_param("g", "Body", {"width": 5})["node"] == "box_1"
    err = M.cad_set_param("g", "Body", {"widht": 5})
    assert "error" in err and "Valid params" in err["error"]


def test_catalog_is_one_line_per_type_by_default(store):
    text = M.cad_get_node_catalog(query="box")
    assert isinstance(text, str) and "Box [" in text
    assert isinstance(M.cad_get_node_catalog(full=True), list)
    assert "no node type matches" in M.cad_get_node_catalog(query="zzzzqqq")
    assert M.cad_get_node_def("Box")["type"] == "Box"


def test_add_node_auto_places(store):
    a = M.cad_add_node("g", "Box")
    b = M.cad_add_node("g", "Box")
    g = store.load("g")
    assert g.node(a).position != g.node(b).position


def test_agent_editing_section(store):
    out = M.cad_apply_ops("g", [
        {"op": "add_node", "type": "CodeBlock", "params": {"code": "a = 1\n"},
         "title": "Script"},
        {"op": "add_node", "type": "Box"},
    ])
    assert out["ok"], out
    assert M.cad_edit_code("g", "Script", "a = 1", "a = 2")["line"] == 1
    assert "error" in M.cad_edit_code("g", "Script", "zzz", "y")
    compact = M.cad_get_graph("g")
    assert {n["id"] for n in compact["nodes"]} == {"codeblock_1", "box_1"}
    assert M.cad_set_node("g", "box_1", preview=True)["preview"] is True
    rep = M.cad_validate("g")
    assert rep["ok"] is True and rep["param_issues"] == []
    assert "error" in M.cad_apply_ops("g", [{"op": "remove", "node": "nope"}])
    assert M.cad_arrange("g")["overlaps"] == 0
