"""Screenshot — camera planning, the bounds, and the wiring across surfaces.

Pure-Python: no browser. What matters here is the part that runs BEFORE
Chromium is touched (an agent asking for a 100000px canvas must get a clamped
picture, not an out-of-memory browser) and the fact that the same operation is
reachable from HTTP and MCP rather than implemented twice.

The rendering itself is verified by looking at the picture — which is the whole
point of the feature, and not something a test can assert.
"""

import inspect
from pathlib import Path

import pytest

from cad_nodes import api, screenshot

ROOT = Path(__file__).resolve().parent.parent


# --- camera planning -------------------------------------------------------
def test_the_presets_cover_the_six_faces_and_iso():
    assert set(screenshot.VIEWS) == {"iso", "front", "back", "left", "right",
                                     "top", "bottom"}


def test_top_and_bottom_stop_short_of_the_pole():
    """Straight down leaves the up-vector undefined and the camera rolls to an
    arbitrary heading — so 'top' is 89.9 degrees, not 90."""
    for name in ("top", "bottom"):
        _, elev = screenshot.VIEWS[name]
        assert 89.0 < abs(elev) < 90.0


def test_a_preset_resolves_to_its_angles():
    p = screenshot.plan("front")
    assert (p["azim"], p["elev"]) == screenshot.VIEWS["front"]


def test_explicit_angles_beat_the_preset():
    p = screenshot.plan("front", azim=12.0, elev=34.0)
    assert (p["azim"], p["elev"]) == (12.0, 34.0)


def test_an_unknown_view_names_the_alternatives():
    with pytest.raises(ValueError) as e:
        screenshot.plan("sideways")
    for name in screenshot.VIEWS:
        assert name in str(e.value)


def test_an_unknown_view_is_fine_when_the_angles_are_given():
    """The preset is a convenience, not a gate."""
    p = screenshot.plan("", azim=0.0, elev=0.0)
    assert p["azim"] == 0.0


@pytest.mark.parametrize("kw,key,expect", [
    ({"width": 10 ** 6}, "width", 4000),
    ({"width": 1}, "width", 64),
    ({"height": 10 ** 6}, "height", 4000),
    ({"scale": 99}, "scale", 4),
    ({"scale": 0}, "scale", 1),
    ({"zoom": 10 ** 4}, "zoom", 20.0),
    ({"zoom": 0.0}, "zoom", 0.05),
])
def test_the_request_is_clamped_before_a_browser_sees_it(kw, key, expect):
    assert screenshot.plan(**kw)[key] == expect


# --- the page contract -----------------------------------------------------
def test_the_camera_is_placed_z_up():
    """The CAD scene is Z-up; three's default is Y-up, and getting this wrong
    silently tips every screenshot on its side."""
    assert "camera.up.set(0, 0, 1)" in screenshot._CAMERA_JS


def test_it_waits_for_the_graph_before_running_it():
    """openGraph is async. Running before it settles executes an EMPTY graph and
    the wait for previews then times out with nothing to explain it."""
    src = inspect.getsource(screenshot.render)
    assert src.index("_nodes.length > 0") < src.index("window.runGraph()")


def test_swiftshader_is_requested_because_there_is_no_gpu():
    assert "--use-gl=swiftshader" in screenshot._ARGS
    assert "--no-sandbox" in screenshot._ARGS          # unprivileged uid 1000
    assert "--disable-dev-shm-usage" in screenshot._ARGS   # 64MB /dev/shm


def test_run_false_still_renders_something():
    """Reusing the viewport is an optimisation; handing back an empty frame
    would be a lie — and so would handing back a frame of the PREVIOUS graph."""
    src = inspect.getsource(screenshot.render)
    assert "if run or not have or stale:" in src


def test_an_edited_graph_is_never_shot_stale():
    """The warm page only re-navigates when the URL changes, so without this an
    agent that edits a graph and shoots it with run=0 is handed the geometry from
    before the edit, silently. The graph.json mtime is what notices."""
    src = inspect.getsource(screenshot.render)
    assert "_graph_mtime(graph_id)" in src
    assert "stale" in src
    # Re-read in place rather than reload: the editor guards `beforeunload`
    # while the doc is dirty and a navigation stalls on it.
    assert "window.openGraph" in src


def test_unknown_mtime_counts_as_stale():
    """A project whose file cannot be stat'd must reload, not silently reuse."""
    assert screenshot._graph_mtime("no-such-project-here") < 0


# --- one operation, three surfaces ----------------------------------------
def test_the_api_op_is_a_coroutine():
    """The only async op in api.py — it drives a browser, not the kernel."""
    assert inspect.iscoroutinefunction(api.screenshot)


def test_the_api_op_rejects_an_unknown_project_before_rendering():
    src = inspect.getsource(api.screenshot)
    assert src.index("store.load(graph_id)") < src.index("_shot.render")


def test_the_http_route_exists_and_takes_the_camera_args():
    src = (ROOT / "server.py").read_text()
    assert '@app.get("/api/graph/{name}/screenshot")' in src
    route = src.split('@app.get("/api/graph/{name}/screenshot")')[1].split("\n@app.")[0]
    for arg in ("view", "azim", "elev", "zoom", "width", "height",
                "projection", "node", "isolate", "hq", "chrome", "run", "scale"):
        assert f"{arg}:" in route, arg
    assert 'media_type="image/png"' in route


def test_the_http_route_reports_whether_it_ran():
    src = (ROOT / "server.py").read_text()
    assert "X-Noodle-Ran" in src


def test_a_missing_browser_is_a_503_not_a_500():
    """Playwright absent is a deployment problem; saying 500 sends an agent
    hunting for a bug in its graph."""
    src = (ROOT / "server.py").read_text()
    assert "except ScreenshotUnavailable as e:" in src
    assert "HTTPException(503" in src


def test_mcp_exposes_it_as_an_image():
    src = (ROOT / "mcp_server.py").read_text()
    assert "async def cad_screenshot(" in src
    assert "Image(data=png, format=\"png\")" in src


# --- deployment drift ------------------------------------------------------
def test_playwright_is_pinned_and_chromium_is_installed():
    reqs = (ROOT / "requirements.txt").read_text()
    assert "playwright==" in reqs
    docker = (ROOT / "Dockerfile").read_text()
    assert "playwright install chromium-headless-shell" in docker
    assert "PLAYWRIGHT_BROWSERS_PATH=/opt/playwright" in docker


def test_with_deps_is_not_used():
    """`playwright install --with-deps` resolves an UBUNTU package set and dies
    on Debian with 'ttf-ubuntu-font-family has no installation candidate',
    taking the browser install down with it. The libs are listed by hand."""
    docker = (ROOT / "Dockerfile").read_text()
    commands = [ln for ln in docker.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("--with-deps" in ln for ln in commands)
    assert "fonts-liberation" in docker          # or every label renders blank


def test_only_the_headless_shell_is_installed():
    """`playwright install chromium` fetches the full browser AND the shell —
    549MB + 309MB — and noodle never opens a window. The shell does WebGL2 on
    SwiftShader, so the full browser is 549MB of nothing."""
    docker = (ROOT / "Dockerfile").read_text()
    assert "chromium-headless-shell" in docker
    assert screenshot._CHANNEL == "chromium-headless-shell"


def test_the_browser_is_readable_by_the_unprivileged_user():
    docker = (ROOT / "Dockerfile").read_text()
    assert "chmod -R a+rX /opt/playwright" in docker


def test_iso_looks_from_the_front_not_the_back():
    """A positive azimuth puts the camera behind anything modelled facing front,
    and every default shot comes back with its lettering mirrored. That is what
    the first version did, and a picture is how it was caught."""
    azim, elev = screenshot.VIEWS["iso"]
    assert -90.0 < azim < 0.0
    assert 20.0 < elev < 50.0


def test_the_shot_page_never_writes_the_graph():
    """The agent's page loads the real editor, which saves before every run — so a
    shot taken with a stale in-memory graph wrote that stale copy over the file on
    disk. Taking a picture must never destroy the thing being pictured. /execute
    runs the graph ON DISK, so skipping the save is also more correct."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "webui" / "nodes.html").read_text()
    assert "if (!window.__noodleShot) await window.saveGraph();" in src
    # and the flag is still stamped before the page loads
    assert "window.__noodleShot = true;" in inspect.getsource(screenshot)
