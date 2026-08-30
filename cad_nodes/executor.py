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
import threading
import time
import uuid
from pathlib import Path

from .graph import Graph
from .transpiler import transpile

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
    if "nameerror" in low:
        return ("Nome non definito nel codice generato.",
                "Probabile errore in un nodo CodeBlock o Expression.")
    # Fallback: surface the raw exception.
    return (exc_line or "Errore di esecuzione.", "")


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


def build_script(code: str, stl_path: Path, view_path: Path,
                 quality: str = "live", write_stl: bool = True,
                 progress_path: Path | None = None) -> str:
    lin, ang = _QUALITY.get(quality, _QUALITY["live"])
    # The PREAMBLE guards __PROGRESS_PATH__ with try/except NameError (same idiom
    # as __MEMO__), so assigning it *before* the transpiled code sticks: the node
    # _ev() calls then append to this file while the run is still going, and the
    # editor tails it. No header = progress is a no-op.
    head = f"__PROGRESS_PATH__ = {str(progress_path)!r}\n" if progress_path else ""
    # write_stl=False (live runs) skips the STL export in the epilogue — an empty
    # path makes extract_and_write no-op it. The STL is regenerated on demand by
    # the download/render routes, saving ~0.9s on every live re-run.
    return head + code + _EPILOGUE.format(
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

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    def _read_sentinel(self, timeout: float):
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
        t.join(timeout)
        if t.is_alive():
            return None
        return box.get("v")

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-u", _WORKER_PATH],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, cwd=_REPO_ROOT,
        )
        ready = self._read_sentinel(timeout=120)
        if not ready or not ready.get("ready"):
            self._kill()
            raise RuntimeError("warm worker failed to import build123d")

    def shutdown(self) -> None:
        """Terminate the resident worker, freeing its build123d memory. The next
        run() lazily respawns it (if warm mode is still on)."""
        with self._lock:
            self._kill()

    def run(self, script_path: Path, cwd: Path, timeout: float) -> dict:
        with self._lock:
            if not self._alive():
                self._spawn()
            job = json.dumps({"cmd": "run", "script_path": str(script_path),
                              "cwd": str(cwd)})
            try:
                self._proc.stdin.write(job + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._kill()
                raise
            res = self._read_sentinel(timeout)
            if res is None:                 # timed out or crashed mid-job
                self._kill()
                return {"timeout": True}
            return res


_WORKER = WarmWorker()


def warm_status() -> dict:
    """Current warm-worker mode + whether a resident process is alive."""
    return {"enabled": _warm_enabled, "alive": _WORKER._alive()}


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
        result["node_errors"][nid] = {"exception": raw, "message": msg, "hint": hint}
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


def execute_code(code: str, workdir: Path, timeout: int = 120,
                 quality: str = "live", write_stl: bool = True,
                 run_id: str | None = None) -> dict:
    """Execute already-transpiled code. Uses the warm worker (build123d kept
    loaded) with a fallback to a cold subprocess. Returns a result dict.

    write_stl=False skips the STL export (live preview runs don't need it — it is
    regenerated on demand for download/export).

    run_id names THIS run inside progress.jsonl, so a tailer can tell whose
    events it is reading — see the header line written below."""
    workdir.mkdir(parents=True, exist_ok=True)
    stl_path = workdir / "output.stl"
    view_path = workdir / "view.json"
    script_path = workdir / "_run.py"
    progress_path = workdir / "progress.jsonl"

    if view_path.exists():
        view_path.unlink()
    # Every run opens progress.jsonl with a header line naming itself. That id is
    # the ONLY thing that distinguishes one run's events from the next one's:
    # the file lives at a fixed path per project and two warm runs of the same
    # graph write near-identical bytes, so size, mtime and offset can all agree
    # across a run boundary. A tailer that watched the size alone missed entire
    # runs (measured: 5/5 nodes lost on a small graph) — the glow simply never
    # fired, at random, which is what this header exists to make impossible.
    header = {"k": "run", "r": run_id or uuid.uuid4().hex, "t": time.time()}
    progress_path.write_text(json.dumps(header) + "\n")

    script_text = build_script(code, stl_path, view_path, quality, write_stl,
                               progress_path=progress_path)
    script_path.write_text(script_text)

    try:
        # --- warm path ---------------------------------------------------
        if _warm_enabled:
            try:
                res = _WORKER.run(script_path, workdir, timeout)
                if res.get("timeout"):
                    return _timeout_result(code, timeout)
                stderr = (res.get("error") or "").strip() or None
                return _finalize(code, script_text, res.get("stdout", ""),
                                 stderr, view_path, stl_path)
            except Exception:
                pass  # worker unavailable → fall back to a cold subprocess

        # --- cold fallback -----------------------------------------------
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True, timeout=timeout, cwd=str(workdir),
            )
        except subprocess.TimeoutExpired:
            return _timeout_result(code, timeout)

        stderr = proc.stderr if proc.returncode != 0 else None
        return _finalize(code, script_text, proc.stdout, stderr, view_path, stl_path)
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


def execute_graph(graph: Graph, workdir: Path, timeout: int = 120,
                  quality: str = "live", write_stl: bool = True,
                  run_id: str | None = None) -> dict:
    """Transpile + execute a graph end-to-end. memo=True: on the warm worker,
    nodes whose content hash is unchanged are restored from the persistent
    cache (shapes AND preview meshes) — only the dirty subtree re-runs."""
    code = transpile(graph, memo=True)
    return execute_code(code, workdir, timeout=timeout, quality=quality,
                        write_stl=write_stl, run_id=run_id)


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
    out_path = workdir / "subshapes.json"
    script_path = workdir / "_subshapes.py"
    if out_path.exists():
        out_path.unlink()
    script_path.write_text(code + _SUBSHAPE_EPILOGUE.format(
        repo_root=_REPO_ROOT, node_id=node_id, kind=kind, out=str(out_path)))

    def _read():
        if out_path.exists():
            try:
                return json.loads(out_path.read_text())
            except Exception:
                return None
        return None

    if _warm_enabled:
        try:
            res = _WORKER.run(script_path, workdir, timeout)
            if not res.get("timeout"):
                data = _read()
                if data is not None:
                    return data
                return {"success": False, "error": (res.get("error") or "no output")[:600]}
        except Exception:
            pass  # fall back to a cold subprocess

    try:
        proc = subprocess.run([sys.executable, str(script_path)],
                              capture_output=True, text=True,
                              timeout=timeout, cwd=str(workdir))
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"timed out after {timeout}s"}
    data = _read()
    if data is not None:
        return data
    return {"success": False, "error": (proc.stderr or "no output")[:600]}


_TOOL_EPILOGUE = """

# --- {func} (injected by executor) ---
import sys as _sys, json as _json
_sys.path.insert(0, {repo_root!r})
from cad_nodes.slice_summary import {func} as _tool
try:
    _data = _tool(__result__, {kwargs})
except Exception as _e:
    _data = {{"success": False, "error": f"{{type(_e).__name__}}: {{_e}}"}}
with open({out!r}, "w") as _f:
    _json.dump(_data, _f)
"""


def _run_slice(code: str, workdir: Path, func: str, kwargs: str,
               timeout: int) -> dict:
    """Run `code` (which must define __result__) + a slice_summary-tool
    epilogue calling `func(__result__, kwargs)`; return the JSON it writes.
    Warm worker with a cold-subprocess fallback."""
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / f"_{func}.json"
    script_path = workdir / f"_{func}.py"
    if out_path.exists():
        out_path.unlink()
    script_path.write_text(code + _TOOL_EPILOGUE.format(
        repo_root=_REPO_ROOT, func=func, kwargs=kwargs, out=str(out_path)))

    def _read():
        if out_path.exists():
            try:
                return json.loads(out_path.read_text())
            except Exception:
                return None
        return None

    if _warm_enabled:
        try:
            res = _WORKER.run(script_path, workdir, timeout)
            if res.get("timeout"):
                return {"success": False, "error": f"timed out after {timeout}s"}
            data = _read()
            if data is not None:
                return data
            return {"success": False, "error": (res.get("error") or "no output")[:600]}
        except Exception:
            pass  # fall back to a cold subprocess

    try:
        proc = subprocess.run([sys.executable, str(script_path)],
                              capture_output=True, text=True,
                              timeout=timeout, cwd=str(workdir))
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"timed out after {timeout}s"}
    data = _read()
    if data is not None:
        return data
    return {"success": False, "error": (proc.stderr or "no output")[:600]}


def _import_code(path: Path) -> str:
    # STL: __result__ stays the PATH — the slice tools slice meshes themselves
    # (triangle/plane intersection + arc-fitting); OCCT's section() segfaults
    # on the mesh Face that import_stl returns.
    if path.suffix.lower() == ".stl":
        return f"__result__ = {str(path)!r}\n"
    return ("from build123d import *\n"
            f"__result__ = import_step({str(path)!r})\n")


def slice_summary_graph(graph: Graph, workdir: Path, n_per_axis: int = 10,
                        timeout: int = 120) -> dict:
    """Symbolic slice summary of the graph's own result (the verify half of
    the retro-engineering loop — see cad_nodes/slice_summary.py)."""
    return _run_slice(transpile(graph, memo=True), workdir, "summarize",
                      f"n_per_axis={int(n_per_axis)}", timeout)


def slice_summary_file(path: Path, workdir: Path, n_per_axis: int = 10,
                       timeout: int = 120) -> dict:
    """Symbolic slice summary of a STEP file (the perception half)."""
    return _run_slice(_import_code(path), workdir, "summarize",
                      f"n_per_axis={int(n_per_axis)}", timeout)


def section_outline_graph(graph: Graph, workdir: Path, axis: str = "z",
                          position: float = 0.0, timeout: int = 120) -> dict:
    """Exact edge-by-edge outline of ONE section of the graph's result."""
    return _run_slice(transpile(graph, memo=True), workdir, "outline",
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
    workdir.mkdir(parents=True, exist_ok=True)
    out_path = workdir / f"output.{ext}"
    script_path = workdir / "_export.py"

    code = transpile(graph)
    script = code + (
        f"\n# --- export (injected) ---\n"
        f"from build123d import {func}\n"
        f"{func}(__result__, {str(out_path)!r})\n"
    )
    script_path.write_text(script)

    proc = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, text=True, timeout=timeout, cwd=str(workdir),
    )
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"Export failed:\n{proc.stderr}")
    return out_path
