"""The per-workflow export index (cad_nodes/export_index.py) — pure Python.

What it must guarantee: every file in exports/ can say which node (or button, or
bake bundle) wrote it, whether the graph changed since, and a file the index
never heard of still finds its Export node by name.
"""
import ast
import json
from pathlib import Path

from cad_nodes import export_index as xi
from cad_nodes.graph import Graph
from cad_nodes.transpiler import PREAMBLE, transpile

ROOT = Path(__file__).resolve().parent.parent


def _graph(size=10.0, pos=(0, 0), ui=None):
    params = {"length": size, "width": 5.0, "height": 5.0}
    if ui:
        params["_ui"] = ui
    return {"name": "t", "nodes": [
        {"id": "box", "type": "Box", "params": params, "position": list(pos)},
        {"id": "exp", "type": "ExportSTEP", "params": {"path": "part.step"}, "title": "Final part"},
    ], "connections": [
        {"id": "l1", "from_node": "box", "from_socket": "result", "to_node": "exp", "to_socket": "shape"},
    ]}


def test_graph_key_ignores_layout_and_ui_but_not_geometry():
    k = xi.graph_key(_graph())
    assert k == xi.graph_key(_graph(pos=(400, 900)))            # moved a node
    assert k == xi.graph_key(_graph(ui={"length": {"min": 0, "max": 99}}))  # slider window
    assert k != xi.graph_key(_graph(size=11.0))                 # a real param edit


def test_last_line_wins_and_deleted_files_drop_out(tmp_path):
    ex = tmp_path / "exports"
    ex.mkdir()
    (ex / "a.step").write_text("x")
    xi.record(tmp_path, "a.step", "button", fmt="step")
    xi.record(tmp_path, "a.step", "node", node="exp")
    xi.record(tmp_path, "gone.stl", "button")                   # file never written / deleted
    got = xi.load(tmp_path)
    assert set(got) == {"a.step"}
    assert got["a.step"]["via"] == "node" and got["a.step"]["node"] == "exp"
    assert xi.INDEX_NAME not in got                             # the index is not an export


def test_torn_line_costs_one_label_not_the_index(tmp_path):
    ex = tmp_path / "exports"
    ex.mkdir()
    (ex / "a.step").write_text("x")
    xi.record(tmp_path, "a.step", "button")
    with open(ex / xi.INDEX_NAME, "a") as f:
        f.write('{"file": "b.st')                               # crash mid-write
    assert xi.load(tmp_path)["a.step"]["via"] == "button"


def test_enrichment_with_the_current_graph(tmp_path):
    g = _graph()
    (tmp_path / "exports").mkdir()
    for name in ("part.step", "legacy.step", "x.zip"):
        (tmp_path / "exports" / name).write_text("x")
    xi.record(tmp_path, "part.step", "node", node="exp", graph=xi.graph_key(g))
    xi.record(tmp_path, "x.zip", "bundle", graph="stale0000000",
              contents=[{"node": "box", "files": ["01_Box_box.step"]},
                        {"node": "deleted", "files": []}])
    got = xi.load(tmp_path, g)
    part = got["part.step"]
    assert part["node_title"] == "Final part" and part["node_type"] == "ExportSTEP"
    assert part["node_exists"] is True and part["fresh"] is True
    assert got["x.zip"]["fresh"] is False
    assert got["x.zip"]["contents"][0]["title"] == "Box"        # catalog label
    assert got["x.zip"]["contents"][1]["node_exists"] is False
    assert "legacy.step" not in got                             # no line, no node writes it


def test_an_unindexed_file_finds_its_export_node_by_name(tmp_path):
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "part.step").write_text("x")        # exported before the index
    got = xi.load(tmp_path, _graph())["part.step"]
    assert got["node"] == "exp" and got["guessed"] is True


def test_export_nodes_hand_their_id_to_the_sandbox():
    code = transpile(Graph.from_dict(_graph()))
    assert "_out('part.step', 'exp')" in code


def test_the_worker_side_line_is_the_format_load_reads(tmp_path, monkeypatch):
    """`_out` lives in the PREAMBLE (the generated script cannot import
    cad_nodes), so it writes the line itself — pin that load() understands it."""
    src = PREAMBLE[PREAMBLE.index("try:\n    __GRAPH_KEY__"):]
    src = src[:src.index("\ndef _panel(")]
    g = {"_json": json, "os": __import__("os"), "__GRAPH_KEY__": "k123"}
    exec(src, g)
    monkeypatch.chdir(tmp_path)
    path = g["_out"]("../../evil/part.step", "exp")
    assert path == str(Path("exports") / "part.step")           # still sandboxed
    Path(path).write_text("x")
    got = xi.load(tmp_path)["part.step"]
    assert got["node"] == "exp" and got["graph"] == "k123" and got["fmt"] == "step"


def test_bundle_route_is_registered_before_the_format_route():
    """/export/{fmt} would otherwise swallow "bundle" as a format name (400)."""
    src = (ROOT / "server.py").read_text()
    assert src.index('"/api/graph/{name}/export/bundle"') < src.index('"/api/graph/{name}/export/{fmt}"')
    ast.parse(src)
