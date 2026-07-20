"""Render the live viewport to a PNG — the agent's eyes.

Why this exists: numbers do not catch everything. The Thread node shipped with a
correct volume (1922mm3), a watertight mesh, exact major diameter and 237 green
tests — and no thread on the bolt at all, because the example wired a shank as
fat as the nominal diameter and the union filled every groove. Nothing in the
API could have reported that. A picture reported it immediately.

So the renderer is **the real viewer**, not a second one. This drives headless
Chromium over the actual `/nodes` page: same `viewer.js`, same materials,
finishes, selective bloom, same camera code. What the agent sees is what the
user sees — which is the entire point, and the reason a numpy rasterizer was
rejected: it would be a second renderer, free to drift from the first, and blind
to exactly the things (glass, emissive, rainbow, bloom) most recently worked on.

No GPU needed: verified pixel-identical under SwiftShader with `--disable-gpu`.

The browser is kept WARM, like the execution worker, because the cost is all in
the launch and none in the frame.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

# Camera presets, as (azimuth, elevation) in degrees. Azimuth is measured in the
# XY plane from +X; elevation from that plane. The scene is Z-up (CAD), so the
# poles are top/bottom rather than the graphics convention.
VIEWS = {
    # Front-RIGHT-top, the CAD convention, and the elevation is the true
    # isometric atan(1/sqrt2). This matters more than it looks: with a positive
    # azimuth the camera sits BEHIND anything modelled facing front, and every
    # default screenshot comes back with its lettering mirrored — which is
    # exactly what the first version did.
    "iso": (-45.0, 35.264),
    "front": (-90.0, 0.0),
    "back": (90.0, 0.0),
    "right": (0.0, 0.0),
    "left": (180.0, 0.0),
    "top": (-90.0, 89.9),          # not 90: straight down leaves `up` undefined
    "bottom": (-90.0, -89.9),
}

_BASE_URL = os.environ.get("NOODLE_BASE_URL", "http://127.0.0.1:8090")

# Launch flags. --no-sandbox because the container runs unprivileged as uid 1000;
# --disable-dev-shm-usage because Docker's default /dev/shm is 64MB and Chromium
# will crash on a large canvas; the SwiftShader pair because there is no GPU.
# Only the headless shell is installed (the full browser is 549MB we never open
# a window with), so the channel is named rather than left to playwright's
# implicit pick. It does WebGL2 through SwiftShader.
_CHANNEL = os.environ.get("NOODLE_BROWSER_CHANNEL", "chromium-headless-shell")

_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
         "--use-gl=swiftshader", "--enable-unsafe-swiftshader"]

_lock = asyncio.Lock()
_pw = None
_browser = None
_page = None
_page_key: tuple = ()          # (scale, hq) — a change means a fresh page


class ScreenshotUnavailable(RuntimeError):
    """Playwright or its browser is missing — a deployment problem, not a bug."""


async def _ensure_page(scale: int, hq: bool, width: int, height: int):
    global _pw, _browser, _page, _page_key
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:                       # pragma: no cover - deploy path
        raise ScreenshotUnavailable(
            "playwright is not installed — screenshots need it plus a Chromium "
            "build (`pip install playwright && playwright install chromium`)."
        ) from e

    if _browser is None or not _browser.is_connected():
        if _pw is None:
            _pw = await async_playwright().start()
        try:
            _browser = await _pw.chromium.launch(
                channel=_CHANNEL, args=_ARGS)
        except Exception as e:                     # pragma: no cover - deploy path
            raise ScreenshotUnavailable(
                "could not launch Chromium (%s). Run `playwright install "
                "chromium-headless-shell` in the image." % e) from e
        _page = None

    key = (scale, hq)
    if _page is None or _page.is_closed() or key != _page_key:
        if _page is not None and not _page.is_closed():
            await _page.close()
        _page = await _browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=scale)
        # The HQ path is read from localStorage at boot, so it has to be set
        # before the page script runs — not toggled afterwards.
        await _page.add_init_script(
            "localStorage.setItem('noodle:settings:hqRender', %r);"
            % ("1" if hq else "0"))
        _page_key = key
    else:
        await _page.set_viewport_size({"width": width, "height": height})
    return _page


# The camera work happens in the page because that is where the real viewer is:
# frame() already knows how to fit the shown geometry without moving the eye.
_CAMERA_JS = """
([azim, elev, zoom, nodeId, isolate]) => {
  const v = window._noodle.viewer, THREE = window._noodle.THREE;
  let framed = null;
  if (nodeId) {
    v.previewGroup.traverse(o => {
      if (!framed && o.userData && o.userData.nodeId === nodeId) framed = o;
    });
    if (!framed) return {error: 'no preview for node ' + nodeId};
    if (isolate) v.previewGroup.children.forEach(c => {
      c.visible = (c === framed || c.getObjectById(framed.id) != null);
    });
  }
  v.frame();
  const box = new THREE.Box3();
  box.setFromObject(framed || v.previewGroup);
  if (box.isEmpty()) return {error: 'nothing to frame'};
  const tgt = new THREE.Vector3(); box.getCenter(tgt);
  const size = new THREE.Vector3(); box.getSize(size);
  const md = Math.max(size.x, size.y, size.z) || 10;
  const d = md * 2.0 * zoom;
  const a = azim * Math.PI / 180, e = elev * Math.PI / 180;
  v.camera.up.set(0, 0, 1);                       // Z-up CAD scene
  v.controls.target.copy(tgt);
  v.camera.position.set(tgt.x + d * Math.cos(e) * Math.cos(a),
                        tgt.y + d * Math.cos(e) * Math.sin(a),
                        tgt.z + d * Math.sin(e));
  v.camera.lookAt(tgt);
  if (v.camera.isOrthographicCamera) { v.camera.zoom = 1 / zoom; }
  v.camera.updateProjectionMatrix();
  v.controls.update();
  return {size: [size.x, size.y, size.z]};
}
"""

_HIDE_CHROME = """
() => {
  const s = document.createElement('style');
  s.id = '__noodle_shot__';
  s.textContent = '.viewer-wrap > *:not(canvas){display:none !important}';
  document.head.appendChild(s);
}
"""


def plan(view: str = "iso", azim: Optional[float] = None,
         elev: Optional[float] = None, zoom: float = 1.0, width: int = 900,
         height: int = 700, scale: int = 2) -> dict:
    """Resolve and clamp the camera request. Pure, and separate from render()
    so the bounds are testable without a browser in the room: an agent picking
    width=100000 should get a clamped picture, not an out-of-memory browser."""
    if view and view not in VIEWS and (azim is None or elev is None):
        raise ValueError("unknown view %r — pick one of %s, or give azim+elev"
                         % (view, ", ".join(sorted(VIEWS))))
    a, e = VIEWS.get(view, VIEWS["iso"])
    if azim is not None:
        a = float(azim)
    if elev is not None:
        e = float(elev)
    return {"azim": a, "elev": e,
            "zoom": max(0.05, min(float(zoom), 20.0)),
            "width": max(64, min(int(width), 4000)),
            "height": max(64, min(int(height), 4000)),
            "scale": max(1, min(int(scale), 4))}


async def render(graph_id: str, *, view: str = "iso",
                 azim: Optional[float] = None, elev: Optional[float] = None,
                 zoom: float = 1.0, width: int = 900, height: int = 700,
                 projection: str = "", node: str = "", isolate: bool = False,
                 hq: bool = True, chrome: bool = False, run: bool = True,
                 scale: int = 2, base_url: str = "",
                 timeout: float = 90.0) -> tuple[bytes, dict]:
    """Screenshot `graph_id`'s viewport. Returns (png_bytes, meta).

    `meta` reports what actually happened — notably whether the graph was
    re-executed, since `run=False` falls back to running when the viewport has
    nothing on it rather than handing back an empty frame.
    """
    p = plan(view, azim, elev, zoom, width, height, scale)
    a, e, zoom = p["azim"], p["elev"], p["zoom"]
    width, height, scale = p["width"], p["height"], p["scale"]
    base = (base_url or _BASE_URL).rstrip("/")
    ms = int(timeout * 1000)

    async with _lock:                    # one shared page: shots are serialised
        page = await _ensure_page(scale, hq, width, height)
        url = f"{base}/nodes?p={graph_id}"
        if page.url.split("#")[0] != url:
            await page.goto(url, timeout=ms)
        await page.wait_for_function(
            "() => window._noodle && window._noodle.viewer && window._noodle.lgraph"
            " && window._noodle.lgraph._nodes.length > 0", timeout=ms)

        # openGraph is async: running before it has settled executes an EMPTY
        # graph, and then the wait below times out with nothing to explain it.
        await page.wait_for_timeout(400)

        ran = False
        have = await page.evaluate(
            "() => window._noodle.viewer.previewGroup.children.length")
        if run or not have:
            ran = True
            await page.evaluate("() => window.runGraph()")
            await page.wait_for_function(
                "() => window._noodle.viewer.previewGroup.children.length > 0",
                timeout=ms)
            await page.wait_for_timeout(250)

        if projection in ("persp", "ortho"):
            await page.evaluate("(m) => window._noodle.viewer.setProjection(m)",
                                projection)
        info = await page.evaluate(_CAMERA_JS, [a, e, zoom, node or None, isolate])
        if isinstance(info, dict) and info.get("error"):
            raise ValueError(info["error"])
        if not chrome:
            await page.evaluate(_HIDE_CHROME)
        await page.wait_for_timeout(200)           # let a frame land

        png = await page.locator("#viewer-canvas").screenshot()

        if not chrome:                             # leave the page as we found it
            await page.evaluate(
                "() => { const s = document.getElementById('__noodle_shot__');"
                " if (s) s.remove(); }")
        if isolate:
            await page.evaluate(
                "() => window._noodle.viewer.previewGroup.children"
                ".forEach(c => c.visible = true)")

    meta = {"graph": graph_id, "view": view, "azim": a, "elev": e, "zoom": zoom,
            "width": width, "height": height, "scale": scale, "ran": ran,
            "bytes": len(png)}
    if isinstance(info, dict) and info.get("size"):
        meta["size_mm"] = [round(v, 3) for v in info["size"]]
    return png, meta


async def shutdown() -> None:
    """Drop the warm browser (server shutdown, or to reclaim its ~100MB)."""
    global _pw, _browser, _page, _page_key
    try:
        if _browser is not None and _browser.is_connected():
            await _browser.close()
    finally:
        _browser = _page = None
        _page_key = ()
    if _pw is not None:
        try:
            await _pw.stop()
        finally:
            _pw = None
