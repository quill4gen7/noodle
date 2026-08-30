"""Reactivity: does the editor keep ANSWERING while it works?

Speed is how long a run takes. Reactivity is whether the app stays alive during
it — the canvas keeps drawing, the glow keeps moving, a click still registers.
An app that is merely slow tells you it is working; an app that stops answering
looks broken, and that is the complaint these tests are about.
"""
from __future__ import annotations

import statistics
import time

import pytest

from .conftest import budget, open_graph, rtt_ms, wait_ready

pytestmark = pytest.mark.perf

OPEN_BUDGET_MS = 20000


def test_main_thread_stays_responsive_during_a_run(page, any_graph):
    """Sample the main thread while a graph executes.

    The run itself happens on the server, so the browser should be nearly idle —
    any long block here is the editor's own doing (a synchronous rebuild, a
    monster JSON parse) and it is what swallows clicks mid-run.
    """
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    page.evaluate("() => { window.__done = false; window.runGraph().finally(()=>{ window.__done = true; }); }")

    samples = []
    deadline = time.time() + 90
    while time.time() < deadline:
        samples.append(rtt_ms(page))
        if page.evaluate("() => window.__done"):
            break
        time.sleep(0.1)

    assert len(samples) >= 3, "the run finished too fast to sample"
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 < budget(400), (
        f"main thread p95 {p95:.0f}ms (worst {max(samples):.0f}ms) during a run of {any_graph} "
        f"— the UI stops answering while it works"
    )


def test_canvas_keeps_drawing_during_a_run(page, any_graph):
    """The glow is an animation. If rAF stalls, a 'running' node stops breathing
    and the user cannot tell a working app from a hung one."""
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    page.evaluate("() => { window.__done = false; window.__probe.frames.length = 0; "
                  "window.runGraph().finally(()=>{ window.__done = true; }); }")
    time.sleep(3)
    frames = page.evaluate("() => window.__probe.frames.slice()")
    if len(frames) < 10:
        pytest.skip("not enough frames sampled (run too short)")
    worst = max(frames)
    assert worst < budget(1000), f"the canvas stalled for {worst:.0f}ms during a run"
    med = statistics.median(frames)
    assert med < budget(100), f"median frame time {med:.0f}ms during a run (< 10fps)"


def test_a_second_run_still_glows(page, any_graph):
    """Supersede a run and the NEXT one must still light up.

    `runGraph` aborts whatever is in flight; the aborted run's rejection handler
    then calls `closeProgress` → `endExecGlow`, which mutates the module-global
    `execGlow` — by then the *new* run's object. Live mode supersedes a run on
    every 120ms debounce tick, so if this is broken the glow dies precisely when
    the user is working fastest.
    """
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    page.evaluate("() => window.runGraph()")     # first, deliberately not awaited
    time.sleep(0.2)
    page.evaluate("() => { window.__done = false; window.runGraph().finally(()=>{ window.__done=true; }); }")

    glowed = False
    deadline = time.time() + 90
    while time.time() < deadline:
        if page.evaluate("() => { const g = window._noodle.execGlow; return !!(g && g.byId && g.byId.size); }"):
            glowed = True
            break
        if page.evaluate("() => window.__done"):
            break
        time.sleep(0.1)

    assert glowed, (
        "the run that superseded another produced no glow at all — the aborted "
        "run's endExecGlow() cleared the new run's execGlow"
    )


def test_every_run_glows_in_the_editor(page, any_graph):
    """Five runs in a row, IN THE BROWSER, must each light their nodes.

    The backend tests in test_progress_truth.py all passed while this was broken:
    the server produced every event correctly and the editor threw them away. The
    client dispatches its progress GET up to ~90ms after the POST and takes
    another ~90ms to connect, so a warm run (~350ms) was over before the stream
    arrived — and `closeProgress` tore it down on POST-resolve. Only run 1 glowed;
    runs 2-5 received nothing at all.

    So this test watches what the user watches, and nothing less counts.
    """
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    page.evaluate("""() => {
        window.__glowed = [];
        setInterval(() => {
            const g = window._noodle.execGlow;
            if (!g || !g.byId || !g.byId.size) return;
            const cur = window.__glowed[window.__glowed.length - 1];
            if (cur && cur.id === g.id) for (const k of g.byId.keys()) cur.nodes.add(k);
            else window.__glowed.push({id: g.id, nodes: new Set(g.byId.keys())});
        }, 16);
    }""")

    for _ in range(5):
        page.evaluate("() => window.runGraph()")
        time.sleep(1.2)

    runs = page.evaluate("() => window.__glowed.map(r => ({id: r.id, n: r.nodes.size}))")
    assert len(runs) == 5, f"expected 5 glow sessions, saw {len(runs)}: {runs}"
    dark = [r for r in runs if not r["n"]]
    assert not dark, f"{len(dark)} of 5 runs lit nothing: {runs}"
    counts = {r["n"] for r in runs}
    assert len(counts) == 1, (
        f"runs of the same graph lit different numbers of nodes: {runs} — the glow is not repeatable"
    )


def test_param_edit_feels_immediate(page, any_graph):
    """Time from touching a param to the viewport reflecting it.

    Drag anticipation replays a transform locally in Three.js so the part moves
    at 60fps while the engine re-bakes in the background. What is measured here
    is the *first* visible response, which is the number a user feels.
    """
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    page.evaluate("() => window.runGraph()")
    try:
        page.wait_for_function(
            "window._noodle.viewer && window._noodle.viewer.previewGroup.children.length > 0",
            timeout=120000)
    except Exception:
        pytest.skip("the graph produced no previews to react to")

    target = page.evaluate("""() => {
        for (const n of window._noodle.lgraph._nodes){
            if (!n.widgets) continue;
            for (const w of n.widgets){
                if (typeof w.value === 'number' && w.callback)
                    return {node: n.id, widget: w.name};
            }
        }
        return null;
    }""")
    if not target:
        pytest.skip("no numeric widget to drive")

    dt = page.evaluate("""(t) => {
        const n = window._noodle.lgraph.getNodeById(t.node);
        const w = n.widgets.find(w => w.name === t.widget);
        const t0 = performance.now();
        w.callback(w.value + 1, null, n);       // exactly the path a drag uses
        return performance.now() - t0;
    }""", target)
    assert dt < budget(150), (
        f"a param edit blocked the main thread for {dt:.0f}ms — a drag would stutter"
    )
    assert not page.errors, f"page errors on a param edit: {page.errors[:5]}"


def test_editor_survives_rapid_param_changes(page, any_graph):
    """A fast drag fires many changes in a row. None may be dropped or crash,
    and the app must still be responsive when the flurry ends."""
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    target = page.evaluate("""() => {
        for (const n of window._noodle.lgraph._nodes){
            if (!n.widgets) continue;
            for (const w of n.widgets){
                if (typeof w.value === 'number' && w.callback) return {node: n.id, widget: w.name};
            }
        }
        return null;
    }""")
    if not target:
        pytest.skip("no numeric widget to drive")

    page.evaluate("""(t) => {
        const n = window._noodle.lgraph.getNodeById(t.node);
        const w = n.widgets.find(w => w.name === t.widget);
        const base = w.value;
        for (let i = 0; i < 60; i++) w.callback(base + Math.sin(i / 6), null, n);
    }""", target)

    assert rtt_ms(page) < budget(500), "the editor stopped answering after a rapid param sweep"
    real = [e for e in page.errors if "WebGL" not in e and "GL Driver" not in e]
    assert not real, f"errors during a rapid param sweep: {real[:5]}"
    wait_ready(page, 5000)
