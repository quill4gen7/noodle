"""Live browser/CAD smoke test using ONLY disposable projects.

Run against the container (already includes Playwright + Chromium):
    docker exec -i noodle python - < tests/ui/smoke.py
Or: NOODLE_URL=http://localhost:8090 python tests/ui/smoke.py
Does not read or modify existing user graphs; no benchmark fixtures required.
"""
import asyncio
import json
import os
import re
import urllib.request
import uuid

from playwright.async_api import async_playwright

BASE = os.environ.get('NOODLE_URL', 'http://localhost:8090')


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=150) as response:
        return json.loads(response.read())


async def main():
    names = ['ui-regression-' + uuid.uuid4().hex[:12] for _ in range(2)]
    errors = []
    try:
        for name in names:
            api('POST', '/api/graph/' + name, {
                'name': name, 'nodes': [{'id': 'box', 'type': 'Box',
                    'params': {'width': 10, 'height': 10, 'depth': 10}, 'position': [100, 100]}],
                'connections': []})
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            context = await browser.new_context(viewport={'width': 1440, 'height': 900})
            async def offline(route):
                if route.request.url.startswith(BASE):
                    await route.continue_()
                else:
                    await route.abort()  # boot must not depend on a CDN
            await context.route('**/*', offline)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(BASE + '/nodes?p=' + names[0])
            await page.wait_for_function("document.getElementById('status').textContent==='ready'")
            await page.evaluate('window.runGraph()')
            assert await page.evaluate('window._noodle.viewer.previewGroup.children.length') == 1
            geo = await page.evaluate('window._noodle.viewer.previewGroup.children[0].geometry.uuid')
            await page.evaluate('window.runGraph()')
            assert await page.evaluate('window._noodle.viewer.previewGroup.children[0].geometry.uuid') == geo
            await page.evaluate('''async name=>{
              const view=await (await fetch('/api/graph/'+name+'/view')).json();
              window._noodle.viewer.renderPreviews(view.previews,{colorOf:()=> '#ff0000'});
            }''', names[0])
            assert await page.evaluate('window._noodle.viewer.previewGroup.children[0].geometry.uuid') == geo
            assert await page.evaluate('window._noodle.viewer.previewGroup.children[0].material.color.getHexString()') == 'ff0000'
            # Real autosave with Live off, not just a fake clock.
            await page.evaluate('''()=>{const n=window._noodle.lgraph._nodes[0];
                n.properties.width=12;for(const w of n.widgets) if(w.cadParam==='width') w.value=12;
                window._noodle.paramChange(n);}''')
            await page.wait_for_function("document.getElementById('doc-status').classList.contains('dirty')")
            await page.wait_for_function("document.getElementById('doc-status').classList.contains('saved')", timeout=10000)
            saved = api('GET', '/api/graph/' + names[0])
            assert saved['nodes'][0]['params']['width'] == 12
            # Rendering settles fully, but an external camera mutation wakes it.
            await page.wait_for_timeout(1000)
            await page.evaluate('''()=>{window.draws=0;const v=window._noodle.viewer;
                const render=v._renderFrame.bind(v);v._renderFrame=(...args)=>{window.draws++;render(...args);};}''')
            await page.wait_for_timeout(500)
            assert await page.evaluate('window.draws') == 0, 'viewport keeps rendering while idle'
            await page.evaluate('window._noodle.viewer.camera.position.x+=10;window._noodle.viewer.controls.update()')
            await page.wait_for_timeout(200)
            assert await page.evaluate('window.draws') > 0
            # A late response from A must never paint B.
            held, release = asyncio.Event(), asyncio.Event()
            async def delay_run(route):
                response = await route.fetch()
                held.set()
                await release.wait()
                try:
                    await route.fulfill(response=response)
                except Exception:
                    pass  # switching project aborts the browser request
            await page.route('**/execute?*', delay_run)
            await page.evaluate('window.runGraph(); void 0')
            await asyncio.wait_for(held.wait(), timeout=30)
            await page.evaluate('name=>window.openGraph(name)', names[1])
            release.set()
            await page.wait_for_timeout(500)
            assert await page.evaluate('window._noodle.viewer.previewGroup.children.length') == 0
            await page.unroute('**/execute?*', delay_run)
            # Save error must leave a draft and never start execution.
            async def fail_save(route):
                if route.request.method == 'POST':
                    await route.fulfill(status=500, content_type='application/json', body='{"detail":"disk full"}')
                else:
                    await route.continue_()
            # the save carries ?base_version=… (live sync), so match the query too
            await page.route(re.compile(r'.*/api/graph/' + re.escape(names[1]) + r'(\?.*)?$'), fail_save)
            executes = []
            page.on('request', lambda req: executes.append(req.url) if '/execute?' in req.url else None)
            await page.evaluate('''()=>{const n=window._noodle.lgraph._nodes[0];n.properties.width=13;
                for(const w of n.widgets) if(w.cadParam==='width') w.value=13;
                window._noodle.paramChange(n);}''')
            await page.evaluate('window.runGraph()')
            assert not executes
            assert 'Save failed' in await page.locator('#status').inner_text()
            assert await page.evaluate('name=>!!localStorage.getItem("noodle:draft:"+name)', names[1])
            await page.wait_for_timeout(1200)
            assert 'failed' in await page.locator('#doc-status').get_attribute('class')
            # Keyboard modal: focus trapped and returned to invoking button.
            await page.locator('#btn-project').focus()
            await page.evaluate('window.promptNewGraph(); void 0')
            await page.wait_for_selector('[role="dialog"]')
            await page.keyboard.press('Escape')
            assert await page.locator('[role="dialog"]').count() == 0
            assert await page.evaluate('document.activeElement.id') == 'btn-project'
            assert not errors, errors
            await browser.close()
        print('PASS: offline boot, real CAD runs, GPU reuse, autosave, idle rendering, stale responses, save failure, modal focus')
    finally:
        for name in names:
            try:
                api('DELETE', '/api/projects/' + name)
            except Exception:
                pass


if __name__ == '__main__':
    asyncio.run(main())
