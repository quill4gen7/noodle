"""🖥 Schermo / ∿ Curva (webui/view-screen.js): strokes on a glass in front of
the part. A Curva stroke also keeps the points the spline goes through, so the
viewer can take it up again — the server must keep them, and refuse junk."""
from pathlib import Path

import pytest

from cad_nodes import api
from test_view_plane import store, PLANE_Z3  # noqa: F401 — the fixture

ROOT = Path(__file__).resolve().parent.parent
VIEW = (ROOT / "webui" / "view.html").read_text()
SCREEN_JS = (ROOT / "webui" / "view-screen.js").read_text()


def _curve_stroke(**curve):
    pts = [[50 + i, 3, 3] for i in range(5)]
    return {"color": "#ef4444", "width": 0.5, "g": 1, "points": pts,
            "normals": [[0, 0, 1]] * len(pts), "plane": PLANE_Z3, "curve": curve}


def test_a_curve_keeps_its_points_and_whether_it_is_closed(store):
    note = api.add_note(store, "demo", "g1", {"strokes": [
        _curve_stroke(points=[[50, 3, 3], [52.123456, 4, 3], [54, 3, 3]], closed=True)]})
    s = note["strokes"][0]
    assert s["curve"] == {"points": [[50, 3, 3], [52.1235, 4, 3], [54, 3, 3]], "closed": True}
    assert note["marks"][0]["kind"] == "plane"          # still an ordinary plane mark
    # and it survives the round trip the viewer resumes from
    again = [n for n in api.gen_notes_raw(store, "demo", "g1") if n["id"] == note["id"]][0]
    assert again["strokes"][0]["curve"]["closed"] is True


def test_a_stroke_without_a_curve_has_none(store):
    st = _curve_stroke(); del st["curve"]
    note = api.add_note(store, "demo", "g1", {"strokes": [st]})
    assert "curve" not in note["strokes"][0]


@pytest.mark.parametrize("bad", [[[0, 0, 0]], {"points": []}, {"points": [[0, 0]]},
                                 {"points": [[0, 0, 0]] * 501}, {"points": "x"}])
def test_bad_curves_are_refused(store, bad):
    st = _curve_stroke(); st["curve"] = bad
    with pytest.raises(ValueError):
        api.add_note(store, "demo", "g1", {"strokes": [st]})


def test_the_viewer_saves_and_resumes_the_curve_and_its_reshape():
    # sent with the note, read back on resume, kept in the undo history
    assert "curve: curveOut(s.curve)" in VIEW and "curveIn(s.curve)" in VIEW
    assert "a.type === 'reshape'" in VIEW and "geomIn(r.before)" in VIEW
    # the tool takes a saved curve up again and records ONE reshape step
    assert "function reopen(s)" in SCREEN_JS and "type: 'reshape'" in SCREEN_JS
