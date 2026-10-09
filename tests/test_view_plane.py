"""⊞ The pencil's working plane (PLAN_VIEW_TOOLS §4) — drawing in the void.

A stroke drawn on the plane touches no piece, so without help the agent would
read «a red line, on nothing». What is pinned here: such a gesture is a mark of
`kind: "plane"`, it carries the plane, and it says which piece of the FROZEN gen
it is nearest to and how far — «this arm goes on up to here». A gesture that
starts on the part and runs off its edge onto the plane is ONE mark, on the part
AND of kind plane.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from cad_nodes import api
from cad_nodes.graph import Graph
from cad_nodes.store import GraphStore

ROOT = Path(__file__).resolve().parent.parent
VIEW = (ROOT / "webui" / "view.html").read_text()
PLANE_JS = (ROOT / "webui" / "view-plane.js").read_text()
MCP = (ROOT / "mcp_server.py").read_text()
HELP = (ROOT / "cad_nodes" / "AGENT_HELP.md").read_text()

NODES = [{"id": "n1", "type": "Box", "title": "Braccio", "params": {}},
         {"id": "n2", "type": "Box", "title": "Base", "params": {}}]


def _box(lo, hi):
    """A closed box mesh, as view.json carries it."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
         [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
    t = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
         [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    return {"vertices": v, "triangles": t}


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path)
    s.save("demo", Graph.from_dict({"name": "demo", "nodes": NODES, "connections": []}))
    (s.dir("demo") / "view.json").write_text(json.dumps({"previews": {
        "n1": {"kind": "Solid", "mesh": _box([0, 0, 0], [40, 6, 6])},       # the arm, along +x
        "n2": {"kind": "Solid", "mesh": _box([-30, -30, -20], [30, 30, -10])},
    }}))
    api.snapshot(s, "demo", run=False)
    return s


PLANE_Z3 = {"origin": [40, 3, 3], "normal": [0, 0, 1]}


def test_a_stroke_in_the_void_is_a_plane_mark_near_its_piece(store):
    # in the arm's own plane, from 10 mm past its tip to 30 mm past it
    pts = [[50 + i, 3, 3] for i in range(21)]
    note = api.add_note(store, "demo", "g1", {"strokes": [
        {"color": "#ef4444", "width": 0.5, "g": 1, "points": pts,
         "normals": [[0, 0, 1]] * len(pts), "plane": PLANE_Z3}]})
    (m,) = note["marks"]
    assert m["kind"] == "plane" and m["on"] == []
    assert m["plane"] == {"origin": [40, 3, 3], "normal": [0, 0, 1]}
    near = m["near_piece"]
    assert near["node"] == "n1" and near["title"] == "Braccio"
    assert near["distance_mm"] == pytest.approx(10, abs=0.01)
    # the stroke itself: no piece, the plane, its own nearest piece
    s = note["strokes"][0]
    assert "piece" not in s and "node" not in s and s["plane"]["normal"] == [0, 0, 1]


def test_a_stroke_that_runs_off_the_part_is_one_mark_on_it_and_on_the_plane(store):
    on_part = {"color": "#ef4444", "width": 0.5, "g": 7, "piece": "n1",
               "points": [[30 + i, 3, 6] for i in range(11)]}
    # the pen leaves the arm at its tip and goes on, on the plane, from the same point
    off = {"color": "#ef4444", "width": 0.5, "g": 7, "plane": {"origin": [35, 3, 6], "normal": [0, 0, 1]},
           "points": [[40 + i, 3, 6] for i in range(16)]}
    note = api.add_note(store, "demo", "g1", {"strokes": [on_part, off]})
    (m,) = note["marks"]
    assert m["kind"] == "plane" and [o["node"] for o in m["on"]] == ["n1"]
    assert m["near_piece"]["node"] == "n1" and m["near_piece"]["distance_mm"] == pytest.approx(0, abs=0.01)
    assert m["shape"] == "line" and m["length_mm"] == pytest.approx(25, abs=0.1)
    # the agent reads it the same way on the way back out
    (agent,) = api.list_notes(store)
    assert agent["marks"][0]["kind"] == "plane" and agent["marks"][0]["near_piece"]["node"] == "n1"


def test_the_nearest_piece_is_measured_to_its_faces_not_its_vertices(store):
    # above the middle of the base's top face: 30 mm from any vertex, 5 mm from the face
    note = api.add_note(store, "demo", "g1", {"strokes": [
        {"g": 1, "points": [[0, 20, -5]], "plane": {"origin": [0, 0, -5], "normal": [0, 0, 1]}}]})
    near = note["marks"][0]["near_piece"]
    assert near["node"] == "n2" and near["distance_mm"] == pytest.approx(5, abs=0.01)


def test_a_surface_mark_has_no_kind_and_old_notes_read_as_before(store):
    note = api.add_note(store, "demo", "g1", {"strokes": [
        {"g": 1, "piece": "n1", "points": [[5, 3, 6], [9, 3, 6]]}]})
    (m,) = note["marks"]
    assert "kind" not in m and "near_piece" not in m and "plane" not in m


def test_bad_planes_are_refused(store):
    for pl in ([0, 0, 1], {"origin": [0, 0, 0]}, {"origin": [0, 0, 0], "normal": [0, 0, 0]},
               {"origin": [0, 0], "normal": [0, 0, 1]}, {"origin": [0, 0, 0], "normal": [0, "z", 1]}):
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"strokes": [{"g": 1, "points": [[1, 2, 3]], "plane": pl}]})


def test_a_gen_without_meshes_still_takes_a_plane_stroke(tmp_path):
    # only a curve drawn: nothing with faces to be near to
    s = GraphStore(tmp_path)
    s.save("bare", Graph.from_dict({"name": "bare", "nodes": NODES, "connections": []}))
    (s.dir("bare") / "view.json").write_text(json.dumps({"previews": {
        "n1": {"kind": "Curve", "polylines": [[[0, 0, 0], [1, 0, 0]]]}}}))
    api.snapshot(s, "bare", run=False)
    note = api.add_note(s, "bare", "g1", {"strokes": [{"g": 1, "points": [[1, 2, 3]], "plane": PLANE_Z3}]})
    assert note["marks"][0]["kind"] == "plane" and "near_piece" not in note["marks"][0]


def test_point_to_triangle_distance_against_brute_force():
    rng = np.random.default_rng(3)
    A, B, C = (rng.normal(size=(40, 3)) for _ in range(3))
    P = rng.normal(size=(15, 3)) * 2
    got = api._point_tri_dist(P, A, B, C)
    # brute force: a dense barycentric sampling of every triangle
    k = 60
    u, v = np.meshgrid(np.linspace(0, 1, k), np.linspace(0, 1, k))
    keep = (u + v) <= 1
    u, v = u[keep], v[keep]
    for j in range(len(A)):
        S = A[j] + np.outer(u, B[j] - A[j]) + np.outer(v, C[j] - A[j])
        brute = np.linalg.norm(P[:, None, :] - S[None], axis=-1).min(1)
        assert np.all(got[:, j] <= brute + 1e-9)          # never farther than a real point
        edge = max(np.linalg.norm(B[j] - A[j]), np.linalg.norm(C[j] - A[j])) / (k - 1)
        assert np.all(brute - got[:, j] <= edge * 1.5)     # and as near as the sampling can tell


def test_the_viewer_has_the_plane_selector_and_draws_off_the_part():
    # the selector, its modes and the shared pipeline: pen, 3D pen and paint
    assert "registerTool" in PLANE_JS and "'⊞'" in PLANE_JS
    for mode in ("surface", "xy", "xz", "yz", "view"):
        assert f"'{mode}'" in PLANE_JS
    assert "inkHit" in VIEW and "VP.planeHit" in VIEW
    # a stroke that leaves the part continues on the plane, and is split at a
    # depth jump like the pen already does
    assert "plane:" in VIEW and "s.plane" in VIEW
    # the data the server reads
    assert "plane: s.plane" in VIEW
    # Shift+wheel moves the plane, Alt+click re-anchors it
    assert "shiftKey" in PLANE_JS and "altKey" in VIEW


def test_the_agent_is_told_about_plane_marks():
    assert "near_piece" in MCP and '"plane"' in MCP
    assert "near_piece" in HELP and "kind" in HELP
