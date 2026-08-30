"""Every CAD-engine call must leave the event loop.

An engine call is seconds of blocking CPU (a graph run, or a cold build123d
import at ~2.7s). Made from an `async def` it freezes the whole server for its
duration — including /api/graph/{name}/progress, the SSE stream reporting on the
very run in flight. Six routes did exactly that (render, download, export,
slice_summary, section_outline, subshapes); only /execute was off the loop.

The check is structural, and that is what makes it cheap: a wrapped call passes
the engine function by NAME (`off_loop(execute_graph, …)`), so the function is
never the target of a Call node. An unwrapped one always is. So "no ast.Call on
an engine name anywhere in server.py" catches a regression without importing
FastAPI, build123d or anything else.
"""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server.py"

# Everything in server.py that reaches cad_nodes.executor. `set_warm` is here
# for a different reason than the rest: it does no CPU work itself, it WAITS on
# the warm worker's lock — which a run holds for its whole duration. Blocking the
# loop on a lock is blocking the loop.
ENGINE_CALLS = {
    "execute_graph",
    "export_graph",
    "extract_subshapes_for_node",
    "slice_summary",
    "section_outline",
    "set_warm",
}


def _tree():
    return ast.parse(SERVER.read_text())


def _called_name(node):
    """The bare name a Call targets: f(…) -> 'f', mod.f(…) -> 'f'."""
    fn = node.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return None


def test_no_engine_call_runs_on_the_event_loop():
    offenders = [
        f"line {n.lineno}: {_called_name(n)}(...)"
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call) and _called_name(n) in ENGINE_CALLS
    ]
    assert not offenders, (
        "these engine calls block the event loop; pass them through off_loop() "
        "instead of calling them:\n  " + "\n  ".join(offenders))


def test_off_loop_actually_wraps_every_engine_function():
    """The mirror of the test above: it would also pass if the calls simply
    vanished. Pin that each engine function is really handed to off_loop."""
    wrapped = set()
    for n in ast.walk(_tree()):
        if isinstance(n, ast.Call) and _called_name(n) == "off_loop" and n.args:
            first = n.args[0]
            if isinstance(first, ast.Name):
                wrapped.add(first.id)
            elif isinstance(first, ast.Attribute):
                wrapped.add(first.attr)
    assert ENGINE_CALLS <= wrapped, f"never offloaded: {ENGINE_CALLS - wrapped}"


def test_off_loop_is_the_only_way_across():
    """One named helper, not scattered to_thread calls — so the rule stays
    greppable and its reasoning lives in exactly one docstring."""
    tree = _tree()
    inside = {                                  # off_loop's own body is the one
        id(n) for fn in ast.walk(tree)          # legitimate to_thread call site
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "off_loop"
        for n in ast.walk(fn)
    }
    direct = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "to_thread"
        and id(n) not in inside
    ]
    assert not direct, (
        f"asyncio.to_thread called directly at lines {direct}; use off_loop()")
