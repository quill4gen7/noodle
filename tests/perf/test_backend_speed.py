"""Speed and stability of the engine, measured through the live HTTP API.

Three questions:
  - is a repeat run fast?      (the memo cache is the app's central perf claim)
  - is it *consistently* fast? (a p95 far above the median is a stability bug)
  - does a run block the server? (/execute is on asyncio.to_thread precisely so
    that it does not — nothing else can be served while it does, the progress
    stream included, and that reads to the user as a total freeze)
"""
from __future__ import annotations

import statistics
import threading
import time
import urllib.error

import pytest

from .conftest import budget, execute, get, timed

pytestmark = pytest.mark.perf


# Warm = every node served from the memo store. CLAUDE.md quotes ~0.5s on the
# lego brick; 4s is a generous ceiling that still catches a cache that stopped
# working (which would put it back at ~8.5s).
WARM_BUDGET = 4.0


def test_warm_execute_is_fast(graph_name):
    """Second run of an unchanged graph must be served by the memo cache."""
    execute(graph_name)                       # prime
    _, warm = timed(execute, graph_name)
    assert warm < budget(WARM_BUDGET), (
        f"{graph_name}: warm re-run took {warm:.2f}s (budget {budget(WARM_BUDGET):.1f}s) "
        "— the memo cache is not serving it"
    )


def test_warm_execute_is_consistent(graph_name):
    """Repeat runs must not scatter: a p95 far off the median is instability."""
    execute(graph_name)                       # prime
    runs = [timed(execute, graph_name)[1] for _ in range(5)]
    med = statistics.median(runs)
    worst = max(runs)
    assert worst < budget(WARM_BUDGET) * 2, f"{graph_name}: slowest warm run {worst:.2f}s of {runs}"
    # Absolute floor of 0.5s so sub-100ms runs aren't held to a ratio.
    assert worst <= max(med * 4, 0.5), (
        f"{graph_name}: warm runs scatter — median {med:.2f}s, worst {worst:.2f}s, all {runs}"
    )


def test_every_node_reports_a_timing(graph_name):
    """node_timings is what the cost badges and the glow are drawn from.

    A node missing from it is a node the editor can say nothing about — so this
    pins the *coverage* of the instrumentation, not its speed.
    """
    res = execute(graph_name)
    graph = get(f"/api/graph/{graph_name}")
    timings = res.get("node_timings") or {}
    # Editor-only nodes never execute and are correctly absent.
    EDITOR_ONLY = {"Note", "RefImage", "ToAgent"}
    top_level = [n for n in graph["nodes"]
                 if n.get("type") not in EDITOR_ONLY and not n.get("parent")]
    missing = [n["id"] for n in top_level if n["id"] not in timings]
    assert not missing, (
        f"{graph_name}: {len(missing)}/{len(top_level)} nodes reported no timing "
        f"(first few: {missing[:8]}) — these can never light up or show a cost"
    )


def test_server_stays_responsive_during_a_run(any_graph):
    """/health must keep answering while a graph executes.

    If /execute ever comes off asyncio.to_thread this fails, and it is the exact
    shape of "the whole app froze while I was working".
    """
    execute(any_graph)                        # prime, so we time a warm run
    lat: list[float] = []
    done = threading.Event()

    def run():
        try:
            execute(any_graph)
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    while not done.is_set():
        _, dt = timed(get, "/health", 10)
        lat.append(dt * 1000)
        time.sleep(0.05)
    t.join(timeout=5)

    if len(lat) < 3:
        pytest.skip("run finished too fast to sample the event loop")
    worst = max(lat)
    assert worst < budget(1000), (
        f"/health blocked for {worst:.0f}ms during a run of {any_graph} "
        f"(samples n={len(lat)}, median {statistics.median(lat):.0f}ms) — the event loop is held"
    )


def test_concurrent_executes_both_succeed(any_graph):
    """Two clients on one project must not break each other.

    The warm worker serialises on its own lock, so this is about the HTTP layer
    and the shared project workdir surviving the overlap — not about speed.
    """
    execute(any_graph)
    out: list[object] = []

    def run():
        try:
            out.append(execute(any_graph))
        except (urllib.error.URLError, OSError, ValueError) as e:
            out.append(e)

    ts = [threading.Thread(target=run) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=240)

    assert len(out) == 2, "a concurrent /execute never returned"
    bad = [o for o in out if isinstance(o, Exception)]
    assert not bad, f"concurrent execute failed: {bad}"
    assert all(o.get("view", {}).get("success") for o in out), "a concurrent run produced no view"


def test_list_endpoints_are_cheap():
    """The boot path. nodes.html awaits these before it can show anything, with
    no timeout and no catch — so a slow one is a blank editor, not a slow one."""
    for path, cap in (("/api/projects", 2.0), ("/api/nodes", 3.0), ("/api/wiretypes", 1.0)):
        _, dt = timed(get, path, 30)
        assert dt < budget(cap), f"GET {path} took {dt:.2f}s (budget {budget(cap):.1f}s) — boot stalls here"
