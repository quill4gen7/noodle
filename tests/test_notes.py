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
    new = VIEW[VIEW.index("function makeLabel"):]
    new = new[:new.index("\n}\n")]
    assert "for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++)" in new and "planeSize(" in new
    # two styles, the user's choice, remembered; a decal that cannot be made
    # becomes a plate — never the one-sided flying card it used to be
    assert 'id="d-lstyle"' in VIEW and "localStorage.getItem('noodle:view:labelStyle')" in VIEW
    assert "if (mesh && !under.ridged)" in VIEW and "mat.map.dispose(); mat.dispose(); L.surface = 'tag';" in VIEW
    assert "PlaneGeometry" not in VIEW
    # the plate faces the camera and shows through the part, faded
    tag = VIEW[VIEW.index("function tagObject"):]
    tag = tag[:tag.index("\n}\n")]
    assert "new THREE.Sprite(" in tag and "depthTest: !ghost" in tag and "for (const ghost of [false, true])" in tag
    assert "style: L.style" in VIEW and "style: l.style || 'tag'" in VIEW
    # labels are data in the note, and part of the undo / eraser / views machinery
    assert "labels: words.map(L => ({ text: L.text" in VIEW
    for t in ("else if (a.type === 'label')", "else if (a.type === 'edit')", "er.labels.push(L)",
              "[...draft, ...labels, ...dmeasures, ...shapes].filter(x => x.view === i)"):
        assert t in VIEW
    # saved notes draw their labels too
    assert "for (const l of n.labels || [])" in VIEW
    assert "labels" in HELP[HELP.index("cad_notes"):] and "near_marks" in HELP
    assert "near_marks" in MCP


def test_a_decal_label_keeps_the_surface_it_was_fitted_to(store):
    """▭ decal on a regular surface is laid on a FITTED patch; the fit travels
    with the label so the viewer redraws a saved note without refitting."""
    cyl = {**LABEL, "style": "decal", "surface": "cylinder",
           "fit": {"centre": [0, 0, 0], "radius": 7.0, "convex": 1, "axis": [0, 0, 2]}}
    note = api.add_note(store, "demo", "g1", {"labels": [cyl, {**LABEL, "style": "decal"}, LABEL]})
    c, d, t = note["labels"]
    assert c["surface"] == "cylinder" and c["fit"]["axis"] == [0, 0, 1] and c["fit"]["radius"] == 7.0
    assert d["surface"] == "decal" and "fit" not in d          # absent: what the style implies
    assert t["surface"] == "tag"
    for bad in ({**cyl, "fit": None}, {**cyl, "fit": {**cyl["fit"], "axis": [0, 0, 0]}},
                {**cyl, "fit": {**cyl["fit"], "radius": -1}}, {**cyl, "surface": "torus"},
                {**cyl, "surface": "sphere", "fit": {"centre": [0, 0, float("nan")], "radius": 3}}):
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"labels": [bad]})


def test_regular_surfaces_are_fitted_and_mapped_without_projection():
    fit = VIEW[VIEW.index("function fitSurface"):]
    fit = fit[:fit.index("\n}\n")]
    # plane → cylinder (axis from the normals, RANSAC over pairs, radius from the
    # POINTS: the mesh has flat facets) → sphere only if normals turn round two axes
    for t in ("surface: 'plane'", "minEigvec(N)", "N[i].clone().cross(N[j])", "// Kåsa",
              "ax && ax.lam > 0.01 ?", "return { surface: 'decal' }"):
        assert t in fit, t
    assert "const ARC_MAX = 150 * Math.PI / 180" in VIEW
    patch = VIEW[VIEW.index("function patchGeometry"):]
    patch = patch[:patch.index("\n}\n")]
    assert "uv.push(U, V)" in patch and "Math.cos(al)" in patch     # arc length round the axis
    assert "surface: L.surface || null, fit: fitOut(L.fit)" in VIEW and "fit: fitIn(l.fit)" in VIEW


def test_the_agent_tags_the_pieces_of_a_gen_beside_it(store):
    """«coperchio v2» pinned on a piece: tags.json beside the gen (the gen's
    own files never change), node by id or exact title, `at` optional."""
    gen_files = sorted(p.name for p in store.gen_dir("demo", "g1").iterdir())
    out = api.tag_gen(store, "demo", "g1", [{"text": "corpo v2", "node": "Body"},
                                            {"text": "qui", "node": "n1", "at": [1, 2, 3], "color": "#22D3EE"}])
    a, b = out["tags"]
    assert a == {"text": "corpo v2", "node": "n1", "title": "Body", "tag": 1}
    assert b["at"] == [1, 2, 3] and b["color"] == "#22d3ee" and b["tag"] == 2
    assert out["ref"] == "demo/g1"
    assert api.gen_tags(store, "demo", "g1") == out["tags"]
    assert sorted(p.name for p in store.gen_dir("demo", "g1").iterdir()) == sorted(gen_files + ["tags.json"])
    api.tag_gen(store, "demo", "g1", [{"text": "altro", "node": "n1"}], replace=False)
    assert [t["tag"] for t in api.gen_tags(store, "demo", "g1")] == [1, 2, 3]
    assert len(api.tag_gen(store, "demo", "g1", [{"text": "solo", "node": "n1"}])["tags"]) == 1


def test_bad_tags_are_refused_and_name_the_pieces(store):
    with pytest.raises(ValueError, match=r"n1 \(Body\)"):
        api.tag_gen(store, "demo", "g1", [{"text": "x", "node": "Lid"}])
    for bad in ({"text": "", "node": "n1"}, {"text": "x" * 121, "node": "n1"},
                {"text": "x", "node": "n1", "at": [0, float("nan"), 0]},
                {"text": "x", "node": "n1", "at": [0, 0]}, {"text": "x", "node": "n1", "color": "cyan"}):
        with pytest.raises(ValueError):
            api.tag_gen(store, "demo", "g1", [bad])
    with pytest.raises(ValueError):
        api.tag_gen(store, "demo", "g1", [{"text": "x", "node": "n1"}] * 41)
    with pytest.raises(KeyError):
        api.tag_gen(store, "demo", "g9", [])


def test_snapshot_tags_in_one_call_and_a_bad_tag_does_not_fail_it(store):
    out = api.snapshot(store, "demo", run=False, tags=[{"text": "corpo", "node": "n1"}])
    assert out["tags"][0]["node"] == "n1" and api.gen_tags(store, "demo", out["gen"])[0]["text"] == "corpo"
    out = api.snapshot(store, "demo", run=False, tags=[{"text": "x", "node": "nope"}])
    assert "tags_error" in out and out["gen"]


def test_tag_routes_tool_help_and_viewer():
    assert '@app.get("/api/graph/{name}/gens/{gen}/tags")' in SERVER
    assert SERVER.index('gens/{gen}/tags")') < SERVER.index('gens/{gen}/{part}")')
    assert "def cad_tag_gen" in MCP and "tags=tags" in MCP
    assert "cad_tag_gen" in HELP
    # drawn as plates in the agent's look, hideable (#tags=0), tap = select the piece
    assert "const AGENT_INK" in VIEW and "T.obj = tagObject(T, tex, t.color || AGENT_INK)" in VIEW
    assert "ps.push('tags=0')" in VIEW and "hashParam('tags') === '0'" in VIEW
    assert "if (tg) { select(tg.key); return; }" in VIEW and "function tagBadge" in VIEW


def test_painted_text_is_data_and_its_letters_are_not_marks(store):
    """✎ Vernice: the letters are pen strokes (`label` → kind "text"). The agent
    reads the WORDS in `labels`; forty «line» marks for one word would bury
    the one circle the user actually drew."""
    letters = [{"g": 7, "label": 0, "points": [[5 + i, 9, 10], [5 + i, 10, 10]]} for i in range(6)]
    note = api.add_note(store, "demo", "g1", {
        "strokes": [{"g": 1, "piece": "n1", "points": _circle()}, *letters],
        "labels": [{**LABEL, "style": "paint", "text": "foro 8"}]})
    assert [s.get("kind") for s in note["strokes"]] == [None] + ["text"] * 6
    assert note["strokes"][1]["label"] == 1
    (mark,) = note["marks"]
    assert mark["shape"] == "loop" and mark["labels"] == ["foro 8"]
    lb = note["labels"][0]
    assert lb["style"] == "paint" and lb["surface"] == "paint" and lb["near_marks"] == [1]
    assert len(api.list_notes(store)[0]["marks"]) == 1
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"strokes": [{"label": 3, "points": [[0, 0, 0]]}],
                                           "labels": [{**LABEL, "style": "paint"}]})


def test_paint_text_goes_through_the_pens_raycast():
    # a single-stroke font, public domain, with its provenance
    assert "Hershey Roman Simplex" in VIEW and "const HERSHEY = [" in VIEW
    paint = VIEW[VIEW.index("function paintText"):]
    paint = paint[:paint.index("\n}\n")]
    # every sample is the pen's raycast; broken off the surface and on depth jumps
    assert "surfaceHit(sx, sy)" in paint and "off the part: the line breaks" in paint
    assert "label: L" in paint and "draft.push(cur)" in paint
    # default style, remembered; ↶ and ⌫ treat the text as one gesture
    assert "let labelStyle = 'paint'" in VIEW and "er.strokes.push(...takeLabelStrokes(L))" in VIEW
    assert "label: words.indexOf(s.label)" in VIEW


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def test_a_picture_placed_on_the_part_is_an_asset_of_the_note(store):
    """Img: the picture travels beside the note (aK.img1.png), checked by its
    magic bytes; the note says where it lies, like a label."""
    im = {"at": [5, 5, 10], "normal": [0, 0, 1], "up": [0, 1, 0], "size_mm": [4, 2], "piece": "n1",
          "surface": "plane"}
    note = api.add_note(store, "demo", "g1", {"images": [im, {**im, "at": [6, 6, 10]}]}, None, None,
                        [PNG, JPEG])
    a, b = note["images"]
    assert a["file"] == f"{note['id']}.img1.png" and b["file"] == f"{note['id']}.img2.jpg"
    assert a["node"] == "n1" and a["surface"] == "plane" and "text" not in a and "color" not in a
    assert api.note_asset(store, "demo", "g1", note["id"], 1) == (PNG, "image/png")
    lean = api.list_notes(store)[0]
    assert lean["images"][0]["image_path"].endswith(".img1.png") and "/img/1" in lean["images"][0]["image_url"]
    store.delete_gen_note("demo", "g1", note["id"])
    assert not list((store.gen_dir("demo", "g1") / "notes").glob("*.img*"))


def test_bad_placed_pictures_are_refused(store):
    im = {"at": [5, 5, 10], "normal": [0, 0, 1], "up": [0, 1, 0], "size_mm": [4, 2]}
    for blobs in ([b"GIF89a" + b"0" * 100], [None], [], [PNG + b"0" * (4 * 1024 * 1024)]):
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"images": [im]}, None, None, blobs)
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"images": [{**im, "normal": [0, 0, 0]}]}, None, None, [PNG])
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"images": [im] * 9}, None, None, [PNG] * 9)


def test_the_image_tool_and_its_route():
    assert 'id="d-imgfile" accept="image/*"' in VIEW and "const IMG_MAX = 1600" in VIEW
    assert "images: pictures.map(L => ({ data: L.img.data" in VIEW
    assert '@app.get("/api/graph/{name}/gens/{gen}/notes/{note_id}/img/{k}")' in SERVER
    assert "images" in HELP[HELP.index("cad_notes"):] and "image_path" in MCP


MEASURE = {"kind": "distance", "value": 12.4, "approx": False,
           "a": {"at": [0, 0, 10], "normal": [0, 0, 1], "piece": "n1"},
           "b": {"at": [12.4, 0, 10], "piece": "n1", "snap": "vertex"}}


def test_a_dimension_is_data_in_the_note(store):
    # 📏 the browser measures; the note keeps what and where, named by piece
    note = api.add_note(store, "demo", "g1", {"measures": [MEASURE, {
        "kind": "diameter", "value": 8.0, "approx": True,
        "a": {"at": [5, 5, 10], "snap": "circle_center",
              "circle": {"center": [5, 5, 10], "axis": [0, 0, 1], "r": 4}}}]})
    d, o = note["measures"]
    assert d["value"] == 12.4 and d["unit"] == "mm" and d["approx"] is False
    assert d["a"]["snap"] == "free" and d["b"]["snap"] == "vertex"
    assert d["a"]["node"] == "n1" and d["a"]["title"] == "Body"
    assert o["kind"] == "diameter" and "b" not in o and o["a"]["circle"]["r"] == 4
    # a note of dimensions alone is a note
    assert api.gen_notes_raw(store, "demo", "g1")[0]["measures"][0]["value"] == 12.4


def test_an_axis_locked_dimension_says_which_axis(store):
    note = api.add_note(store, "demo", "g1", {"measures": [{**MEASURE, "axis": "z"}]})
    assert note["measures"][0]["axis"] == "z"


def test_bad_dimensions_are_refused(store):
    bad = [{**MEASURE, "kind": "volume"}, {**MEASURE, "value": -1}, {**MEASURE, "value": float("nan")},
           {**MEASURE, "b": None}, {**MEASURE, "a": {"at": [0, 0]}}, {**MEASURE, "axis": "w"},
           {**MEASURE, "a": {**MEASURE["a"], "snap": "magnet"}}, {**MEASURE, "view": 0},
           {**MEASURE, "text": "x" * 201}, "12 mm"]
    for b in bad:
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"measures": [b]})
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"measures": [MEASURE] * 51})


def test_the_measure_tool_is_part_of_the_draft():
    assert 'id="d-measure"' in VIEW and 'id="d-mmode"' in VIEW
    # a dimension rides the undo / eraser / views machinery like a stroke
    for t in ("actions.push({ type: 'measure', M })", "else if (a.type === 'measure')",
              "er.measures.push(M)", "measures: dmeasures.map(M =>"):
        assert t in VIEW
    # the tool never captures the pointer: a drag on the part still orbits
    assert "if (tool === 'hand' || tool === 'measure' || tool === 'shape' || e.button !== 0" in VIEW
    # saved notes draw their dimensions
    assert "for (const m of n.measures || [])" in VIEW
    # the snaps come from webui/measure.js (tested in tests/ui/measure.test.cjs)
    assert "import * as MS from '/static/measure.js';" in VIEW
    assert (ROOT / "webui" / "measure.js").exists()
    for mode in ("auto", "p2p", "edge", "hole", "faces"):
        assert f'<option value="{mode}">' in VIEW


SHAPE = {"kind": "cylinder", "size": [6, 6, 10], "center": [5, 5, 15], "quat": [0, 0, 0, 1],
         "anchor": [5, 5, 10], "normal": [0, 0, 1], "color": "#ef4444", "piece": "n1"}


def test_a_shape_is_data_in_the_note(store):
    # ▣ Forme: «a Ø 6 pin here» — kind, size, centre, axis, the piece it sits on
    note = api.add_note(store, "demo", "g1", {"shapes": [SHAPE, {**SHAPE, "kind": "box",
        "quat": [0.2588, 0, 0, 0.9659], "size": [4, 2, 3]}]})
    c, b = note["shapes"]
    assert c["axis"] == [0, 0, 1] and c["node"] == "n1" and c["title"] == "Body"
    assert b["axis"] == pytest.approx([0, -0.5, 0.866], abs=1e-3), "the box was turned 30° about x"
    n, = api.list_notes(store)
    assert n["shapes"][0]["summary"] == "cylinder Ø 6,00 × 10,00 mm centred at (5.00, 5.00, 15.00), axis (0.00, 0.00, 1.00), on Body"


def test_bad_shapes_are_refused(store):
    bad = [{**SHAPE, "kind": "cone"}, {**SHAPE, "size": [6, 0, 10]}, {**SHAPE, "quat": [0, 0, 0, 0]},
           {**SHAPE, "quat": [0, 0, 1]}, {**SHAPE, "center": [1, 2]}, {**SHAPE, "color": "red"},
           {**SHAPE, "view": 0}, "a pin"]
    for b in bad:
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"shapes": [b]})
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"shapes": [SHAPE] * 31})


def test_the_shape_tool_stays_on_the_parts():
    assert 'id="d-shape"' in VIEW and 'id="d-shapekind"' in VIEW
    # moving is NOT free: the shape follows a surface hit, or does not move
    move = VIEW[VIEW.index("if (d.h.type === 'move') {"):]
    move = move[:move.index("} else if")]
    assert "stickAt(e.clientX, e.clientY)" in move and "if (!hit) return;" in move
    # a corner never passes the opposite one, and the minimum follows the PIECE
    assert "Math.max(l.x * s0.x, min)" in VIEW and "function shapeMin(S)" in VIEW
    assert "const box = pieceBox(S);" in VIEW[VIEW.index("function shapeMin(S)"):][:200]
    assert "leafIndex.get(S.piece)" in VIEW[VIEW.index("function pieceBox(S)"):][:200]
    # the marks shrink with the shape; the grab volume does not
    assert "hs = Math.min(grab, 0.1 * side)" in VIEW
    # a shape rides the draft like a stroke
    for t in ("actions.push({ type: 'shape', S, before: null })", "else if (a.type === 'shape')",
              "er.shapes.push(S)", "shapes: shapes.map(S =>", "for (const sh of n.shapes || [])"):
        assert t in VIEW
    assert "`shapes`" in HELP[HELP.index("cad_notes"):] and "Basic SHAPES" in MCP


def test_the_draw_tools_wrap_on_a_phone():
    # 604px of tools in a 390px screen scrolled the whole viewer sideways
    assert "#dbar .grp{flex-wrap:wrap;}" in VIEW
    assert "#vp[data-tool=shape] #d-shapekind{display:inline-block;}" in VIEW


def test_shapes_have_move_arrows_on_a_leash():
    # the classic X/Y/Z arrows — but the shape may not wander off the part:
    # its centre stays in the piece's box grown by a quarter of the piece
    assert "type: 'arrow'" in VIEW and "function shapeLeash(S)" in VIEW
    assert "expandByScalar(Math.max(0.25 * L" in VIEW
    arrow = VIEW[VIEW.index("} else if (d.h.type === 'arrow') {"):]
    arrow = arrow[:arrow.index("} else if (d.h.type === 'center')")]
    assert "k = Math.min(Math.max(k, lo), hi);" in arrow


def test_two_edges_are_measured_side_to_side():
    # lato–lato: Auto on two edges, and a mode of its own
    assert '<option value="edges">' in VIEW and "'edges'" in VIEW
    assert "o.polyline = q.polyline;" in VIEW
    # a silhouette edge is taken even when the pointer grazes just off the part
    assert "offPart && !['vertex', 'edge', 'circle_center'].includes(f.snap)" in VIEW
    M = (ROOT / "cad_nodes" / "measure.py").read_text()
    assert 'if "edge" in A and "edge" in B:' in M      # exact: the two edges' closest points


def test_shapes_are_ruled_in_millimetres_in_their_colour():
    # graph paper in the shape's colour, in the shape's OWN mm (local position
    # × size), 1 / 5 / 10 mm rules, the 1 mm one fading where it gets too dense
    assert "function mmGridMaterial(color, size)" in VIEW
    # the REST position/normal: on a bent shape the paper bends with the body
    assert "vGP = restPos * uSize; vGN = restNrm;" in VIEW
    for rule in ("mmRule(uv, 1.0,", "mmRule(uv, 5.0,", "mmRule(uv, 10.0,"):
        assert rule in VIEW
    assert "smoothstep(0.12, 0.33, dense)" in VIEW
    assert "const body = new THREE.Mesh(geo, mmGridMaterial(S.color, S.size));" in VIEW


def test_shapes_wear_one_set_of_handles_at_a_time():
    # quill: buttons to pick move / rotate / the cage / no gizmo, instead of
    # the arrows and the rings being there always, together
    for m in ("move", "rotate", "cage", "none"):
        assert f'data-gm="{m}"' in VIEW
    assert "const GIZMO_MODES = ['move', 'rotate', 'cage', 'none'];" in VIEW
    assert "let gizmoMode = 'cage', cageMode = 'scale';" in VIEW
    for key in ("noodle:view:gizmoMode", "noodle:view:cageMode"):
        assert f"localStorage.getItem('{key}')" in VIEW and f"localStorage.setItem('{key}'" in VIEW
    # refreshCage builds ONLY the active mode's handles, one branch per mode
    rc = VIEW[VIEW.index("function refreshCage() {"):]
    rc = rc[:rc.index("\n}\n")]
    assert "switch (gizmoMode) {" in rc
    assert "case 'move': cageArrows(G); break;" in rc
    assert "case 'rotate': cageRings(G); break;" in rc
    assert "if (cageMode === 'scale') { cageCorners(G); cageCentre(G);" in rc
    assert "case 'none': break;" in rc
    # 'deform' is task C's: until it lands the toggle is there and disabled
    assert "const CAGE_MODES = ['scale'];" in VIEW and "cm.disabled = CAGE_MODES.length < 2;" in VIEW
    # a second tap on Gabbia flips Scala ⇄ Deforma
    assert "if (m === 'cage' && gizmoMode === 'cage') return toggleCageMode();" in VIEW
    # G / R / C, only with the shape tool; no H (it already hides the piece)
    assert "setGizmoMode({ g: 'move', r: 'rotate', c: 'cage' }[k])" in VIEW
    # on a phone the modes are one row that never widens the page
    assert "#s-modes{display:flex;gap:4px;flex-basis:100%;justify-content:center;flex-wrap:nowrap;}" in VIEW
    assert "max-width:calc(100vw - 16px)" in VIEW
    # the tap that sets a shape down opens the bar under the finger on a phone:
    # its compatibility click must not press the button that just appeared
    assert "const sBarGhost = () => performance.now() - sBarOpened < 400;" in VIEW
    assert "if (!sBarGhost()) setGizmoMode(b.dataset.gm);" in VIEW
    assert "if (selShape && !sBarGhost()) deleteShape(selShape);" in VIEW


def test_scale_mode_has_six_face_handles_in_the_axis_colours():
    # task D: in Gabbia → Scala the six face centres stretch ONE axis each,
    # ±X red / ±Y green / ±Z blue; corners stay white, the centre uniform
    rc = VIEW[VIEW.index("function refreshCage() {"):]
    rc = rc[:rc.index("\n}\n")]
    assert "if (cageMode === 'scale') { cageCorners(G); cageCentre(G); cageFaces(G); }" in rc
    assert "const RING_INK = ['#ef4444', '#22c55e', '#3b82f6'];" in VIEW
    fc = VIEW[VIEW.index("function cageFaces({ S, q, grab, hs, half }) {"):]
    fc = fc[:fc.index("\n}\n")]
    assert "for (let a = 0; a < 3; a++) {" in fc and "for (const sg of [-1, 1]) {" in fc
    assert "const handle = { type: 'face', axis: a, s };" in fc
    assert "handleMat(RING_INK[a])" in fc
    assert "handleMat('#ffffff')" in VIEW[VIEW.index("function cageCorners("):]  # corners stay white
    # a face sits ON the body: it is grabbed in its whole radius there
    assert "h.px > (h.type === 'face' ? 1 : 0.5) * grabPx" in VIEW
    # the drag: along the face's outward axis, opposite face fixed, signed min
    assert "if (h && h.type === 'face') {" in VIEW
    br = VIEW[VIEW.index("} else if (d.h.type === 'face') {"):]
    br = br[:br.index("} else if (d.h.type === 'arrow') {")]
    assert "const L = Math.max(d.size0[c] + (t - d.t0), min);" in br
    assert "min = shapeMin(S)" in br
    assert "if (S.kind === 'sphere') sz.set(L, L, L);" in br
    assert "if (S.kind === 'cylinder' && a < 2) sz.x = sz.y = L;" in br
    assert "const center = d.fixed.clone().addScaledVector(d.axis, L / 2);" in br


DEFORMED = [[0, 0, 0]] * 7 + [[0.25, -0.1, 0.3]]


def test_a_deformed_shape_carries_its_cage(store):
    # ▣ Deforma: the 8 corner offsets AND the 8 corners in the world, so a
    # text-only reader sees the bent shape without interpolating
    corners = [[i, i + 0.5, 2 * i] for i in range(8)]
    note = api.add_note(store, "demo", "g1", {"shapes": [
        {**SHAPE, "ffd": DEFORMED, "corners": corners},
        {**SHAPE, "ffd": [[0, 0, 0]] * 8, "corners": corners},      # all zero = not deformed
        SHAPE]})
    bent, flat, plain = note["shapes"]
    assert bent["ffd"] == DEFORMED and bent["corners"] == corners
    assert "ffd" not in flat and "corners" not in flat and "ffd" not in plain
    s = [sh["summary"] for sh in api.list_notes(store)[0]["shapes"]]
    assert "deformed" in s[0] and "deformed" not in s[1] and "deformed" not in s[2]
    assert s[0].startswith("cylinder Ø 6,00 × 10,00 mm, deformed by its cage")


def test_bad_deformations_are_refused(store):
    corners = [[0, 0, 0]] * 8
    bad = [{**SHAPE, "ffd": DEFORMED[:7]}, {**SHAPE, "ffd": [[0, 0]] * 8},
           {**SHAPE, "ffd": [[0, 0, 0]] * 7 + [[10, 0, 0]]}, {**SHAPE, "ffd": [[0, 0, 0]] * 7 + [[0, -12.5, 0]]},
           {**SHAPE, "ffd": [[0, 0, 0]] * 7 + [[0, float("nan"), 0]]}, {**SHAPE, "ffd": "bent"},
           {**SHAPE, "ffd": DEFORMED, "corners": corners[:7]},
           {**SHAPE, "ffd": DEFORMED, "corners": [[0, 0]] * 8},
           {**SHAPE, "ffd": DEFORMED, "corners": [[0, 0, float("inf")]] * 8}]
    for b in bad:
        with pytest.raises(ValueError):
            api.add_note(store, "demo", "g1", {"shapes": [b]})


def test_the_cage_deforms():
    # C: Gabbia → Deforma is on, its own branch in refreshCage and pointermove
    assert "CAGE_MODES.push('deform');" in VIEW
    assert VIEW.index("CAGE_MODES.push('deform');") < VIEW.index("localStorage.getItem('noodle:view:cageMode')")
    rc = VIEW[VIEW.index("function refreshCage() {"):]
    rc = rc[:rc.index("\n}\n")]
    assert "else if (cageMode === 'deform') { cageDeformCorners(G); }" in rc
    assert "import * as FFD from '/static/ffd.js';" in VIEW
    # the pure math lives in webui/ffd.js (tests/ui/ffd.test.cjs) and ships to the static pages
    assert (ROOT / "webui" / "ffd.js").exists()
    assert '"ffd.js"' in (ROOT / "scripts" / "build_pages.py").read_text()
    # amber corners, a COPY of the geometry bent and its normals recomputed
    fn = VIEW[VIEW.index("function cageDeformCorners("):]
    fn = fn[:fn.index("\n}\n")]
    assert "type: 'ffd'" in fn and "handleMat(DEFORM_INK)" in fn and "const DEFORM_INK = '#f59e0b';" in VIEW
    geo = VIEW[VIEW.index("function shapeGeoOf(S)"):]
    geo = geo[:geo.index("\n}\n")]
    for t in ("geo.setAttribute('restPos', pos.clone());", "FFD.deformPositions(pos.array, S.ffd)", "geo.computeVertexNormals();"):
        assert t in geo
    assert "const g = new THREE.Group(), geo = shapeGeoOf(S);" in VIEW
    # the drag: on the view plane, into the shape's normalised frame, constrained
    br = VIEW[VIEW.index("} else if (d.h.type === 'ffd') {"):]
    br = br[:br.index("} else if", 1)]
    assert "onViewPlane(e.clientX, e.clientY, d.grab)" in br
    assert ".applyQuaternion(d.q0.clone().invert()).divide(S.size)" in br
    assert "FFD.moveCorner(S.ffd, d.h.i," in br and "shapeMin(S)" in br
    # the cage lines follow the moved corners
    assert "FFD.CAGE_EDGES.flatMap" in VIEW[VIEW.index("function cageOutline("):][:600]
    # undo carries the deformation; the note says it; eraser + leash see the bent box
    assert "ffd: FFD.copyFfd(S.ffd) }" in VIEW[VIEW.index("function shapeState(S)"):].split("\n")[0]
    assert "S.ffd = FFD.copyFfd(st.ffd);" in VIEW[VIEW.index("function setShapeState(S, st)"):].split("\n")[0]
    assert "o.ffd = S.ffd.map(d => d.map(r4)); o.corners = shapeCorners(S, q).map(v3);" in VIEW
    for f in ("function shapeContains(S, p, r)", "function shapeLeash(S)"):
        assert "shapeExtent(S)" in VIEW[VIEW.index(f):][:500]
    assert "`ffd`" in HELP and "`corners`" in HELP and "`ffd`" in MCP and "`corners`" in MCP


def test_a_pen_colour_recolours_the_selected_shape():
    # with a shape in hand a colour paints THE SHAPE (and the pen), instead of
    # switching tool; without one it stays the pen's colour as before
    fn = VIEW[VIEW.index("function recolorShape(c, merge)"):]
    fn = fn[:fn.index("\n}\n")]
    assert "if (!S) return false;" in fn and "pen.color = c;" in fn
    assert "S.color = c; drawShape(S);" in fn
    assert "actions.push({ type: 'shape', S, before: shapeState(S), recolor: merge })" in fn
    assert "if (recolorShape(c, false)) return;" in VIEW
    assert "if (recolorShape(e.target.value, true)) return;" in VIEW
    # undo puts the old colour back: the shape's state carries it
    st = VIEW[VIEW.index("function shapeState(S)"):].split("\n")[0]
    assert "color: S.color" in st
    assert "if (st.color) S.color = st.color;" in VIEW[VIEW.index("function setShapeState(S, st)"):].split("\n")[0]
    # …and the note says it
    assert "const o = { kind: S.kind, color: S.color," in VIEW


def test_shapes_have_standard_sizes():
    # quill: «bottoncini per misure standard 10mm 1mm e 50% del volume del
    # pezzo originale» — 50% of the piece the shape SITS ON
    for sp, label in (("1", "1mm"), ("10", "10mm"), ("half", "½ vol")):
        assert f'data-sp="{sp}"' in VIEW and f">{label}</button>" in VIEW
    # the shape's own volume, from its size fields, per kind
    fn = VIEW[VIEW.index("function shapeVolume(S)"):]
    fn = fn[:fn.index("\n}\n")]
    assert "if (S.kind === 'sphere') return Math.PI / 6 * s.x * s.y * s.z;" in fn
    assert "if (S.kind === 'cylinder') return Math.PI / 4 * s.x * s.y * s.z;" in fn
    assert "return s.x * s.y * s.z;" in fn
    # the piece's: the gen's own volume (a body's, or a fanned part's triangles)
    pv = VIEW[VIEW.index("function pieceVolume(key)"):]
    pv = pv[:pv.index("\n}\n")]
    assert "pv.bodies[i].volume" in pv and "meshVolume(g, gr.start, gr.count)" in pv and "v = pv.volume;" in pv
    assert "genPreviews = view.previews || {};" in VIEW
    # uniform scale to half, one undo step, re-shot views, through the ghost guard
    fn = VIEW[VIEW.index("function setShapePreset(S, p)"):]
    fn = fn[:fn.index("\n}\n")]
    assert "Math.cbrt(0.5 * vp / vs)" in fn
    assert "new THREE.Vector3(+p, +p, +p)" in fn
    assert "actions.push({ type: 'shape', S, before: shapeState(S) });" in fn
    assert "refreshViews(viewsOf([S])); takeView([S]);" in fn
    assert "if (selShape && !sBarGhost() && !b.disabled) setShapePreset(selShape, b.dataset.sp);" in VIEW
    # no volume known → the button says why instead of doing nothing
    assert "b.disabled = v == null;" in VIEW and "syncSizePresets(S);" in VIEW
    # a drag never snaps the 1 mm preset back up: the minimum is capped at 1 mm
    assert "return Math.min(Math.max(0.02 * L, 1e-3), SHAPE_MM_FLOOR);" in VIEW
    assert "const SHAPE_MM_FLOOR = 1;" in VIEW


def test_the_shape_features_work_together():
    # E (integration, found in the browser): a drag that ends where it began —
    # a body slid off the parts, a handle held at its minimum — is NOT an undo
    # step (↶ used to spend one press on nothing)
    end = VIEW[VIEW.index("const endShapeDrag = e => {"):]
    end = end[:end.index("\n};\n")]
    assert "if (d.moved && !sameShapeState(d.before, shapeState(d.S))) {" in end
    fn = VIEW[VIEW.index("function sameShapeState(a, b)"):]
    fn = fn[:fn.index("\n}\n")]
    for f in ("a.anchor.equals(b.anchor)", "a.rot.equals(b.rot)", "a.size.equals(b.size)", "a.color === b.color",
              "JSON.stringify(a.ffd) === JSON.stringify(b.ffd)"):
        assert f in fn
    # «· deformato» is a span of its own: words on a desktop, an amber ≈ on a
    # phone, and the phone bar takes its whole width — at 390 px the size row
    # (cubo 10,00 × 10,00 × 10,00 + presets + Togli + ✓) used to wrap
    assert "className: 's-def', textContent: ' · deformato'" in VIEW
    phone = VIEW[VIEW.index("#s-dims{font-size:11px;}"):][:400]
    assert "#s-dims .s-def{font-size:0;" in phone and "content:'≈'" in phone
    assert "#s-bar{width:max-content;}" in VIEW
