"""Fixtures for the perf/stability suite.

These tests drive the LIVE app (server + real browser), unlike the rest of
`tests/`, which is pure-Python and needs nothing running. They are therefore
opt-in: without `NOODLE_PERF=1` every test here skips, so `pytest tests/`
stays a fast, hermetic run.

    NOODLE_PERF=1 python -m pytest tests/perf -v

Env knobs (all optional):
    NOODLE_URL        base url            (default http://localhost:8090)
    NOODLE_CHROMIUM   browser binary      (default /usr/bin/chromium)
    NOODLE_PERF_SLOW  multiply every time budget by this factor (slow machine)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("NOODLE_URL", "http://localhost:8090").rstrip("/")
CHROMIUM = os.environ.get("NOODLE_CHROMIUM", "/usr/bin/chromium")
SLOW = float(os.environ.get("NOODLE_PERF_SLOW", "1"))


def budget(seconds: float) -> float:
    """A time budget, scaled by NOODLE_PERF_SLOW so a slow box can still pass."""
    return seconds * SLOW


# --------------------------------------------------------------------------- http

def get(path: str, timeout: float = 30):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def post(path: str, timeout: float = 180):
    req = urllib.request.Request(f"{BASE}{path}", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def execute(name: str, timeout: float = 180) -> dict:
    """POST /execute and return the result dict (node_timings, node_cached, view…)."""
    return post(f"/api/graph/{name}/execute", timeout=timeout)


def timed(fn, *a, **kw):
    """Return (result, elapsed_seconds)."""
    t0 = time.perf_counter()
    out = fn(*a, **kw)
    return out, time.perf_counter() - t0


# --------------------------------------------------------------------------- gating

def _server_up() -> bool:
    try:
        get("/health", timeout=3)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


@pytest.fixture(scope="session", autouse=True)
def _require_opt_in():
    if os.environ.get("NOODLE_PERF") != "1":
        pytest.skip("perf suite is opt-in: set NOODLE_PERF=1 (needs the app running)",
                    allow_module_level=True)
    if not _server_up():
        pytest.skip(f"no noodle server at {BASE} (docker compose up -d)",
                    allow_module_level=True)


@pytest.fixture(scope="session")
def projects() -> list[str]:
    return [p["name"] for p in get("/api/projects")]


# The graphs the suite exercises. Small → heavy, so a regression shows up as a
# shifted profile rather than one number. Any missing project is skipped, not
# an error: `projects/` is gitignored and every checkout has a different set.
BENCH_GRAPHS = ["lego-brick", "voronoi-3d-lattice", "galton-board"]


@pytest.fixture(params=BENCH_GRAPHS)
def graph_name(request, projects) -> str:
    if request.param not in projects:
        pytest.skip(f"project {request.param!r} not present")
    return request.param


@pytest.fixture(scope="session")
def any_graph(projects) -> str:
    """One graph that exists, for tests that need *a* subject rather than all of them."""
    for n in BENCH_GRAPHS:
        if n in projects:
            return n
    if not projects:
        pytest.skip("no projects to test against")
    return projects[0]


# --------------------------------------------------------------------------- browser

@pytest.fixture(scope="session")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    if not os.path.exists(CHROMIUM):
        pytest.skip(f"no chromium at {CHROMIUM} (set NOODLE_CHROMIUM)")
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM)
        yield b
        b.close()


# Installed before any page script runs, so nothing escapes it. Wraps
# EventSource to record every progress message and every open/close — that is
# how the client-side glow and the socket-leak tests observe the app without
# needing hooks inside nodes.html.
_PROBE = """
window.__probe = { es: [], errors: [], frames: [] };
const _ES = window.EventSource;
window.EventSource = function(url, cfg){
  const es = new _ES(url, cfg);
  const rec = { url: String(url), opened: Date.now(), closed: null, msgs: [] };
  window.__probe.es.push(rec);
  es.addEventListener('message', (m)=>{
    try { rec.msgs.push(JSON.parse(m.data)); } catch(e) { rec.msgs.push({raw: m.data}); }
  });
  const _close = es.close.bind(es);
  es.close = ()=>{ rec.closed = Date.now(); return _close(); };
  return es;
};
window.EventSource.prototype = _ES.prototype;
for (const k of ['CONNECTING','OPEN','CLOSED']) window.EventSource[k] = _ES[k];

// rAF frame times, for the reactivity tests.
(function(){
  let last = 0;
  function tick(t){
    if (last) window.__probe.frames.push(t - last);
    if (window.__probe.frames.length > 4000) window.__probe.frames.shift();
    last = t; requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
})();
"""


@pytest.fixture
def page(browser):
    """A fresh instrumented page. Fails the test on any uncaught page error."""
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    ctx.add_init_script(_PROBE)
    pg = ctx.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: pg.errors.append(f"console.error: {m.text}") if m.type == "error" else None)
    # A native beforeunload/confirm dialog is itself a freeze — record and dismiss
    # it rather than letting playwright hang on it.
    pg.dialogs = []
    pg.on("dialog", lambda d: (pg.dialogs.append(d.type), d.dismiss()))
    yield pg
    ctx.close()


def open_graph(page, name: str, timeout_ms: int = 30000) -> float:
    """Open a project through the real editor. Returns seconds until it is usable."""
    t0 = time.perf_counter()
    page.goto(f"{BASE}/nodes?p={name}")
    wait_ready(page, timeout_ms)
    return time.perf_counter() - t0


def wait_ready(page, timeout_ms: int = 30000):
    page.wait_for_function(
        "window._noodle && window._noodle.lgraph && window._noodle.lgraph._nodes.length > 0",
        timeout=timeout_ms,
    )


def rtt_ms(page) -> float:
    """Main-thread round trip. A blocked UI shows up here as tens of ms → seconds."""
    t0 = time.perf_counter()
    page.evaluate("1")
    return (time.perf_counter() - t0) * 1000
