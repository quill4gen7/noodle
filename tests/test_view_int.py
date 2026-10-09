"""Integration of PLAN_VIEW_TOOLS phase 2 (✂ A, 🔍 B, plates C, ⊞ D) — the
seams between the four, pinned as source facts (pure Python)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEWER = (ROOT / "webui/viewer.js").read_text()
PLATES = (ROOT / "webui/view-plates.js").read_text()


def test_a_frame_request_made_inside_a_frame_is_kept():
    # _tick clears the dirty flag BEFORE drawing, so an invalidate() from a hook
    # running inside the frame (the plates easing aside) asks for the next one
    tick = VIEWER[VIEWER.index("this._tick = () => {"):]
    tick = tick[:tick.index("\n    };")]
    assert tick.index("this._dirty = false;") < tick.index("this._renderFrame(true)")
    # …and the plates no longer need to step out of the frame to ask
    assert "queueMicrotask" not in PLATES
    assert "if (moving) viewer.invalidate();" in PLATES


SECTION = (ROOT / "webui/section.js").read_text()


def test_the_cut_reaches_what_aspetto_adds_after_bind():
    # a 🔍 proxy (fan-out piece with a look) and a ghost's edges are children
    # added AFTER bind: the hook runs every frame, per object, once
    assert "for (const [obj, Ls, keyOf] of this._objs || NONE) this._hook(obj, Ls, keyOf);" in SECTION
    assert "px.userData.lookProxy" in SECTION and "if (!o.material || o.userData._secPrev) return;" in SECTION
    # the proxy is what is drawn: its colour for the hatch, its visibility for the cap
    assert "function proxyOf(L)" in SECTION and "if (px) return px.visible;" in SECTION
    # a ghost's cap stays see-through, and in the opaque list (stencil order)
    assert "THREE.CustomBlending" in SECTION and "transparent: true" not in SECTION


def test_first_hit_is_one_function_for_ghost_and_cut():
    VIEW = (ROOT / "webui/view.html").read_text()
    fh = VIEW[VIEW.index("function hitOf(hits, skipGhost)"):]
    fh = fh[:fh.index("\n}\n")]
    assert "SECUI.sec.hides(" in fh and "SECUI.sec.capAt(ray.ray, seen)" in fh
    assert "if (skipGhost)" in fh
    assert "return hitOf(hits, true) || hitOf(hits, false);" in VIEW


PLANE = (ROOT / "webui/view-plane.js").read_text()


def test_the_work_plane_is_a_light_sheet_and_shows_where_it_cuts_the_part():
    # fainter, fading to the border, a tighter margin (it covered g64's whole view)
    assert "smoothstep(0.45, 1.0, d)" in PLANE and "max(0.025, r * 0.17) * fade" in PLANE
    assert "m = 0.06 * span + 3" in PLANE and "edge * 0.6" not in PLANE
    # the line where the plane meets the part: ✂'s CPU slice, once per plane,
    # never under a pressed pointer, and only what the section left
    assert "import { sliceTriangles } from '/static/section-core.js';" in PLANE
    assert "if (pressed) return cutLater();" in PLANE
    assert "m.clippingPlanes && m.clippingPlanes.length" in PLANE
    assert "SECTION_HOOK" not in PLANE


def test_both_row_icons_leave_the_name_room():
    VIEW = (ROOT / "webui/view.html").read_text()
    assert ".row .lk,.row .cut{width:24px;}" in VIEW and ".row .lk,.row .cut{width:36px;}" in VIEW
