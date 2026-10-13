"""A bounding box is read from GEOMETRY, never from a tessellation.

build123d's Shape.bounding_box() (optimal=True and False alike) lets OCCT read
the triangulation / PolygonOnTriangulation stored on the edges. Once a face that
shares a curve's edges has been tessellated off the XY plane (the preview does
it, Populate does it), that data is wrong for the curve:

    w = Wire([Spline((220,-200,60),(220,-100,150),(220,-20,60)),
              Edge.make_line((220,-20,60),(220,-200,60))])
    w.bounding_box().size                 # (0, 180, 90.28)   right
    Face(w).tessellate(0.5, 0.3)
    w.bounding_box().size                 # (90, 270, 180.27) WRONG

So the engine measures through ONE helper — `_bbox` in the PREAMBLE, its twin
`_geom_bbox` in mesh_extractor for the imported modules — and these tests pin
that no other call site reads build123d's box. Pure Python: the behaviour itself
needs build123d and was measured in the image (see CLAUDE.md, worker notes).
"""
import ast
import pathlib
import re

from cad_nodes.gh.runtime import GH_PREAMBLE
from cad_nodes.transpiler import PREAMBLE

ROOT = pathlib.Path(__file__).resolve().parent.parent / "cad_nodes"


def _bbox_calls(tree):
    """(enclosing function name, receiver source) for every `x.bounding_box(...)`."""
    out = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "bounding_box"):
                out.append((fn, ast.unparse(child.func.value)))
            walk(child, name)

    walk(tree, None)
    return out


def _fn_src(src, name):
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\Z)", src, re.S)
    assert m, f"{name} not found"
    return m.group(0)


def test_preamble_has_one_bbox_helper_that_skips_triangulation():
    body = _fn_src(PREAMBLE, "_bbox")
    assert "AddOptimal_s(_w, _b, False, False)" in body      # useTriangulation=False
    assert "Add_s(_w, _b, False)" in body                    # fast path, same rule


def test_preamble_never_reads_build123d_bbox_outside_the_helper():
    calls = _bbox_calls(ast.parse(PREAMBLE))
    # the helper's own fallback (no .wrapped: not a B-Rep) and manifold3d's
    # Manifold.bounding_box() in the mechanism collision check are not build123d
    allowed = {("_bbox", "_s"), ("_mechanism", "ma"), ("_mechanism", "mb")}
    stray = [c for c in calls if c not in allowed]
    assert not stray, f"Shape.bounding_box() reads the triangulation — use _bbox: {stray}"


def test_gh_bbox_is_the_core_helper():
    body = _fn_src(GH_PREAMBLE, "_gh_bbox")
    assert "return _bbox(_s)" in body
    assert "BRepBndLib" not in body          # one implementation, not two


def test_worker_modules_measure_through_geom_bbox():
    for mod in ("mesh_extractor.py", "measure.py", "slice_summary.py", "bake.py"):
        calls = _bbox_calls(ast.parse((ROOT / mod).read_text()))
        stray = [c for c in calls if c != ("_geom_bbox", "shape")]
        assert not stray, f"{mod}: {stray} — use mesh_extractor._geom_bbox"
    me = (ROOT / "mesh_extractor.py").read_text()
    assert "AddOptimal_s(w, b, False, False)" in _fn_src(me, "_geom_bbox")
    # the live path keeps its speed (poles, ~ms) — but without triangulation
    assert "_geom_bbox(shape, exact=False)" in _fn_src(me, "_live_bbox")
