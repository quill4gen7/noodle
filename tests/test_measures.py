"""📏 The agent's dimensions on a generation (measures.json) and the user's
dimensions as the agent reads them (cad_notes `measures`).

What could go wrong silently, and is pinned here: a dimension written INTO the
immutable gen, a status that says ok on a value outside its tolerance, an agent
forced to guess points it could have measured, a user's «Ø ≈ 8,00» that reaches
the agent with no word of where it was taken, and a surface (HTTP, MCP, help,
static preview) that forgets the new file.
"""

import json
from pathlib import Path

import pytest

from cad_nodes import api
from cad_nodes.graph import Graph
from cad_nodes.store import GraphStore

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text()
MCP = (ROOT / "mcp_server.py").read_text()
VIEW = (ROOT / "webui" / "view.html").read_text()
HELP = (ROOT / "cad_nodes" / "AGENT_HELP.md").read_text()
PAGES = (ROOT / "scripts" / "build_pages.py").read_text()

BOX = {"id": "n1", "type": "Box", "title": "Body", "params": {"length": 10, "width": 10, "height": 10}}


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path)
    s.save("demo", Graph.from_dict({"name": "demo", "nodes": [BOX], "connections": []}))
    (s.dir("demo") / "view.json").write_text(json.dumps({"previews": {"n1": {
        "kind": "Solid", "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                                  "triangles": [[0, 1, 2]]}}}}))
    api.snapshot(s, "demo", run=False)
    return s


def test_a_dimension_sits_beside_the_gen(store):
    before = sorted(p.name for p in store.gen_dir("demo", "g1").iterdir())
    out = api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [3, 4, 0], "text": "diag"}])
    m, = out["measures"]
    assert m["kind"] == "distance" and m["value"] == 5 and m["unit"] == "mm" and m["measure"] == 1
    assert out["ref"] == "demo/g1" and out["url"].endswith("/view/demo/g1")
    after = sorted(p.name for p in store.gen_dir("demo", "g1").iterdir())
    assert after == sorted(before + ["measures.json"])
    assert api.gen_measures(store, "demo", "g1")[0]["text"] == "diag"


def test_the_status_is_judged_from_the_tolerance(store):
    ms = api.measure_gen(store, "demo", "g1", [
        {"a": [0, 0, 0], "b": [0, 0, 1.2], "expected": 1.6, "tolerance": 0.1},     # too thin
        {"a": [0, 0, 0], "b": [0, 0, 1.65], "expected": 1.6, "tolerance": 0.1},    # inside
        {"a": [0, 0, 0], "b": [0, 0, 30], "expected": 30},                         # no tolerance
        {"a": [0, 0, 0], "b": [0, 0, 1], "expected": 9, "tolerance": 0.1, "status": "check"},
    ])["measures"]
    assert [m["status"] for m in ms] == ["fail", "ok", "check", "check"], "an explicit status wins"
    assert "status" not in api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [1, 0, 0]}])["measures"][0]


def test_a_hole_is_a_circle_and_its_value_follows(store):
    m, = api.measure_gen(store, "demo", "g1", [{"kind": "diameter",
        "circle": {"center": [5, 5, 10], "axis": [0, 0, 2], "r": 4}, "node": "Body"}])["measures"]
    assert m["value"] == 8 and m["circle"]["axis"] == [0, 0, 1]
    assert m["node"] == "n1" and m["title"] == "Body"


def test_between_measures_the_frozen_graph_not_a_guess(store, monkeypatch):
    seen = {}

    def fake(st, g, gen, pairs):
        seen.update(graph=g, gen=gen, pairs=dict(pairs))
        return {i: {"distance": 1.234567, "at_a": [0, 0, 0], "at_b": [0, 0, 1.234567]} for i in pairs}
    monkeypatch.setattr(api, "_between_points", fake)
    ms = api.measure_gen(store, "demo", "g1", [
        {"a": [0, 0, 0], "b": [1, 0, 0]},
        {"between": ["n1", "n1.body"], "expected": 1.6, "tolerance": 0.1, "text": "parete"}])["measures"]
    assert seen == {"graph": "demo", "gen": "g1", "pairs": {1: ("n1", "n1.body")}}
    m = ms[1]
    assert m["value"] == 1.2346 and m["b"] == [0, 0, 1.2346] and m["between"] == ["n1", "n1.body"]
    assert m["status"] == "fail" and m["approx"] is False


def test_replace_false_appends_and_numbers_on(store):
    api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [1, 0, 0]}])
    out = api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [2, 0, 0]}], replace=False)
    assert [m["measure"] for m in out["measures"]] == [1, 2]
    assert len(api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [2, 0, 0]}])["measures"]) == 1


def test_offset_lifts_the_line(store):
    m, = api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [1, 0, 0], "offset": [0, 0, 5]}])["measures"]
    assert m["n"] == [0, 0, 1] and m["off"] == 5


@pytest.mark.parametrize("bad", [
    {"a": [0, 0, 0]},                                        # no b, no between
    {"a": [0, 0], "b": [1, 0, 0]},
    {"kind": "volume", "a": [0, 0, 0], "b": [1, 0, 0]},
    {"kind": "diameter"},                                    # no circle
    {"kind": "diameter", "circle": {"center": [0, 0, 0], "axis": [0, 0, 0], "r": 2}},
    {"a": [0, 0, 0], "b": [1, 0, 0], "status": "great"},
    {"a": [0, 0, 0], "b": [1, 0, 0], "tolerance": -1, "expected": 1},
    {"a": [0, 0, 0], "b": [1, 0, 0], "node": "nope"},
    {"a": [0, 0, 0], "b": [1, 0, 0], "text": "x" * 121},
    {"between": ["n1"]},
    "12 mm",
])
def test_bad_dimensions_are_refused(store, bad):
    with pytest.raises(ValueError):
        api.measure_gen(store, "demo", "g1", [bad])


def test_at_most_forty(store):
    with pytest.raises(ValueError):
        api.measure_gen(store, "demo", "g1", [{"a": [0, 0, 0], "b": [1, 0, 0]}] * 41)


def test_unknown_gen_is_a_key_error(store):
    with pytest.raises(KeyError):
        api.measure_gen(store, "demo", "g9", [])


def test_snapshot_pins_dimensions_and_a_bad_one_does_not_fail_it(store):
    out = api.snapshot(store, "demo", label="v2", run=False,
                       measures=[{"a": [0, 0, 0], "b": [0, 0, 2]}])
    assert out["measures"][0]["value"] == 2
    out = api.snapshot(store, "demo", label="v3", run=False, measures=[{"a": [0, 0, 0]}])
    assert out["gen"] and "measures_error" in out


def test_the_agent_reads_the_users_dimensions_next_to_their_marks(store):
    circle = [[5 + 4 * c, 5 + 4 * s, 10] for c, s in ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 0))]
    api.add_note(store, "demo", "g1", {
        "strokes": [{"color": "#ef4444", "width": 0.5, "piece": "n1", "g": 1, "points": circle},
                    {"color": "#3b82f6", "width": 0.5, "g": 2, "points": [[40, 40, 0], [50, 40, 0]]}],
        "measures": [{"kind": "diameter", "value": 8.0, "approx": True,
                      "a": {"at": [5, 5, 10], "snap": "circle_center", "piece": "n1",
                            "circle": {"center": [5, 5, 10], "axis": [0, 0, 1], "r": 4}}},
                     {"kind": "face_gap", "value": 10, "a": {"at": [5, 5, 10], "snap": "face", "piece": "n1"},
                      "b": {"at": [5, 5, 0], "snap": "face", "piece": "n1"}, "text": "troppo?"}]})
    n, = api.list_notes(store)
    d, g = n["measures"]
    assert d["summary"] == "diameter Ø ≈ 8,00 on Body (circle_center)"
    assert d["near_marks"] == [1] and n["marks"][0]["measures"] == ["Ø ≈ 8,00"]
    assert g["near_marks"] == [], "4 mm off the rim, a quarter of 10 is 2.5: not this mark"
    assert g["summary"] == "gap between parallel faces 10,00 mm on Body (face → face) — «troppo?»"
    assert "measures" not in n["marks"][1], "the blue line is nowhere near"


def test_every_surface_knows_measures():
    assert '@app.post("/api/graph/{name}/gens/{gen}/measures")' in SERVER
    assert '@app.get("/api/graph/{name}/gens/{gen}/measures")' in SERVER
    assert "off_loop(api.measure_gen" in SERVER, "`between` executes: never on the loop"
    assert "def cad_measure_gen(" in MCP and "measures=measures" in MCP
    assert "cad_measure_gen" in HELP and "between" in HELP and "#measures=0" in HELP
    assert "`measures`" in HELP[HELP.index("cad_notes"):]
    # the static preview answers the read and ships the snapping module
    assert "notes|tags|measures)$" in PAGES and '"measures.json"' in PAGES and '"measure.js"' in PAGES
    # the viewer: agent dimensions loaded, coloured by status, explained on tap
    assert "await loadMeasures(base);" in VIEW and "STATUS_INK[M.status]" in VIEW
    assert "const mz = measureAt(e.clientX, e.clientY);" in VIEW


def test_an_end_given_as_a_bare_point_is_read_as_one():
    # measures.json stores ends as [x,y,z]; Array.prototype.at made `e.at || e`
    # hand a FUNCTION to the vector maker, and every agent dimension vanished
    assert "V3(e.at || e)" not in VIEW and "Array.isArray(e) ? e : e.at" in VIEW


# --- phase 5: ✓ esatto — the browser's dimension redone on the frozen B-Rep ----

def test_a_piece_key_becomes_a_measure_reference():
    assert api._piece_ref("n8") == "n8"
    assert api._piece_ref("n8.2") == "n8[2]", "n8.2 would read as an output named 2"


def test_exact_asks_the_frozen_graph_for_the_same_features(store, monkeypatch):
    from cad_nodes import executor
    seen = {}

    def fake(graph, workdir, queries, timeout=120):
        seen["nodes"], seen["q"] = [n.id for n in graph.nodes], queries
        return {"success": True, "results": [{"op": "exact", "kind": "diameter", "value": 8.0,
                                              "a": [5, 5, 10], "found": ["circle"]}]}
    monkeypatch.setattr(executor, "measure_graph", fake)
    out = api.exact_measure(store, "demo", "g1", {"kind": "diameter", "value": 7.98,
        "a": {"at": [5, 5, 10], "snap": "circle_center", "piece": "n1.0",
              "circle": {"center": [5, 5, 10], "axis": [0, 0, 1], "r": 3.99}}})
    assert seen["nodes"] == ["n1"] and seen["q"][0]["op"] == "exact" and seen["q"][0]["nodes"] == ["n1[0]"]
    assert out == {"kind": "diameter", "value": 8.0, "a": [5, 5, 10], "found": ["circle"],
                   "exact": True, "delta": 0.02}


def test_exact_on_a_mesh_says_why(store, monkeypatch):
    from cad_nodes import executor
    monkeypatch.setattr(executor, "measure_graph", lambda *a, **k: {"success": True, "results": [
        {"op": "exact", "error": "ValueError: n1: no geometry (value is Mesh)"}]})
    with pytest.raises(ValueError, match="mesh"):
        api.exact_measure(store, "demo", "g1", {"kind": "distance",
            "a": {"at": [0, 0, 0], "piece": "n1"}, "b": {"at": [1, 0, 0], "piece": "n1"}})


def test_exact_query_is_validated_without_build123d():
    from cad_nodes.measure import refs_of
    assert refs_of({"op": "exact", "nodes": ["n1", "n2"], "measure": {}}) == ["n1", "n2"]
    with pytest.raises(ValueError):
        refs_of({"op": "exact", "nodes": [], "measure": {}})
    with pytest.raises(ValueError):
        refs_of({"op": "exact", "nodes": ["n1"]})


def test_the_viewer_offers_exact_and_the_server_answers_off_the_loop():
    assert '@app.post("/api/graph/{name}/gens/{gen}/measures/exact")' in SERVER
    assert "off_loop(api.exact_measure" in SERVER
    assert "async function verifyExact(M)" in VIEW and 'id="m-exact"' in VIEW
    assert "...(M.exact ? { exact: true } : {})" in VIEW, "a verified draft dimension says so in the note"
