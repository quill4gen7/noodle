# noodle — nanobot Skill

## Description
**Node-based** parametric CAD. You compose a graph of nodes; the backend
transpiles it to **build123d** (Python), runs it in an isolated worker and
returns STL + a mesh for the viewport. One geometry engine: build123d
(OpenCASCADE). No OpenSCAD/CadQuery.

## Service
- **Docker**: container `noodle` on port **8090**
- **API base**: `http://localhost:8090`
- **Node editor**: `http://localhost:8090/nodes`
- **Code view** (build123d generated from the graph, read-only): `http://localhost:8090/ui`
- **Agent guide**: `GET /api/agent/help` (MCP `cad_help`) — read it first;
  `?topic=screenshots|retroeng|print|threads|fluid` for detail.

## Main endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/agent/help` | Orientation guide (markdown) |
| GET | `/api/nodes?query=…` · `/api/nodes/{type}` | Catalog, one line per type · one type in detail |
| GET | `/api/projects` | List projects (graphs have `backend: "nodegraph"`) |
| POST | `/api/graph/{name}` | Create/overwrite a graph (body: `{name, nodes, connections}`) |
| GET | `/api/graph/{name}/compact` | Read the graph compactly (`?node=` one node in full) |
| POST | `/api/graph/{name}/ops` | Atomic, validated batch of edits (`{ops: [...]}`) |
| POST | `/api/graph/{name}/set_param` · `/edit_code` | Validated param edit (node by id or title) · CodeBlock str-replace |
| POST | `/api/graph/{name}/execute?lean=1` | Run → lean summary + per-node errors (`{overrides}` body: try values unsaved) |
| GET | `/api/graph/{name}/screenshot` | PNG of the viewport |
| POST | `/api/graph/{name}/arrange` | Tidy the node layout |
| GET | `/api/graph/{name}/export/{fmt}` | Export (`step`, `stl`, `gltf`) |
| DELETE | `/api/projects/{name}` | Delete a project |
| POST | `/api/copilot/chat` | Copilot: natural language → graph edits |
| GET | `/api/system/health` · `/api/system/logs` · POST `/api/system/restart` | Health / backend logs / restart |
| GET | `/health` | Health check |

## How an agent builds a model
A graph is `{nodes, connections}`. Each node has `id`, `type` (from the
catalog), `params`, optional `title`; each connection links
`from_node/from_socket` → `to_node/to_socket` (typed wires).

Preferred routes, in order:
1. **MCP server** (`mcp_server.py` → `cad_nodes.api`): `cad_apply_ops`,
   `cad_set_param`, `cad_edit_code`, `cad_execute`, `cad_screenshot`… — the
   source of truth, shared with the copilot. See `AGENTS.md`.
2. **HTTP**: the same operations (`/ops`, `/set_param`, `/edit_code`,
   `/execute?lean=1`); read `node_errors` (`node_id → message`) and fix.
3. **Copilot**: `POST /api/copilot/chat` in natural language.

Never hand-edit `graph.json`: the API validates, keeps ids stable and places
new nodes; an open editor tab would overwrite a hand edit anyway.

## Running the service
```bash
cd ~/projects/noodle
docker compose up -d --build   # start
docker compose logs -f         # logs
docker restart noodle          # after changes to server.py / cad_nodes
docker compose down            # stop
```
