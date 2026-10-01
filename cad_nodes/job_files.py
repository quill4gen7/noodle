"""Atomic persistence and per-run files (no CAD imports).

Job cwd remains the project directory: relative asset/export paths keep working.
Only internal scripts and results live in isolated directories.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path


def atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data.encode('utf-8') if isinstance(data, str) else data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def atomic_copy(source: Path, target: Path) -> None:
    """Publish large immutable outputs without loading another copy into RAM."""
    fd, tmp = tempfile.mkstemp(prefix=f'.{target.name}-', dir=target.parent)
    os.close(fd)
    try:
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def cleanup_runs(workdir: Path, keep: int = 16, grace: float = 180) -> None:
    """Retain recent completed results for downloads/SSE, never prune active jobs."""
    root = workdir / '.runs'
    if not root.exists():
        return
    completed = []
    for job in root.iterdir():
        try:
            stamp = (job / 'complete').stat().st_mtime
            completed.append((stamp, job))
        except OSError:
            continue
    cutoff = time.time() - grace
    for stamp, job in sorted(completed, reverse=True)[keep:]:
        if stamp < cutoff:
            shutil.rmtree(job, ignore_errors=True)


def run_dir(workdir: Path, run_id: str) -> Path:
    # Caller IDs are opaque, never interpreted as filesystem paths.
    key = hashlib.sha256(run_id.encode('utf-8')).hexdigest()
    return workdir / '.runs' / key


def progress_file(workdir: Path, run_id: str) -> Path:
    return run_dir(workdir, run_id) / 'progress.jsonl'
