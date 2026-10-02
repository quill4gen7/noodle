"""Generations + the read-only viewer (/view/<graph>/<gen>).

A generation is a frozen copy of one run so a link an agent sends keeps
showing THAT result while the workflow moves on. What can fail silently, and is
pinned here: a link whose target changes (numbers reused, the live view read
instead of the copy), duplicates piling up when an agent snapshots in a loop,
and path traversal through the gen id.
"""

import json
from pathlib import Path

import pytest

from cad_nodes import api
from cad_nodes.graph import Graph
from cad_nodes.store import GraphStore, validate_gen_id

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text()
MCP = (ROOT / "mcp_server.py").read_text()
VIEW = (ROOT / "webui" / "view.html").read_text()

BOX = {"id": "n1", "type": "Box", "params": {"length": 10, "width": 10, "height": 10}}


def _view(size=1.0):
    return {"previews": {
        "n1": {"kind": "Solid", "mesh": {"vertices": [[0, 0, 0], [size, 0, 0], [0, size, 0]],
                                         "triangles": [[0, 1, 2]]}},
        "n9": {"kind": "Text", "text": "not drawable"},
    }}


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path)
    s.save("demo", Graph.from_dict({"name": "demo", "nodes": [BOX], "connections": []}))
    (s.dir("demo") / "view.json").write_text(json.dumps(_view()))
    return s


def test_snapshot_freezes_a_copy_with_a_stable_link(store):
    out = api.snapshot(store, "demo", label="first", run=False, base_url="http://h:1/")
    assert out["gen"] == "g1" and not out["reused"]
    assert out["url"] == "http://h:1/view/demo/g1"
    assert [p["id"] for p in out["pieces"]] == ["n1"]          # only drawable ones
    # the live view changes afterwards; the generation does not
    (store.dir("demo") / "view.json").write_text(json.dumps(_view(5.0)))
    assert store.load_gen("demo", "g1", "view") == _view()
    assert store.load_gen("demo", "g1", "graph")["nodes"][0]["type"] == "Box"


def test_identical_result_is_reused_changed_one_is_a_new_gen(store):
    a = api.snapshot(store, "demo", run=False)
    b = api.snapshot(store, "demo", run=False)
    assert b["reused"] and b["gen"] == a["gen"]
    (store.dir("demo") / "view.json").write_text(json.dumps(_view(2.0)))
    c = api.snapshot(store, "demo", run=False)
    assert c["gen"] == "g2" and not c["reused"]
    assert [g["gen"] for g in api.list_gens(store, "demo")] == ["g2", "g1"]


def test_numbers_are_never_reused(store):
    api.snapshot(store, "demo", run=False)
    (store.dir("demo") / "view.json").write_text(json.dumps(_view(3.0)))
    api.snapshot(store, "demo", run=False)
    import shutil
    shutil.rmtree(store.gen_dir("demo", "g1"))
    (store.dir("demo") / "view.json").write_text(json.dumps(_view(4.0)))
    assert api.snapshot(store, "demo", run=False)["gen"] == "g3"


def test_nothing_drawn_is_an_error(store):
    (store.dir("demo") / "view.json").write_text(json.dumps({"previews": {}}))
    with pytest.raises(ValueError):
        api.snapshot(store, "demo", run=False)


@pytest.mark.parametrize("bad", ["..", "g0", "g", "x1", "g1/..", "../g1", "g01"])
def test_gen_ids_are_validated(bad):
    with pytest.raises(ValueError):
        validate_gen_id(bad)


def test_routes_and_tools_exist():
    for r in ('@app.post("/api/graph/{name}/snapshot")', '@app.get("/api/graph/{name}/gens")',
              '@app.get("/api/graph/{name}/gens/{gen}/{part}")', '@app.get("/view/{name}/{gen}"'):
        assert r in SERVER
    route = SERVER.split('@app.post("/api/graph/{name}/snapshot")')[1].split("\n@app.")[0]
    assert "off_loop(api.snapshot" in route             # run=1 executes the graph
    assert "def cad_snapshot" in MCP and "def cad_list_gens" in MCP


def test_the_viewer_reads_only_the_frozen_copy():
    """Reading /api/graph/<name>/view would show the LIVE result, which is
    exactly what a fixed link must not do. And the viewer writes nothing."""
    assert "/gens/${GEN}" in VIEW or "gens/${GEN}" in VIEW
    assert "/view`" not in VIEW and "/view'" not in VIEW
    assert "method:" not in VIEW and "'POST'" not in VIEW and "'PUT'" not in VIEW


# --- timelines -----------------------------------------------------------------
VIEWER = (ROOT / "webui" / "viewer.js").read_text()
NODES = (ROOT / "webui" / "nodes.html").read_text()


def test_the_replay_math_exists_once_and_both_pages_use_it():
    """The /view player and the editor's live scrub must move a part the same
    way — so the replay lives in viewer.js and nowhere else."""
    for fn in ("dropMatrixAt", "keyInterp", "sceneBodyPose", "poseAnim"):
        assert f"export function {fn}(" in VIEWER
        assert f"function {fn}(" not in NODES and f"function {fn}(" not in VIEW
    assert "poseAnim" in NODES.split("function applyDropAnim(")[1].split("\n}")[0]
    assert "poseAnim(a.obj, a.anim" in VIEW


def test_the_link_params_survive_the_first_hash_write():
    """writeHash() runs before the timeline exists; reading `play`/`t` from the
    live location.hash afterwards lost them (autoplay silently did nothing)."""
    assert "const HASH0 = location.hash;" in VIEW
    body = VIEW.split("function hashParam(")[1].split("\n}")[0]
    assert "HASH0" in body and "location.hash" not in body


def test_snapshot_reports_the_timeline(store):
    v = _view()
    v["previews"]["n1"]["anim"] = {"kind": "keys", "T": 1.2, "t": 0,
                                   "times": [0, 1.2], "pos": [[0, 0, 0]] * 2,
                                   "quat": [[0, 0, 0, 1]] * 2}
    (store.dir("demo") / "view.json").write_text(json.dumps(v))
    out = api.snapshot(store, "demo", run=False)
    assert out["timeline"] == {"seconds": 1.2}
    assert out["pieces"][0].get("animated") is True
    (store.dir("demo") / "view.json").write_text(json.dumps(_view(9.0)))
    assert api.snapshot(store, "demo", run=False)["timeline"] is None


def test_the_ui_is_revalidated_so_it_is_never_half_old_half_new():
    """A cached old viewer.js under a new page = a module import error = a blank
    page (it happened right after poseAnim moved into viewer.js)."""
    mw = SERVER.split("async def _revalidate_ui(")[1].split("\n\n")[0]
    assert '"/static/"' in mw and '"/nodes"' in mw and '"/view/"' in mw
    assert '"no-cache"' in mw


def test_every_movement_has_its_own_track():
    """A sequence the agent must choreograph by padding clocks is exactly what
    went wrong on walle. Each plan is a track with its own t, slider and ▶; the
    master slider still moves them all, and a per-track pose rides the hash."""
    assert 'id="tl-tracks"' in VIEW and 'id="tl-trk"' in VIEW
    body = VIEW.split("function poseTrack(")[1].split("\n}")[0]
    assert "poseAnim(a.obj, a.anim, a.t)" in body
    assert "poseTrack(a, clock.t)" in VIEW.split("function poseAll(")[1].split("\n}")[0]
    assert "hashParam('tt')" in VIEW and "'tt='" in VIEW
    # framing samples the whole movement, then puts EVERY track back where it was
    frame = VIEW.split("function frameVisible(")[1].split("\n}")[0]
    assert "poseTrack(a, keep[i])" in frame
