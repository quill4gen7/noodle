"""Look at a REAL project in the real editor, without writing it.

Why this exists (raccordo, 2026-10-05): an agent verified slider fixes in a
headless page on COPIES of the project, on other nodes, with JS newer than the
user's open tab, and reported "it works" while the user saw something else on
node n48. This probe removes the three gaps:

  * same project, same node — it opens `/nodes?p=<project>&readonly=1`, so the
    page is the user's graph from disk (`_ui` slider windows included) and can
    never save, draft, thumbnail or run (see READ_ONLY in webui/nodes.html);
  * same code — it prints the build the page runs and the server's current one
    (`/api/system/ui-build`); the user's tab shows the build in its log pane and
    sends it as `ui=` on every /version poll, so `docker logs noodle | grep
    'version?ui='` says which build the user is on;
  * the editor's VIEW, not the file — for each slider of the node it prints the
    window and label the canvas actually draws, next to the value on disk.

Run INSIDE the container (it has Chromium):
    docker exec -i noodle python - raccordo n48 < scripts/editor_probe.py 2>/dev/null
Optional third argument: base URL (default http://localhost:8090).
Requests go out with User-Agent "HeadlessChrome", logged as client=headless.
"""
import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "raccordo"
NODE = sys.argv[2] if len(sys.argv) > 2 else None
BASE = sys.argv[3] if len(sys.argv) > 3 else "http://localhost:8090"

READ_NODE = """(nid) => {
  const N = window._noodle, S = window.__noodleSync;
  const n = N.lgraph._nodes.find(n => S.graphIdOf(n) === nid);
  if (!n) return null;
  // the window is resolved at DRAW time: bring the node on screen and draw it
  N.lcanvas.ds.scale = 1; N.lcanvas.ds.offset = [200 - n.pos[0], 200 - n.pos[1]];
  N.lcanvas.draw(true, true);
  const sliders = (n.widgets || []).filter(w => w.type === 'cadslider').map(w => {
    const ui = n.properties._ui && n.properties._ui[w.uiKey];
    return {param: w.cadParam || w.name, value: w.value, prop: n.properties[w.cadParam || w.name],
            ui: ui || null, win: w._win || null};
  });
  return {id: nid, type: n.cadType, title: n.title, sliders,
          ui: n.properties._ui || null};
}"""


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.loads(r.read())


async def main():
    disk = _get(f"/api/graph/{PROJECT}/version?graph=1")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = await (await b.new_context(viewport={"width": 1600, "height": 1000})).new_page()
        writes = []
        page.on("request", lambda r: writes.append(f"{r.method} {r.url}")
                if r.method not in ("GET", "HEAD") else None)
        errs, refused = [], []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: refused.append(m.text) if "[readonly] refused" in m.text else None)
        await page.goto(f"{BASE}/nodes?p={PROJECT}&readonly=1")
        await page.wait_for_function("document.getElementById('status').textContent==='ready'",
                                     timeout=60000)
        build = await page.evaluate("() => window._noodle.build")
        ro = await page.evaluate("() => window._noodle.readOnly")
        try:
            server_build = _get("/api/system/ui-build")["build"]
        except Exception:
            server_build = "(server without /api/system/ui-build: restart needed)"
        print(f"project {PROJECT} · disk version {disk['version']}")
        print(f"page build {build} · server build {server_build} · readonly={ro}")
        ids = [NODE] if NODE else [n["id"] for n in disk["graph"]["nodes"]]
        by_id = {n["id"]: n for n in disk["graph"]["nodes"]}
        for nid in ids:
            got = await page.evaluate(READ_NODE, nid)
            if not got:
                print(f"{nid}: NOT on the canvas")
                continue
            if NODE is None and not got["sliders"]:
                continue
            dp = by_id.get(nid, {}).get("params", {})
            print(f"{nid} {got['type']} title={got['title']!r} _ui(disk)={dp.get('_ui')} _ui(canvas)={got['ui']}")
            for s in got["sliders"]:
                win = s["win"] or {}
                src = "⚙ custom" if s["ui"] and s["ui"].get("min") is not None else "auto"
                print(f"   {s['param']:>10}: canvas {s['value']!r:>10}  disk {dp.get(s['param'])!r:>10}  "
                      f"window {win.get('lo')}…{win.get('hi')} ({src})")
        after = _get(f"/api/graph/{PROJECT}/version")["version"]
        print(f"writes sent by the page: {writes or 'none'} · refused by readonly: {refused or 'none'}")
        print(f"disk version after: {after} ({'unchanged' if after == disk['version'] else 'CHANGED'})")
        if errs:
            print("page errors:", errs)
        await b.close()


asyncio.run(main())
