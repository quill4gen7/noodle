#!/usr/bin/env python3
"""Read a Grasshopper .gh and say what it contains — no Rhino needed.

    python scripts/gh_report.py FILE.gh                 # the graph, as text
    python scripts/gh_report.py FILE.gh --geometry OUT.json
        # + decode the Rhino geometry cached in the file (needs rhino3dm)

The geometry JSON maps each object's number (#k, as in the report) to its
decoded records; cad_nodes.gh.geometry.curve_code() turns curve records into a
CodeBlock that rebuilds them exactly.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cad_nodes.gh import definition, geometry  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--geometry", metavar="OUT.json")
    a = ap.parse_args()
    d = definition.read(a.file)
    print(definition.report(d))
    if a.geometry:
        if not geometry.have_rhino3dm():
            print("rhino3dm is not installed: geometry not decoded", file=sys.stderr)
            return 2
        out, bad = {}, 0
        for o in d.objects:
            recs = []
            for p in o.inputs if o.floating else []:
                for v in p.data or []:
                    if isinstance(v, dict) and v.get("on_data"):
                        try:
                            recs.append(geometry.decode(v["on_data"]))
                        except Exception as e:      # one bad blob must not stop the rest
                            bad += 1
                            recs.append({"kind": "error", "error": str(e)})
            if recs:
                out[str(o.k)] = {"type": o.type, "nick": o.nick, "records": recs}
        with open(a.geometry, "w") as f:
            json.dump(out, f)
        n = sum(len(v["records"]) for v in out.values())
        print(f"geometry: {n} objects from {len(out)} parameters -> {a.geometry}"
              + (f" ({bad} failed)" if bad else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
