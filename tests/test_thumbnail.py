"""Workflow thumbnails — the contract, across the four files that carry it.

The picture itself is verified by looking at it (that is the whole point of a
thumbnail). What a test can hold down is everything that fails SILENTLY:

  * the shot must come from the editor's own canvas, not from a second browser
    — the agent's screenshot API (cad_nodes/screenshot.py) re-executes the
      graph in a headless page, which is exactly the cost this feature avoids;
  * the agent's headless page must not upload thumbnails of its own, or every
    agent screenshot silently overwrites the user's picture with the agent's
    camera angle;
  * a thumbnail must never be taken from a viewport that does not match what
    was just saved, or the library lists a lie;
  * an empty viewport must leave the last good picture alone rather than
    replacing it with a black frame that reads as a bug.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = (ROOT / "server.py").read_text()
NODES = (ROOT / "webui" / "nodes.html").read_text()
VIEWER = (ROOT / "webui" / "viewer.js").read_text()
HOME = (ROOT / "webui" / "home.html").read_text()
SHOT = (ROOT / "cad_nodes" / "screenshot.py").read_text()


# --- the endpoint ----------------------------------------------------------
def test_the_thumbnail_is_uploaded_not_rendered_server_side():
    """It is a PUT, not a call into screenshot.render(): rendering it here
    would mean a second browser and a second execution of the graph."""
    assert '@app.put("/api/projects/{name}/thumb")' in SERVER
    assert '@app.get("/api/projects/{name}/thumb")' in SERVER
    route = SERVER.split('@app.put("/api/projects/{name}/thumb")')[1].split("\n@app.")[0]
    assert "_shot.render" not in route and "screenshot.render" not in route


def test_the_upload_is_guarded():
    route = SERVER.split('@app.put("/api/projects/{name}/thumb")')[1].split("\n@app.")[0]
    assert "require_project(name)" in route          # path traversal + existence
    assert "_THUMB_MAX_BYTES" in route               # not an unbounded write
    assert "\\xff\\xd8\\xff" in route                 # a JPEG, not a stray body


def test_the_write_is_atomic():
    """The library reads this file while the editor writes it."""
    route = SERVER.split('@app.put("/api/projects/{name}/thumb")')[1].split("\n@app.")[0]
    assert ".replace(" in route


def test_a_missing_thumbnail_is_a_404_not_a_black_png():
    route = SERVER.split('@app.get("/api/projects/{name}/thumb")')[1].split("\n@app.")[0]
    assert "HTTPException(404" in route


def test_the_listing_carries_the_mtime_as_a_cache_buster():
    """A bare bool would leave every card showing a stale picture forever."""
    listing = SERVER.split('@app.get("/api/projects")')[1].split("\n@app.")[0]
    assert "st_mtime" in listing and '"thumb"' in listing


# --- the capture -----------------------------------------------------------
def test_the_snapshot_reads_the_canvas_that_is_already_drawn():
    assert "snapshot({" in VIEWER
    snap = VIEWER.split("snapshot({")[1].split("\n  }")[0]
    assert "toDataURL('image/jpeg'" in snap


def test_the_render_sequence_is_not_duplicated():
    """A second copy of the bloom sequence would drift from the loop's."""
    assert VIEWER.count("_glowComposer.render()") == 1
    assert "_renderFrame(true)" in VIEWER            # the animate loop
    assert "_renderFrame(false)" in VIEWER           # the thumbnail, no nav gizmo


def test_the_snapshot_restores_the_camera():
    """It re-frames to fit the part; leaving that applied would yank the
    viewport out from under the user on every save."""
    snap = VIEWER.split("snapshot({")[1].split("\n  }")[0]
    assert "finally" in snap
    for restored in ("cam.position.copy(saved.pos)", "cam.zoom = saved.zoom",
                     "this.controls.target.copy(saved.tgt)",
                     "this.grid.visible = saved.grid",
                     "this.axes.visible = saved.axes"):
        assert restored in snap, restored


def test_an_empty_viewport_yields_no_thumbnail():
    """Nothing rendered must keep the last good picture, not blank it."""
    snap = VIEWER.split("snapshot({")[1].split("\n  }")[0]
    assert "return null" in snap
    assert "if (!url) return;" in NODES


# --- when it fires ---------------------------------------------------------
def test_the_shot_needs_the_viewport_to_match_what_was_saved():
    """The gate is not 'live mode' but something stricter: the geometry on
    screen was computed from the graph now on disk. In Live that is true the
    moment the run lands; outside it, a bare save shoots nothing."""
    assert "lastRunJSON !== lastSavedJSON) return;" in NODES
    body = NODES.split("window.runGraph = async function()")[1].split("\n};")[0]
    assert "lastRunJSON = lastSavedJSON;" in body    # set only on a good run


def test_opening_another_graph_disarms_the_shot():
    """The viewport still shows the graph you just left."""
    opened = NODES.split("window.openGraph = async function")[1].split("\n};")[0]
    assert "lastRunJSON = null;" in opened


def test_the_upload_is_debounced():
    """A burst of live runs is one upload, not one per run."""
    fn = NODES.split("function maybeThumb()")[1].split("\n}")[0]
    assert "clearTimeout(thumbTimer)" in fn and "setTimeout(postThumb" in fn


def test_a_failed_upload_never_breaks_the_save():
    fn = NODES.split("async function postThumb()")[1].split("\n}")[0]
    assert "catch" in fn


# --- the agent's headless page is not a user -------------------------------
def test_the_headless_page_is_flagged():
    assert "window.__noodleShot = true;" in SHOT


def test_the_editor_refuses_to_shoot_from_that_page():
    fn = NODES.split("function maybeThumb()")[1].split("\n}")[0]
    assert "if (window.__noodleShot) return;" in fn


# --- where it shows up -----------------------------------------------------
def test_the_library_shows_a_placeholder_when_there_is_none():
    """A never-rendered workflow says so; it does not show a black rectangle.
    Which makes the thumbnail a proof of execution, readable at a glance."""
    assert "thumb ph" in HOME and "Not rendered yet" in HOME
    assert "/thumb?v=" in HOME                       # cache-busted by mtime


def test_the_project_menu_shows_them_too():
    assert "pthumb" in NODES and "/thumb?v=" in NODES


def test_the_file_library_shows_them_too():
    """/library groups exported files by project — the picture says which part
    those files came out of."""
    lib = (ROOT / "webui" / "library.html").read_text()
    assert "pthumb" in lib and "/thumb?v=" in lib
    listing = SERVER.split('@app.get("/api/library")')[1].split("\n@app.")[0]
    assert '"thumb"' in listing
