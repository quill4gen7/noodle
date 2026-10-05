"""executor.operand_meshes — the fixed operands of the editor's boolean drag
anticipation (webui/anticipate.js; the browser side is tests/ui/anticipate.test.cjs).

Pure-Python: execute_code is replaced, so what is checked is the graph that
would be RUN — pruned to what feeds the asked nodes (nothing downstream of the
dragged value may run, or the request costs as much as the re-bake it is meant
to avoid), only the asked nodes previewed, and nothing published."""
import re

from cad_nodes import executor
from cad_nodes.graph import Graph


def _graph():
    return Graph.from_dict({"name": "t", "nodes": [
        {"id": "box", "type": "Box", "params": {}, "position": [0, 0]},
        {"id": "cyl", "type": "Cylinder", "params": {}, "position": [0, 0]},
        {"id": "mv", "type": "Move", "params": {"x": 3}, "position": [0, 0]},
        {"id": "sub", "type": "Subtract", "params": {}, "position": [0, 0], "preview": True},
        {"id": "far", "type": "Sphere", "params": {}, "position": [0, 0], "preview": True},
    ], "connections": [
        {"id": "l1", "from_node": "cyl", "from_socket": "result", "to_node": "mv", "to_socket": "shape"},
        {"id": "l2", "from_node": "box", "from_socket": "result", "to_node": "sub", "to_socket": "a"},
        {"id": "l3", "from_node": "mv", "from_socket": "result", "to_node": "sub", "to_socket": "b"},
    ]})


def test_runs_only_what_feeds_the_operands_and_publishes_nothing(tmp_path, monkeypatch):
    seen = {}

    def fake_execute(code, workdir, **kw):
        seen["code"], seen["kw"] = code, kw
        mesh = {"vertices": [[0, 0, 0]], "triangles": []}
        return {"success": True, "view": {"previews": {
            "box": {"mesh": mesh, "cache_key": "k1"}, "mv": {"mesh": mesh}}}}

    monkeypatch.setattr(executor, "execute_code", fake_execute)
    res = executor.operand_meshes(_graph(), ["box", "mv"], tmp_path)

    assert res["success"] and res["missing"] == []
    assert set(res["meshes"]) == {"box", "mv"}
    assert seen["kw"]["publish"] is False and seen["kw"]["write_stl"] is False
    code = seen["code"]
    previewed = set(re.findall(r"__previews__\[['\"](\w+)['\"]\]", code))
    assert previewed == {"box", "mv"}               # the saved eyes on sub/far are overridden
    # the Subtract (downstream of the drag) and an unrelated node are pruned away
    assert not re.search(r"Sphere\(", code)
    assert "_ev('s', 'sub')" not in code and "_ev('s', 'far')" not in code


def test_unknown_node_is_a_keyerror(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "execute_code", lambda *a, **k: {"success": True})
    try:
        executor.operand_meshes(_graph(), ["nope"], tmp_path)
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_a_missing_operand_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(executor, "execute_code",
                        lambda *a, **k: {"success": True, "view": {"previews": {}}})
    res = executor.operand_meshes(_graph(), ["box"], tmp_path)
    assert res["missing"] == ["box"] and res["meshes"] == {}
