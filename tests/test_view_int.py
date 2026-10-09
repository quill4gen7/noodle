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
