"""
CLI for the node engine — works offline on a graph.json file, no server.

    python -m cad_nodes.cli transpile <graph.json>
        Print the generated build123d code.

    python -m cad_nodes.cli execute  <graph.json> [--workdir DIR] [--summary]
        Transpile + execute (requires build123d) and print the result JSON.
        --summary prints the lean agent summary instead (no code, no meshes,
        rounded floats) — the same shape as MCP cad_execute.

    python -m cad_nodes.cli validate <graph.json>
        Check wiring (bad sockets list the real ones), soft warnings and
        stored params against the catalog. Exit 1 on a hard error or a param
        issue. No build123d needed.

    python -m cad_nodes.cli arrange  <graph.json> [-o OUT]
        Tidy node positions (layout.arrange: no overlaps) and write the graph
        back in place, or to OUT. Prints the layout summary.

    python -m cad_nodes.cli catalog [--compact] [--query TEXT]
        Print the node catalog as JSON, or one signature line per type.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import api, catalog
from .graph import Graph
from .transpiler import transpile


def _load_graph(path: str) -> Graph:
    data = json.loads(Path(path).read_text())
    return Graph.from_dict(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cad_nodes")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_t = sub.add_parser("transpile", help="graph.json -> build123d code")
    p_t.add_argument("graph")

    p_e = sub.add_parser("execute", help="graph.json -> run + view JSON")
    p_e.add_argument("graph")
    p_e.add_argument("--workdir", default=None)
    p_e.add_argument("--summary", action="store_true",
                     help="lean agent summary: no code, no meshes, rounded")

    p_v = sub.add_parser("validate", help="check wiring + params, no run")
    p_v.add_argument("graph")

    p_a = sub.add_parser("arrange", help="tidy node positions")
    p_a.add_argument("graph")
    p_a.add_argument("-o", "--output", default=None,
                     help="write here instead of in place")

    p_c = sub.add_parser("catalog", help="dump node catalog")
    p_c.add_argument("--compact", action="store_true",
                     help="one signature line per type")
    p_c.add_argument("--query", default="", help="substring filter (implies --compact)")

    args = parser.parse_args(argv)

    if args.cmd == "catalog":
        if args.compact or args.query:
            print(api.compact_catalog(query=args.query))
        else:
            print(json.dumps(catalog.as_json(), indent=2))
        return 0

    if args.cmd == "transpile":
        graph = _load_graph(args.graph)
        print(transpile(graph))
        return 0

    if args.cmd == "validate":
        report = api.validation_report(_load_graph(args.graph))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 1

    if args.cmd == "arrange":
        from . import layout
        src = Path(args.graph)
        data = json.loads(src.read_text())
        graph = Graph.from_dict(data)
        summary = layout.arrange(graph)
        # Keep the file's own top-level keys (description, ...) — only the
        # graph proper is rewritten.
        out = {**data, **graph.to_dict()}
        Path(args.output or src).write_text(json.dumps(out, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        return 0

    if args.cmd == "execute":
        from .executor import execute_graph  # local import: needs build123d
        graph = _load_graph(args.graph)
        workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="cadrun_"))
        result = execute_graph(graph, workdir)
        if args.summary:
            print(json.dumps(api.summarize_execute(result), indent=2,
                             ensure_ascii=False))
            return 0 if result["success"] else 1
        printable = {k: v for k, v in result.items() if k != "view"}
        print(json.dumps(printable, indent=2))
        print("--- view ---")
        print(json.dumps(result.get("view"), indent=2))
        return 0 if result["success"] else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
