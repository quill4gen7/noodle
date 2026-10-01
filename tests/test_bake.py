"""bake.py must not re-parent the shapes it exports: on the warm worker they are
the memo cache's own objects, shared with __result__."""
import pytest

bd = pytest.importorskip("build123d")


def test_bake_leaves_the_result_tree_alone(tmp_path):
    from cad_nodes.bake import bake_previews
    a, b = bd.Box(1, 1, 1), bd.Pos(3, 0, 0) * bd.Box(1, 1, 1)
    c = bd.Pos(0, 5, 0) * bd.Box(1, 1, 1)
    result = bd.Compound(children=[a, b])
    man = bake_previews({"n1": [a, c], "n2": b}, result, str(tmp_path), {})
    assert [n["files"] for n in man["nodes"]] == [["01_n1_n1.step", "01_n1_n1.stl"],
                                                   ["02_n2_n2.step", "02_n2_n2.stl"]]
    assert len(result.children) == 2 and a.parent is result and b.parent is result
    bd.export_step(result, str(tmp_path / "result.step"))     # failed before the fix
