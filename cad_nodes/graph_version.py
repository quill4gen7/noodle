"""
Optimistic concurrency for graph.json — the editor and an agent writing the
same project at once.

The incident this exists for: an agent wrote graph.json through the API while
the editor had the project open; the editor's next autosave posted its own,
older canvas over it and the agent's work was gone. Nothing was wrong with
either write on its own — the file simply had two writers and neither knew
about the other.

So every graph carries a VERSION, and a writer may say which version it edited
("base_version"). A write whose base is no longer the file on disk is refused
with the current version and graph (HTTP 409), and the writer merges instead of
overwriting. A write WITHOUT a base still goes through, exactly as before, so
no existing caller breaks.

The version is a hash of the file's bytes, not a counter or an mtime:

- it needs no state beside the file, so it survives a restart and covers EVERY
  writer — the REST route, `GraphStore.save` (api / MCP / copilot), and an agent
  editing graph.json straight on disk;
- mtime is not enough on its own: two writes inside one timestamp tick with the
  same size (a param going 5 → 6) would share it. Hashing ~50 KB is well under a
  millisecond, so there is nothing worth caching.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional


def version_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def read_versioned(path: Path) -> tuple[Optional[str], Optional[dict]]:
    """(version, parsed graph) from ONE read of the file — so the two cannot
    disagree the way a separate hash-then-read could. (None, None) if absent."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        graph = json.loads(data)
    except ValueError:
        graph = None
    return version_of_bytes(data), graph


def current_version(path: Path) -> Optional[str]:
    return read_versioned(path)[0]


class StaleGraphError(Exception):
    """A write based on a version that is no longer on disk."""

    def __init__(self, base: str, current: Optional[str], graph: Optional[dict]):
        super().__init__(
            f"graph changed since version {base}: it is now {current}. "
            "Re-read it (or merge) and write again with the new base_version."
        )
        self.base = base
        self.current = current
        self.graph = graph

    def detail(self) -> dict:
        """The 409 body: enough for the writer to merge without another round trip."""
        return {
            "error": "stale_graph",
            "message": str(self),
            "base_version": self.base,
            "version": self.current,
            "graph": self.graph,
        }


def check_base(path: Path, base_version: Optional[str]) -> Optional[str]:
    """
    Raise StaleGraphError when `base_version` is given and is not the version
    on disk. Returns the current version. A missing base always passes
    (backward compatible), and so does writing a project that does not exist yet.
    """
    version, graph = read_versioned(path)
    if base_version and version is not None and base_version != version:
        raise StaleGraphError(base_version, version, graph)
    return version


def write_graph(path: Path, text: str) -> str:
    """Write graph.json and return the version of exactly what was written."""
    data = text.encode("utf-8")
    Path(path).write_bytes(data)
    return version_of_bytes(data)
