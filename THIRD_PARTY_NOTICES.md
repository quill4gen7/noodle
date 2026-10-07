# Third-party notices

noodle itself is licensed under the **MIT License** (see `LICENSE`). It builds
on the following third-party components, whose licenses are reproduced/located as
noted. None of these change the MIT license of noodle's own source, because
they are used as **dynamically-linked libraries** (Python imports) or as a
**separate process** invoked over a CLI. Browser libraries are redistributed
under their own licenses in `webui/vendor/` (see below).

## Runtime libraries (Python — see `requirements.txt`)

| Component | Role | License |
|---|---|---|
| **OpenCASCADE Technology (OCCT)** — shipped as the `cadquery-ocp` wheel pulled in by build123d | B-Rep geometry kernel | **LGPL-2.1** (with the OPEN CASCADE exception) |
| build123d | high-level modelling API | Apache-2.0 |
| NumPy | numerics in the worker | BSD-3-Clause |
| SciPy | spatial helpers (Voronoi, etc.) in the worker | BSD-3-Clause |
| trimesh | the mesh lane — triangle I/O, repair, transforms (PLAN_MESH_LANE.md) | MIT |
| manifold3d | the mesh lane — booleans + tolerance-bounded simplify | Apache-2.0 |
| FastAPI | HTTP API framework | MIT |
| Starlette | ASGI toolkit (FastAPI dependency) | BSD-3-Clause |
| Uvicorn | ASGI server | BSD-3-Clause |
| python-multipart | multipart parsing | Apache-2.0 |
| mcp | Model Context Protocol SDK | MIT |

### LGPL-2.1 compliance note (OCCT)

OCCT is the only "weak copyleft" dependency. noodle does **not** modify OCCT
and links it only dynamically (via the `cadquery-ocp` Python extension). Under
LGPL-2.1 this permits noodle to remain MIT-licensed, provided we:

- keep this notice (attribution),
- do not strip OCCT's own license/copyright headers from its distribution, and
- allow users to replace the OCCT library with a modified version — which is
  inherent here, since OCCT is a separately-installed pip wheel the user can
  swap (`pip install -U cadquery-ocp`).

## Frontend (bundled/served from `webui/`)

| Component | Role | License |
|---|---|---|
| three.js 0.170.0 | 3D viewport (+ the `DecalGeometry` addon: text on the part in `/view`) | MIT — `webui/vendor/three-0.170.0/LICENSE` |
| litegraph.js 0.7.18 | node graph UI | MIT — `webui/vendor/litegraph-0.7.18/LICENSE` |
| Ace 1.36.2 | code editor | BSD-3-Clause — `webui/vendor/ace-1.36.2/LICENSE` |
| manifold-3d 3.5.4 (JS + WASM) | in-browser booleans for drag anticipation (`webui/anticipate.js`) | Apache-2.0 — `webui/vendor/manifold-3d-3.5.4/LICENSE` |

Runtime files are copied unmodified from pinned npm releases, including their
license notices. `python scripts/vendor_webui.py` refreshes the selected files
and transitive Three addon imports. Both editors work without a CDN connection.

The geometry pipeline is build123d-only; there is no GPL component in the stack
(the former OpenSCAD backend has been removed).
