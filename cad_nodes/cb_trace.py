"""🔦 Traccia — which piece of a CodeBlock's result each `#@param` moves.

quill: «i blocchi di codice sono lunghi e modificare un valore è lentissimo, e
devi sapere cosa stai muovendo: non puoi scoprirlo senza leggere il codice».
The answer is a side run of the block from a rewritten COPY (this module) with
a runtime injected in front of it (cb_trace_rt.py), which records, while the
block really runs:

- which `#@param` every number came from (numbers that remember);
- every piece that enters a boolean, what it cost, and where it ends up;
- the picked edges of a fillet / chamfer, with the radius's params.

Measured on the three longest real blocks (creepyfinger-v4, walle-v7, tars),
the booleans are 69-86% of the run and the pieces that go INTO them rebuild
in < 100 ms in 95-100% of cases (median < 1 ms). So the trace draws, for each
value, the pieces it moves: a piece whole when it costs ≤ budget, else its
parts, else the curves it is made from. The editor lights them up on hover —
no run, nothing recomputed when a slider is released.

The rewrite is pure `ast`: no build123d needed (tests run it as is).
"""
from __future__ import annotations

import ast
from pathlib import Path

from .transpiler import _CB_OUT, _parse_cb_line, parse_codeblock_outputs, parse_codeblock_params

RUNTIME = (Path(__file__).with_name("cb_trace_rt.py")).read_text()
BUDGET_S = 0.100            # a piece that rebuilds within this is drawn whole

# calls left alone: frame-sensitive builtins, and the recorder itself
_SKIP = {"super", "locals", "globals", "vars", "exec", "eval", "isinstance", "issubclass",
         "type", "print", "__cbt_call__", "__cbt_bin__", "__cbt_param__", "__cbt_done__", "__cbt_ln__"}
_OPS = {ast.Add: "add", ast.Sub: "sub", ast.BitAnd: "and", ast.Mult: "mul"}


def _at(line, test):
    """`__cbt_ln__(line) or test`: the block's own line stamped before a
    decision is taken (the value and the truth of `test` are unchanged)."""
    return ast.copy_location(ast.BoolOp(op=ast.Or(), values=[
        ast.Call(func=ast.Name("__cbt_ln__", ast.Load()), args=[ast.Constant(line)], keywords=[]),
        test]), test)


class _Rewrite(ast.NodeTransformer):
    def visit_Compare(self, node):
        self.generic_visit(node)
        return _at(node.lineno, node)

    def _test(self, node):
        self.generic_visit(node)
        node.test = _at(node.lineno, node.test)
        return node

    visit_If = visit_While = visit_IfExp = _test

    def visit_Call(self, node):
        self.generic_visit(node)
        if isinstance(node.func, ast.Name) and node.func.id in _SKIP:
            return node
        return ast.copy_location(ast.Call(
            func=ast.Name("__cbt_call__", ast.Load()),
            args=[ast.Constant(node.lineno), node.func, *node.args],
            keywords=node.keywords), node)

    def visit_BinOp(self, node):
        self.generic_visit(node)
        op = _OPS.get(type(node.op))
        if op is None:
            return node
        return ast.copy_location(ast.Call(
            func=ast.Name("__cbt_bin__", ast.Load()),
            args=[ast.Constant(node.lineno), ast.Constant(op), node.left, node.right],
            keywords=[]), node)

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        op = _OPS.get(type(node.op))
        if op is None or not isinstance(node.target, ast.Name):
            return node
        # x += y  →  x = __cbt_bin__(…, x, y, True): the in-place operator is kept,
        # so a list grown with += is still the same list
        return ast.copy_location(ast.Assign(
            targets=[ast.Name(node.target.id, ast.Store())],
            value=ast.Call(func=ast.Name("__cbt_bin__", ast.Load()),
                           args=[ast.Constant(node.lineno), ast.Constant(op),
                                 ast.Name(node.target.id, ast.Load()), node.value, ast.Constant(True)],
                           keywords=[])), node)


def instrument(code: str) -> str:
    """The block's traced copy. The `#@param` declaration lines are kept
    verbatim (the transpiler turns them into the function's arguments, as for
    the real block) and so are the `#@out` lines; then each numeric param is
    re-bound to a number that remembers its name; then the rewritten body;
    then the outputs handed to the trace. Raises SyntaxError like the block."""
    code = code or "result = None"
    lines = code.splitlines()
    decl_at = {i for i, raw in enumerate(lines, 1) if _parse_cb_line(raw) is not None}
    tree = ast.parse(code)
    body = [s for s in tree.body if s.lineno not in decl_at]
    new = ast.fix_missing_locations(_Rewrite().visit(ast.Module(body=body, type_ignores=[])))
    nums = [d["name"] for d in parse_codeblock_params(code) if d["type"] in ("int", "float")]
    outs = tuple(o["name"] for o in parse_codeblock_outputs(code))
    keep = [lines[i - 1] for i in sorted(decl_at)] + [raw for raw in lines if _CB_OUT.match(raw)]
    pre = [f"{n} = __cbt_param__({n}, {n!r})" for n in nums]
    return "\n".join(keep + pre + [ast.unparse(new) or "pass", f"__cbt_done__(locals(), {outs!r})"]) + "\n"


# a replay of a parked trace (executor.codeblock_trace_replay): no graph, only
# the trace's own function, its pieces meshed by the ordinary epilogue
REPLAY = """
import sys as _cbt_s, json as _cbt_j
__result__, __panels__, __errors__, __timings__, __previews__ = None, {{}}, {{}}, {{}}, {{}}
_cbt_T = getattr(_cbt_s.modules.get("_noodle_cbt"), "traces", {{}}).get({tid!r})
if _cbt_T is None:
    _cbt_res = {{"gone": True}}
else:
    __previews__, _cbt_res = _cbt_T["replay"]({values!r}, {focus!r}, {probe!r}, {heavy!r})
with open({out!r}, "w") as _cbt_f:
    _cbt_j.dump(_cbt_res, _cbt_f)
"""

TAIL = """
# --- codeblock trace (injected by executor) ---
_cbt_finish({budget!r}, {out!r}, tid={tid!r})
"""
