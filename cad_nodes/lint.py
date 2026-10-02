"""
lint — soft, pure-Python checks of a graph: things that are legal but that
mislead whoever reads the design (person or agent). Never an error, never
blocks a run; execute_graph attaches the list as result["lint"] and the
/lint route + MCP cad_lint return it on demand (e.g. right after set_code).

Each finding: {"level": "error"|"warning"|"info", "code", "node", "message", …}.

  codeblock_syntax   a CodeBlock that will not compile (line/col block-relative)
                     — caught at save time instead of at the next run.
  slider_mismatch    a NumberSlider/IntegerSlider wired into a CodeBlock
                     `#@param` whose value/min/max disagree with what the code
                     declares: the code reads `h = 12 #@param min=4 max=30`
                     while the graph actually feeds 10 from a 4..25 slider.
  cb_override        a hidden per-CodeBlock override (params["_cb"]) — the
                     value that runs is NOT the one written in the code; also
                     overrides that are dead (param wired, or no longer declared).
  cb_out_unassigned  a `#@out` the code never assigns (the socket will be None).
  animate_chain      an Animate fed by another Animate. It does NOT play the two
                     in sequence: the downstream one moves its input FROZEN at
                     the upstream's own `t`, and only the last plan is replayed —
                     "open, then close" becomes "stay closed, then swing into
                     the body" (walle's lid). One Animate per moving part; the
                     /view player gives each its own slider.
"""

from __future__ import annotations

import ast

from . import catalog
from .graph import Graph
from .transpiler import (check_codeblock, parse_codeblock_outputs,
                         parse_codeblock_params)

_SLIDERS = ("NumberSlider", "IntegerSlider")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _differs(a, b) -> bool:
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return a != b
    return abs(fa - fb) > 1e-9


def _slider_state(node) -> dict:
    """value/min/max a slider actually has: its per-instance drag window
    (params._ui.value) over the catalog's default bounds."""
    pdef = catalog.get(node.type).param("value")
    ui = ((node.params or {}).get("_ui") or {}).get("value") or {}
    return {"value": (node.params or {}).get("value", pdef.default if pdef else None),
            "min": ui.get("min", pdef.min if pdef else None),
            "max": ui.get("max", pdef.max if pdef else None)}


def _assigned_names(code: str) -> tuple[set, set]:
    """(names bound anywhere, string constants) in a block — best effort."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set(), set()
    bound, strs = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            strs.add(n.value)
    return bound, strs


def _fmt(v) -> str:
    f = _num(v)
    if f is None:
        return repr(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def lint_graph(graph: Graph) -> list[dict]:
    """Every soft finding for `graph` (see module doc), in node order."""
    out: list[dict] = []
    by_id = {n.id: n for n in graph.nodes}
    for node in graph.nodes:
        if node.type == "Animate":
            for (src_id, _sock) in graph.inputs_of(node.id).get("shape", []):
                src = by_id.get(src_id)
                if src is not None and src.type == "Animate":
                    out.append({"level": "warning", "code": "animate_chain",
                                "node": node.id, "upstream": src_id,
                                "message": f"Animate {node.id} is fed by Animate {src_id}: "
                                           "they do not play in sequence — this one moves "
                                           f"{src_id}'s result frozen at {src_id}'s own t, "
                                           "and only this plan is replayed. Use ONE Animate "
                                           "per moving part, wired from the part itself; "
                                           "the viewer gives each its own slider."})
        if node.type != "CodeBlock":
            continue
        code = (node.params or {}).get("code") or ""
        bad = check_codeblock(code)
        if bad:
            out.append({"level": "error", "code": "codeblock_syntax", "node": node.id,
                        "line": bad["line"], "col": bad["col"],
                        "message": f"{bad['message']} at line {bad['line']}"
                                   + (f", col {bad['col']}" if bad["col"] else "")})
        decls = {d["name"]: d for d in parse_codeblock_params(code)}
        feeds = graph.inputs_of(node.id)

        # sliders wired into a #@param
        for name, d in decls.items():
            for (src_id, _sock) in feeds.get(name, []):
                src = by_id.get(src_id)
                if src is None or src.type not in _SLIDERS:
                    continue
                s = _slider_state(src)
                diffs = []
                if _differs(s["value"], d["default"]):
                    diffs.append(f"value {_fmt(s['value'])} vs default {_fmt(d['default'])}")
                for k in ("min", "max"):
                    if d[k] is not None and s[k] is not None and _differs(s[k], d[k]):
                        diffs.append(f"{k} {_fmt(s[k])} vs {_fmt(d[k])}")
                v = _num(s["value"])
                outside = v is not None and (
                    (d["min"] is not None and v < d["min"] - 1e-9)
                    or (d["max"] is not None and v > d["max"] + 1e-9))
                bounds = any(not x.startswith("value") for x in diffs)
                if diffs:
                    label = f"{src.title!r} ({src_id})" if src.title else src_id
                    out.append({
                        "level": "warning" if (outside or bounds) else "info",
                        "code": "slider_mismatch", "node": node.id, "param": name,
                        "slider": src_id,
                        "slider_state": s,
                        "declared": {k: d[k] for k in ("default", "min", "max")},
                        "message": f"#@param {name} is driven by slider {label}: "
                                   + "; ".join(diffs)
                                   + (" — slider value is OUTSIDE the declared range"
                                      if outside else
                                      " — the code's literal is not what runs")})

        # hidden / dead per-block overrides
        for name, val in ((node.params or {}).get("_cb") or {}).items():
            d = decls.get(name)
            if d is None:
                out.append({"level": "warning", "code": "cb_override", "node": node.id,
                            "param": name, "value": val, "stale": True,
                            "message": f"override _cb.{name}={_fmt(val)} targets a "
                                       "#@param the code no longer declares (dead)"})
            elif feeds.get(name):
                out.append({"level": "info", "code": "cb_override", "node": node.id,
                            "param": name, "value": val, "dead": True,
                            "message": f"override _cb.{name}={_fmt(val)} is ignored: "
                                       "the socket is wired"})
            elif _differs(val, d["default"]):
                out.append({"level": "warning", "code": "cb_override", "node": node.id,
                            "param": name, "value": val, "default": d["default"],
                            "message": f"hidden override: {name} runs as {_fmt(val)} "
                                       f"but the code says {_fmt(d['default'])} "
                                       f"(node.params._cb)"})

        # #@out never assigned
        outs = parse_codeblock_outputs(code)
        if outs and not bad:
            bound, strs = _assigned_names(code)
            for o in outs:
                if o["name"] not in bound and o["name"] not in strs:
                    out.append({"level": "warning", "code": "cb_out_unassigned",
                                "node": node.id, "output": o["name"],
                                "message": f"#@out {o['name']} is never assigned "
                                           "(the socket will carry None)"})
    return out
