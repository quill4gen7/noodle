# Driving noodle from an AI agent

noodle exposes the **same** graph engine three ways, all backed by `cad_nodes.api`:

1. **MCP server** — for agents that speak the Model Context Protocol
   (Claude Code, openclaw, Claude Desktop, Cursor, MCP Inspector…).
2. **HTTP API** — plain `POST`/`GET`, works from any language or `curl`.
3. **In-app copilot** — the chat box in the web UI (`POST /api/copilot/chat`).

> **Building or editing models?** Use the MCP tools (or their HTTP twins) and
> read the guide they serve — `cad_help` / `GET /api/agent/help`, source
> `cad_nodes/AGENT_HELP.md`. `CLAUDE.md` is for changing noodle itself, not for
> using it.

This file covers (1) and (2). Start the app first (`./start.sh`, `start.bat`, or
`docker compose up -d --build`) so the container `noodle` is running.

> **Agent on another machine?** noodle serves its own orientation guide — the
> graph model, wire rules, every endpoint/tool, the build and retro-engineering
> loops. Fetch it as your FIRST call and you need nothing from this repo:
> `curl -s http://<host>:8090/api/agent/help` (HTTP) or the `cad_help` tool /
> `cad://help` resource (MCP).

---

## 1. MCP (recommended for agents)

The MCP server (`mcp_server.py`) needs the full build123d environment **and** the
shared `projects/` directory. The cleanest, OS-independent way to get both is to
run it **inside the already-running container** over stdio — no extra install,
identical on Windows and Linux:

```json
{
  "mcpServers": {
    "noodle": {
      "command": "docker",
      "args": ["exec", "-i", "noodle", "python", "mcp_server.py"]
    }
  }
}
```

- **Claude Code** — add it with the CLI (no manual JSON editing):
  ```bash
  claude mcp add noodle -- docker exec -i noodle python mcp_server.py
  ```
- **Claude Desktop** — paste the JSON above into
  `claude_desktop_config.json` (Settings → Developer → Edit Config) and restart.
- **openclaw / other MCP clients** — point them at the same `command`/`args`.

The container shares `projects/` with the web UI, so a graph an agent builds is
**immediately visible in the browser** at <http://localhost:8090/nodes> (reload),
and vice-versa.

### Tools exposed

Start with `cad_help` — the orientation guide, also served over HTTP at
`/api/agent/help`; `cad_help(topic=...)` adds detail on screenshots,
retroeng, print physics, threads and fluids. The source is
`cad_nodes/AGENT_HELP.md`.

| Tool | Purpose |
|---|---|
| `cad_help` | **start here** — the orientation guide; `topic=` for details |
| `cad_list_graphs` / `cad_create_graph` / `cad_delete_graph` | graph lifecycle |
| `cad_get_node_catalog` / `cad_get_node_def` | one signature line per type (`query=` filter; `full=True` for JSON) · one type in detail |
| `cad_get_graph` | compact graph: ids, titles, params, `a.out -> b.in` wires; `node=` for one node in full |
| `cad_apply_ops` | **atomic batch** of add_node / connect / disconnect / set_param / edit_code / set_code / set_node / remove — one save or none |
| `cad_add_node` / `cad_connect` / `cad_set_param` / `cad_set_node` | single edits (nodes by id or title; params validated; new nodes auto-placed) |
| `cad_edit_code` / `cad_set_code` | str-replace inside a CodeBlock (exactly one match) · replace it whole |
| `cad_delete_node` / `cad_delete_connection` | remove |
| `cad_execute` | run → success, per-node errors, lean view summary; `overrides=` to try values unsaved; `include_code=True` for the code |
| `cad_validate` / `cad_arrange` | check without running · tidy the layout |
| `cad_screenshot` | PNG of the real viewport (any node can be isolated) |
| `cad_get_view` | last view; `fmt="mesh"` to include triangles (heavy) |
| `cad_get_code` | the build123d Python the graph transpiles to (large) |
| `cad_get_panels` | read Panel node values |
| `cad_slice_summary` / `cad_section_outline` / `cad_agent_tags` | retro-engineering eyes |
| `cad_export` | write `step` / `stl` / `gltf`, returns the path |

Resources: `cad://help`, `cad://nodes`, `cad://nodes/{type}`,
`cad://graph/{id}`, `cad://graph/{id}/code`, `cad://graph/{id}/view`.
Prompts: `cad_design`, `cad_modify`, `cad_analyze`.

### Recommended loop

1. `cad_help`, then `cad_get_node_catalog(query=...)` to find node types.
2. `cad_create_graph` → one `cad_apply_ops` batch (nodes + wires + params).
   Don't pass positions: new nodes are placed for you; `cad_arrange` tidies.
3. `cad_execute`; if it returns `node_errors` (a `node_id → message` map), fix
   the offending node and re-run.
4. Read the **view summary** (bbox / volume / area / counts) to verify
   dimensions, and `cad_screenshot` to look at it.
5. Iterate with `cad_set_param` / `cad_edit_code` (try values first with
   `cad_execute(overrides=...)`), then `cad_export` when satisfied.

**Do not hand-edit `graph.json`** (docker exec + sed/curl). The tools validate,
keep ids stable and save once; a hand edit skips all of that, and an open
editor tab overwrites it on its next save.

---

## 2. HTTP API (any language)

Base URL: `http://localhost:8090`. No auth (run locally/trusted only — see the
security note below).

```bash
# Orient yourself (markdown guide: model, wires, endpoints, loops)
curl -s localhost:8090/api/agent/help
curl -s 'localhost:8090/api/agent/help?topic=screenshots'

# Discover node types: one line each, filtered
curl -s 'localhost:8090/api/nodes?query=fillet'
curl -s localhost:8090/api/nodes/FilletChamfer

# Create a graph, then edit it in one atomic, validated batch
curl -s -X POST localhost:8090/api/graph/demo \
  -H 'Content-Type: application/json' -d '{"name":"demo","nodes":[],"connections":[]}'
curl -s -X POST localhost:8090/api/graph/demo/ops -H 'Content-Type: application/json' -d '{"ops":[
  {"op":"add_node","type":"Box","params":{"width":20,"height":20,"depth":10},"title":"Body"},
  {"op":"add_node","type":"FilletChamfer","params":{"size":2}},
  {"op":"connect","from":"$0.result","to":"$1.part"}]}'

# Read it back compactly; execute with the lean summary; look at it
curl -s localhost:8090/api/graph/demo/compact
curl -s -X POST 'localhost:8090/api/graph/demo/execute?lean=1'
curl -s 'localhost:8090/api/graph/demo/screenshot?view=iso' -o demo.png --fail

# Export STEP
curl -s localhost:8090/api/graph/demo/export/step
```

Every MCP editing tool has an HTTP twin (`/compact`, `/ops`, `/set_param`,
`/edit_code`, `/validate`, `/arrange`, `/execute?lean=1`) — the full table is in
the guide. A graph is `{nodes, connections}`; each node has `id`, `type`,
`params`, optional `title`/`position`; each connection links
`from_node/from_socket → to_node/to_socket` over a typed wire.

---

## 3. Offline CLI (a graph.json file, no server)

```bash
docker exec -i noodle python -m cad_nodes.cli validate projects/demo/graph.json   # wiring + params, exit 1 on errors
docker exec -i noodle python -m cad_nodes.cli arrange  projects/demo/graph.json   # tidy layout in place (-o OUT)
docker exec -i noodle python -m cad_nodes.cli execute  projects/demo/graph.json --summary
docker exec -i noodle python -m cad_nodes.cli catalog --query thread
```

---

## Security

The engine runs graph code — including `CodeBlock` / `Expression` nodes — as
**arbitrary Python in a subprocess, not yet sandboxed**. The HTTP API and MCP
server are **unauthenticated**. Run noodle **single-user, locally, trusted input
only**; never expose port 8090 to an untrusted network. Sandboxing is tracked as
**D3** in `PLAN_NODE_CAD.md`.
