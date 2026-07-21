"""Group children (BuildPart/BuildSketch): parameters and progress events.

A group's children are substituted INLINE into the parent's `with` block rather
than emitted as statements of their own, and that one difference had let two
bugs sit unnoticed — no saved project uses a group, so nothing exercised the
path at all.

Pure-Python: these assert on the generated source, no build123d needed.
"""
from __future__ import annotations

import pytest

from cad_nodes.graph import Graph
from cad_nodes.transpiler import transpile


def _group_graph() -> Graph:
    return Graph.from_dict({
        "name": "grouptest",
        "nodes": [
            {"id": "g1", "type": "BuildPart", "params": {}, "position": [0, 0]},
            {"id": "c1", "type": "Box",
             "params": {"width": 10, "height": 11, "depth": 12},
             "position": [0, 0], "parent": "g1"},
            {"id": "c2", "type": "Cylinder", "params": {"radius": 3, "height": 20},
             "position": [0, 0], "parent": "g1"},
        ],
        "connections": [],
    })


# --- params-as-inputs must not wipe a child's widgets ----------------------

def test_child_keeps_its_params():
    """A child's parameters must survive into the generated call.

    `_input_values` reports "None" for every UNWIRED socket, and Box's width/
    height/depth are params-as-inputs (§5b) — sockets sharing a param name. The
    group path merged that over the param values blindly, so every child came
    out as `Box(None, None, None)` and the group died with an OCCT constructor
    TypeError. `_emit_simple` had the fallback rule inline; now both share
    `_merge_inputs`.
    """
    code = transpile(_group_graph())
    assert "Box(10.0, 11.0, 12.0" in code, f"child params lost:\n{code}"
    assert "Cylinder(3.0, 20.0" in code, f"child params lost:\n{code}"
    assert "Box(None" not in code and "Cylinder(None" not in code


def test_group_node_itself_keeps_its_params():
    """The same blind merge applied to the group node, not just its children."""
    code = transpile(_group_graph())
    assert "with BuildPart()" in code, code


# --- progress events -------------------------------------------------------

def test_children_emit_progress_events():
    """Each child brackets itself in _ev(), so the editor can light it up.

    Without this a BuildPart of twenty nodes showed ONE glow and the twenty
    stayed dark for the whole run, however long they took.
    """
    code = transpile(_group_graph(), memo=True)
    for cid in ("c1", "c2"):
        assert f"_ev('s', {cid!r})" in code, f"{cid} never reports a start:\n{code}"
        assert f"_ev('e', {cid!r}" in code, f"{cid} never reports an end:\n{code}"


def test_child_failure_is_reported_and_still_fails_the_group():
    """A child's inner try/except REPORTS and re-raises.

    It must not swallow the error — a failing child has always failed its whole
    group and that stays true. But without the report the exception would unwind
    straight past the child's start event, leaving that node breathing amber for
    the rest of the run: the same lie any unclosed span tells.
    """
    code = transpile(_group_graph(), memo=True)
    assert "_ev('e', 'c1', __timings__['c1'], False, True)" in code, code
    # the re-raise keeps the group's own error handling intact
    assert "        raise" in code, code


def test_children_are_reported_cached_on_a_memo_hit():
    """On a cache hit the whole block is skipped, children included.

    They must still report, or a group's children would light up on a cold run
    and go dark on every warm one — which looks exactly like the glow failing
    at random, the bug this all came from.
    """
    code = transpile(_group_graph(), memo=True)
    hit = code.split("else:", 1)[1] if "else:" in code else ""
    for cid in ("c1", "c2"):
        assert f"__cached__[{cid!r}] = True" in code, f"{cid} not reported cached:\n{code}"
        assert f"_ev('e', {cid!r}, 0.0, True)" in code, f"{cid} has no cached event:\n{code}"
    assert hit, "expected a memo-hit branch"


def test_no_progress_events_without_memo():
    """The /ui code view stays clean — _ev is an execute-path concern only."""
    code = transpile(_group_graph())
    assert "_ev(" not in code.split("# --- nodes ---")[-1], code


@pytest.mark.parametrize("group_type,kind", [("BuildPart", "part"), ("BuildSketch", "sketch")])
def test_empty_group_still_emits(group_type, kind):
    """A group with no children must not generate a syntactically broken block."""
    g = Graph.from_dict({
        "name": "t",
        "nodes": [{"id": "g1", "type": group_type, "params": {}, "position": [0, 0]}],
        "connections": [],
    })
    code = transpile(g, memo=True)
    compile(code, "<generated>", "exec")     # the real assertion
    assert "pass" in code
