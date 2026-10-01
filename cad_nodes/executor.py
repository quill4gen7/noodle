"""
Executor — runs transpiled build123d code in a sandboxed subprocess and
collects the STL + view JSON via mesh_extractor.

Subprocess pattern: write a wrapper script, run `python3` with a timeout,
capture stdout/stderr.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import export_index
from .graph import Graph
from .job_files import atomic_copy, atomic_write, cleanup_runs, run_dir
from .transpiler import Transpiler, transpile

# Marker the transpiler appends to each statement (see transpiler._annot).
_NODE_MARK = re.compile(r"# @node:(\S+) \(([^)]*)\)")
_TB_FRAME = re.compile(r'File "[^"]*_run\.py", line (\d+)')


def _humanize(exc_line: str) -> tuple[str, str]:
    """Map a raw exception line to (message, hint) in plain language."""
    low = exc_line.lower()
    if "failed creating a fillet" in low or "failed creating a chamfer" in low:
        return ("Raggio/lunghezza troppo grande per questa geometria.",
                "Riduci il valore: deve stare entro lo spigolo più piccolo del "
                "pezzo. (build123d offre max_fillet() per il massimo valido.)")
    if any(k in low for k in ("command not done", "stdfail_notdone", "brep_api")):
        return ("L'operazione geometrica non è riuscita: il kernel non ha potuto "
                "costruire la forma.",
                "Di solito un parametro è fuori scala. Riduci il raggio di "
                "Fillet/Chamfer, assicurati che lo spessore di Shell sia minore "
                "del pezzo, e che il profilo di Revolve non attraversi l'asse.")
    if "constructionerror" in low or "standard_constructionerror" in low:
        return ("Geometria di costruzione non valida.",
                "Controlla che le dimensioni siano positive e non nulle.")
    if "list index out of range" in low or "indexerror" in low:
        return ("Indice fuori dai limiti della lista.",
                "Un nodo ListItem/ListSlice punta oltre la lunghezza della lista.")
    if "zerodivision" in low:
        return ("Divisione per zero.",
                "Controlla i nodi Divide / Expression a monte.")
    if "is not closed" in low or "wire is not closed" in low:
        return ("Il contorno non è chiuso.",
                "MakeFace/Extrude richiedono uno sketch o un wire chiuso.")
    if low.startswith("syntaxerror") or low.startswith("indentationerror"):
        return ("Errore di sintassi nel codice del CodeBlock.",
                "Correggi la riga indicata (line/col sono relativi al blocco).")
    if "nameerror" in low:
        return ("Nome non definito nel codice generato.",
                "Probabile errore in un nodo CodeBlock o Expression.")
    # Fallback: surface the raw exception.
    return (exc_line or "Errore di esecuzione.", "")


# Suffix the transpiler appends to a CodeBlock's error (runtime _cb_where, or the
# SyntaxError stub): " (CodeBlock line 12)" / " (CodeBlock line 3, col 7)".
_CB_WHERE = re.compile(r"\(CodeBlock line (\d+)(?:, col (\d+))?\)\s*$")


def _codeblock_where(raw: str) -> dict:
    """{"line": N[, "col": C]} — block-relative, 1-based — when a node error
    carries a CodeBlock location; {} otherwise."""
    m = _CB_WHERE.search(raw or "")
    if not m:
        return {}
    out = {"line": int(m.group(1))}
    if m.group(2):
        out["col"] = int(m.group(2))
    return out


def _diagnose(stderr: str, script_text: str) -> dict:
    """Correlate a traceback to the culprit node and translate the error."""
    src_lines = script_text.splitlines()
    frame_nums = [int(m.group(1)) for m in _TB_FRAME.finditer(stderr or "")]

    node_id = node_type = culprit = None
    # Walk frames innermost-first; the deepest line carrying a @node marker wins
    # (a CodeBlock's inner line has none, so we fall back to its call site).
    for ln in reversed(frame_nums):
        if 1 <= ln <= len(src_lines):
            mm = _NODE_MARK.search(src_lines[ln - 1])
            if mm:
                node_id, node_type = mm.group(1), mm.group(2)
                culprit = src_lines[ln - 1].split("# @node:")[0].strip()
                break

    exc_line = ""
    for line in reversed((stderr or "").strip().splitlines()):
        if line.strip():
            exc_line = line.strip()
            break
    message, hint = _humanize(exc_line)
    return {
        "node_id": node_id,
        "node_type": node_type,
        "culprit": culprit,
        "exception": exc_line,
        "message": message,
        "hint": hint,
    }


def _degenerate_warning(view: dict) -> str | None:
    """Flag a non-error result that is empty (e.g. a boolean that removed all
    geometry). Skip 2D/section results, which legitimately have zero volume."""
    if not view or not view.get("success"):
        return None
    kind = (view.get("kind") or "").lower()
    if "sketch" in kind or "face" in kind or "wire" in kind:
        return None
    counts = view.get("counts") or {}
    vol = view.get("volume")
    if counts.get("solids") == 0 and (vol in (None, 0) or (isinstance(vol, (int, float)) and abs(vol) < 1e-9)):
        return ("Il risultato è vuoto (volume nullo, nessun solido): "
                "un'operazione booleana potrebbe aver eliminato tutta la geometria.")
    return None

# Repo root = parent of the cad_nodes package, so the subprocess can do
# `from cad_nodes.mesh_extractor import extract_and_write`.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_EPILOGUE = """

# --- view extraction (injected by executor) ---
import sys as _sys
_sys.path.insert(0, {repo_root!r})
from cad_nodes.mesh_extractor import extract_and_write
if globals().get('__PROGRESS_PATH__'):
    with open(__PROGRESS_PATH__, 'a') as _pf:
        _pf.write('{{"k":"phase","phase":"preview"}}\\n')
extract_and_write(__result__, {stl!r}, {view!r}, __panels__, __previews__, {lin}, {ang}, __errors__, __timings__,
                  hashes=globals().get("__hashes__") or {{}}, memo=globals().get("__MEMO__"),
                  cached_nodes=globals().get("__cached__") or {{}})
"""

# Tessellation level-of-detail: (linear_frac of bbox diagonal, angular tol rad).
# Live preview is coarse (~10-30x fewer triangles, ~10x smaller payload); the
# STEP/STL bake path (export_graph) is unaffected and stays exact/fine.
_QUALITY = {
    "live": (0.02, 0.4),
    "fine": (0.004, 0.15),
}


def _graph_key(graph) -> str | None:
    try:
        return export_index.graph_key(graph.to_dict())
    except Exception:
        return None      # a missing label never blocks a run


def _key_head(graph_key: str | None) -> str:
    """Name the graph state this run was built from, so an Export node's `_out`
    can stamp its line in exports/index.jsonl (cad_nodes/export_index.py).
    Same guarded-global idiom as __PROGRESS_PATH__."""
    return f"__GRAPH_KEY__ = {graph_key!r}\n" if graph_key else ""


def build_script(code: str, stl_path: Path, view_path: Path,
                 quality: str = "live", write_stl: bool = True,
                 progress_path: Path | None = None,
                 graph_key: str | None = None) -> str:
    lin, ang = _QUALITY.get(quality, _QUALITY["live"])
    # The PREAMBLE guards __PROGRESS_PATH__ with try/except NameError (same idiom
    # as __MEMO__), so assigning it *before* the transpiled code sticks: the node
    # _ev() calls then append to this file while the run is still going, and the
    # editor tails it. No header = progress is a no-op.
    head = (f"__PROGRESS_PATH__ = {str(progress_path)!r}\n"
            "with open(__PROGRESS_PATH__, 'a') as _pf:\n"
            "    _pf.write('{\"k\":\"phase\",\"phase\":\"running\"}\\n')\n") if progress_path else ""
    # write_stl=False (live runs) skips the STL export in the epilogue — an empty
    # path makes extract_and_write no-op it. The STL is regenerated on demand by
    # the download/render routes, saving ~0.9s on every live re-run.
    return _key_head(graph_key) + head + code + _EPILOGUE.format(
        repo_root=_REPO_ROOT, stl=(str(stl_path) if write_stl else ""),
        view=str(view_path), lin=lin, ang=ang,
    )


# ---------------------------------------------------------------------------
# Warm worker — keeps build123d imported across runs (the cold import is ~2.7s,
# the actual build+tessellate is 5-165ms). A separate process keeps the web
# server build123d-free and isolated from OCCT crashes; it runs one job at a
# time (serialised by a lock) and is respawned if it dies or times out.
# ---------------------------------------------------------------------------
_WORKER_PATH = str(Path(__file__).resolve().parent / "worker.py")
_SENTINEL = "@@CADWORKER@@"
# Runtime-mutable: seeded from the env, but toggled live via set_warm() (UI
# switch). When off, execute uses a cold subprocess per run and NO build123d
# process stays resident — freeing ~300-500 MB while noodle is idle.
_warm_enabled = os.environ.get("CAD_WARM_WORKER", "1") != "0"


class WarmWorker:
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._queued = 0
        self._busy = False

    def status(self) -> dict:
        with self._state_lock:
            return {"busy": self._busy, "queued": self._queued}

    def _alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
            self._proc = None

    def _read_sentinel(self, timeout: float, cancel_path: Path | None = None):
        """Read worker stdout until a SENTINEL line; ignore other noise.
        Returns the parsed dict, or None on timeout."""
        box: dict = {}
        proc = self._proc

        def reader():
            for line in proc.stdout:
                if line.startswith(_SENTINEL):
                    try:
                        box["v"] = json.loads(line[len(_SENTINEL):])
                    except Exception:
                        box["v"] = None
                    return

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        deadline = time.monotonic() + max(0, timeout)
        while t.is_alive():
            if cancel_path is not None and cancel_path.exists():
                return {"cancelled": True}
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            t.join(min(left, 0.1))
        return box.get("v")

    def _spawn(self, timeout: float = 120, cancel_path: Path | None = None) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-u", _WORKER_PATH],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=_REPO_ROOT,
        )
        ready = self._read_sentinel(timeout=timeout, cancel_path=cancel_path)
        if ready and ready.get("ready"):
            return
        cancelled = bool(ready and ready.get("cancelled"))
        self._kill()
        if cancelled:
            raise _Cancelled()
        raise RuntimeError("warm worker failed to import build123d")

    def shutdown(self) -> None:
        """Terminate the resident worker, freeing its build123d memory. The next
        run() lazily respawns it (if warm mode is still on)."""
        with self._lock:
            self._kill()

    def run(self, script_path: Path, cwd: Path, timeout: float,
            cancel_path: Path | None = None) -> dict:
        deadline = time.monotonic() + timeout  # includes queue AND startup
        with self._state_lock:
            self._queued += 1
        acquired = False
        try:
            while not acquired:
                if cancel_path is not None and cancel_path.exists():
                    return {"cancelled": True}
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"timeout": True, "phase": "queue"}
                acquired = self._lock.acquire(timeout=min(left, 0.1))
        finally:
            with self._state_lock:
                self._queued -= 1
        with self._state_lock:
            self._busy = True
        try:
            if cancel_path is not None and cancel_path.exists():
                return {"cancelled": True}
            if not self._alive():
                try:
                    self._spawn(max(0, deadline - time.monotonic()), cancel_path)
                except _Cancelled:
                    return {"cancelled": True}
            if time.monotonic() >= deadline:
                return {"timeout": True, "phase": "startup"}
            job = json.dumps({"cmd": "run", "script_path": str(script_path),
                              "cwd": str(cwd)})
            try:
                self._proc.stdin.write(job + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._kill()
                raise
            res = self._read_sentinel(deadline - time.monotonic(), cancel_path)
            if res is None or res.get("cancelled"):
                self._kill()  # only this job owns the worker lock
                return res or {"timeout": True}
            return res
        finally:
            with self._state_lock:
                self._busy = False
            self._lock.release()


class _Cancelled(Exception):
    """Internal: spawn aborted because this job's cancel file appeared."""


_WORKER = WarmWorker()


def warm_status() -> dict:
    """Current warm-worker mode + whether a resident process is alive."""
    return {"enabled": _warm_enabled, "alive": _WORKER._alive(), **_WORKER.status()}


def set_warm(enabled: bool) -> dict:
    """Toggle warm-worker mode at runtime. Turning it off also shuts the resident
    process down immediately (frees its memory); turning it on lets the next run
    spawn it. Returns the new status."""
    global _warm_enabled
    _warm_enabled = bool(enabled)
    if not _warm_enabled:
        _WORKER.shutdown()
    return warm_status()


def _timeout_result(code: str, timeout: float) -> dict:
    return {"success": False, "code": code, "warnings": [],
            "errors": f"Execution timed out after {timeout}s", "view": None,
            "error_detail": {
                "node_id": None, "node_type": None, "culprit": None,
                "exception": "Timeout",
                "message": f"Esecuzione interrotta dopo {timeout}s.",
                "hint": "Il grafo è troppo pesante o un'operazione si è bloccata."}}


def _finalize(code: str, script_text: str, stdout: str, stderr,
              view_path: Path, stl_path: Path) -> dict:
    """Build the result dict from a finished run (warm or cold).

    Execution is best-effort per node: a single node's runtime error is caught
    (recorded in view["node_errors"]) and never blocks the rest of the graph.
    """
    view = None
    if view_path.exists():
        try:
            view = json.loads(view_path.read_text())
        except Exception:
            view = None

    ran = stderr is None
    raw_errors = (view or {}).get("node_errors") or {}
    has_geo = bool(view and (view.get("success") or view.get("previews")))

    result = {
        "success": bool(ran and has_geo),
        "code": code,
        "stdout": stdout,
        "errors": stderr,
        "view": view,
        "warnings": [],
        "node_errors": {},
        "node_timings": (view or {}).get("node_timings") or {},
        "node_cached": (view or {}).get("node_cached") or [],
        "stl": str(stl_path) if stl_path.exists() else None,
    }

    # Per-node errors: humanise, surface as warnings, but don't block.
    for nid, raw in raw_errors.items():
        msg, hint = _humanize(raw)
        entry = {"exception": raw, "message": msg, "hint": hint}
        entry.update(_codeblock_where(raw))
        result["node_errors"][nid] = entry
    if raw_errors:
        result["warnings"].append(
            f"{len(raw_errors)} nodo/i in errore (workflow continuato): "
            + ", ".join(sorted(raw_errors)))

    if not ran:
        # Hard failure: a top-level traceback (e.g. bad generated code).
        detail = _diagnose(stderr or "", script_text)
        result["error_detail"] = detail
        if not result["errors"]:
            result["errors"] = detail["message"]
        return result

    if not has_geo:
        # Ran, but nothing drawable came out.
        if raw_errors:
            nid, raw = next(iter(raw_errors.items()))
            msg, hint = _humanize(raw)
            result["error_detail"] = {"node_id": nid, "node_type": None,
                                      "culprit": None, "exception": raw,
                                      "message": msg, "hint": hint}
        else:
            result["error_detail"] = {
                "node_id": None, "node_type": None, "culprit": None,
                "exception": (view or {}).get("error", ""),
                "message": "Nessun risultato da visualizzare: collega un nodo "
                           "che produce geometria a un'estremità.",
                "hint": ""}
        result["errors"] = result["error_detail"]["message"]
        return result

    warn = _degenerate_warning(view)
    if warn:
        result["warnings"].append(warn)
    return result


_PUBLISH_LOCK = threading.Lock()


def _run_script(script_path: Path, cwd: Path, timeout: float,
                cancel_path: Path | None = None) -> dict:
    """Shared warm/cold path for preview and exports; bounded and cancellable."""
    deadline = time.monotonic() + timeout
    if _warm_enabled:
        try:
            return _WORKER.run(script_path, cwd, timeout, cancel_path)
        except _Cancelled:
            return {"cancelled": True}
        except Exception:
            pass  # failed worker startup/pipe: cold fallback within same budget
    if cancel_path is not None and cancel_path.exists():
        return {"cancelled": True}
    if time.monotonic() >= deadline:
        return {"timeout": True}
    with subprocess.Popen([sys.executable, str(script_path)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=str(cwd)) as proc:
        while True:
            if cancel_path is not None and cancel_path.exists():
                proc.kill()
                proc.communicate()
                return {"cancelled": True}
            left = deadline - time.monotonic()
            if left <= 0:
                proc.kill()
                proc.communicate()
                return {"timeout": True}
            try:
                stdout, stderr = proc.communicate(timeout=min(left, 0.1))
                return {"stdout": stdout, "error": stderr if proc.returncode else ""}
            except subprocess.TimeoutExpired:
                continue


def execute_code(code: str, workdir: Path, timeout: int = 120,
                 quality: str = "live", write_stl: bool = True,
                 run_id: str | None = None, graph_key: str | None = None) -> dict:
    """Execute already-transpiled code. Uses the warm worker (build123d kept
    loaded) with a fallback to a cold subprocess. Returns a result dict.

    write_stl=False skips the STL export (live preview runs don't need it — it is
    regenerated on demand for download/export).

    run_id names THIS run inside progress.jsonl, so a tailer can tell whose
    events it is reading — see the header line written below."""
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or uuid.uuid4().hex
    jobdir = run_dir(workdir, run_id)
    jobdir.mkdir(parents=True, exist_ok=True)
    # Exclusive claim: retrying an ID may read its progress but cannot overwrite it.
    with (jobdir / 'claimed').open('x'):
        pass
    stl_path = jobdir / "output.stl"
    view_path = jobdir / "view.json"
    script_path = jobdir / "_run.py"
    progress_path = jobdir / "progress.jsonl"
    # Each run owns its script, view and progress for its entire lifetime,
    # including queue wait. The root file is only an atomic latest-run pointer
    # for legacy subscribers and publication, never a shared worker output.
    header = {"k": "run", "r": run_id, "t": time.time()}
    progress_path.write_text(json.dumps(header) + "\n")
    with _PUBLISH_LOCK:
        atomic_write(workdir / "progress.jsonl", json.dumps(header) + "\n")
    with progress_path.open('a') as f:
        f.write(json.dumps({"k": "phase", "phase": "queued"}) + '\n')

    script_text = build_script(code, stl_path, view_path, quality, write_stl,
                               progress_path=progress_path, graph_key=graph_key)
    script_path.write_text(script_text)

    try:
        res = _run_script(script_path, workdir, timeout, jobdir / 'cancel')
        if res.get("timeout"):
            return _timeout_result(code, timeout)
        if res.get("cancelled"):
            return {"success": False, "cancelled": True, "code": code,
                    "errors": "Execution cancelled", "view": None}
        stderr = (res.get("error") or "").strip() or None
        result = _finalize(code, script_text, res.get("stdout", ""), stderr, view_path, stl_path)
        result['run_id'] = run_id
        with _PUBLISH_LOCK:
            try:
                latest = json.loads((workdir / 'progress.jsonl').read_text()).get('r')
            except (OSError, ValueError):
                latest = None  # project removed externally
            if latest == run_id and result['success']:
                for source in (view_path, stl_path):
                    if source.exists():
                        atomic_copy(source, workdir / source.name)
        return result
    finally:
        # THE RUN SAYS WHEN IT IS OVER, so the progress stream has a definite end
        # and can hang up by itself. In a `finally` because a crashed or timed-out
        # run must close its stream too — that is exactly when a node is left
        # glowing amber with no end event.
        #
        # Without this the stream had no way to know it was finished: it waited
        # for more events until 180s of silence, and detecting the client's
        # departure did not work (Request.is_disconnected() never fired once
        # inside a StreamingResponse — measured). So every run leaked a live
        # connection for three minutes; Chrome allows 6 per host, so from the
        # second run on, the editor's next EventSource never connected at all and
        # the glow went dark. That was the real reason it "stopped at random".
        try:
            with progress_path.open("a") as f:
                f.write(json.dumps({"k": "done", "r": header["r"]}) + "\n")
                f.flush()
        except OSError:
            pass
        if jobdir.exists():
            (jobdir / 'complete').touch()
        cleanup_runs(workdir)


def execute_graph(graph: Graph, workdir: Path, timeout: int = 120,
                  quality: str = "live", write_stl: bool = True,
                  run_id: str | None = None) -> dict:
    """Transpile + execute a graph end-to-end. memo=True: on the warm worker,
    nodes whose content hash is unchanged are restored from the persistent
    cache (shapes AND preview meshes) — only the dirty subtree re-runs."""
    code = transpile(graph, memo=True)
    result = execute_code(code, workdir, timeout=timeout, quality=quality,
                          write_stl=write_stl, run_id=run_id,
                          graph_key=_graph_key(graph))
    # Soft findings (cad_nodes/lint.py) ride along; they never fail a run.
    try:
        from .lint import lint_graph
        result["lint"] = lint_graph(graph)
    except Exception:  # noqa: BLE001
        result["lint"] = []
    return result


_SUBSHAPE_EPILOGUE = """

# --- sub-shape extraction (injected by executor) ---
import sys as _sys, json as _json
_sys.path.insert(0, {repo_root!r})
from cad_nodes.mesh_extractor import extract_subshapes as _extract_sub
_shp = __previews__.get({node_id!r})
if _shp is None:
    _shp = __result__
try:
    _data = _extract_sub(_shp, {kind!r})
except Exception as _e:
    _data = {{"success": False, "error": str(_e)}}
with open({out!r}, "w") as _f:
    _json.dump(_data, _f)
"""


def extract_subshapes_for_node(graph: Graph, node_id: str, kind: str,
                               workdir: Path, timeout: int = 60) -> dict:
    """Run the graph and return the pickable sub-shapes (edges/faces/vertices)
    of `node_id`'s output shape, for the interactive selection picker."""
    workdir.mkdir(parents=True, exist_ok=True)
    # Force-preview the target so its shape lands in __previews__[node_id].
    try:
        graph.node(node_id).preview = True
    except KeyError:
        return {"success": False, "error": f"no node {node_id!r}"}

    # memo=True: right after an execute, the whole graph is warm in the cache,
    # so the picker's re-run costs almost nothing.
    code = transpile(graph, memo=True)
    return _run_json_tool(code, workdir, _SUBSHAPE_EPILOGUE, timeout,
                          node_id=node_id, kind=kind)


_TOOL_EPILOGUE = """

# --- {func} (injected by executor) ---
import sys as _sys, json as _json
_sys.path.insert(0, {repo_root!r})
from cad_nodes.slice_summary import {func} as _tool
try:
    _data = _tool(__result__, {kwargs})
except Exception as _e:
    _data = {{"success": False, "error": f"{{type(_e).__name__}}: {{_e}}"}}
with open({out}, "w") as _f:
    _json.dump(_data, _f)
"""


def _run_json(script: str, workdir: Path, stem: str, timeout: int) -> dict:
    """Run a script that ends by writing its answer to `<workdir>/_<stem>.json`
    (the name is passed in as the format field `out`) and return that JSON.
    Warm worker with a cold-subprocess fallback."""
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    # Each call gets its own temp dir (as every run gets its own .runs/ dir): two
    # tools on the same project never read each other's half-written answer.
    with tempfile.TemporaryDirectory(prefix=f".{stem}-", dir=workdir) as tmp:
        out_path = Path(tmp) / "result.json"
        script_path = Path(tmp) / "_run.py"
        script_path.write_text(script.replace("@@OUT@@", repr(str(out_path))))
        res = _run_script(script_path, workdir, timeout)
        if res.get("timeout"):
            return {"success": False, "error": f"timed out after {timeout}s"}
        if out_path.exists():
            try:
                return json.loads(out_path.read_text())
            except Exception:
                pass
        return {"success": False, "error": (res.get("error") or "no output")[:600]}


def _run_json_tool(code: str, workdir: Path, epilogue: str, timeout: int, **kwargs) -> dict:
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.tool-', dir=workdir) as tmp:
        out = Path(tmp) / 'result.json'
        script = Path(tmp) / '_run.py'
        script.write_text(code + epilogue.format(repo_root=_REPO_ROOT, out=str(out), **kwargs))
        res = _run_script(script, workdir, timeout)
        if not res.get('timeout') and not res.get('error') and out.exists():
            return json.loads(out.read_text())
        return {'success': False, 'error': (res.get('error') or 'timeout / no output')[:600]}


def _run_slice(code: str, workdir: Path, func: str, kwargs: str,
               timeout: int) -> dict:
    """Run `code` (which must define __result__) + a slice_summary-tool
    epilogue calling `func(__result__, kwargs)`; return the JSON it writes."""
    return _run_json(code + _TOOL_EPILOGUE.format(
        repo_root=_REPO_ROOT, func=func, kwargs=kwargs, out="@@OUT@@"),
        workdir, func, timeout)


def _graph_code(graph: Graph, node: str | None = None) -> str:
    """The graph's program; with `node` (a reference: n5, n5.out, n5[2]) the
    tools read THAT node's value instead of the combined preview result."""
    if not node:
        return transpile(graph, memo=True)
    from .measure import _base_ref, parse_ref
    t = Transpiler(graph, memo=True)
    code = t.run()
    expr = _ref_exprs(graph, t, [node])[_base_ref(node)]
    idx = parse_ref(node)["idx"]
    if idx is not None:
        expr = f"list({expr} or [])[{idx}]"
    return code + f"\n__result__ = {expr}\n"


def _ref_exprs(graph: Graph, t: Transpiler, refs: list[str]) -> dict[str, str]:
    """{base reference: python expression} for node references (measure.parse_ref
    grammar; an index `[i]` is dropped — the caller applies it) against a
    transpiled program. Resolution mirrors Transpiler._src_expr (a per-socket var
    if the node has one, else its single var). The expression reads the var via
    globals().get, so a var that was never bound reads None, not NameError."""
    from . import catalog
    from .graph import codeblock_output
    from .measure import _base_ref, parse_ref
    out = {}
    for ref in refs:
        p = parse_ref(ref)
        try:
            node = graph.node(p["node"])
        except KeyError:
            raise ValueError(f"{ref}: no node {p['node']!r} in the graph") from None
        sock = p["out"]
        if sock and catalog.get(node.type).output(sock) is None \
                and codeblock_output(node, sock) is None:
            raise ValueError(f"{ref}: {node.type} {node.id} has no output {sock!r}")
        var = t.out_var_of.get((node.id, sock or "result")) or t.var_of.get(node.id)
        if var is None:
            raise ValueError(f"{ref}: node {node.id} ({node.type}) has no value of "
                             "its own (inside a group, bypassed or a sink)")
        out[_base_ref(ref)] = f"globals().get({var!r})"
    return out


_MEASURE_EPILOGUE = """

# --- measure (injected by executor) ---
import sys as _sys, json as _json
_sys.path.insert(0, {repo_root!r})
from cad_nodes.measure import run_queries as _rq
_vals = {{{vals}}}
try:
    _data = _rq(_vals, _json.loads({queries!r}), __errors__)
except Exception as _e:
    _data = {{"success": False, "error": f"{{type(_e).__name__}}: {{_e}}"}}
_data["node_errors"] = {{k: v for k, v in __errors__.items() if k in {nodes!r}}}
with open(@@OUT@@, "w") as _f:
    _json.dump(_data, _f, separators=(",", ":"))
"""


def measure_graph(graph: Graph, workdir: Path, queries: list[dict],
                  timeout: int = 120) -> dict:
    """Geometry facts about node outputs (see cad_nodes/measure.py): run the
    graph (memo: nearly free right after an execute) and answer `queries`
    ({"op": props|interference|distance|section|probe|summary, …}) against the
    runtime values of the nodes they name. Bad queries fail alone."""
    from .measure import _base_ref, refs_of
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries: a non-empty list of {op, node|a+b|nodes, …}")
    if len(queries) > 50:
        raise ValueError("at most 50 queries per call")
    t = Transpiler(graph, memo=True)
    code = t.run()
    exprs: dict[str, str] = {}
    bad: dict[int, str] = {}
    for i, q in enumerate(queries):
        try:
            refs = refs_of(q)
            exprs.update(_ref_exprs(graph, t, [_base_ref(r) for r in refs]))
        except ValueError as e:
            bad[i] = str(e)
    ok = [q for i, q in enumerate(queries) if i not in bad]
    nodes = sorted({k.split(".")[0] for k in exprs})
    if ok:
        vals = ", ".join(f"{k!r}: {v}" for k, v in exprs.items())
        data = _run_json(code + _MEASURE_EPILOGUE.format(
            repo_root=_REPO_ROOT, vals=vals, queries=json.dumps(ok), nodes=nodes),
            workdir, "measure", timeout)
    else:
        data = {"success": True, "results": []}
    if not data.get("success"):
        return data
    # stitch the pre-flight failures back in their original positions
    it = iter(data.get("results", []))
    results = []
    for i, q in enumerate(queries):
        if i in bad:
            head = {k: q[k] for k in ("op", "node", "a", "b", "nodes") if k in q}
            results.append({**head, "error": f"ValueError: {bad[i]}"})
        else:
            results.append(next(it, {"op": q.get("op"), "error": "no result"}))
    data["results"] = results
    return data


def _import_code(path: Path) -> str:
    # STL: __result__ stays the PATH — the slice tools slice meshes themselves
    # (triangle/plane intersection + arc-fitting); OCCT's section() segfaults
    # on the mesh Face that import_stl returns.
    if path.suffix.lower() == ".stl":
        return f"__result__ = {str(path)!r}\n"
    return ("from build123d import *\n"
            f"__result__ = import_step({str(path)!r})\n")


def slice_summary_graph(graph: Graph, workdir: Path, n_per_axis: int = 10,
                        timeout: int = 120, node: str | None = None) -> dict:
    """Symbolic slice summary of the graph's own result (the verify half of
    the retro-engineering loop — see cad_nodes/slice_summary.py). `node` (n5,
    n51.body, n51[3]) slices that node's output instead of the combined one."""
    return _run_slice(_graph_code(graph, node), workdir, "summarize",
                      f"n_per_axis={int(n_per_axis)}", timeout)


def slice_summary_file(path: Path, workdir: Path, n_per_axis: int = 10,
                       timeout: int = 120) -> dict:
    """Symbolic slice summary of a STEP file (the perception half)."""
    return _run_slice(_import_code(path), workdir, "summarize",
                      f"n_per_axis={int(n_per_axis)}", timeout)


def section_outline_graph(graph: Graph, workdir: Path, axis: str = "z",
                          position: float = 0.0, timeout: int = 120,
                          node: str | None = None) -> dict:
    """Exact edge-by-edge outline of ONE section of the graph's result (or of
    one node's output, `node` as in slice_summary_graph)."""
    return _run_slice(_graph_code(graph, node), workdir, "outline",
                      f"axis={str(axis)!r}, position={float(position)}", timeout)


def section_outline_file(path: Path, workdir: Path, axis: str = "z",
                         position: float = 0.0, timeout: int = 120) -> dict:
    """Exact edge-by-edge outline of ONE section of a STEP file."""
    return _run_slice(_import_code(path), workdir, "outline",
                      f"axis={str(axis)!r}, position={float(position)}", timeout)


_EXPORTERS = {
    "step": ("export_step", "step"),
    "stl": ("export_stl", "stl"),
    "gltf": ("export_gltf", "gltf"),
}


def export_graph(graph: Graph, workdir: Path, fmt: str = "step",
                 timeout: int = 120) -> Path:
    """Transpile, execute and write `__result__` to a file in `fmt`."""
    fmt = fmt.lower()
    if fmt not in _EXPORTERS:
        raise ValueError(f"Unsupported export format {fmt!r}; "
                         f"choose from {sorted(_EXPORTERS)}")
    func, ext = _EXPORTERS[fmt]
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    cleanup_runs(workdir)
    code = transpile(graph, memo=True)
    jobdir = run_dir(workdir, uuid.uuid4().hex)
    jobdir.mkdir(parents=True)
    (jobdir / 'claimed').touch()
    out_path = jobdir / f"output.{ext}"
    script_path = jobdir / "_export.py"
    script = _key_head(_graph_key(graph)) + code + (
        f"\n# --- export (injected) ---\n"
        f"from build123d import {func}\n"
        f"if __result__ is None:\n"
        f"    raise RuntimeError('the graph has no final result (its last node is an "
        f"Export/Display node, or it failed) - use Everything visible (STEP+STL zip)')\n"
        f"{func}(__result__, {str(out_path)!r})\n"
    )
    script_path.write_text(script)

    try:
        res = _run_script(script_path, workdir.resolve(), timeout)
        if res.get('timeout') or res.get('error') or not out_path.exists():
            raise RuntimeError(f"Export failed: {res.get('error') or 'timeout / no output'}")
        atomic_copy(out_path, workdir / out_path.name)
        return out_path  # immutable file: a concurrent export cannot replace the download
    finally:
        (jobdir / 'complete').touch()


_BAKE_EPILOGUE = """

# --- bake every previewed node (injected by executor) ---
import sys as _sys, json as _json
_sys.path.insert(0, {repo_root!r})
from cad_nodes.bake import bake_previews as _bake
_bake(__previews__, __result__, {outdir!r}, {labels!r})
"""


def export_bundle(graph: Graph, workdir: Path, labels: dict | None = None,
                  timeout: int = 180) -> tuple[Path, dict]:
    """Bake every PREVIEWED node (what the viewport shows) to STEP + STL and zip
    them with a manifest.json. Returns (zip path inside the run dir, manifest).
    The zip is immutable, like export_graph's output: the caller copies it."""
    import zipfile
    workdir = workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    cleanup_runs(workdir)
    code = transpile(graph, memo=True)
    jobdir = run_dir(workdir, uuid.uuid4().hex)
    jobdir.mkdir(parents=True)
    (jobdir / 'claimed').touch()
    outdir = jobdir / "bake"
    script_path = jobdir / "_bake.py"
    script_path.write_text(
        _key_head(_graph_key(graph)) + code
        + _BAKE_EPILOGUE.format(repo_root=_REPO_ROOT, outdir=str(outdir),
                                labels=labels or {}))
    try:
        res = _run_script(script_path, workdir, timeout)
        man_path = outdir / "manifest.json"
        if res.get('timeout') or res.get('error') or not man_path.exists():
            raise RuntimeError(f"Bake failed: {(res.get('error') or 'timeout / no output')[-600:]}")
        manifest = json.loads(man_path.read_text())
        if not any(n.get("files") for n in manifest.get("nodes", [])):
            raise RuntimeError("Nothing to export: no previewed node produced a solid, "
                               "a surface, a mesh or a curve.")
        zpath = jobdir / "bundle.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(outdir.iterdir()):
                z.write(f, f.name)
        return zpath, manifest
    finally:
        (jobdir / 'complete').touch()
