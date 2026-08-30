"""Is the execution glow TRUTHFUL?

The glow is what the editor tells you about its own work: amber = running now,
green = recomputed, blue = the memo cache served it, red = it threw. Every one
of those claims comes down the SSE stream at /api/graph/{name}/progress, and a
lost event is not a cosmetic loss — it is the editor asserting something false
about a node.

The oracle is the run's own answer. /execute returns `node_timings` (every node
that really executed) and `node_cached` (which of them were served from the memo
store). Those come back over a plain, reliable HTTP response, so they are the
ground truth the *unreliable* stream must be checked against:

    events(SSE)  must cover  node_timings(HTTP)
    event.c      must match  node_cached(HTTP)

The interesting cases are all about the stream being keyed on a FILE PATH plus a
byte offset rather than on a run identity: two runs in a row write a nearly
identical progress.jsonl into the same shared path, and the tailer has no way to
tell "nothing new" from "a new run rewrote the same bytes".
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from .conftest import BASE, execute

pytestmark = pytest.mark.perf


class ProgressStream:
    """Subscribe to the SSE progress stream the way the editor does.

    Used as a context manager so the subscription is live BEFORE /execute is
    POSTed. With `run` set it exercises the editor's exact path — the id is
    passed to both the stream and the POST, so the server matches events to
    that run rather than inferring which run owns the project's progress file.
    With `run` None it exercises the fallback used by MCP and curl.
    """

    def __init__(self, name: str, run: str | None = None):
        self.run = run
        self.name = name
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._open = threading.Event()
        self._t: threading.Thread | None = None

    def _pump(self):
        url = f"{BASE}/api/graph/{self.name}/progress"
        if self.run:
            url += f"?run={self.run}"
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                self._open.set()
                for raw in r:
                    if self._stop.is_set():
                        return
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        try:
                            self.events.append(json.loads(line[5:].strip()))
                        except ValueError:
                            pass
        except Exception:
            self._open.set()

    def __enter__(self):
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()
        # Wait for the server to actually accept the stream. The browser cannot
        # do this — `new EventSource()` returns before its GET is dispatched,
        # which is why the editor's own subscription races the POST.
        self._open.wait(timeout=10)
        time.sleep(0.3)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        return False

    def drain(self, seconds: float = 1.0):
        """Let the 50ms tailer poll deliver anything written just before the end."""
        time.sleep(seconds)
        return self.events

    def starts(self) -> list[str]:
        return [e["n"] for e in self.events if e.get("k") == "s"]

    def ends(self) -> list[str]:
        return [e["n"] for e in self.events if e.get("k") == "e"]


def _run_with_stream(name: str) -> tuple[dict, ProgressStream]:
    with ProgressStream(name) as st:
        res = execute(name)
        st.drain(1.0)
    return res, st


def test_stream_covers_every_executed_node(graph_name):
    """No node may execute invisibly.

    A node missing from the stream never lights up — the glow "stops" partway
    through the graph, which is exactly the reported symptom.
    """
    execute(graph_name)                       # prime, so we test the warm path
    res, st = _run_with_stream(graph_name)

    expected = set((res.get("node_timings") or {}).keys())
    assert expected, f"{graph_name}: the run reported no node timings at all"
    seen = set(st.starts())
    missing = expected - seen
    assert not missing, (
        f"{graph_name}: {len(missing)}/{len(expected)} executed nodes emitted no start event "
        f"({sorted(missing)[:8]}) — those nodes never glow"
    )


def test_every_start_is_closed(graph_name):
    """A node that starts and never ends breathes amber forever.

    Until the POST resolves and endExecGlow() sweeps it, the editor is claiming
    that node is still working. On a long run that is minutes of a lie.
    """
    execute(graph_name)
    _, st = _run_with_stream(graph_name)
    starts, ends = st.starts(), st.ends()
    assert starts, f"{graph_name}: no start events at all"
    unclosed = [n for n in starts if starts.count(n) > ends.count(n)]
    assert not unclosed, f"{graph_name}: start with no end for {sorted(set(unclosed))[:8]}"


def test_cached_flag_matches_the_run(graph_name):
    """Blue means 'the cache served it'. It has to be true.

    A wrong flag turns the editor's profiler into a liar: you optimise the node
    it painted green when the real cost was somewhere else.
    """
    execute(graph_name)
    res, st = _run_with_stream(graph_name)
    truth = set(res.get("node_cached") or [])
    claimed = {e["n"] for e in st.events if e.get("k") == "e" and e.get("c")}
    ended = set(st.ends())
    # Only judge nodes we actually received an end event for.
    assert (truth & ended) == (claimed & ended), (
        f"{graph_name}: cache flag disagrees with the run — "
        f"stream says cached but run says not: {sorted((claimed - truth) & ended)[:6]}; "
        f"run says cached but stream says not: {sorted((truth - claimed) & ended)[:6]}"
    )


def test_back_to_back_runs_each_report_in_full(any_graph):
    """Three identical runs in a row, each with its own subscription.

    This is the one that reproduces "the glow stops at random". Consecutive runs
    of an unchanged graph write an almost byte-identical progress.jsonl to the
    SAME shared path. The tailer detects a new run only by seeing the file get
    SHORTER (`size < offset`) and it polls every 50ms — so a warm run that
    rewrites the same number of bytes inside one poll window is invisible, and
    that run glows not at all.
    """
    execute(any_graph)                        # prime: now every run is warm and fast
    lost = []
    for i in range(3):
        res, st = _run_with_stream(any_graph)
        expected = set((res.get("node_timings") or {}).keys())
        seen = set(st.starts())
        if not expected:
            continue
        coverage = len(expected & seen) / len(expected)
        if coverage < 1.0:
            lost.append(f"run {i + 1}: {coverage:.0%} of {len(expected)} nodes")
    assert not lost, (
        f"{any_graph}: repeat runs lost progress events — {lost}. "
        "The stream is keyed on a file path + byte offset, not on a run identity."
    )


def test_run_id_subscription_is_exact(graph_name):
    """The editor's path: one id, given to both the stream and the POST.

    This is what removes the guesswork. Ten warm runs in a row of the same graph
    — each writing a nearly identical progress.jsonl to the same path, the case
    that used to lose entire runs — must each be reported in full.
    """
    execute(graph_name)                       # prime: from here every run is warm
    for i in range(10):
        rid = f"perftest-{graph_name}-{i}-{int(time.time() * 1000)}"
        with ProgressStream(graph_name, run=rid) as st:
            res = _execute_with_run(graph_name, rid)
            st.drain(0.8)
        expected = set((res.get("node_timings") or {}).keys())
        seen = set(st.starts())
        assert expected <= seen, (
            f"{graph_name} run {i + 1}/10 (id {rid}): missing "
            f"{sorted(expected - seen)[:8]} of {len(expected)} nodes"
        )


def test_a_stream_ignores_a_different_run(any_graph):
    """A stream asking for run X must not be fed run Y's events.

    Two clients on one project (or a thumbnail bake next to an editor run) share
    one progress file; without an id the second client glows the first's nodes.
    """
    execute(any_graph)
    with ProgressStream(any_graph, run="perftest-nobody-will-ever-run-this") as st:
        _execute_with_run(any_graph, f"perftest-other-{int(time.time() * 1000)}")
        st.drain(1.0)
    assert not st.events, (
        f"a stream subscribed to a run that never happened received "
        f"{len(st.events)} events from a different run: {st.events[:4]}"
    )


def _execute_with_run(name: str, run_id: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/graph/{name}/execute?run={run_id}", method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def test_two_projects_do_not_cross_talk(projects):
    """Concurrent runs on different projects must not appear in each other's stream."""
    from .conftest import BENCH_GRAPHS
    have = [g for g in BENCH_GRAPHS if g in projects]
    if len(have) < 2:
        pytest.skip("need two bench projects present")
    a, b = have[0], have[1]
    for g in (a, b):
        execute(g)

    ids_a = {n["id"] for n in urllib_json(f"/api/graph/{a}")["nodes"]}
    ids_b = {n["id"] for n in urllib_json(f"/api/graph/{b}")["nodes"]}
    only_b = ids_b - ids_a

    with ProgressStream(a) as sa, ProgressStream(b) as sb:
        t = threading.Thread(target=execute, args=(b,), daemon=True)
        t.start()
        execute(a)
        t.join(timeout=240)
        sa.drain(1.0)
        sb.drain(1.0)

    stray = {n for n in sa.starts() if n in only_b}
    assert not stray, f"stream for {a} carried node ids belonging to {b}: {sorted(stray)[:6]}"


def urllib_json(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=30) as r:
        return json.loads(r.read())
