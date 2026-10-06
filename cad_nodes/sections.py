"""Read a CodeBlock as the nodes it already contains — without changing it.

An agent writes one long CodeBlock (the TARS-pet body is ~570 lines: params,
derived dimensions, helper functions, a front shell built up over five
sections, a back shell over seven, legs, feet, reference parts). The code is
fine; what is missing is a way to SEE its structure. This module derives it
from the AST, so neither the agent nor the user has to change how they write:

- **sections** — split at the block's own section headers (a comment line like
  ``# ---- servo ----`` or ``# ======== frontale ========``). A block with no
  headers is grouped by the variable each statement builds (``body``, ``lid``,
  ``ruote`` …), with `#@param` lines → *Parametri*, pure arithmetic → *Quote*
  and ``def``s → *Funzioni*.
- **kind** — ``params`` | ``quote`` | ``funcs`` | ``part`` | ``chain``. A *chain*
  section takes a shape an earlier section made and keeps working on it
  (``bezel -= …``): the steps of a part, as a feature tree would show them.
- **edges** — which names flow from one section into another, resolved
  statement by statement to the last assignment before the use (straight-line
  reaching definitions; good enough for top-level CAD code). A call to a helper
  adds the helper's free variables at the call site, because Python binds them
  when the function runs.

Pure Python (``ast``), no build123d: safe for the server, the MCP and tests.
``instrument()`` is the one piece that touches code: it returns a COPY with a
marker after each section, used only by the sections run (timings, values,
one section's preview). The block itself is never rewritten.
"""
from __future__ import annotations

import ast
import re
from typing import Any

# A section header: a comment line at column 0 made of a run of - or = and a
# title, e.g. "# ---- servo ----", "# -------- frontale (cornice) --------".
_HEADER = re.compile(r"^#\s*[-=]{3,}\s*(?P<title>[^-=].*?)\s*[-=]*\s*$")
_PARAM = re.compile(r"#@param\b")
# Calls that keep a statement "pure arithmetic" (a dimension, not geometry).
_NUMERIC_CALLS = {"min", "max", "abs", "round", "int", "float", "bool", "len",
                  "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
                  "radians", "degrees", "hypot", "floor", "ceil", "pow", "sum",
                  "Vector", "Axis", "Location"}
# Methods that change their receiver in place: `ghost.append(x)` writes ghost.
_MUTATORS = {"append", "extend", "insert", "update", "add", "pop", "remove",
             "clear", "sort", "reverse", "setdefault", "discard"}


def _names(node: ast.AST, ctx) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ctx)}


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _is_numeric_expr(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if _call_name(n) not in _NUMERIC_CALLS:
                return False
        elif isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                            ast.GeneratorExp, ast.Dict, ast.Set, ast.List)):
            return False
    return True


def _local_binds(node: ast.stmt) -> set[str]:
    """Names a statement binds BEFORE it uses them: loop/comprehension targets,
    `with … as`, and any plain assignment inside a compound statement. A read
    of such a name is local, not a value flowing in from an earlier section
    (`for sx in (-1, 1): … sx …` does not read the `sx` of the last section)."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.comprehension):
            out |= _names(n.target, ast.Store)
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.If, ast.With,
                         ast.AsyncWith, ast.Try)):
        for n in ast.walk(node):
            if isinstance(n, (ast.For, ast.AsyncFor)):
                out |= _names(n.target, ast.Store)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    out |= _names(t, ast.Store)
            elif isinstance(n, ast.withitem) and n.optional_vars is not None:
                out |= _names(n.optional_vars, ast.Store)
    return out


class _Stmt:
    __slots__ = ("node", "start", "end", "writes", "reads", "calls", "kind", "key", "local")

    def __init__(self, node: ast.stmt, lines: list[str]):
        self.node = node
        self.start, self.end = node.lineno, node.end_lineno or node.lineno
        self.reads = _names(node, ast.Load)
        self.writes = _names(node, ast.Store)
        self.calls = {_call_name(c) for c in ast.walk(node) if isinstance(c, ast.Call)} - {None}
        for a in ast.walk(node):                 # x += …, also inside a loop body
            if isinstance(a, ast.AugAssign) and isinstance(a.target, ast.Name):
                self.writes.add(a.target.id)
                self.reads.add(a.target.id)
        for c in ast.walk(node):                 # ghost.append(...) writes ghost
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr in _MUTATORS and isinstance(c.func.value, ast.Name)):
                self.writes.add(c.func.value.id)
                self.reads.add(c.func.value.id)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self.kind = "def"
            self.writes = {node.name}
            self.reads = set()
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.kind = "def"
            self.writes = {(a.asname or a.name).split(".")[0] for a in node.names}
        elif any(_PARAM.search(lines[i - 1]) for i in range(self.start, self.end + 1)):
            self.kind = "param"
        elif (isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
              and node.value is not None and _is_numeric_expr(node.value)
              and all(isinstance(t, (ast.Name, ast.Tuple)) for t in
                      (node.targets if isinstance(node, ast.Assign) else [node.target]))):
            self.kind = "quote"
        else:
            self.kind = "geo"
        self.key = None
        self.local = _local_binds(node)


def _free_vars(fn: ast.FunctionDef) -> set[str]:
    args = {a.arg for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs}
    if fn.args.vararg:
        args.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        args.add(fn.args.kwarg.arg)
    return _names(fn, ast.Load) - args - _names(fn, ast.Store)


def _comment_before(lines: list[str], lineno: int) -> str:
    """The comment line right above `lineno` (a nicer subtitle than a var name)."""
    i = lineno - 2
    while i >= 0 and not lines[i].strip():
        i -= 1
    if i >= 0 and lines[i].lstrip().startswith("#") and not _HEADER.match(lines[i]):
        return lines[i].strip().lstrip("#").strip()[:120]
    return ""


def analyze(code: str) -> dict[str, Any]:
    """The sections of a CodeBlock, their kinds and the names flowing between
    them. Never raises on bad code: a block that does not parse comes back as
    one section with `error` set."""
    lines = (code or "").splitlines()
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        return {"sections": [{"id": "s1", "title": "codice", "kind": "part",
                              "ranges": [[1, max(1, len(lines))]], "lines": len(lines)}],
                "edges": [], "outputs": {}, "headers": False,
                "error": f"{e.msg} (line {e.lineno})"}
    stmts = [_Stmt(n, lines) for n in tree.body]
    numeric: set[str] = set()                 # names that only hold dimensions
    for s in stmts:
        if s.kind == "param":
            numeric |= s.writes
        elif s.kind == "quote":
            if s.reads - s.writes <= numeric | _NUMERIC_CALLS | {"math", "pi"}:
                numeric |= s.writes
            else:
                s.kind = "geo"                # e.g. `result = body`
    funcs = {s.node.name: s.node for s in stmts
             if isinstance(s.node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    free = {name: _free_vars(fn) for name, fn in funcs.items()}

    def called_free(s: _Stmt) -> set[str]:
        """Free variables of every helper this statement calls, transitively."""
        out, todo, seen = set(), [c for c in s.calls if c in funcs], set()
        while todo:
            f = todo.pop()
            if f in seen:
                continue
            seen.add(f)
            for v in free[f]:
                if v in funcs:
                    todo.append(v)
                else:
                    out.add(v)
        return out

    headers = [(i, m.group("title").strip()) for i, l in enumerate(lines, 1)
               if (m := _HEADER.match(l)) and m.group("title").strip()]
    sections: list[dict] = []
    owner: dict[int, int] = {}                  # stmt index -> section index

    if len(headers) >= 2:
        bounds = headers + [(len(lines) + 1, None)]
        first_header = headers[0][0]
        if any(s.start < first_header for s in stmts):
            sections.append({"title": "Inizio", "subtitle": "", "ranges": []})
        for (ln, title), _nxt in zip(headers, bounds[1:], strict=True):
            sections.append({"title": title, "subtitle": "", "ranges": [], "_from": ln})
        for k, s in enumerate(stmts):
            if s.start < first_header:
                owner[k] = 0
            else:
                idx = max(i for i, (ln, _t) in enumerate(headers) if ln <= s.start)
                owner[k] = idx + (1 if sections[0]["title"] == "Inizio" and "_from" not in sections[0] else 0)
    else:
        # No headers: group by what each statement builds.
        later_reads: dict[str, int] = {}
        for _k, s in enumerate(stmts):
            for n in s.reads:
                later_reads[n] = later_reads.get(n, 0) + 1
        groups: dict[str, int] = {}

        def group(key: str, title: str, sub: str = "") -> int:
            if key not in groups:
                groups[key] = len(sections)
                sections.append({"title": title, "subtitle": sub, "ranges": []})
            return groups[key]

        writers: dict[str, int] = {}
        for k, s in enumerate(stmts):
            if s.kind == "param":
                owner[k] = group("__params__", "Parametri")
            elif s.kind == "def":
                owner[k] = group("__funcs__", "Funzioni")
            elif s.kind == "quote":
                owner[k] = group("__quote__", "Quote")
            else:
                cand = [n for n in s.writes if n not in ("result",)] or sorted(s.writes)
                # a loop counter is not what a loop builds: prefer names read later
                cand.sort(key=lambda n: (-(n in writers), -later_reads.get(n, 0), n))
                # single-assignment wrapper of ONE earlier group (dummy = Compound(ghost))
                src = [n for n in s.reads if n in writers]
                if (len(s.writes) == 1 and len(src) == 1
                        and not any(w in writers for w in s.writes)):
                    owner[k] = writers[src[0]]
                elif cand:
                    key = next((n for n in cand if n in writers), cand[0])
                    owner[k] = writers.get(key) if key in writers else group(
                        key, key, _comment_before(lines, s.start))
                else:                                          # bare expression
                    owner[k] = owner.get(k - 1, group("__misc__", "Altro"))
            for w in s.writes:
                if s.kind not in ("param", "def", "quote") and w not in writers:
                    writers[w] = owner[k]

    # ranges (merge adjacent statements of one section into one line span)
    for k, s in enumerate(stmts):
        rng = sections[owner[k]]["ranges"]
        if rng and s.start - rng[-1][1] <= 1 + _gap_comments(lines, rng[-1][1], s.start):
            rng[-1][1] = s.end
        else:
            rng.append([s.start, s.end])

    # def-use: the last writer before each read
    last: dict[str, int] = {}
    edges: dict[tuple[int, int], set[str]] = {}
    reads_from: list[set[str]] = [set() for _ in sections]
    written: list[set[str]] = [set() for _ in sections]
    for k, s in enumerate(stmts):
        sec = owner[k]
        need = ((s.reads - s.local) | called_free(s) | (s.calls & set(funcs))) - {"result"} \
            if s.kind != "def" else set()
        for n in need:
            w = last.get(n)
            if w is not None and owner[w] != sec:
                edges.setdefault((owner[w], sec), set()).add(n)
                reads_from[sec].add(n)
        for n in s.writes:
            last[n] = k
            written[sec].add(n)

    params = {n for s in stmts if s.kind == "param" for n in s.writes}
    out_names = re.findall(r"#@out\s+(\w+)", code or "")
    outputs = {n: _sid(owner[last[n]]) for n in out_names + ["result"] if n in last}
    exported = {n for (a, b), ns in edges.items() for n in ns} | set(outputs)

    result = []
    for i, sec in enumerate(sections):
        members = [stmts[k] for k in owner if owner[k] == i]
        kinds = {m.kind for m in members}
        if not members:
            kind = "part"
        elif kinds <= {"param"}:
            kind = "params"
        elif kinds <= {"def"}:
            kind = "funcs"
        elif kinds <= {"param", "quote"}:
            kind = "quote"
        else:
            kind = "part"
        chain = sorted(n for n in written[i] & reads_from[i] if n not in params)
        if kind == "part" and chain:
            kind = "chain"
        uses_params = sorted(n for m in members for n in (m.reads | called_free(m)) if n in params)
        result.append({
            "id": _sid(i), "title": sec["title"], "subtitle": sec.get("subtitle", ""),
            "kind": kind, "ranges": sec["ranges"],
            "lines": sum(b - a + 1 for a, b in sec["ranges"]),
            "statements": len(members),
            "params": sorted(set(uses_params)) if kind not in ("params",) else
                      sorted(n for m in members for n in m.writes),
            "functions": sorted(m.node.name for m in members
                                if isinstance(m.node, (ast.FunctionDef, ast.AsyncFunctionDef))),
            "produces": sorted(n for n in written[i] if n in exported),
            "chain": chain if kind == "chain" else [],
            "unused": kind in ("part", "chain") and not (written[i] & exported),
            "_writes": sorted(written[i]),
            "_end": max((m.end for m in members), default=0),
        })
    # `result = [frontale, retro, gamba(-1), …]`: which section makes each item
    items = []
    res_stmt = next((stmts[k] for k in sorted(owner, reverse=True)
                     if "result" in stmts[k].writes and isinstance(stmts[k].node, ast.Assign)), None)
    if res_stmt is not None and isinstance(res_stmt.node.value, (ast.List, ast.Tuple)):
        res_k = stmts.index(res_stmt)
        for idx, el in enumerate(res_stmt.node.value.elts):
            src = ast.get_source_segment(code, el) or ""
            sec = None
            if isinstance(el, ast.Name) and el.id in last:
                w = max((k for k in range(res_k) if el.id in stmts[k].writes), default=None)
                sec = owner[w] if w is not None else None
            else:
                # a call or an expression: the latest geometry section among what it uses
                used = _names(el, ast.Load) | {_call_name(c) for c in ast.walk(el)
                                                if isinstance(c, ast.Call)}
                cands = []
                for n in used:
                    w = max((k for k in range(res_k + 1) if n in stmts[k].writes
                             and stmts[k].kind not in ("param", "quote")), default=None)
                    if w is not None:
                        cands.append(w)
                sec = owner[max(cands)] if cands else None
            items.append({"index": idx, "expr": src[:60], "section": _sid(sec) if sec is not None else None})
    return {
        "headers": len(headers) >= 2,
        "result_items": items,
        "sections": result,
        "edges": [{"from": _sid(a), "to": _sid(b), "names": sorted(ns)}
                  for (a, b), ns in sorted(edges.items())],
        "outputs": outputs,
        "params": len(params),
        "lines": len(lines),
    }


def _gap_comments(lines: list[str], after: int, before: int) -> int:
    """Blank/comment lines between two statements (they stay in the same span)."""
    gap = 0
    for i in range(after, before - 1):
        t = lines[i].strip()
        if not t or (t.startswith("#") and not _HEADER.match(lines[i])):
            gap += 1
        else:
            return 0
    return gap


def _sid(i: int) -> str:
    return f"s{i + 1}"


MARK = "__sec_mark__"


def instrument(code: str, analysis: dict | None = None) -> str:
    """A COPY of the block with ``__sec_mark__('<id>', locals())`` after the last
    statement of every section (and ``'__start__'`` before the first non-param
    statement). A section spread over several ranges gets one marker per range.
    For the sections run only — the stored block never changes.
    Markers sit at column 0 of the block, i.e. at the body's top level."""
    analysis = analysis or analyze(code)
    if analysis.get("error"):
        return code
    lines = code.splitlines()
    # a marker after EVERY contiguous range: a section spread over the block
    # (Quote, or `result = body` far below) is timed as the sum of its ranges
    after: dict[int, list[str]] = {}
    for s in analysis["sections"]:
        for _a, b in s["ranges"]:
            after.setdefault(b, []).append(s["id"])
    tree = ast.parse(code)
    first = next((n.lineno for n in tree.body
                  if not _PARAM.search(lines[n.lineno - 1])), None)
    out = []
    for i, l in enumerate(lines, 1):
        if first is not None and i == first:
            out.append(f"{MARK}('__start__', locals())")
        out.append(l)
        for sid in after.get(i, ()):
            out.append(f"{MARK}({sid!r}, locals())")
    return "\n".join(out) + "\n"


def compact(analysis: dict) -> dict:
    """The shape the API returns: no private keys, no empty fields; `ranges`
    stays a list of [first, last] line pairs (1-based, inclusive)."""
    secs = []
    for s in analysis["sections"]:
        d = {k: v for k, v in s.items() if not k.startswith("_") and v not in ([], "", False)}
        d.setdefault("ranges", [])
        secs.append(d)
    return {**{k: v for k, v in analysis.items() if k != "sections"}, "sections": secs}
