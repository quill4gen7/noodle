"""
noodle — Unified API server for AI + webui CAD modeling.
Engine: node graphs transpiled to build123d and run in an isolated worker.
"""

import asyncio
import collections
import itertools
import json
import logging
import os
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Node-based CAD engine (pure imports; build123d only used in the subprocess)
from cad_nodes import api, catalog, layout
from cad_nodes.graph import Graph, ValidationError
from cad_nodes.screenshot import ScreenshotUnavailable
from cad_nodes.transpiler import transpile, transpile_with_map
from cad_nodes.executor import execute_graph, export_graph, extract_subshapes_for_node
from cad_nodes.store import GraphStore, stamp_agent_tags, validate_graph_id
from cad_nodes.copilot import run_chat, copilot_status
from cad_nodes import fonts as fontlib

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECTS_DIR = Path("/app/projects")
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Shared custom-font library (uploaded .ttf/.otf, reusable across projects). It
# lives UNDER projects/ (the only writable mount) but is NOT a project — filtered
# out of project/library listings by name. See cad_nodes/fonts.py.
FONTS_DIR = PROJECTS_DIR / fontlib.FONTS_DIRNAME
_RESERVED_PROJECT_DIRS = {fontlib.FONTS_DIRNAME}

# Personal add-node search aliases, {node_type: [word, ...]}. Lives under
# projects/ for the same reason FONTS_DIR does — it is the only writable mount —
# but as a FILE, so the project/library listings (which filter on is_dir()) never
# see it, and the name starts with "_" which validate_graph_id forbids, so it can
# never collide with a project. Kept deliberately plain and greppable: aliases
# that earn their keep get promoted BY HAND into NodeDef.aliases in catalog.py.
ALIASES_PATH = PROJECTS_DIR / "_aliases.json"
_ALIAS_MAX_WORDS = 12
_ALIAS_MAX_LEN = 40

# User feedback/report drops (see docs/FEEDBACK_FIX_GUIDE.md). A dedicated rw
# volume kept out of projects/ so a coding agent can find them at the repo root.
FEEDBACK_DIR = Path("/app/feedback")
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

APP_VERSION = "0.1.0"

app = FastAPI(title="noodle", version="0.1.0")

# Serve webui static files
app.mount("/static", StaticFiles(directory="/app/webui"), name="static")


# ---------------------------------------------------------------------------
# System: backend log capture (ring buffer) + health/uptime
# ---------------------------------------------------------------------------
# A small in-memory ring buffer that the UI polls via /api/system/logs so the
# uvicorn + app logs are visible in-app (no terminal needed). Capped so it can't
# grow unbounded; each entry carries a monotonic seq for incremental polling.
_BOOT_TIME = time.time()
_LOG_BUFFER: "collections.deque[dict]" = collections.deque(maxlen=2000)
_LOG_SEQ = itertools.count(1)
_LOG_LOCK = threading.Lock()

# A progress stream with nothing to say for this long gives up, so an editor tab left
# open overnight doesn't hold a generator forever. Comfortably longer than the 120s
# execute timeout, so a slow-but-alive run is never cut off mid-flight.
_PROGRESS_MAX_IDLE = 180.0

logger = logging.getLogger("noodle")


class _RingBufferHandler(logging.Handler):
    """Logging handler that appends formatted records into _LOG_BUFFER."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        # Don't let the UI's own health/log pollers spam the console buffer.
        if record.name == "uvicorn.access" and ("/api/system/logs" in msg or "/api/system/health" in msg):
            return
        with _LOG_LOCK:
            _LOG_BUFFER.append({
                "seq": next(_LOG_SEQ),
                "ts": record.created,
                "level": record.levelname.lower(),
                "source": "backend",
                "logger": record.name,
                "msg": msg,
            })


_RING_HANDLER = _RingBufferHandler()
_RING_HANDLER.setLevel(logging.INFO)


def _install_log_capture() -> None:
    """Attach the ring-buffer handler to the root + uvicorn loggers.

    Called at import AND on startup: uvicorn reconfigures logging when it boots,
    so re-attaching after startup guarantees access/error logs are captured.
    """
    root = logging.getLogger()
    if _RING_HANDLER not in root.handlers:
        root.addHandler(_RING_HANDLER)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # Capture only at the root and let uvicorn's loggers propagate up, so each
    # record lands in the buffer exactly once (no double-capture).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.propagate = True
        if _RING_HANDLER in lg.handlers:
            lg.removeHandler(_RING_HANDLER)


_install_log_capture()


@app.on_event("startup")
async def _on_startup() -> None:
    _install_log_capture()
    try:
        seeded = GraphStore(PROJECTS_DIR).seed_examples()
        if seeded:
            logger.info("seeded example projects: %s", ", ".join(seeded))
    except Exception:  # seeding is a nicety — never let it block startup
        logger.exception("example seeding failed")
    logger.info("noodle backend ready (pid %s)", os.getpid())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class CopilotPayload(BaseModel):
    graph: str
    messages: list   # [{role: "user"|"assistant", content: str}, ...]


class ParamPatch(BaseModel):
    node_id: str
    param: str       # built-in param name, or "_cb.<name>" for a CodeBlock override
    value: object    # number / bool / str — coerced + clamped server-side


class AliasPayload(BaseModel):
    aliases: list[str] = []             # [] clears this node's personal aliases


class FeedbackPayload(BaseModel):
    project: Optional[str] = None       # current project/graph name, if any
    message: str                        # free-text feedback (required)
    severity: str = "bug"               # "bug" | "idea" | "question"
    context: dict = {}                   # client-collected, non-sensitive context
    graph: Optional[dict] = None         # current graph snapshot (opt-in)
    logs: list = []                      # recent backend log entries (opt-in)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def project_dir(name: str) -> Path:
    # validate_graph_id rejects path separators and ".." so a crafted project
    # name can never resolve outside PROJECTS_DIR.
    try:
        return PROJECTS_DIR / validate_graph_id(name)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def require_project(name: str) -> Path:
    d = project_dir(name)
    if not d.is_dir():
        raise HTTPException(404, f"Project '{name}' not found")
    return d


async def off_loop(fn, *args, **kwargs):
    """Run a CAD-engine call in a worker thread instead of on the event loop.

    EVERY route that reaches cad_nodes.executor — execute, render, download,
    export, slice_summary, section_outline, subshapes — must go through here.
    Those calls are seconds of blocking CPU (a graph run, or a cold build123d
    import at ~2.7s), and while one holds the loop NOTHING else is served: not
    the next request, and not /api/graph/{name}/progress, the SSE stream that
    reports on the very run in flight. Six of the seven used to be called
    directly from `async def` and froze the server for their duration;
    `subshapes` is the one that hurt most, since the selection picker calls it
    on every click.

    They serialise anyway inside the warm worker's own lock (executor.py), so
    moving them off the loop makes concurrent calls queue rather than freeze.

    NOT for /screenshot: the work happens in the browser process, so that
    coroutine only awaits I/O (and the run it triggers goes through /execute,
    which is already off the loop). Await, don't offload, when there is no
    blocking CPU to move.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


# ---------------------------------------------------------------------------
# System controls (single-user/local app): health, logs, restart
# ---------------------------------------------------------------------------
@app.get("/api/system/health")
async def system_health():
    """Always-on backend health used by the top-bar status dot."""
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptime_s": round(time.time() - _BOOT_TIME, 1),
        "pid": os.getpid(),
    }


@app.get("/api/system/logs")
async def system_logs(since: int = 0, limit: int = 500):
    """Incremental backend log stream for the in-app console.

    Pass the previously returned `last` as `since` to fetch only new lines.
    """
    with _LOG_LOCK:
        items = [e for e in _LOG_BUFFER if e["seq"] > since]
    if limit and len(items) > limit:
        items = items[-limit:]
    last = items[-1]["seq"] if items else since
    return {"entries": items, "last": last}


@app.get("/api/system/warm")
async def system_warm_get():
    """Warm-worker state: whether the persistent build123d process is enabled
    and currently resident. The UI shows this as a toggle."""
    from cad_nodes import executor
    return executor.warm_status()


@app.post("/api/system/warm")
async def system_warm_set(body: dict = Body(default={})):
    """Toggle the persistent (warm) worker. Off shuts it down to free memory
    while noodle is idle; on lets the next run spawn it (first run pays the
    ~2.7s build123d import, later runs skip it).

    Off the loop like the engine routes, though for a different reason: this one
    does no CPU work itself, it WAITS. Turning warm off calls WarmWorker.
    shutdown(), which takes the same lock a run holds for its whole duration —
    so clicking the toggle mid-run froze the server until that run finished
    (measured: /health at 601ms during a render). The wait itself is correct and
    stays: killing the worker out from under a running job would be worse. It is
    also bounded, since a run cannot outlive its own timeout.
    """
    from cad_nodes import executor
    return await off_loop(executor.set_warm, bool(body.get("enabled", True)))


@app.post("/api/system/restart")
async def system_restart():
    """Restart the backend process.

    Acceptable because the app is local/single-user. uvicorn runs as PID 1 under
    `restart: unless-stopped`, so exiting hands control back to Docker, which
    brings the process straight back up. The UI then polls /api/system/health
    until it answers again.
    """
    logger.warning("restart requested via /api/system/restart — exiting for supervisor restart")

    def _die() -> None:
        time.sleep(0.4)  # let the HTTP response flush first
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_die, daemon=True).start()
    return {"status": "restarting"}


# ---------------------------------------------------------------------------
# Feedback / report drops
# ---------------------------------------------------------------------------
# A local, non-sensitive feedback channel: the UI saves a report here so an
# external coding agent (Claude Code via MCP, see AGENTS.md) can pick it up,
# reproduce the issue, fix it safely, and open a PR. The end-to-end workflow —
# git safety, repo rules, and the *mandatory AI-use disclosure* in the PR — lives
# in .claude/skills/feedback-fix/SKILL.md → docs/FEEDBACK_FIX_GUIDE.md.

def _slugify(text: str, default: str = "report") -> str:
    keep = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
    slug = "-".join(p for p in keep.split("-") if p)[:40]
    return slug or default


def _git_commit() -> Optional[str]:
    """Best-effort short commit hash; None if .git isn't available in the image."""
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="/app", capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


def _render_report_md(fid: str, fb: "FeedbackPayload", stamp: dict) -> str:
    ctx = fb.context or {}
    errs = ctx.get("node_errors") or {}
    err_lines = "\n".join(f"  - `{k}`: {v}" for k, v in errs.items()) or "  - (nessuno)"
    lines = [
        f"# Feedback report — {stamp['severity']}",
        "",
        f"- **id**: `{fid}`",
        f"- **created (UTC)**: {stamp['created_utc']}",
        f"- **project**: {fb.project or '(nessuno)'}",
        f"- **app version**: {stamp['version']}"
        + (f" · commit `{stamp['commit']}`" if stamp.get("commit") else ""),
        f"- **client**: {ctx.get('user_agent', '?')}",
        f"- **url**: {ctx.get('url', '?')}",
        "",
        "## Messaggio",
        "",
        fb.message.strip() or "(vuoto)",
        "",
        "## Contesto tecnico",
        "",
        "- ultimi errori per-nodo:",
        err_lines,
        f"- snapshot grafo allegato: {'sì (`graph.snapshot.json`)' if fb.graph else 'no'}",
        f"- log backend allegati: {'sì (`backend.log`)' if fb.logs else 'no'}",
        "",
        "## Come riprodurre",
        "",
        "1. Apri il progetto indicato (o carica `graph.snapshot.json`).",
        "2. Esegui il grafo e osserva il comportamento descritto sopra.",
        "",
        "---",
        "",
        "## Per l'agente di coding",
        "",
        "Prima di modificare codice, leggi **`.claude/skills/feedback-fix/SKILL.md`**",
        "e la guida **`docs/FEEDBACK_FIX_GUIDE.md`**: regole git per tornare a uno",
        "stato sicuro, regole di reload della repo, e la **disclosure obbligatoria**",
        "(agenti/modelli usati) da includere nella PR.",
    ]
    return "\n".join(lines) + "\n"


@app.post("/api/feedback")
async def save_feedback(fb: FeedbackPayload):
    """Persist a user feedback/report drop under feedback/<ts>-<slug>/."""
    if not (fb.message or "").strip():
        raise HTTPException(400, "message is required")

    now = time.gmtime()
    ts = time.strftime("%Y%m%d-%H%M%S", now)
    fid = f"{ts}-{_slugify(fb.project or fb.message)}"
    stamp = {
        "id": fid,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
        "severity": fb.severity,
        "version": APP_VERSION,
        "commit": _git_commit(),
    }

    d = FEEDBACK_DIR / fid
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({
        **stamp,
        "project": fb.project,
        "message": fb.message,
        "context": fb.context,
        "has_graph": bool(fb.graph),
        "has_logs": bool(fb.logs),
    }, indent=2))
    (d / "report.md").write_text(_render_report_md(fid, fb, stamp))
    if fb.graph:
        (d / "graph.snapshot.json").write_text(json.dumps(fb.graph, indent=2))
    if fb.logs:
        lines = []
        for e in fb.logs:
            if isinstance(e, dict):
                lines.append(f"{e.get('ts','')} {e.get('level','')} {e.get('logger','')} {e.get('msg','')}")
            else:
                lines.append(str(e))
        (d / "backend.log").write_text("\n".join(lines) + "\n")

    logger.info("feedback saved: %s", fid)
    return {"status": "saved", "id": fid, "dir": f"feedback/{fid}", "path": str(d)}


@app.get("/api/feedback")
async def list_feedback():
    """List saved feedback reports (newest first) for tooling / triage."""
    items = []
    for d in sorted(FEEDBACK_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        rep = d / "report.json"
        if not rep.exists():
            continue
        try:
            data = json.loads(rep.read_text())
        except Exception:
            continue
        msg = (data.get("message") or "").strip().splitlines()
        items.append({
            "id": data.get("id", d.name),
            "created_utc": data.get("created_utc"),
            "project": data.get("project"),
            "severity": data.get("severity"),
            "summary": msg[0] if msg else "",
        })
    return {"reports": items}


# ---------------------------------------------------------------------------
# WebUI
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home():
    page = Path("/app/webui/home.html")
    if page.exists():
        return page.read_text()
    # Fall back to the editor so a fresh checkout without home.html still lands somewhere usable.
    return HTMLResponse('<h1>noodle</h1><p><a href="/nodes">Open the node editor</a></p>')


@app.get("/ui", response_class=HTMLResponse)
async def webui():
    index = Path("/app/webui/index.html")
    if index.exists():
        return index.read_text()
    return HTMLResponse("<h1>noodle</h1><p>WebUI not found</p>", status_code=404)


@app.get("/nodes", response_class=HTMLResponse)
async def webui_nodes():
    page = Path("/app/webui/nodes.html")
    if page.exists():
        return page.read_text()
    return HTMLResponse("<h1>noodle</h1><p>Node editor not found</p>", status_code=404)


@app.get("/library", response_class=HTMLResponse)
async def webui_library():
    page = Path("/app/webui/library.html")
    if page.exists():
        return page.read_text()
    return HTMLResponse("<h1>noodle</h1><p>Library not found</p>", status_code=404)


# ---------------------------------------------------------------------------
# Projects CRUD
# ---------------------------------------------------------------------------
@app.get("/api/projects")
async def list_projects():
    projects = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if d.is_dir() and d.name not in _RESERVED_PROJECT_DIRS:
            meta = {}
            meta_path = d / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
            thumb = d / THUMB_NAME
            projects.append({
                "name": d.name,
                "backend": meta.get("backend", "nodegraph"),
                "description": meta.get("description", ""),
                # the listing carries the thumbnail's mtime rather than a bare
                # flag: it doubles as the cache-buster for <img src>, so a
                # freshly re-shot workflow shows its new picture immediately.
                "thumb": int(thumb.stat().st_mtime) if thumb.exists() else 0,
            })
    return projects


@app.delete("/api/projects/{name}")
async def delete_project(name: str):
    d = require_project(name)
    shutil.rmtree(d)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Workflow thumbnails
# ---------------------------------------------------------------------------
# A name in a list does not say what the part is; a picture does. The picture is
# the one the EDITOR already drew: nodes.html reads its own WebGL canvas back
# after a run and PUTs the JPEG here (see `postThumb`). That is why this is an
# upload endpoint and not a call into cad_nodes/screenshot.py — the agent's eyes
# (§9) drive a SECOND, headless browser, so using them here would re-execute the
# graph to re-draw a frame the user is already looking at. Reading the live
# canvas costs one extra render of a scene that is already on screen: free, and
# it is literally what the user sees, camera angle included.
THUMB_NAME = "thumb.jpg"
_THUMB_MAX_BYTES = 4 * 1024 * 1024


@app.put("/api/projects/{name}/thumb")
async def put_thumb(name: str, request: Request):
    """Store the editor's canvas capture as this workflow's thumbnail."""
    d = require_project(name)
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty thumbnail body")
    if len(data) > _THUMB_MAX_BYTES:
        raise HTTPException(413, "Thumbnail too large "
                                 f"({len(data)} > {_THUMB_MAX_BYTES} bytes)")
    if not data.startswith(b"\xff\xd8\xff"):        # JPEG SOI, never a stray body
        raise HTTPException(415, "Thumbnail must be a JPEG")
    # atomic: the library reads this file while the editor writes it
    tmp = d / (THUMB_NAME + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(d / THUMB_NAME)
    logger.info("thumbnail saved: %s (%d bytes)", name, len(data))
    return {"status": "saved", "name": name, "bytes": len(data)}


@app.get("/api/projects/{name}/thumb")
async def get_thumb(name: str):
    """The stored thumbnail, or 404 — the caller draws its own placeholder
    rather than being handed a black PNG that reads as a bug."""
    thumb = require_project(name) / THUMB_NAME
    if not thumb.exists():
        raise HTTPException(404, f"No thumbnail for '{name}' yet")
    return FileResponse(thumb, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Render & Export
# ---------------------------------------------------------------------------
@app.post("/api/projects/{name}/render")
async def render_project(name: str):
    """Transpile + execute a node graph to build123d, producing output.stl."""
    d = require_project(name)
    result = await off_loop(execute_graph, _load_graph(name), d)
    if not result["success"]:
        raise HTTPException(400, f"Graph execution failed:\n{result.get('errors')}")
    return {
        "status": "rendered",
        "stl": f"/api/projects/{name}/download",
        "warnings": None,
        "view": result["view"],
    }


@app.get("/api/projects/{name}/download")
async def download_stl(name: str):
    d = require_project(name)
    stl = d / "output.stl"
    graph_json = d / "graph.json"
    # Live runs skip the STL export; (re)generate it here on demand when it's
    # missing or older than the graph, so a download always reflects the graph.
    stale = (not stl.exists()) or (
        graph_json.exists() and stl.stat().st_mtime < graph_json.stat().st_mtime)
    if stale:
        try:
            await off_loop(execute_graph, _load_graph(name), d, write_stl=True)
        except Exception as e:
            logger.error("download '%s' re-render failed: %s", name, e)
    if not stl.exists():
        raise HTTPException(404, "No STL could be generated for this graph.")
    return FileResponse(stl, media_type="model/stl", filename=f"{name}.stl")


# ---------------------------------------------------------------------------
# Node-based CAD (graph engine)
# ---------------------------------------------------------------------------
def _load_graph(name: str) -> Graph:
    d = require_project(name)
    gpath = d / "graph.json"
    if not gpath.exists():
        raise HTTPException(404, f"Project '{name}' has no graph.json")
    return Graph.from_dict(json.loads(gpath.read_text()))


@app.get("/api/nodes")
async def node_catalog(category: str = ""):
    """Full node catalog (optionally filtered by category)."""
    nodes = catalog.as_json()
    if category:
        nodes = [n for n in nodes if n.get("category") == category]
    return nodes


def _read_aliases() -> dict[str, list[str]]:
    """The personal alias map (ALIASES_PATH), or {} when absent/unreadable — never
    fatal, a corrupt file must not take the editor down with it."""
    try:
        data = json.loads(ALIASES_PATH.read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: [str(w) for w in v] for k, v in data.items() if isinstance(v, list)}


@app.get("/api/aliases")
async def user_aliases():
    """Personal search aliases: {node_type: [word, ...]}. These sit ALONGSIDE the
    catalog's built-in `NodeDef.aliases` — the editor merges the two in its
    add-node search — and are deliberately kept in a plain, greppable file so a
    word that proves its worth can be PROMOTED BY HAND into catalog.py."""
    return {"aliases": _read_aliases()}


@app.put("/api/aliases/{node_type}")
async def set_user_aliases(node_type: str, payload: AliasPayload = Body(...)):
    """Replace one node's personal aliases (empty list = drop the entry)."""
    if node_type not in catalog.REGISTRY:
        raise HTTPException(404, f"Unknown node type '{node_type}'")
    words, seen = [], set()
    for w in payload.aliases[:_ALIAS_MAX_WORDS]:
        w = " ".join(str(w).split())[:_ALIAS_MAX_LEN].strip()
        if not w or w.lower() in seen:
            continue
        seen.add(w.lower())
        words.append(w)
    data = _read_aliases()
    if words:
        data[node_type] = words
    else:
        data.pop(node_type, None)
    # atomic write: the file is read on every editor boot, never half-written
    tmp = ALIASES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dict(sorted(data.items())), indent=2) + "\n")
    tmp.replace(ALIASES_PATH)
    return {"node_type": node_type, "aliases": words}


@app.get("/api/wiretypes")
async def wire_types():
    """Wire-type compatibility, derived from the cast registry (cad_nodes/casts.py)
    — the single source of truth. The node editor fetches `input_accepts` instead
    of hard-coding it, so the two tables can't drift. See PLAN_DATA_PROTOCOL.md."""
    return {"types": catalog.WIRE_TYPES,
            "input_accepts": catalog.build_input_accepts()}


@app.post("/api/graph/{name}")
async def save_graph(name: str, graph: dict):
    """Create/overwrite a node graph project."""
    graph.setdefault("name", name)
    try:
        Graph.from_dict(graph).validate()
    except ValidationError as e:
        raise HTTPException(400, f"Invalid graph: {e}") from e

    d = project_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    stamp_agent_tags(graph.get("nodes", []))  # date the 'To Agent' tags
    (d / "graph.json").write_text(json.dumps(graph, indent=2))
    (d / "meta.json").write_text(json.dumps({
        "backend": "nodegraph",
        "description": graph.get("description", ""),
    }, indent=2))
    return {"status": "saved", "name": name,
            "nodes": len(graph.get("nodes", [])),
            "connections": len(graph.get("connections", []))}


@app.get("/api/graph/{name}")
async def get_graph(name: str):
    return _load_graph(name).to_dict()


@app.get("/api/graph/{name}/code")
async def get_graph_code(name: str, map: int = 0):
    """Generated build123d source. With `?map=1`, also returns `params`: a
    source map of editable parameter spans (row/col + type/min/max/options) so
    the code view can highlight and inline-edit each value non-destructively."""
    try:
        if map:
            code, params = transpile_with_map(_load_graph(name))
            return {"code": code, "params": params}
        return {"code": transpile(_load_graph(name))}
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e


@app.patch("/api/graph/{name}/param")
async def patch_graph_param(name: str, payload: ParamPatch):
    """Edit one parameter value from the code view (built-in param, or a CodeBlock
    `#@param` override when `param` is prefixed `_cb.`). Validates + clamps, then
    re-transpiles — non-destructive, round-trips with the node editor."""
    require_project(name)
    store = GraphStore(PROJECTS_DIR)
    try:
        value = api.patch_param(store, name, payload.node_id, payload.param, payload.value)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "ok", "value": value}


@app.post("/api/graph/{name}/arrange")
async def arrange_graph(name: str, graph: Optional[dict] = Body(default=None)):
    """Tidy node positions — left-to-right by dependency depth, on the nodes' REAL
    on-canvas sizes, so the result cannot contain overlapping nodes (§6c).

    Two modes, one layout engine:

    - **with a graph body** — arrange THAT graph and return it, touching nothing on
      disk. This is what the editor uses: the open canvas, not the saved file, is
      what the user is looking at, so arranging the stored copy would both discard
      unsaved edits and desync undo.
    - **with no body** — load the stored project, arrange, save. For an agent or a
      curl driving a project it is not holding in memory.

    Returns `{status, summary, graph?}`. `summary.group_overlaps` > 0 means some
    group boxes still cut across each other (their members interleave in the
    dependency order); the nodes are still correctly placed.
    """
    require_project(name)
    if graph is not None:
        graph.setdefault("name", name)
        try:
            g = Graph.from_dict(graph)
            g.validate()
        except (ValidationError, KeyError, ValueError) as e:
            raise HTTPException(400, f"Invalid graph: {e}") from e
        try:
            summary = layout.arrange(g)
        except (ValueError, AssertionError) as e:
            raise HTTPException(400, str(e)) from e
        return {"status": "ok", "summary": summary, "graph": g.to_dict()}

    store = GraphStore(PROJECTS_DIR)
    try:
        summary = api.arrange(store, name)
    except (ValueError, AssertionError, KeyError) as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "ok", "summary": summary}


@app.post("/api/graph/{name}/codeblock/{node_id}/scan")
async def scan_codeblock_params(name: str, node_id: str):
    """The `#@param` schema a CodeBlock declares (with effective values), for the
    node editor to render dynamic widgets/sockets."""
    require_project(name)
    store = GraphStore(PROJECTS_DIR)
    try:
        return {"params": api.scan_codeblock(store, name, node_id)}
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e


# Map an imported file's extension to the Import node that reads it.
_IMPORT_NODE_BY_EXT = {
    ".step": "ImportSTEP", ".stp": "ImportSTEP",
    ".stl": "ImportSTL",
    ".svg": "ImportSVG",
    ".dxf": "ImportDXF",
    # raster images feed the TraceImage node (vectorized in its ✎ edit-mode,
    # PLAN_TRACE_IMAGE.md); the traced contours are frozen into the node.
    ".png": "TraceImage", ".jpg": "TraceImage", ".jpeg": "TraceImage",
}


async def _store_asset(d: Path, file: UploadFile) -> dict:
    """Save an uploaded model file into the project's assets/ library and return
    its metadata. The stored path is *project-relative* (e.g. "assets/part.step")
    so it resolves at run time (the worker's cwd is the project dir) and reads
    cleanly in the asset picker. Raises HTTPException(400) on an unsupported type."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _IMPORT_NODE_BY_EXT:
        raise HTTPException(
            400, f"Unsupported file type '{ext or '?'}'. Supported: STEP, STL, SVG, DXF, PNG, JPG.")
    assets = d / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    stem = _slugify(Path(file.filename or "model").stem, "model")
    dest = assets / f"{stem}{ext}"
    n = 1
    while dest.exists():            # never clobber an existing asset
        dest = assets / f"{stem}-{n}{ext}"
        n += 1
    dest.write_bytes(await file.read())
    rel = f"assets/{dest.name}"
    return {"name": dest.name, "path": rel, "ext": ext,
            "node_type": _IMPORT_NODE_BY_EXT[ext]}


@app.get("/api/graph/{name}/assets")
async def list_assets(name: str):
    """List the model files already imported into this project's library."""
    d = require_project(name)
    assets = d / "assets"
    out = []
    if assets.is_dir():
        for f in sorted(assets.iterdir()):
            ext = f.suffix.lower()
            if f.is_file() and ext in _IMPORT_NODE_BY_EXT:
                out.append({"name": f.name, "path": f"assets/{f.name}", "ext": ext})
    return {"assets": out}


@app.post("/api/graph/{name}/asset")
async def upload_asset(name: str, file: UploadFile = File(...)):
    """Upload a file into the project library WITHOUT adding a node (the asset
    picker on an Import node uses this, then points itself at the new file)."""
    d = require_project(name)
    meta = await _store_asset(d, file)
    logger.info("asset stored for '%s': %s", name, meta["path"])
    return {"status": "stored", **meta}


@app.post("/api/graph/{name}/import")
async def import_model(name: str, file: UploadFile = File(...)):
    """Upload a STEP/STL/SVG/DXF file into the project AND add an Import node
    wired to read it. The UI then reloads the graph to show the new node."""
    d = require_project(name)
    meta = await _store_asset(d, file)
    store = GraphStore(PROJECTS_DIR)
    node_id = api.add_node(store, name, meta["node_type"],
                           params={"path": meta["path"]}, position=(80.0, 80.0))
    logger.info("imported %s as %s node %s", meta["name"], meta["node_type"], node_id)
    return {"status": "imported", "node_id": node_id, **meta, "file": meta["name"]}


# --- Custom font library (shared across every project) ------------------------
@app.get("/api/fonts")
async def api_list_fonts():
    """Fonts available to Text nodes: uploaded custom fonts (⬆) + system
    families. The Text `font` picker reads this."""
    return fontlib.list_fonts(FONTS_DIR)


@app.post("/api/fonts")
async def api_upload_font(file: UploadFile = File(...)):
    """Store an uploaded .ttf/.otf/.ttc in the shared library so any Text node
    can use it WITHOUT installing it in the OS. Returns its family name."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in fontlib.FONT_EXTS:
        raise HTTPException(400, f"Unsupported font '{ext or '?'}'. Use TTF, OTF or TTC.")
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = _slugify(Path(file.filename or "font").stem, "font")
    dest = FONTS_DIR / f"{stem}{ext}"
    n = 1
    while dest.exists():                        # never clobber an existing font
        dest = FONTS_DIR / f"{stem}-{n}{ext}"
        n += 1
    dest.write_bytes(await file.read())
    fam = fontlib.family_of(dest)
    logger.info("font stored: %s (family %r)", dest.name, fam)
    return {"status": "stored", "name": fam, "family": fam, "file": dest.name, "ext": ext}


@app.get("/api/fonts/{filename}")
async def api_download_font(filename: str):
    """Stream a custom font file (basename only; guarded) — the /library panel
    loads it via @font-face to render a live preview."""
    if filename != Path(filename).name or filename in ("", ".", ".."):
        raise HTTPException(400, "bad filename")
    # user library first, then the app-bundled fonts (read-only)
    p = FONTS_DIR / filename
    if not p.is_file():
        p = fontlib.BUNDLED_FONTS_DIR / filename
    if not p.is_file() or p.suffix.lower() not in fontlib.FONT_EXTS:
        raise HTTPException(404, "no such font")
    media = {".ttf": "font/ttf", ".otf": "font/otf",
             ".ttc": "font/collection"}.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(p, media_type=media, filename=filename)


@app.delete("/api/fonts/{filename}")
async def api_delete_font(filename: str):
    """Remove a custom font from the shared library (basename only; guarded)."""
    if filename != Path(filename).name or filename in ("", ".", ".."):
        raise HTTPException(400, "bad filename")
    p = FONTS_DIR / filename
    if not p.is_file():
        raise HTTPException(404, "no such font")
    p.unlink()
    logger.info("font deleted: %s", filename)
    return {"status": "deleted", "file": filename}


@app.get("/api/graph/{name}/screenshot")
async def api_screenshot(
    name: str,
    view: str = "iso",
    azim: float = None,
    elev: float = None,
    zoom: float = 1.0,
    width: int = 900,
    height: int = 700,
    projection: str = "",
    node: str = "",
    isolate: bool = False,
    hq: bool = True,
    chrome: bool = False,
    run: bool = True,
    scale: int = 2,
):
    """Render the viewport to a PNG — the agent's eyes on its own geometry.

    This drives headless Chromium over this very server's /nodes page, so the
    image comes out of the REAL viewer (same materials, finishes, bloom). It is
    NOT put through off_loop() like /execute, and deliberately: the work
    happens in the browser process, so this coroutine is only awaiting I/O. The
    graph run it triggers goes through /execute, which is already off the loop.

    `X-Noodle-Ran` says whether the graph was actually re-executed — `run=0`
    reuses what is already on screen, but falls back to running rather than
    returning an empty frame.
    """
    require_project(name)
    try:
        png, meta = await api.screenshot(
            GraphStore(PROJECTS_DIR), name, view=view, azim=azim, elev=elev,
            zoom=zoom,
            width=width, height=height, projection=projection, node=node,
            isolate=isolate, hq=hq, chrome=chrome, run=run, scale=scale)
    except ScreenshotUnavailable as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    logger.info("screenshot '%s' %s %dx%d (%d bytes, ran=%s)",
                name, view, meta["width"], meta["height"], meta["bytes"],
                meta["ran"])
    return Response(
        content=png, media_type="image/png",
        headers={"X-Noodle-Ran": "1" if meta["ran"] else "0",
                 "X-Noodle-Size-Mm": ",".join(str(v) for v in
                                              meta.get("size_mm", [])),
                 "Cache-Control": "no-store"})


@app.post("/api/graph/{name}/execute")
async def execute_graph_project(name: str, run: str | None = None):
    """`run` is a caller-chosen id for this run. The editor generates one, opens
    /progress?run=<id> with it and then POSTs here, so the progress stream can
    match its events to this exact run instead of inferring them from the file."""
    d = require_project(name)
    graph = _load_graph(name)
    logger.info("execute graph '%s' (%d nodes)", name, len(graph.nodes))
    try:
        # Live run: skip the STL export (regenerated on demand by /download).
        # Off the event loop — see off_loop() for why every engine call is.
        result = await off_loop(execute_graph, graph, d, write_stl=False, run_id=run)
    except ValidationError as e:
        logger.error("execute '%s' invalid graph: %s", name, e)
        raise HTTPException(400, str(e)) from e
    if not result["success"]:
        logger.error("execute '%s' failed: %s", name, result.get("errors") or result.get("error_detail"))
        raise HTTPException(400, {
            "message": "Graph execution failed",
            "errors": result.get("errors"),
            "error_detail": result.get("error_detail"),
            "code": result.get("code"),
        })
    node_errors = result.get("node_errors", {})
    if node_errors:
        for nid, err in node_errors.items():
            logger.error("execute '%s' node %s: %s", name, nid, err)
    return {
        "status": "executed",
        "view": result["view"],
        "code": result["code"],
        "warnings": result.get("warnings", []),
        "node_errors": result.get("node_errors", {}),
        # Per-node wall-clock + which ids the memo store served: the editor keeps
        # them on the nodes as cost badges (cache hit vs real re-run).
        "node_timings": result.get("node_timings", {}),
        "node_cached": result.get("node_cached", []),
        # Always offered — /download regenerates the STL on demand if it's stale.
        "stl": f"/api/projects/{name}/download",
    }


async def _tail_progress(path: Path, want_run: str | None = None, request: Request | None = None):
    """Yield SSE events from a run's progress.jsonl as the worker appends to it.

    Runs are identified, not guessed. `executor.execute_code` opens the file with
    a header line naming the run (`{"k":"run","r":<id>}`), and the client passes
    the id it is about to POST as `?run=`, so this stream knows exactly whose
    events it is reading and can start from the file's BEGINNING.

    That indirection is the whole fix. The previous version watched the file size
    and treated a shrink as "a new run started" — but progress.jsonl lives at a
    fixed path per project, and two warm runs of one graph write nearly identical
    bytes. When a run rewrote the file to the same length inside one 50ms poll,
    the tailer saw `size == offset`, concluded nothing had happened, and dropped
    the ENTIRE run (measured: 5/5 nodes on voronoi-3d-lattice, all of
    galton-board; the bigger lego-brick survived because it wrote more). That is
    what made the editor's glow stop "at random" — the small, fast graphs lost
    everything and the slow ones did not.

    Re-reading the whole file each poll is affordable: it is a few hundred short
    lines, and we were already stat()ing it at the same rate.
    """
    seen_run: str | None = None
    sent = 0
    quiet = 0.0
    since_beat = 0.0
    # With no run id to wait for (MCP, curl, an older editor), keep the old
    # intent: ignore whatever is already on disk and report the NEXT run.
    if want_run is None:
        seen_run = _run_id_of(path)

    while quiet < _PROGRESS_MAX_IDLE:
        await asyncio.sleep(0.05)

        # HANG UP WHEN THE CLIENT DOES, and prove the connection is still alive in
        # between. Without this the generator kept polling a stream nobody was
        # reading: it only ever wrote when a node reported, so a finished run left
        # it parked here for the full idle timeout with uvicorn holding the
        # connection open. Chrome allows 6 per host, so after a handful of runs the
        # editor's next EventSource never connected AT ALL — its progress stream
        # silently queued behind its own dead predecessors and the glow stopped.
        # Measured before the fix: runs 2-5 of five received zero events and never
        # fired `open`. The heartbeat is what makes a dead peer detectable — a write
        # to a closed socket is what tells uvicorn to cancel us.
        if request is not None and await request.is_disconnected():
            return
        since_beat += 0.05
        if since_beat >= 1.0:
            since_beat = 0.0
            yield ": ping\n\n"

        try:
            lines = path.read_text().splitlines()
        except OSError:
            quiet += 0.05
            continue
        if not lines:
            quiet += 0.05
            continue

        try:
            head = json.loads(lines[0])
        except ValueError:
            quiet += 0.05
            continue
        if head.get("k") != "run":
            quiet += 0.05
            continue
        run = head.get("r")

        if run != seen_run:          # a different run owns the file now
            if want_run is not None and run != want_run:
                # Someone else's run. Wait for ours rather than glowing their nodes.
                quiet += 0.05
                continue
            seen_run = run
            sent = 0

        # A half-written final line is skipped and picked up whole next poll.
        body = lines[1:]
        if not path.read_text().endswith("\n") and body:
            body = body[:-1]
        if len(body) <= sent:
            quiet += 0.05
            continue
        quiet = 0.0
        done = False
        for line in body[sent:]:
            if not line.strip():
                continue
            yield f"data: {line}\n\n"
            done = done or '"k": "done"' in line or '"k":"done"' in line
        sent = len(body)
        if done:
            # The run marked itself finished (executor, in a finally). Hang up:
            # this stream has nothing left to report, and holding it open is what
            # used to starve the browser of connections.
            return


def _run_id_of(path: Path) -> str | None:
    """The id of the run currently recorded in a progress file, if any."""
    try:
        first = path.read_text().splitlines()[0]
        head = json.loads(first)
    except (OSError, IndexError, ValueError):
        return None
    return head.get("r") if head.get("k") == "run" else None


@app.get("/api/graph/{name}/progress")
async def graph_progress(request: Request, name: str, run: str | None = None):
    """Live per-node execution events (SSE) for the run in flight on this graph.

    Emitted by the generated code itself (transpiler `_ev`), so it works on the warm
    worker AND the cold subprocess. The client closes the stream when its POST to
    /execute resolves.

    `run` is the id the caller is about to POST to /execute. Passing it makes the
    subscription exact — no guessing which run the file holds, and no race with
    the POST landing first (which it routinely does, since `new EventSource()`
    returns before its GET is dispatched).
    """
    d = require_project(name)
    return StreamingResponse(
        _tail_progress(d / "progress.jsonl", run, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/agent/help")
async def agent_help_route():
    """Self-contained orientation guide for a remote agent (markdown text)."""
    return PlainTextResponse(api.agent_help(), media_type="text/markdown")


@app.get("/api/agent/tags")
async def agent_tags_route():
    """Provenance index of every 'To Agent' tag node across all projects."""
    return api.agent_tags(GraphStore(PROJECTS_DIR))


@app.get("/api/graph/{name}/section_outline")
async def graph_section_outline(name: str, axis: str = "z", pos: float = 0.0,
                                path: str = ""):
    """One exact section, edge by edge (the slice_summary 'microscope')."""
    require_project(name)
    try:
        data = await off_loop(api.section_outline, GraphStore(PROJECTS_DIR),
                              name, axis, pos, path or None)
    except (ValidationError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    if not data.get("success"):
        raise HTTPException(400, {"message": "Section outline failed",
                                  "error": data.get("error")})
    return data


@app.get("/api/graph/{name}/slice_summary")
async def graph_slice_summary(name: str, path: str = "", n: int = 10):
    """Symbolic cross-section summary (retro-engineering, PLAN_RETROENG).
    Without `path`: slices the graph's own result. With `path` (project-relative
    STEP, e.g. assets/part.step): slices that file."""
    require_project(name)
    store = GraphStore(PROJECTS_DIR)
    try:
        data = await off_loop(api.slice_summary, store, name, path or None, n)
    except (ValidationError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    if not data.get("success"):
        raise HTTPException(400, {"message": "Slice summary failed",
                                  "error": data.get("error")})
    return data


@app.post("/api/graph/{name}/subshapes/{node_id}")
async def graph_subshapes(name: str, node_id: str, kind: str = "edge"):
    """Pickable sub-shapes (edges/faces/vertices) of a node's output shape,
    for the interactive selection picker."""
    if kind not in ("edge", "face", "vertex", "shape"):
        raise HTTPException(400, f"Unknown kind {kind!r}")
    d = require_project(name)
    graph = _load_graph(name)
    try:
        data = await off_loop(extract_subshapes_for_node, graph, node_id, kind, d)
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e
    if not data.get("success"):
        raise HTTPException(400, {"message": "Sub-shape extraction failed",
                                  "error": data.get("error")})
    return data


@app.get("/api/copilot/status")
async def copilot_status_route():
    """Which LLM backend the copilot will use (provider/model/keyed)."""
    return copilot_status()


@app.post("/api/copilot/chat")
async def copilot_chat(payload: CopilotPayload):
    """Drive the natural-language copilot: it edits payload.graph via the api
    tools and returns a reply plus whether the graph changed (UI should reload)."""
    require_project(payload.graph)
    result = run_chat(payload.graph, payload.messages, GraphStore(PROJECTS_DIR))
    return result


@app.get("/api/graph/{name}/view")
async def get_graph_view(name: str):
    d = require_project(name)
    vpath = d / "view.json"
    if not vpath.exists():
        raise HTTPException(404, "No view yet. Call /execute first.")
    return json.loads(vpath.read_text())


# Real geometry export: transpile + execute + write the file in the requested
# format, then stream it back. (The /download route only serves the STL.)
_EXPORT_MEDIA = {
    "step": ("model/step", "step"),
    "stl": ("model/stl", "stl"),
    "gltf": ("model/gltf+json", "gltf"),
}


@app.get("/api/graph/{name}/export/{fmt}")
async def export_graph_project(name: str, fmt: str):
    fmt = fmt.lower()
    if fmt not in _EXPORT_MEDIA:
        raise HTTPException(400, f"Unsupported format {fmt!r}; "
                                 f"choose from {sorted(_EXPORT_MEDIA)}")
    d = require_project(name)
    graph = _load_graph(name)
    media, ext = _EXPORT_MEDIA[fmt]
    try:
        out_path = await off_loop(export_graph, graph, d, fmt)
    except ValidationError as e:
        raise HTTPException(400, str(e)) from e
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, f"Export failed: {e}") from e
    return FileResponse(out_path, media_type=media, filename=f"{name}.{ext}")


# ---------------------------------------------------------------------------
# File library — global view of every project's exported outputs (exports/)
# and imported assets (assets/), with download + upload. Backs /library.
# ---------------------------------------------------------------------------
# Media types for anything the library serves for download.
_LIB_MEDIA = {
    ".step": "model/step", ".stp": "model/step",
    ".stl": "model/stl",
    ".3mf": "model/3mf",
    ".gltf": "model/gltf+json", ".glb": "model/gltf-binary",
    ".svg": "image/svg+xml",
    ".dxf": "image/vnd.dxf",
    ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
}
# The two sandboxed sub-folders the library exposes. Nothing else in a
# project dir (graph.json, _run.py, output.stl, …) is ever listed or served.
_LIB_KINDS = ("exports", "assets")


def _lib_entries(d: Path, kind: str) -> list[dict]:
    """List downloadable files in a project's exports/ or assets/ folder."""
    folder = d / kind
    out: list[dict] = []
    if folder.is_dir():
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in _LIB_MEDIA:
                st = f.stat()
                out.append({
                    "name": f.name,
                    "kind": kind,
                    "ext": f.suffix.lower(),
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "url": f"/api/library/{d.name}/{kind}/{f.name}",
                })
    return out


@app.get("/api/library")
async def library_list():
    """Every downloadable file across all projects, grouped by project.
    exports/ = files written by Export nodes; assets/ = imported models."""
    projects = []
    for d in sorted(PROJECTS_DIR.iterdir()):
        if not d.is_dir() or d.name in _RESERVED_PROJECT_DIRS:
            continue
        files = _lib_entries(d, "exports") + _lib_entries(d, "assets")
        thumb = d / THUMB_NAME
        projects.append({"project": d.name, "files": files,
                         "thumb": int(thumb.stat().st_mtime) if thumb.exists() else 0})
    return {"projects": projects}


@app.get("/api/library/{name}/{kind}/{filename}")
async def library_download(name: str, kind: str, filename: str):
    """Stream a single library file. `kind` is exports|assets; `filename` is
    a plain basename (path traversal is rejected by both the guard below and
    project_dir's validate_graph_id)."""
    if kind not in _LIB_KINDS:
        raise HTTPException(404, f"Unknown library folder {kind!r}")
    if filename != Path(filename).name or filename in ("", ".", ".."):
        raise HTTPException(400, "Invalid filename")
    d = require_project(name)
    f = d / kind / filename
    if not f.is_file():
        raise HTTPException(404, "File not found")
    media = _LIB_MEDIA.get(f.suffix.lower(), "application/octet-stream")
    return FileResponse(f, media_type=media, filename=filename)


@app.delete("/api/library/{name}/{kind}/{filename}")
async def library_delete(name: str, kind: str, filename: str):
    """Delete a single library file (exports/ or assets/)."""
    if kind not in _LIB_KINDS:
        raise HTTPException(404, f"Unknown library folder {kind!r}")
    if filename != Path(filename).name or filename in ("", ".", ".."):
        raise HTTPException(400, "Invalid filename")
    d = require_project(name)
    f = d / kind / filename
    if not f.is_file():
        raise HTTPException(404, "File not found")
    f.unlink()
    logger.info("library file deleted: %s/%s/%s", name, kind, filename)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Backends list
# ---------------------------------------------------------------------------
@app.get("/api/backends")
async def list_backends():
    return [
        {"id": "nodegraph", "name": "Node CAD (build123d)", "type": "Visual graph -> build123d"},
    ]
