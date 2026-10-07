"""Notes for the agent — strokes the user draws on a generation in /view.

What can go wrong silently, and is pinned here: a note that lands IN the
immutable gen (or anywhere but beside it), a stroke whose piece no longer says
which node it was drawn on, a "circle" the agent cannot tell from a line, a
done note that keeps being handed to the agent, and ids through which a path
could escape.
"""

import json
import math
from pathlib import Path

import pytest

from cad_nodes import api
from cad_nodes.graph import Graph
from cad_nodes.store import GraphStore, validate_note_id

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text()
MCP = (ROOT / "mcp_server.py").read_text()
VIEW = (ROOT / "webui" / "view.html").read_text()
HELP = (ROOT / "cad_nodes" / "AGENT_HELP.md").read_text()

BOX = {"id": "n1", "type": "Box", "title": "Body",
       "params": {"length": 10, "width": 10, "height": 10}}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 300


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path)
    s.save("demo", Graph.from_dict({"name": "demo", "nodes": [BOX], "connections": []}))
    (s.dir("demo") / "view.json").write_text(json.dumps({"previews": {"n1": {
        "kind": "Solid", "mesh": {"vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                                  "triangles": [[0, 1, 2]]}}}}))
    api.snapshot(s, "demo", run=False)
    return s


def _circle(cx=5, cy=5, z=10, r=2, n=24):
    return [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n), z]
            for i in range(n + 1)]


def test_a_note_sits_beside_the_gen_and_names_the_piece(store):
    gen_files = sorted(p.name for p in store.gen_dir("demo", "g1").iterdir())
    note = api.add_note(store, "demo", "g1", {
        "text": "questo foro si può fare meglio",
        "strokes": [{"color": "#ef4444", "width": 0.5, "piece": "n1", "points": _circle()},
                    {"color": "#3b82f6", "width": 0.5, "points": [[0, 0, 10], [8, 0, 10]]}],
        "camera": {"position": [30, -30, 30], "target": [5, 5, 5]}}, JPEG)
    assert note["id"] == "a1"
    red, blue = note["marks"]
    assert red["on"] == [{"node": "n1", "title": "Body", "type": "Box"}] and red["color_name"] == "red"
    assert note["strokes"][0]["node"] == "n1"
    assert red["shape"] == "loop" and blue["shape"] == "line"
    assert red["centre"] == pytest.approx([5, 5, 10], abs=0.2)
    # the gen's own files are untouched; the note is in notes/
    assert sorted(p.name for p in store.gen_dir("demo", "g1").iterdir()) == sorted(gen_files + ["notes"])
    assert (store.gen_dir("demo", "g1") / "notes" / "a1.jpg").read_bytes() == JPEG


def test_the_agent_gets_open_notes_newest_first_without_raw_points(store):
    api.add_note(store, "demo", "g1", {"text": "one", "strokes": []})
    api.add_note(store, "demo", "g1", {"text": "two", "strokes": [{"points": [[1, 2, 3]]}]})
    out = api.list_notes(store, base_url="http://h/")
    assert [n["text"] for n in out] == ["two", "one"]
    two = out[0]
    assert two["ref"] == "demo/g1#a2" and two["url"] == "http://h/view/demo/g1#note=a2"
    assert "strokes" not in two and two["marks"][0]["shape"] == "dot"
    assert api.list_notes(store, points=True)[0]["strokes"][0]["points"] == [[1, 2, 3]]
    api.resolve_note(store, "demo", "g1", "a2", reply="fatto, vedi g2")
    assert [n["id"] for n in api.list_notes(store)] == ["a1"]
    done = [n for n in api.list_notes(store, include_done=True) if n["id"] == "a2"][0]
    assert done["done"]["reply"] == "fatto, vedi g2"
    # the gallery counts OPEN notes only
    assert api.recent_gens(store)[0]["notes"] == 1


def test_a_circle_broken_by_the_hole_is_still_one_loop(store):
    """The pen lifts wherever the circle leaves the surface — over the hole it
    is drawn round — so one gesture arrives as several strokes. Read stroke by
    stroke that is three lines; the agent must still be told 'circled'."""
    c = _circle()
    note = api.add_note(store, "demo", "g1", {"strokes": [
        {"g": 1, "piece": "n1", "points": c[:9]}, {"g": 1, "piece": "n1", "points": c[11:20]},
        {"g": 1, "piece": "n1", "points": c[21:]}]})
    assert [s["shape"] for s in note["strokes"]] == ["line"] * 3
    (mark,) = note["marks"]
    assert mark["shape"] == "loop" and mark["on"] == [{"node": "n1", "title": "Body", "type": "Box"}]
    assert mark["centre"] == pytest.approx([5, 5, 10], abs=0.3)


def test_a_C_round_a_hole_is_a_loop_a_straight_line_and_an_S_are_not(store):
    c = _circle(n=40)
    note = api.add_note(store, "demo", "g1", {"strokes": [
        {"g": 1, "points": c[:31]},                                   # 270°: an open C
        {"g": 2, "points": [[i, 0, 0] for i in range(10)]},
        {"g": 3, "points": [[i, math.sin(i / 2), 0] for i in range(20)]}]})
    assert [m["shape"] for m in note["marks"]] == ["loop", "line", "line"]


def test_a_mark_drawn_from_another_angle_gets_its_own_picture(store):
    """The main picture is the LAST view. A cross drawn under the head from
    below is not in it: its mark must point at the picture of its own view."""
    V1 = b"\xff\xd8\xff\xe0" + b"1" * 300
    note = api.add_note(store, "demo", "g1", {
        "camera": {"position": [0, -50, 20], "target": [0, 0, 0], "aspect": 1.6},
        "views": [{"camera": {"position": [0, 0, -60], "target": [0, 0, 0], "aspect": 1.6}}],
        "strokes": [{"g": 1, "view": 0, "points": [[-3, 0, -16], [3, 0, -16]]},
                    {"g": 2, "points": [[8, 0, 0], [8, 3, 0]]}]}, JPEG, [V1])
    cross, side = note["marks"]
    assert cross["view"] == 1 and side["view"] == 0
    assert api.note_image(store, "demo", "g1", note["id"], mark=1) == V1
    assert api.note_image(store, "demo", "g1", note["id"], mark=2) == JPEG
    lean = api.list_notes(store)[0]
    assert lean["marks"][0]["image_path"].endswith(f"{note['id']}.v1.jpg")
    assert "image_path" not in lean["marks"][1] and lean["views"] == 1
    assert note["camera"]["aspect"] == 1.6
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"strokes": [{"view": 3, "points": [[0, 0, 0]]}]})
    store.delete_gen_note("demo", "g1", note["id"])
    assert not list((store.gen_dir("demo", "g1") / "notes").glob("*.jpg"))


def test_a_deleted_note_id_is_never_reused(store):
    a = api.add_note(store, "demo", "g1", {"text": "x"})
    store.delete_gen_note("demo", "g1", a["id"])
    assert api.add_note(store, "demo", "g1", {"text": "y"})["id"] == "a2"


def test_bad_notes_are_refused(store):
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"text": "", "strokes": []})
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"strokes": [{"points": [[0, 0, float("nan")]]}]})
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"strokes": [{"color": "red", "points": [[0, 0, 0]]}]})
    with pytest.raises(KeyError):
        api.add_note(store, "demo", "g9", {"text": "x"})
    for bad in ("../a1", "a0", "g1", "a1.json", ""):
        with pytest.raises(ValueError):
            validate_note_id(bad)


def test_routes_tools_and_help_exist():
    for r in ('@app.get("/api/notes")', '@app.post("/api/graph/{name}/gens/{gen}/notes")',
              '@app.get("/api/graph/{name}/gens/{gen}/notes/{note_id}.jpg")',
              '@app.patch("/api/graph/{name}/gens/{gen}/notes/{note_id}")'):
        assert r in SERVER
    # declared before the generic gens/{gen}/{part}, which would swallow `notes`
    assert SERVER.index('gens/{gen}/notes")') < SERVER.index('gens/{gen}/{part}")')
    for t in ("def cad_notes", "def cad_note_image", "def cad_note_done"):
        assert t in MCP
    assert "cad_notes" in HELP


def test_the_pen_paints_on_the_surface_and_leaves_the_background_to_orbit():
    # strokes come from a raycast on the part, not from screen coordinates, and a
    # press that misses the part is left to OrbitControls
    assert "function surfaceHit" in VIEW and "if (!hit && !onLabel) return;" in VIEW
    assert "vp.addEventListener('pointerdown'" in VIEW and "}, true);" in VIEW
    # the picture is the user's own view, not a re-framed one — and one more per
    # view the user drew from, taken when the pen lifts
    assert "snapshot({ frame: false" in VIEW
    assert "if (mine.length) { actions.push({ type: 'pen', g: p.g }); takeView(mine); }" in VIEW
    # a note opened on another screen backs off by the aspect ratio, then
    # until every stroke is inside the picture
    assert "c.aspect / now" in VIEW and "!inside()" in VIEW


def test_a_view_photo_never_shows_strokes_that_were_taken_back():
    """prova-disegno/g1#a1: ↶ removed «xBIG» from the model but not from the
    photo of its view, and an agent read the leftover «x» as "remove the hole".
    Undo re-shoots every view that lost something, from that view's own camera;
    a view left with nothing is dropped (null keeps the other indices valid)."""
    assert "function refreshViews" in VIEW and "function shootFrom" in VIEW
    undo = VIEW[VIEW.index("function undo()"):]
    assert "refreshViews(touched)" in undo[:undo.index("\n}\n")]
    assert "if (!left.length) { v.image = null; continue; }" in VIEW
    # a dropped view (image null) is not sent; its strokes fall back to the main picture
    assert "if (!v || !v.image || sameCam(v.cam, camera))" in VIEW


def test_the_eraser_takes_whole_draft_strokes_and_undo_gives_them_back():
    """⌫ hits in 3D: distance from the point on the PART to each stroke's
    polyline, radius from the size buttons in px → mm. ↶ is a stack of
    actions, so it restores what an erase removed, in the original order."""
    assert 'id="d-erase"' in VIEW
    erase = VIEW[VIEW.index("function eraseAt"):]
    erase = erase[:erase.index("\n}\n")]
    assert "strokeDist(s, hit.p) > r + s.width / 2" in erase and "mmPerPx(hit.p)" in erase
    assert "er.strokes.push(s)" in erase
    assert "else if (a.type === 'erase') { restore(a);" in VIEW
    assert "draft.sort((a, b) => a.k - b.k)" in VIEW
    # an erase drag is ONE action, pushed at pointer-up; a pinch undoes it on the spot
    assert "actions.push({ type: 'erase', ...p.erased })" in VIEW
    assert "if (p.mode === 'erase') restore(p.erased);" in VIEW
    # only the draft: saved notes are never touched by the eraser
    assert "notesData" not in erase


LABEL = {"text": "qui 8 mm", "at": [7.5, 5, 10], "normal": [0, 0, 2], "up": [0, 1, 0],
         "size_mm": [4, 1.5], "color": "#FFFFFF", "piece": "n1"}


def test_a_label_is_data_next_to_the_mark_it_talks_about(store):
    """Text written ON the part reaches the agent as text, tied to the marks it
    sits next to — a text-only model reads «qui 8 mm» about THAT circle."""
    note = api.add_note(store, "demo", "g1", {
        "strokes": [{"g": 1, "piece": "n1", "points": _circle()},           # mark 1: round (5,5)
                    {"g": 2, "points": [[40, 40, 10], [45, 40, 10]]}],     # mark 2: far away
        "labels": [LABEL, {**LABEL, "text": "lontano", "at": [100, 100, 10]}]})
    near, alone = note["labels"]
    assert near["text"] == "qui 8 mm" and near["near_marks"] == [1] and near["label"] == 1
    assert near["normal"] == [0, 0, 1]                   # normalised
    assert near["color"] == "#ffffff" and near["color_name"] == "white"
    assert near["node"] == "n1" and near["title"] == "Body"
    assert near["style"] == "tag"                        # absent = a plate (old notes too)
    assert alone["near_marks"] == []
    assert note["marks"][0]["labels"] == ["qui 8 mm"] and "labels" not in note["marks"][1]
    lean = api.list_notes(store)[0]
    assert lean["labels"][0]["near_marks"] == [1] and lean["marks"][0]["labels"] == ["qui 8 mm"]
    assert "strokes" not in lean


def test_a_note_of_labels_only_is_a_note(store):
    note = api.add_note(store, "demo", "g1", {"labels": [LABEL]})
    assert note["marks"] == [] and note["labels"][0]["near_marks"] == []
    assert api.list_notes(store)[0]["labels"][0]["text"] == "qui 8 mm"


def test_bad_labels_are_refused(store):
    bad = [{**LABEL, "text": "  "}, {**LABEL, "text": "x" * 201}, {**LABEL, "at": [0, 0, float("inf")]},
           {**LABEL, "normal": [0, 0, 0]}, {**LABEL, "size_mm": [1]}, {**LABEL, "color": "white"},
           {**LABEL, "view": 0}, {**LABEL, "style": "sticker"}, "qui"]
    for b in bad:
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"labels": [b]})
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"labels": [LABEL] * 61})


def test_the_text_tool_projects_a_decal_and_sends_labels():
    # three's DecalGeometry, vendored at the pinned version, imported via the map
    assert "from 'three/addons/geometries/DecalGeometry.js'" in VIEW
    assert (ROOT / "webui/vendor/three-0.170.0/examples/jsm/geometries/DecalGeometry.js").exists()
    assert 'id="d-texttool"' in VIEW
    # the normal is the surface under the WHOLE box, size measured on its plane
    new = VIEW[VIEW.index("function newLabel"):]
    new = new[:new.index("\n}\n")]
    assert "for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++)" in new and "planeSize(" in new
    # two styles, the user's choice, remembered; a decal that cannot be made
    # becomes a plate — never the one-sided flying card it used to be
    assert 'id="d-lstyle"' in VIEW and "localStorage.getItem('noodle:view:labelStyle')" in VIEW
    assert "if (mesh && !under.ridged)" in VIEW and "return tagObject(L); }" in VIEW
    assert "PlaneGeometry" not in VIEW
    # the plate faces the camera and shows through the part, faded
    tag = VIEW[VIEW.index("function tagObject"):]
    tag = tag[:tag.index("\n}\n")]
    assert "new THREE.Sprite(" in tag and "depthTest: !ghost" in tag and "for (const ghost of [false, true])" in tag
    assert "style: L.style" in VIEW and "style: l.style || 'tag'" in VIEW
    # labels are data in the note, and part of the undo / eraser / views machinery
    assert "labels: labels.map(L => ({ text: L.text" in VIEW
    for t in ("else if (a.type === 'label')", "else if (a.type === 'edit')", "er.labels.push(L)",
              "[...draft, ...labels].filter(x => x.view === i)"):
        assert t in VIEW
    # saved notes draw their labels too
    assert "for (const l of n.labels || [])" in VIEW
    assert "labels" in HELP[HELP.index("cad_notes"):] and "near_marks" in HELP
    assert "near_marks" in MCP
