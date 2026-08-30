"""What happens to the editor while the engine is busy with someone else's run.

This is the file that reproduces the reported freeze. Every other test here runs
against an idle server and passes; the app only misbehaves under CONTENTION,
because there is exactly one warm worker and it is serialised by a single lock.

Measured during development: a lego-brick execute that normally takes 0.43s took
**272 seconds** — the whole time a second browser tab was mid-run on a heavier
graph. Nothing at the requesting end says so. The run just does not come back,
which is indistinguishable from a hang.
"""
from __future__ import annotations

import threading
import time

import pytest

from .conftest import BENCH_GRAPHS, budget, execute, get, open_graph, rtt_ms, timed, wait_ready

pytestmark = pytest.mark.perf

OPEN_BUDGET_MS = 20000


@pytest.fixture
def busy_engine(projects):
    """Hold the worker with a real run for the duration of the test.

    LIMITATION, worth knowing before trusting a pass: only the FIRST churn run
    is cold. After it the memo store serves the rest in milliseconds, so this
    fixture applies far less pressure than a user dragging a slider (every edit
    invalidates a subtree and re-runs it cold). The honest way to reproduce the
    272s case is to edit a heavy graph in one tab while measuring in another —
    which this suite will not do, because it would write to the user's projects.
    """
    heavy = next((g for g in reversed(BENCH_GRAPHS) if g in projects), None)
    if heavy is None:
        pytest.skip("no bench project to load the engine with")
    # Make it cold so the run is long enough to overlap with: a warm re-run
    # would be served from the memo store in milliseconds.
    try:
        get("/api/system/restart", timeout=5)
    except Exception:
        pass
    stop = threading.Event()

    def churn():
        while not stop.is_set():
            try:
                execute(heavy, timeout=300)
            except Exception:
                return

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    time.sleep(0.5)
    yield heavy
    stop.set()
    t.join(timeout=5)


def test_editor_still_opens_while_the_engine_is_busy(page, projects, busy_engine):
    """The boot path must not wait on the engine.

    nodes.html awaits /api/nodes and /api/projects with no timeout and no catch;
    if either can be held up by a run in flight, the reload lands on a blank
    editor and stays there.
    """
    other = next((g for g in BENCH_GRAPHS if g in projects and g != busy_engine), None)
    if other is None:
        pytest.skip("need a second bench project")
    dt = open_graph(page, other, OPEN_BUDGET_MS)
    assert dt < budget(20), f"the editor took {dt:.1f}s to open while the engine was busy"
    assert rtt_ms(page) < budget(500), "the editor is not answering while the engine is busy"


def test_reload_while_the_engine_is_busy(page, projects, busy_engine):
    """F5 during a long run — the literal 'load a workflow and reload it' case."""
    other = next((g for g in BENCH_GRAPHS if g in projects and g != busy_engine), None)
    if other is None:
        pytest.skip("need a second bench project")
    open_graph(page, other, OPEN_BUDGET_MS)
    for i in range(2):
        t0 = time.perf_counter()
        page.reload()
        try:
            wait_ready(page, OPEN_BUDGET_MS)
        except Exception:
            pytest.fail(f"reload #{i + 1} never booted while the engine was busy "
                        f"(dialogs={page.dialogs}) — this is the reported freeze")
        assert time.perf_counter() - t0 < budget(20), f"reload #{i + 1} was starved by the engine"


def test_a_queued_run_is_bounded(projects, busy_engine):
    """A run that queues behind another client's must still be bounded.

    It is not: the wait is however long the other graph takes, with no cap, no
    queue position and no feedback. This test documents the ceiling we consider
    acceptable — if it fails, the fix is not a bigger number, it is telling the
    client it is waiting.
    """
    other = next((g for g in BENCH_GRAPHS if g in projects and g != busy_engine), None)
    if other is None:
        pytest.skip("need a second bench project")
    execute(other, timeout=300)               # prime (already queued behind the churn)
    _, dt = timed(execute, other, 300)
    assert dt < budget(60), (
        f"a warm run of {other} took {dt:.0f}s while {busy_engine} held the worker — "
        "the user sees a frozen editor with no indication that it is queued"
    )


@pytest.mark.xfail(reason="known gap: nothing reports engine busy/queue state (not a regression)",
                   strict=False)
def test_health_reports_that_the_engine_is_busy(busy_engine):
    """Whatever else is true, the app must be able to SAY it is busy.

    A freeze the user can see explained is a wait; one that is silent is a bug
    report. If this fails there is no signal the editor could surface.
    """
    h = get("/health", timeout=10)
    assert isinstance(h, dict) and h, "/health returned nothing usable"
    busy_signal = any(k for k in h if "busy" in k.lower() or "queue" in k.lower() or "run" in k.lower())
    assert busy_signal, (
        f"/health exposes no busy/queue signal (keys: {sorted(h)}) — the editor has "
        "nothing to show the user while their run is stuck behind another one"
    )
