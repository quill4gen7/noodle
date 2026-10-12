#!/usr/bin/env python3
"""Translate a Grasshopper .gh into a noodle graph, one node per component.

    python scripts/gh_import.py FILE.gh --out graph.json
    python scripts/gh_import.py FILE.gh --geometry GEO.json --out graph.json
        # GEO.json = gh_report.py FILE.gh --geometry GEO.json, run where
        # rhino3dm is installed; without it Rhino curves are not rebuilt
    python scripts/gh_import.py FILE.gh --project NAME [--server URL] [--arrange]
        # save it as a project through the server API (never by hand)

The mapping is cad_nodes/gh/components.py; what it cannot translate is
printed as a report (components not in the table, unmapped outputs, flags).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cad_nodes.gh import definition, importer  # noqa: E402


def _post(url, body=None):
    req = urllib.request.Request(url, method="POST",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "noodle-gh-import"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{url}: HTTP {e.code} {e.read().decode(errors='replace')[:2000]}")


def _exists(url) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "noodle-gh-import"})) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--geometry", metavar="GEO.json")
    ap.add_argument("--out", metavar="graph.json")
    ap.add_argument("--project")
    ap.add_argument("--server", default="http://localhost:8090")
    ap.add_argument("--arrange", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite an existing project")
    a = ap.parse_args()
    d = definition.read(a.file)
    kw = {"decode": importer.decode_from_report(a.geometry)} if a.geometry else {}
    res = importer.import_definition(d, **kw)
    print(res.report())
    if a.project:
        res.graph["name"] = a.project
    if a.out:
        with open(a.out, "w") as f:
            json.dump(res.graph, f, indent=1)
    if a.project:
        base = f"{a.server}/api/graph/{a.project}"
        if not a.force and _exists(base):
            print(f"project {a.project!r} exists: --force to overwrite it", file=sys.stderr)
            return 1
        _post(base, res.graph)
        if a.arrange:
            _post(f"{base}/arrange")
        print(f"saved as project {a.project!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
