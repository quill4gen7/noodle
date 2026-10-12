#!/usr/bin/env python3
"""Compare an imported graph, output by output, with Grasshopper's own run.

    python scripts/gh_compare.py GRAPH.json MAP.json REF.json [--out TABLE.json]

GRAPH.json / MAP.json come from `gh_import.py FILE.gh --out … --map …`;
REF.json from `gh_measure.py` run inside Rhino (count of items, branches,
sum of length/area/volume and bbox of every output parameter). Needs
build123d: run it where noodle runs (the container).

For each GH output that landed on a noodle socket it prints one line:
    OK      counts and measures agree (1e-3 relative, 1e-3 mm absolute)
    DIFF    they do not — what differs, Rhino's value first
    EMPTY   noodle produced nothing where GH produced something
The first DIFF/EMPTY along a chain is usually the one worth reading.
"""
import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cad_nodes.graph import Graph  # noqa: E402
from cad_nodes.transpiler import Transpiler  # noqa: E402
from cad_nodes.executor import _run_json  # noqa: E402

_EPILOGUE = r'''

# --- gh_compare (injected) ---
import json as _json
from build123d import Shape, ShapeList, Vector
def _gc_items(v, depth=0):
    if isinstance(v, (list, tuple, ShapeList)):
        out, br = [], 0
        for x in v:
            if isinstance(x, (list, tuple, ShapeList)):
                br += 1
            out += _gc_items(x, depth + 1)[0]
        return out, br
    return ([] if v is None else [v]), 0

def _gc_stats(v):
    items, br = _gc_items(v)
    s = {"items": len(items), "branches": br or 1, "sum": {}, "types": {}}
    lo, hi = [1e300] * 3, [-1e300] * 3
    def grow(b):
        for i, (a, c) in enumerate(((b.min.X, b.max.X), (b.min.Y, b.max.Y), (b.min.Z, b.max.Z))):
            lo[i] = min(lo[i], a); hi[i] = max(hi[i], c)
    for x in items:
        tn = type(x).__name__
        s["types"][tn] = s["types"].get(tn, 0) + 1
        try:
            if isinstance(x, Shape):
                grow(x.bounding_box())
                sol = x.solids()
                if sol:
                    s["sum"]["volume"] = s["sum"].get("volume", 0) + sum(z.volume for z in sol)
                fa = x.faces()
                if fa:
                    s["sum"]["area"] = s["sum"].get("area", 0) + sum(f.area for f in fa)
                elif x.edges():
                    s["sum"]["length"] = s["sum"].get("length", 0) + sum(e.length for e in x.edges())
            elif isinstance(x, Vector):
                grow(type("B", (), {"min": x, "max": x})())
            elif getattr(x, "_noodle_mesh", False):
                b = x.tm.bounds
                grow(type("B", (), {"min": Vector(*b[0]), "max": Vector(*b[1])})())
                if x.tm.is_watertight:
                    s["sum"]["volume"] = s["sum"].get("volume", 0) + float(x.tm.volume)
        except Exception as _e:
            s.setdefault("err", str(_e))
    if lo[0] < 1e299:
        s["bbox"] = lo + hi
    return s

_gc = {}
for _key, _var in @@VARS@@.items():
    try:
        _gc[_key] = _gc_stats(globals().get(_var))
    except Exception as _e:
        _gc[_key] = {"err": str(_e)}
with open(@@OUT@@, "w") as _f:
    _json.dump({"success": True, "stats": _gc, "errors": {k: str(v)[:200] for k, v in __errors__.items()}}, _f)
'''


def _close(a, b, rel=1e-3, ab=1e-3):
    return abs(a - b) <= max(ab, rel * max(abs(a), abs(b)))


def compare(ref_out: dict, mine: dict) -> tuple[str, str]:
    if not ref_out.get("items"):
        return ("SKIP", "GH empty") if not mine.get("items") else ("DIFF", f"GH empty, noodle {mine['items']}")
    if not mine or not mine.get("items"):
        return "EMPTY", f"GH {ref_out['items']} items"
    why = []
    if ref_out["items"] != mine["items"]:
        why.append(f"items {ref_out['items']}≠{mine['items']}")
    for k in ("volume", "area", "length"):
        r, m = ref_out.get("sum", {}).get(k), mine.get("sum", {}).get(k)
        if r is not None and m is not None and not _close(r, m):
            why.append(f"{k} {r:.6g}≠{m:.6g}")
        elif r is not None and m is None and r > 1e-6:
            why.append(f"{k} {r:.6g}≠—")
    rb, mb = ref_out.get("bbox"), mine.get("bbox")
    if rb and mb and not all(_close(a, b, 1e-3, 0.01) for a, b in zip(rb, mb)):
        why.append("bbox " + ",".join(f"{x:.4g}" for x in rb) + " ≠ " + ",".join(f"{x:.4g}" for x in mb))
    return ("DIFF", "; ".join(why)) if why else ("OK", "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("graph")
    ap.add_argument("map")
    ap.add_argument("ref")
    ap.add_argument("--out")
    ap.add_argument("--workdir", default="/tmp/gh_compare")
    a = ap.parse_args()
    g = Graph.from_dict(json.load(open(a.graph)))
    omap = json.load(open(a.map))
    ref = json.load(open(a.ref))
    t = Transpiler(g, memo=True)
    code = t.run()
    vars_ = {}
    for key, srcs in omap.items():
        for nid, sock in srcs:
            v = t.out_var_of.get((nid, sock)) or t.var_of.get(nid)
            if v:
                vars_[f"{nid}.{sock}"] = v
    data = _run_json(code + _EPILOGUE.replace("@@VARS@@", repr(vars_)),
                     pathlib.Path(a.workdir), "ghcmp", 900)
    if not data.get("success"):
        print(data.get("error"))
        return 2
    stats = data["stats"]
    rows, tally = [], {}
    for o in ref["objects"]:
        for j, out in enumerate(o.get("outputs", [])):
            srcs = omap.get(f"{o['k']}:{j}")
            if not srcs:
                continue
            mine = {"items": 0, "sum": {}, "branches": 0}
            bbs = []
            for nid, sock in srcs:
                s = stats.get(f"{nid}.{sock}") or {}
                mine["items"] += s.get("items", 0)
                for k, v in s.get("sum", {}).items():
                    mine["sum"][k] = mine["sum"].get(k, 0) + v
                if s.get("bbox"):
                    bbs.append(s["bbox"])
            if bbs:
                mine["bbox"] = [min(b[i] for b in bbs) for i in range(3)] + \
                               [max(b[i] for b in bbs) for i in range(3, 6)]
            verdict, why = compare(out, mine)
            tally[verdict] = tally.get(verdict, 0) + 1
            rows.append({"k": o["k"], "name": o["name"], "nick": o["nick"], "out": out["name"],
                         "verdict": verdict, "why": why,
                         "nodes": [f"{n}.{s}" for n, s in srcs], "gh_messages": o.get("messages")})
    for r in rows:
        if r["verdict"] in ("DIFF", "EMPTY"):
            print(f"{r['verdict']:5} #{r['k']} {r['name']} '{r['nick']}'.{r['out']} "
                  f"[{', '.join(r['nodes'])}] {r['why']}")
    print(" ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    if a.out:
        json.dump({"rows": rows, "tally": tally, "noodle_errors": data.get("errors"),
                   "stats": stats},
                  open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
