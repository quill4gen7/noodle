"""Does the editor survive being USED — loaded, switched, reloaded, re-run?

Every test here has a hard timeout, because the failure being hunted is a hang:
"if I load a workflow and reload it, it freezes". A hang is not a slow test, it
is a failed one, so nothing in this file is allowed to wait indefinitely.
"""
from __future__ import annotations

import time

import pytest

from .conftest import budget, open_graph, rtt_ms, wait_ready

pytestmark = pytest.mark.perf

OPEN_BUDGET_MS = 20000


def test_open_a_graph(page, any_graph):
    dt = open_graph(page, any_graph, OPEN_BUDGET_MS)
    assert dt < budget(15), f"opening {any_graph} took {dt:.1f}s"
    assert not page.dialogs, f"a native dialog blocked the open: {page.dialogs}"
    assert not page.errors, f"page errors while opening: {page.errors[:5]}"


def test_switch_and_return(page, projects):
    """Open A, switch to B, come back to A — the reported hang.

    `openGraph` early-returns when the name is unchanged, so "reload the same
    workflow" from the menu is a silent no-op; the only way to observe a real
    reload is to leave and return.
    """
    from .conftest import BENCH_GRAPHS
    have = [g for g in BENCH_GRAPHS if g in projects]
    if len(have) < 2:
        pytest.skip("need two bench projects present")
    a, b = have[0], have[1]

    open_graph(page, a, OPEN_BUDGET_MS)
    for i, name in enumerate([b, a, b, a]):
        t0 = time.perf_counter()
        page.evaluate("n => window.openGraph(n)", name)
        try:
            wait_ready(page, OPEN_BUDGET_MS)
        except Exception as e:
            pytest.fail(f"switch #{i + 1} to {name} never became usable: {type(e).__name__}")
        dt = time.perf_counter() - t0
        assert dt < budget(15), f"switch #{i + 1} to {name} took {dt:.1f}s"
        assert rtt_ms(page) < budget(500), f"main thread blocked after switching to {name}"
    assert not page.dialogs, f"a native dialog blocked a switch: {page.dialogs}"


def test_hard_reload_boots(page, any_graph):
    """F5. The boot path awaits /api/nodes and /api/projects with no timeout and
    no catch, so a backend busy with someone else's run leaves a blank editor."""
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    for i in range(3):
        t0 = time.perf_counter()
        page.reload()
        try:
            wait_ready(page, OPEN_BUDGET_MS)
        except Exception:
            pytest.fail(f"reload #{i + 1} never booted (blank editor) — dialogs={page.dialogs}")
        dt = time.perf_counter() - t0
        assert dt < budget(15), f"reload #{i + 1} took {dt:.1f}s"
    assert not page.dialogs, (
        f"reload was blocked by a native dialog {page.dialogs} — an unsaved-changes "
        "beforeunload prompt reads to the user as a freeze"
    )


def test_reload_does_not_land_dirty(page, any_graph):
    """A freshly opened graph must not immediately count as modified.

    If it does, `beforeunload` arms itself and the NEXT reload pops the browser's
    'Leave site?' dialog — which is indistinguishable from a freeze.
    """
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    time.sleep(2.5)                            # checkDirty runs on a 1s interval
    state = page.evaluate("() => document.body.dataset.docstate || window.__docState || null")
    dirty = page.evaluate("() => !!document.querySelector('[data-docstate=\"dirty\"], .doc-dirty')")
    assert not dirty, f"the graph went dirty on its own after opening (docstate={state})"


def test_progress_stream_is_always_closed(page, any_graph):
    """Every run's EventSource must be closed when the run ends.

    A leaked one keeps auto-reconnecting forever. A handful of them exhausts the
    browser's 6-connections-per-origin budget and every later request in the tab
    queues — which presents as the whole UI freezing.
    """
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    for _ in range(3):
        page.evaluate("() => window.runGraph()")
    time.sleep(1.0)
    leaked = page.evaluate("""() => window.__probe.es
        .filter(e => e.url.includes('/progress') && e.closed === null).length""")
    assert leaked == 0, (
        f"{leaked} progress EventSource(s) left open after the runs finished — "
        "each keeps reconnecting and holds a socket"
    )


def test_repeated_open_does_not_leak(page, projects):
    """Ten open cycles must not grow the scene or the heap without bound.

    `openGraph` does not clear previewMeshes/previewAnims/lastView, so this is
    where an accumulating leak would show up first.
    """
    from .conftest import BENCH_GRAPHS
    have = [g for g in BENCH_GRAPHS if g in projects]
    if len(have) < 2:
        pytest.skip("need two bench projects present")
    a, b = have[0], have[1]
    open_graph(page, a, OPEN_BUDGET_MS)

    def snap():
        return page.evaluate("""() => ({
            heap: performance.memory ? performance.memory.usedJSHeapSize : 0,
            previews: window._noodle.viewer ? window._noodle.viewer.previewGroup.children.length : 0,
            es: window.__probe.es.length,
        })""")

    first = snap()
    for i in range(10):
        page.evaluate("n => window.openGraph(n)", b if i % 2 == 0 else a)
        wait_ready(page, OPEN_BUDGET_MS)
    last = snap()

    if first["heap"]:
        growth = (last["heap"] - first["heap"]) / 1e6
        assert growth < 250, f"JS heap grew {growth:.0f}MB over 10 open cycles"
    assert last["previews"] < 5000, f"previewGroup grew to {last['previews']} children"
    assert not page.errors, f"page errors during the open cycles: {page.errors[:5]}"


def test_no_console_errors_on_a_full_cycle(page, any_graph):
    """open → run → switch away → return, with a clean console throughout."""
    open_graph(page, any_graph, OPEN_BUDGET_MS)
    page.evaluate("() => window.runGraph()")
    time.sleep(2)
    page.reload()
    wait_ready(page, OPEN_BUDGET_MS)
    # WebGL driver chatter is noise from the software rasteriser, not our bug.
    real = [e for e in page.errors if "WebGL" not in e and "GL Driver" not in e]
    assert not real, f"console/page errors: {real[:8]}"
