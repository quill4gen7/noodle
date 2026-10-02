"""executor.codeblock_sections_run — runs an instrumented COPY, publishes nothing."""
import pytest

pytest.importorskip("build123d")

from cad_nodes.executor import codeblock_sections_run  # noqa: E402
from cad_nodes.graph import Graph  # noqa: E402

CODE = '''\
w = 20.0  #@param float min=5 max=40
# ---- quote ----
half = w / 2
# ---- shell ----
shell = Box(w, w, 10)
# ---- holes ----
for sx in (-1, 1):
    shell -= Pos(sx * half / 2, 0, 0) * Cylinder(1, 20)
result = shell
'''


def _graph(code=CODE):
    return Graph.from_dict({"name": "t", "nodes": [
        {"id": "cb", "type": "CodeBlock", "params": {"code": code}, "position": [0, 0],
         "preview": True}], "connections": []})


def test_timings_values_and_one_sections_shapes(tmp_path):
    res = codeblock_sections_run(_graph(), "cb", tmp_path, section="s4")
    assert res["success"], res.get("error")
    ids = {s["title"]: s["id"] for s in res["analysis"]["sections"]}
    assert set(res["timings"]) >= {ids["quote"], ids["shell"], ids["holes"]}
    assert res["values"][ids["quote"]] == {"half": 10.0}
    assert res["snapped"] == ["s4:shell"]                      # what holes hands on
    assert list(res["view"]["previews"]) == ["s4:shell"]
    # a side run: no view.json / output.stl / latest-run pointer in the project
    assert not (tmp_path / "view.json").exists()
    assert not (tmp_path / "progress.jsonl").exists()


def test_a_failing_section_is_named(tmp_path):
    bad = CODE.replace("Cylinder(1, 20)", "Cylinder(1, undefined_name)")
    res = codeblock_sections_run(_graph(bad), "cb", tmp_path)
    assert not res["success"]
    assert res["failed_in"] == next(s["id"] for s in res["analysis"]["sections"]
                                    if s["title"] == "holes")
