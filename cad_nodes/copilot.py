"""
CAD Copilot — an in-app assistant that builds/edits node graphs from natural
language by calling the same `cad_nodes.api` functions the MCP server exposes.

Provider-agnostic over the OpenAI-compatible chat-completions wire format, so it
works with a FREE local model by default (Ollama) and with any keyed provider
that speaks the same format (Groq, OpenRouter, Gemini's OpenAI endpoint, …).

Configuration (env):
  COPILOT_BASE_URL   OpenAI-compatible base, e.g. https://api.groq.com/openai/v1
                     (unset → local Ollama)
  COPILOT_API_KEY    bearer token for the above (unset for local Ollama)
  COPILOT_MODEL      model id (default: qwen2.5 for Ollama)
  OLLAMA_HOST        Ollama base (default http://host.docker.internal:11434)

The web server stays build123d-free: this module only orchestrates HTTP + the
(already build123d-free) api layer; geometry runs in the execution worker.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import api
from .graph import ValidationError
from .store import GraphStore

MAX_STEPS = int(os.environ.get("COPILOT_MAX_STEPS", "24"))


# ── provider resolution ─────────────────────────────────────────────────────
def _provider() -> dict:
    base = os.environ.get("COPILOT_BASE_URL", "").rstrip("/")
    if base:
        return {"base": base, "key": os.environ.get("COPILOT_API_KEY", ""),
                "model": os.environ.get("COPILOT_MODEL", ""), "local": False}
    host = os.environ.get("OLLAMA_HOST", "http://host.docker.internal:11434").rstrip("/")
    return {"base": host + "/v1", "key": "",
            "model": os.environ.get("COPILOT_MODEL", "qwen2.5"), "local": True}


def copilot_status() -> dict:
    p = _provider()
    return {"provider": "ollama" if p["local"] else "openai-compatible",
            "base_url": p["base"], "model": p["model"], "keyed": bool(p["key"])}


def _chat(provider: dict, messages: list, tools: list) -> dict:
    """One OpenAI-compatible /chat/completions call. Returns the message dict."""
    body = json.dumps({
        "model": provider["model"], "messages": messages,
        "tools": tools, "tool_choice": "auto",
        "temperature": 0.2, "stream": False,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if provider["key"]:
        headers["Authorization"] = "Bearer " + provider["key"]
    req = urllib.request.Request(provider["base"] + "/chat/completions",
                                 data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]


# ── tool surface (bound to one graph) ───────────────────────────────────────
def _compact_catalog() -> str:
    # The same one-line signatures MCP/HTTP agents get (api.compact_catalog),
    # in the copilot's leaner flavour: param names only, hidden types kept.
    return api.compact_catalog(include_hidden=True, defaults=False)


def _lean_view(v: dict) -> dict:
    if not isinstance(v, dict):
        return {"success": False}
    out = {"success": v.get("success"), "kind": v.get("kind"),
           "volume": v.get("volume"), "counts": v.get("counts")}
    ne = v.get("node_errors") or {}
    if ne:
        out["node_errors"] = {k: (e.get("message") if isinstance(e, dict) else e)
                              for k, e in ne.items()}
    if v.get("warnings"):
        out["warnings"] = v["warnings"]
    return out


def _make_tools(store: GraphStore, graph_id: str, state: dict):
    """Return (openai_tool_specs, dispatch(name, args)->str)."""
    def graph_summary():
        g = store.load(graph_id)
        return {
            "nodes": [{"id": n.id, "type": n.type, "params": n.params} for n in g.nodes],
            "connections": [{"from": f"{c.from_node}.{c.from_socket}",
                             "to": f"{c.to_node}.{c.to_socket}"} for c in g.connections],
        }

    def do(name, a):
        if name == "get_graph":
            return graph_summary()
        if name == "get_node_def":
            return api.get_node_def(a["node_type"])
        if name == "add_node":
            n = len(store.load(graph_id).nodes)
            pos = (120 + (n % 6) * 230, 160 + (n // 6) * 180)
            nid = api.add_node(store, graph_id, a["node_type"],
                               a.get("params") or {}, position=pos)
            state.setdefault("created", set()).add(nid)  # safe to fully edit this session
            state["changed"] = True
            return {"node_id": nid}
        if name == "copy_node":
            g = store.load(graph_id)
            src = g.node(a["node_id"])  # KeyError if missing -> surfaced to model
            n = len(g.nodes)
            pos = (120 + (n % 6) * 230, 160 + (n // 6) * 180)
            nid = api.add_node(store, graph_id, src.type, dict(src.params), position=pos)
            state.setdefault("created", set()).add(nid)
            state["changed"] = True
            return {"node_id": nid, "type": src.type}
        if name == "connect":
            cid = api.connect(store, graph_id, a["from_node"], a["from_socket"],
                              a["to_node"], a["to_socket"])
            state["changed"] = True
            return {"connection_id": cid}
        if name == "set_param":
            # Guardrail: never silently rewrite the code of a custom (CodeBlock)
            # node that already existed before this session. Composing workflows
            # and configuring freshly-created custom nodes is fine; mutating an
            # existing one must go through copy_node + the user's go-ahead.
            node = store.load(graph_id).node(a["node_id"])
            params = a.get("params") or {}
            if (node.type == "CodeBlock" and "code" in params
                    and a["node_id"] not in state.get("created", ())):
                return {"error": "refused: editing the code of an existing custom "
                        "node is not allowed. Warn the user, then call copy_node "
                        "and modify the copy, leaving the original intact."}
            api.set_param(store, graph_id, a["node_id"], params)
            state["changed"] = True
            return {"ok": True}
        if name == "delete_node":
            api.delete_node(store, graph_id, a["node_id"])
            state["changed"] = True
            return {"ok": True}
        if name == "execute":
            # api.execute returns the executor result dict; the drawable view is
            # nested. Surface BOTH the view summary and the failure diagnostics —
            # a success:false with no explanation leaves the model guessing.
            res = api.execute(store, graph_id)
            out = _lean_view(res.get("view") or {})
            out["success"] = bool(res.get("success"))
            if res.get("errors"):
                out["errors"] = str(res["errors"])[:600]
            if res.get("warnings"):
                out["warnings"] = res["warnings"]
            ne = res.get("node_errors") or {}
            if ne:
                out["node_errors"] = {nid: (e.get("message") if isinstance(e, dict) else str(e))
                                      for nid, e in ne.items()}
            det = res.get("error_detail")
            if det:
                out["error_detail"] = {k: det[k] for k in
                                       ("node_id", "message", "hint", "exception")
                                       if det.get(k)}
            return out
        if name == "slice_summary":
            gid = a.get("graph") or graph_id   # read-only cross-graph is fine
            res = api.slice_summary(store, gid, a.get("path") or None,
                                    int(a.get("n_per_axis") or 10))
            if not res.get("success"):
                return {"error": res.get("error", "slice failed")}
            return {"summary": res["text"]}
        if name == "section_outline":
            gid = a.get("graph") or graph_id
            res = api.section_outline(store, gid, a.get("axis") or "z",
                                      float(a.get("position") or 0.0),
                                      a.get("path") or None)
            if not res.get("success"):
                return {"error": res.get("error", "outline failed")}
            return {"outline": res["text"]}
        if name == "list_agent_tags":
            return {"tags": api.agent_tags(store)}
        return {"error": f"unknown tool {name}"}

    def dispatch(name, args):
        try:
            return json.dumps(do(name, args))
        except (ValidationError, ValueError, KeyError) as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})
        except Exception as e:  # noqa: BLE001 — surface to the model so it can adapt
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def fn(name, desc, props, required):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        }}

    s = {"type": "string"}
    specs = [
        fn("get_graph", "List the current nodes (id, type, params) and connections.", {}, []),
        fn("get_node_def", "Get the inputs/outputs/params of one node type.",
           {"node_type": s}, ["node_type"]),
        fn("add_node", "Add a node. Returns its node_id. params is a dict of param->value. "
           "Use type 'CodeBlock' to create a custom node from scratch (its `code` param holds build123d code).",
           {"node_type": s, "params": {"type": "object"}}, ["node_type"]),
        fn("copy_node", "Duplicate an existing node (same type and params/code) and return the new node_id. "
           "This is how you modify a custom node: copy it, then edit the COPY — never the original.",
           {"node_id": s}, ["node_id"]),
        fn("connect", "Wire from_node.from_socket -> to_node.to_socket. Errors on incompatible wires.",
           {"from_node": s, "from_socket": s, "to_node": s, "to_socket": s},
           ["from_node", "from_socket", "to_node", "to_socket"]),
        fn("set_param", "Set parameters on a node. params is a dict of param->value.",
           {"node_id": s, "params": {"type": "object"}}, ["node_id", "params"]),
        fn("delete_node", "Delete a node and its connections.", {"node_id": s}, ["node_id"]),
        fn("execute", "Run the graph; returns volume, counts, and any per-node errors.", {}, []),
        fn("slice_summary", "Symbolic cross-section summary (your 'eyes'). Slices the shape "
           "with ~n_per_axis planes per axis and returns compact text: a bbox+volume checksum, "
           "then per-axis stacks with runs of identical sections merged into intervals; each "
           "loop is classified (circle/rect/rrect/slot, or a poly fallback) with its holes. "
           "Without `path` it slices the CURRENT graph result; with `path` (project-relative, "
           "e.g. 'assets/part.step') it slices that STEP file. Use it to read a target part "
           "AND to verify a reconstruction by comparing the two summaries as text. "
           "`graph` (optional) reads from another project — e.g. where a ToAgent tag points.",
           {"path": s, "n_per_axis": {"type": "integer"}, "graph": s}, []),
        fn("section_outline", "The microscope: ONE exact cross-section at axis=position, "
           "every loop edge by edge (LINE/CIRCLE, 2D endpoints, radius+center for arcs). "
           "Use it when the slice_summary line for a section is ambiguous (poly fallback, "
           "unclear joins). Same path/graph semantics as slice_summary.",
           {"axis": s, "position": {"type": "number"}, "path": s, "graph": s}, ["axis", "position"]),
        fn("list_agent_tags", "The provenance index: every 'To Agent' tag node the user "
           "placed, across ALL projects — label, date, graph, node id, and the tagged "
           "source (an ImportSTEP's file path, etc.). When the user says 'part X in "
           "workflow Y from yesterday', resolve it here, then slice_summary(graph=..., "
           "path=...).", {}, []),
    ]
    return specs, dispatch


SYSTEM = """You are CAD Copilot, an assistant inside a node-based parametric CAD app built on build123d.
You edit ONE graph by calling tools. Geometry flows through typed wires:
  geometry (solids), sketch (2D), curve, vector (points), selection (picked sub-shapes), data.
Scope — you ONLY assemble graphs; you never touch the application:
- You CANNOT and MUST NOT modify the app's code or the definition/behaviour of any
  built-in node type. You have no tool for that — never claim to have done it.
- What you may do freely: compose workflows by adding/connecting/parameterising the
  existing catalog nodes, and create brand-new CUSTOM nodes from scratch (add a
  'CodeBlock' node and write its `code`).
- A custom node is a CodeBlock. You may NOT edit the `code` of a custom node that
  already existed before this conversation. If the user wants to change such a node:
  (1) tell the user you will leave the original untouched and work on a copy,
  (2) call copy_node to duplicate it, (3) edit ONLY the copy. set_param will refuse
  an in-place code edit of a pre-existing custom node — that is expected, not a bug.
- Don't delete nodes you didn't create without saying so.

Rules:
- Call get_graph first to see what already exists; reuse nodes, don't duplicate.
- Use ONLY node types from the catalog below, with their exact socket names and wire types.
  If unsure about a node's sockets/params, call get_node_def.
- Build minimally: primitives (Box, Cylinder, Circle…) -> operations (Extrude, Revolve) ->
  booleans (Union/Subtract/Intersect) -> modifiers (Fillet, Chamfer). To place a primitive,
  wire a ConstructPoint into its optional `origin` input, or set its params.
- After making changes, call execute and check for node_errors; if any, fix and execute again.
- When done, reply briefly in the user's language: what you built and the result (volume/errors).
  Keep the final reply short — the user sees the graph update visually.

Retro-engineering — rebuilding a part from a file:
0. LOCATE: if the user refers to a tagged part ('part X', 'in workflow Y', 'from
   yesterday'), call list_agent_tags and match label/date/graph; the tag's source gives
   the file path, and graph=... lets slice_summary read it from that project.
1. PERCEIVE: call slice_summary(path='assets/<file>.step'). Read the checksum, then the
   Z stack first; the X/Y stacks are confirmation. A section that stays constant over an
   interval IS an extrusion of that profile; the interval endpoints give its height and
   position. Holes listed on a loop are holes through that profile. Where one summary
   line is ambiguous (a poly fallback, an unclear join), zoom with
   section_outline(axis, position) — exact edges, no guessing.
2. PROCEDURALIZE — rebuild intent, not coordinates:
   - N equal circles regularly placed -> ONE hole + ArrayLinear/ArrayPolar (count stays a
     parameter), not N copied nodes.
   - Small corner arcs / a 'rrect' outline -> model the sharp shape and add a Fillet node
     downstream, don't trace the rounded geometry.
   - Overall dimensions and counts -> node params (sliders), so the rebuild stays editable.
   - The USER's stated intent about what to parameterize always WINS over these defaults.
3. VERIFY: after building, call execute, then slice_summary() with NO path (= your own
   result) and compare the two texts line by line: bbox, volume, radii, positions, interval
   heights. Fix each mismatch and re-verify. Small numeric noise (<0.1) is fine. Report the
   residual differences honestly in your final reply.

Node catalog (type [category] in:(socket:wire) out:(socket:wire) params):
%s
"""


def run_chat(graph_id: str, messages: list, store: GraphStore | None = None) -> dict:
    """Drive the tool-use loop. `messages` is the chat history [{role, content}].
    Returns {reply, changed, steps, error?}."""
    store = store or GraphStore()
    if not store.exists(graph_id):
        return {"reply": "", "changed": False, "error": f"no graph {graph_id!r}"}

    provider = _provider()
    state = {"changed": False}
    tools, dispatch = _make_tools(store, graph_id, state)
    convo = [{"role": "system", "content": SYSTEM % _compact_catalog()}] + list(messages)

    steps = 0
    try:
        while steps < MAX_STEPS:
            steps += 1
            msg = _chat(provider, convo, tools)
            convo.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"reply": msg.get("content") or "", "changed": state["changed"], "steps": steps}
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {}
                result = dispatch(fn["name"], args)
                convo.append({"role": "tool", "tool_call_id": call.get("id", ""),
                              "name": fn["name"], "content": result})
        return {"reply": "(reached the step limit — the graph may be partially built)",
                "changed": state["changed"], "steps": steps}
    except urllib.error.HTTPError as e:   # subclass of URLError — catch first
        detail = e.read().decode(errors="replace")[:400]
        return {"reply": "", "changed": state["changed"],
                "error": f"LLM provider error {e.code}: {detail}"}
    except urllib.error.URLError as e:
        hint = (" Is Ollama running? Start it with `ollama serve` and pull a tool-capable "
                "model, e.g. `ollama pull qwen2.5`.") if provider["local"] else ""
        return {"reply": "", "changed": state["changed"],
                "error": f"LLM provider unreachable ({provider['base']}): {e}.{hint}"}
