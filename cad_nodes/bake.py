"""Bake what the viewport shows into files — runs INSIDE the execution worker.

The editor's 📦 export (and the home page's quick export) do not export
``__result__``: they export every node whose eye is on — ``__previews__``, the
exact dict the viewport is drawn from (``Transpiler._previewed`` is the one gate
that fills it, see CLAUDE.md §4). One node = one STEP + one STL, named after the
node, so a zip of a scene with a jar, its cap and six bolts comes out as the
pieces you were looking at rather than one anonymous blob.

The bake is at the SAVED parameter values: a Drop/Animate is exported at the
``t`` its slider holds, posed exactly as the engine baked it (the live 60fps
scrub is a browser replay and never reaches here).

What cannot go to a format is recorded, not raised — a bundle with one curve
node in it still delivers the solids:
  - a mesh (the trimesh lane) has no B-Rep, so it gets an STL and no STEP
    (``MeshToSolid`` is the explicit bridge, and it costs minutes, §5c);
  - a curve has no triangles, so it gets a STEP and no STL;
  - bare points get nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .mesh_extractor import _as_shape, _deflection, _is_mesh, _is_point


def _flatten(value, out: list) -> list:
    if isinstance(value, (list, tuple)):
        for v in value:
            _flatten(v, out)
    elif value is not None:
        out.append(value)
    return out


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-._")
    return (s or fallback)[:48]


def _as_brep(shapes: list):
    """One shape for the files, WITHOUT re-parenting the inputs.

    mesh_extractor._as_shape builds ``Compound(children=shapes)``, and assigning
    ``children`` moves each shape under the new compound — it leaves the parent
    it had. On the warm worker these are the memo cache's own objects, shared with
    ``__result__``: after one bake the result's assembly tree was missing pieces
    and the next ⬇ STEP export of the result failed ("Failed to write STEP file";
    STL and glTF, which do not walk the tree, still worked). ``Compound(list)``
    builds the compound from the shapes without touching their parents.

    A single shape is written as is, unless it has a parent (a piece of another
    compound): build123d cannot STEP-export such a shape directly ("Failed to
    write STEP file"), so it too goes through ``Compound([shape])``."""
    if len(shapes) == 1 and getattr(shapes[0], "parent", None) is None:
        return _as_shape(shapes[0])
    from build123d import Compound
    try:
        return Compound(shapes)
    except Exception:
        return _as_shape(shapes)


def _has_triangles(shape) -> bool:
    try:
        return bool(shape.faces())
    except Exception:
        return False


def _export_one(value, base: Path) -> dict:
    """Write ``base``.step/.stl for one previewed value; return what happened."""
    items = _flatten(value, [])
    extras = []
    for v in items:
        extras.extend(getattr(v, "_noodle_extra", None) or [])
    rec: dict = {"files": [], "skipped": {}}
    meshes = [v for v in items if _is_mesh(v)]
    shapes = [v for v in items if not _is_mesh(v) and not _is_point(v)]
    if not meshes and not shapes:
        rec["skipped"]["all"] = "points only — nothing to export"
        return rec

    brep = _as_brep(shapes) if shapes else None
    # STEP: B-Rep only.
    if brep is not None:
        try:
            from build123d import export_step
            p = base.with_suffix(".step")
            export_step(brep, str(p))
            rec["files"].append(p.name)
        except Exception as e:
            rec["skipped"]["step"] = f"{type(e).__name__}: {e}"[:300]
    else:
        rec["skipped"]["step"] = "mesh lane — no B-Rep (use Mesh To Solid for STEP)"

    # STL: every triangle, B-Rep tessellated + meshes, in ONE file. Pure B-Rep
    # goes through build123d's own exporter (the same one the ⬇ STL button uses);
    # a mix is concatenated through trimesh at the "fine" deflection.
    try:
        p = base.with_suffix(".stl")
        if brep is not None and not meshes and _has_triangles(brep):
            from build123d import export_stl
            export_stl(brep, str(p))
            rec["files"].append(p.name)
        else:
            import trimesh
            parts = [m.tm for m in meshes]
            if brep is not None and _has_triangles(brep):
                verts, tris = brep.tessellate(_deflection(brep, 0.004), 0.15)
                if tris:
                    parts.append(trimesh.Trimesh(
                        vertices=[[v.X, v.Y, v.Z] for v in verts],
                        faces=[list(t) for t in tris], process=False))
            if parts:
                mesh = parts[0] if len(parts) == 1 else trimesh.util.concatenate(parts)
                mesh.export(str(p))
                rec["files"].append(p.name)
            else:
                rec["skipped"]["stl"] = "curves only — no triangles"
    except Exception as e:
        rec["skipped"]["stl"] = f"{type(e).__name__}: {e}"[:300]

    # A moving Drop container rides the result as `_noodle_extra` (§5d-bis): it
    # is on screen, so it is in the bundle — as its own pair of files.
    if extras:
        sub = _export_one(extras, base.with_name(base.name + "_container"))
        rec["files"] += sub["files"]
        for k, v in sub["skipped"].items():
            rec["skipped"]["container " + k] = v
    if not rec["skipped"]:
        rec.pop("skipped")
    return rec


def bake_previews(previews: dict, result, outdir: str, labels: dict) -> dict:
    """Export every previewed node into ``outdir`` and write ``manifest.json``.

    ``labels`` = {node_id: {"title", "type"}} from the server (the worker only
    sees ids). Falls back to ``__result__`` when no node is previewed, so a
    bundle is never empty while the viewport shows something."""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    entries = list((previews or {}).items())
    if not entries and result is not None:
        entries = [("__result__", result)]
    manifest = {"nodes": []}
    used: set[str] = set()
    for i, (nid, value) in enumerate(entries, 1):
        lab = (labels or {}).get(nid) or {}
        title = lab.get("title") or lab.get("type") or nid
        stem = f"{i:02d}_{_slug(title, 'node')}_{_slug(nid, 'n')}"
        while stem in used:
            stem += "_"
        used.add(stem)
        try:
            rec = _export_one(value, out / stem)
        except Exception as e:           # one bad node never sinks the bundle
            rec = {"files": [], "skipped": {"all": f"{type(e).__name__}: {e}"[:300]}}
        manifest["nodes"].append({"node": nid, "type": lab.get("type"),
                                  "title": title, **rec})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
