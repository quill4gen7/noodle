#!/usr/bin/env python3
"""
Capture every node type's REAL on-canvas size from a live editor.

`cad_nodes/layout.py` mirrors litegraph's `computeSize` in Python so the backend
can place nodes without overlapping them. A mirror can drift from the thing it
mirrors, so it is pinned to reality: this script drops one node of every
registered type into a throwaway project, opens it in a headless browser against
the running server, reads `node.size` back out of litegraph itself, and writes
`tests/fixtures/node_sizes.json`. `tests/test_layout.py` then asserts — in pure
Python, no browser — that layout.py reproduces those numbers.

Re-run it after any change to how nodes.html builds sockets or widgets:

    docker compose up -d            # the server must be up on :8090
    python scripts/capture_node_sizes.py

If the resulting test fails, layout.py is what is wrong. The fixture is reality.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cad_nodes import catalog  # noqa: E402

PROJECT = "sizecapture"          # store.py forbids a leading underscore
URL = "http://localhost:8090"
FIXTURE = ROOT / "tests" / "fixtures" / "node_sizes.json"
CHROMIUM = "/usr/bin/chromium"   # the bundled build mismatches the cache


def build_project() -> pathlib.Path:
    """One node of every registered type, spread far enough apart to all exist."""
    types = sorted(catalog.REGISTRY)
    nodes = [
        {
            "id": f"n{i}",
            "type": t,
            "params": {},
            "position": [(i % 20) * 600, (i // 20) * 900],
        }
        for i, t in enumerate(types)
    ]
    d = ROOT / "projects" / PROJECT
    d.mkdir(parents=True, exist_ok=True)
    (d / "graph.json").write_text(
        json.dumps({"name": PROJECT, "nodes": nodes, "connections": []}, indent=1)
    )
    (d / "meta.json").write_text(json.dumps({"name": PROJECT}))
    print(f"  built {PROJECT} with {len(types)} nodes")
    return d


def capture() -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{URL}/nodes?p={PROJECT}", wait_until="load")
        # openGraph is async: without this wait the graph is still empty and the
        # capture silently returns nothing (the same race as screenshot.py §9).
        page.wait_for_function(
            "() => window._noodle && window._noodle.lgraph"
            " && window._noodle.lgraph._nodes.length > 0",
            timeout=30_000,
        )
        page.wait_for_timeout(1500)  # let deferred widget syncs (CodeBlock) settle

        sizes = page.evaluate(
            """() => {
                const out = {};
                for (const n of window._noodle.lgraph._nodes)
                    if (n.cadType) out[n.cadType] = [n.size[0], n.size[1]];
                return out;
            }"""
        )
        browser.close()

    if errors:
        print(f"  ! {len(errors)} page errors, first: {errors[0][:160]}")
    return sizes


def main() -> int:
    d = build_project()
    try:
        sizes = capture()
    finally:
        shutil.rmtree(d, ignore_errors=True)

    missing = sorted(set(catalog.REGISTRY) - set(sizes))
    if missing:
        print(f"  ! {len(missing)} types never rendered: {missing[:8]}")
    if not sizes:
        print("FAILED: captured nothing — is the server up on :8090?")
        return 1

    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(dict(sorted(sizes.items())), indent=1) + "\n")
    print(f"  wrote {FIXTURE.relative_to(ROOT)} ({len(sizes)} types)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
