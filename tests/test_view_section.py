"""✂ Sezione in /view (PLAN_VIEW_SECTION §1, phase 1) — what is pinned here
without a browser: ONE section state reached from two places, the plane per
piece (never renderer.clippingPlanes), the stencil the caps need, the hash keys,
the single raycast that must skip the cut side, and the `cut` a note carries.
The geometry (slice, closed test, parity) is tests/ui/section.test.cjs."""

import json
import re
from pathlib import Path

import pytest

from cad_nodes import api
from cad_nodes.graph import Graph
from cad_nodes.store import GraphStore

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "webui"
VIEW = (WEB / "view.html").read_text()
VIEWER = (WEB / "viewer.js").read_text()
SECTION = (WEB / "section.js").read_text()
UI = (WEB / "view-section.js").read_text()
CORE = (WEB / "section-core.js").read_text()

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


def _stroke():
    return [{"color": "#ef4444", "width": 0.5, "piece": "n1", "points": [[0, 0, 5], [5, 0, 5]]}]


def test_one_section_tool_replaces_the_placeholder():
    # the placeholder T0 registered is gone, and nothing registers a second one
    assert "disabled: true, title: '✂ Sezione" not in VIEW
    assert VIEW.count("registerTool(SECUI.tool)") == 1
    assert re.search(r"id: 'section', tab: 'tool', key: 'x'", UI)
    # the view bar's ✂ and the tool drive the SAME Section
    assert "$('b-frame').after(btn)" in UI and UI.count("new Section(") == 1
    assert "else if (k === 'x' && !drawing) SECUI.toggle();" in VIEW


def test_the_cut_is_per_piece_and_the_caps_have_a_stencil():
    assert "localClippingEnabled = true" in SECTION
    assert "renderer.clippingPlanes" not in SECTION.replace("never\n//    `renderer.clippingPlanes`", "")
    # set per draw on whatever material the piece wears (a restyle keeps the cut)
    assert "m.clippingPlanes = this.cuts(key(grp))" in SECTION
    assert "stencil: true" in VIEWER
    # caps per piece: the stencil is cleared after each one
    assert "clearStencil()" in SECTION and "NotEqualStencilFunc" in SECTION
    # an open shell / lines / points: cut, never capped
    assert "if (!shut.closed) return;" in SECTION
    # the contour waits while the timeline plays
    assert "SECUI.sec.setMoving(on)" in VIEW and "SECUI.sec.posed()" in VIEW
    # the hatch lives in the plane's own axes, in mm (not screen space)
    assert "uniform vec3 uAx;" in SECTION and "gl_FragCoord" not in SECTION


def test_the_hash_reads_and_writes_cut_cutflip_nocut():
    assert "ps.push(...SECUI.hashParts())" in VIEW
    assert "SECUI.readHash(hashParam)" in VIEW
    for k in ("'cut=' + formatCut", "'cutflip=1'", "'nocut=' + nc"):
        assert k in UI
    assert "param('cut')" in UI and "param('nocut')" in UI and "param('cutflip')" in UI


def test_the_one_raycast_skips_what_the_cut_took_away():
    fh = VIEW[VIEW.index("function firstHit("):VIEW.index("// ── selection in the 3D view")]
    assert "SECUI.sec.hides(" in fh and "SECUI.sec.capAt(" in fh
    # nobody else raycasts the pieces on their own (pen, measure, shapes, tags go through it)
    assert VIEW.count("ray.intersectObjects(viewer.previewGroup.children") == 1


def test_the_piece_rows_get_a_scissors_beside_eye_and_solo():
    assert VIEW.count("SECUI.decorate(") == 2
    assert "row.querySelector('.solo').before(sp)" in UI


def test_a_note_drawn_on_a_section_keeps_the_plane(store):
    note = api.add_note(store, "demo", "g1", {
        "text": "", "strokes": _stroke(),
        "cut": {"axis": "y", "pos": 2.5, "flip": True, "nocut": ["n1"]}})
    assert note["cut"] == {"axis": "y", "pos": 2.5, "flip": True, "nocut": ["n1"], "keeps": "y >= 2.5"}
    (seen,) = api.list_notes(store, "demo", "g1")
    assert seen["cut"]["keeps"] == "y >= 2.5"
    plain = api.add_note(store, "demo", "g1", {"text": "", "strokes": _stroke()})
    assert "cut" not in plain
    assert "const cut = SECUI.noteCut();" in VIEW


@pytest.mark.parametrize("cut", [{"axis": "w", "pos": 1}, {"axis": "z", "pos": "a"},
                                 {"axis": "z", "pos": 1, "nocut": "n1"}, "z:1"])
def test_a_bad_cut_is_refused(store, cut):
    with pytest.raises(ValueError, match="cut"):
        api.add_note(store, "demo", "g1", {"text": "", "strokes": _stroke(), "cut": cut})


def test_the_core_stays_pure():
    # tests/ui/section.test.cjs imports it in node: no three, no DOM
    assert "import" not in CORE
