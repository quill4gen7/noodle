"""Per-workflow export index — where did this STEP come from?

Every file that lands in ``projects/<name>/exports/`` gets a line in
``exports/index.jsonl`` saying WHO wrote it: an Export node (and which one), the
editor's ⬇ Export button (the whole result), or a bake bundle (every previewed
node, STEP + STL, zipped). The library reads it back to label each file with its
source node, link straight to that node in the editor, and say whether the graph
has changed since the file was written.

Why an append-only JSONL and not a JSON document: TWO processes write it. The
server records button/bundle exports, and the worker records Export nodes from
inside the generated script (`_out` in the transpiler PREAMBLE, which cannot
import this module — the generated source stays standalone build123d). A small
``O_APPEND`` write is atomic on a local filesystem, so neither can clobber the
other without a lock; a read-modify-write JSON would lose lines. ``load`` folds
the log: the LAST line for a file wins (a re-export overwrites the file too),
and lines whose file is gone (deleted from the library) drop out.

Line format (one JSON object per line, unknown keys ignored)::

    {"file": "bracket.step", "via": "node"|"button"|"bundle", "t": <epoch s>,
     "node": "n12"?, "fmt": "step"?, "graph": "<graph_key>"?,
     "contents": [{"node", "type", "title", "files": [...]}]?   # bundles only}

Pure Python: no build123d, safe to import from the server and the tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

INDEX_NAME = "index.jsonl"
EXPORTS_DIR = "exports"


def graph_key(graph: dict) -> str:
    """A short hash of what DECIDES the geometry: node types, params (minus the
    editor-only ``_ui`` slider windows), bypass, and the wiring. Positions,
    colours, finishes, titles, groups and preview eyes are left out, so tidying
    or restyling a graph does not mark its exports stale. The worker never sees
    this function — the executor hands it the value (``__GRAPH_KEY__``)."""
    nodes = sorted(
        (n.get("id"), n.get("type"),
         {k: v for k, v in (n.get("params") or {}).items() if k != "_ui"},
         bool(n.get("bypassed")))
        for n in graph.get("nodes") or [])
    conns = sorted(
        (c.get("from_node"), c.get("from_socket"), c.get("to_node"), c.get("to_socket"))
        for c in graph.get("connections") or [])
    blob = json.dumps({"n": nodes, "c": conns}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def index_path(project_dir: Path) -> Path:
    return Path(project_dir) / EXPORTS_DIR / INDEX_NAME


def record(project_dir: Path, file: str, via: str, **meta: Any) -> dict:
    """Append one provenance line for ``exports/<file>``. Never raises: losing a
    label is better than failing the export it describes."""
    entry = {"file": Path(file).name, "via": via, "t": round(time.time(), 3)}
    entry.update({k: v for k, v in meta.items() if v is not None})
    try:
        p = index_path(project_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry, default=str) + "\n").encode("utf-8")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass
    return entry


def _read_lines(project_dir: Path) -> list[dict]:
    p = index_path(project_dir)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue            # a torn line (crash mid-write) costs one label
        if isinstance(e, dict) and isinstance(e.get("file"), str):
            out.append(e)
    return out


def _label(node: dict) -> str:
    """The name the user sees on the canvas: their title, else the type's label."""
    if node.get("title"):
        return str(node["title"])
    try:
        from . import catalog
        return catalog.get(node.get("type", "")).label or node.get("type", "")
    except Exception:
        return node.get("type", "") or ""


def _export_nodes_by_file(graph: Optional[dict]) -> dict[str, dict]:
    """Export* nodes of the CURRENT graph keyed by the basename they write —
    how a file exported before the index existed still finds its node."""
    out: dict[str, dict] = {}
    for n in (graph or {}).get("nodes") or []:
        t = str(n.get("type", ""))
        if not t.startswith("Export"):
            continue
        path = (n.get("params") or {}).get("path")
        if isinstance(path, str) and path.strip():
            out.setdefault(os.path.basename(path.strip()), n)
    return out


def load(project_dir: Path, graph: Optional[dict] = None) -> dict[str, dict]:
    """``{filename: provenance}`` for every file in ``exports/`` with a line in
    the index — or, lacking one, an Export node of ``graph`` that writes that
    name (``via: "node", guessed: True``). With ``graph`` given, each entry also
    learns the node's CURRENT label/type, whether it still exists, and whether
    the graph is unchanged since the export (``fresh``)."""
    d = Path(project_dir) / EXPORTS_DIR
    folded: dict[str, dict] = {}
    for e in _read_lines(project_dir):
        folded[e["file"]] = e
    by_file = _export_nodes_by_file(graph)
    nodes = {n.get("id"): n for n in (graph or {}).get("nodes") or []}
    key_now = graph_key(graph) if graph is not None else None
    out: dict[str, dict] = {}
    try:
        present = {f.name for f in d.iterdir() if f.is_file()} if d.is_dir() else set()
    except OSError:
        present = set()
    present.discard(INDEX_NAME)
    for name in present:
        e = dict(folded.get(name) or {})
        if not e and name in by_file:
            e = {"file": name, "via": "node", "node": by_file[name].get("id"), "guessed": True}
        if not e:
            continue
        nid = e.get("node")
        if nid and graph is not None:
            n = nodes.get(nid)
            e["node_exists"] = n is not None
            if n is not None:
                e["node_type"] = n.get("type")
                e["node_title"] = _label(n)
        for c in e.get("contents") or []:
            n = nodes.get(c.get("node")) if graph is not None else None
            if n is not None:
                c["title"] = _label(n)
            elif graph is not None:
                c["node_exists"] = False
        if key_now and e.get("graph"):
            e["fresh"] = e["graph"] == key_now
        out[name] = e
    return out
