"""The Grasshopper mapping table and the `grasshopper` nodes — pure Python.

The runtime helpers need build123d and are exercised in the worker; here we pin
the contract the importer relies on: every mapped socket exists, every multi-out
helper names its outputs as the node declares them, the GH nodes never clamp,
and the transpiler gives each output of a multi-out node its own variable.
"""
import re
from pathlib import Path

import pytest

from cad_nodes import catalog, catalog_gh
from cad_nodes.gh.components import COMPONENTS
from cad_nodes.gh.runtime import GH_PREAMBLE
from cad_nodes.graph import Graph
from cad_nodes.transpiler import transpile

SAMPLES = Path.home() / "projects" / "gh-samples"


@pytest.mark.parametrize("guid", sorted(COMPONENTS))
def test_every_mapped_socket_exists(guid):
    m = COMPONENTS[guid]
    if m.kind not in ("alias", "new"):
        return
    d = catalog.get(m.noodle)
    ins = {s.name for s in d.inputs}
    pars = {p.name for p in d.params}
    outs = {s.name for s in d.outputs}
    for gh, sock in m.inputs.items():
        assert sock in ins, f"{m.gh}.{gh} -> {m.noodle}.{sock}: no such input"
    for gh, par in m.params.items():
        assert par in pars, f"{m.gh}.{gh} -> {m.noodle}.{par}: no such param"
    for gh, sock in m.outputs.items():
        assert sock in outs, f"{m.gh}.{gh} -> {m.noodle}.{sock}: no such output"
    if m.kind == "new":
        assert d.category == "grasshopper"
        assert d.label == m.gh


def test_every_grasshopper_node_is_in_the_table():
    mapped = {m.noodle for m in COMPONENTS.values()}
    gh = {t for t, d in catalog.REGISTRY.items() if d.category == "grasshopper"}
    # Unit X/Y/Z and the planes are mapped; XY Plane too, though no sample uses it
    # Graft Tree stands for the [graft] FLAG on an input (inserted by the
    # importer, as List Flatten is for [flatten]); no sample uses the component
    flags = {"GhGraftTree", "GhSurfaceParam", "GhBrepParam"}   # importer-inserted
    assert gh - flags <= mapped, sorted(gh - mapped)


def test_multi_out_helpers_return_the_declared_sockets():
    """A multi-out node reads each output by name out of the helper's dict: a
    key spelled differently is an output that is silently always None."""
    for d in catalog.REGISTRY.values():
        if not d.multi_out:
            continue
        helper = re.match(r"\s*(\w+)\(", d.code_template["algebra"]).group(1)
        m = re.search(rf"def {helper}\(.*?(?=\ndef |\Z)", GH_PREAMBLE, re.S)
        assert m, f"{d.type}: helper {helper} not in the GH preamble"
        body = m.group(0)
        for s in d.outputs:
            assert f'"{s.name}"' in body, f"{d.type}: {helper} never returns {s.name!r}"


def test_grasshopper_numbers_are_never_clamped():
    for d in catalog.REGISTRY.values():
        if d.category != "grasshopper":
            continue
        for p in d.params:
            assert p.min is None and p.max is None, f"{d.type}.{p.name} is bounded"


def test_multi_out_gives_each_output_its_own_var():
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "c", "type": "GhCircle", "params": {"radius": 5}},
        {"id": "e", "type": "GhEndPoints", "params": {}},
        {"id": "l", "type": "Line", "params": {}},
    ], "connections": [
        {"id": "w1", "from_node": "c", "from_socket": "circle", "to_node": "e", "to_socket": "curve"},
        {"id": "w2", "from_node": "e", "from_socket": "start", "to_node": "l", "to_socket": "start"},
        {"id": "w3", "from_node": "e", "from_socket": "end", "to_node": "l", "to_socket": "end"},
    ]})
    src = transpile(g)
    picks = re.findall(r"(__out_\d+) = _cb_pick\((\w+), '(start|end)'\)", src)
    assert {p[2] for p in picks} == {"start", "end"}
    start = next(v for v, _, s in picks if s == "start")
    end = next(v for v, _, s in picks if s == "end")
    assert re.search(rf"Line\(_pt\({start}\), _pt\({end}\)\)", src) or \
        re.search(rf"\{{'start': {start}, 'end': {end}\}}", src)


def test_an_unwired_list_access_input_falls_back_to_its_param():
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "r", "type": "GhRectangle", "params": {"x_size": 20, "y_size": 7}},
    ], "connections": []})
    assert "_gh_rectangle(None, 20.0, 7.0, 0.0)" in transpile(g)


def test_mirror_takes_a_wired_plane_and_keeps_the_curve_type():
    d = catalog.get("Mirror")
    assert d.input("plane") is not None and d.output_follows == "shape"


@pytest.mark.parametrize("sample", ["ikeaLack.gh", "barattolo.gh"])
def test_the_sample_files_are_fully_mapped(sample):
    if not (SAMPLES / sample).exists():
        pytest.skip("no gh-samples")
    from cad_nodes.gh import definition
    d = definition.read(SAMPLES / sample)
    missing = {(o.type, o.type_guid) for o in d.objects if o.type_guid not in COMPONENTS}
    assert not missing, sorted(missing)


def test_no_grasshopper_node_has_an_origin_socket():
    """The emitter wraps a socket named `origin` around the node's WHOLE result
    (_at): a GH node whose input is called Origin would be placed twice."""
    for d in catalog.REGISTRY.values():
        if d.category == "grasshopper":
            assert d.input("origin") is None, d.type


def test_grasshopper_list_inputs_go_through_the_branch_fanout():
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "s", "type": "Series", "params": {}},
        {"id": "a", "type": "GhAverage", "params": {}},
    ], "connections": [
        {"id": "w", "from_node": "s", "from_socket": "result", "to_node": "a", "to_socket": "input"},
    ]})
    src = transpile(g)
    assert re.search(r"_gh_fanout\(lambda input: _gh_average\(input\), \{\}, \{'input': __out_\d+\}\)", src)


def _fanout_fragment():
    """_fanout + _gh_fanout lifted out of the PREAMBLE, run with no build123d."""
    from cad_nodes.transpiler import PREAMBLE

    def grab(name):
        m = re.search(rf"\ndef {name}\(.*?(?=\n\S)", PREAMBLE, re.S)
        return m.group(0)
    ns = {"ShapeList": type("ShapeList", (list,), {}), "_FANOUT_MAX": 2000}
    for name in ("_is_seq", "_fanout", "_gh_nested", "_gh_fanout"):
        exec(grab(name), ns)
    return ns


def test_branch_fanout_runs_a_list_input_once_per_branch():
    ns = _fanout_fragment()
    mean = lambda input: sum(input) / len(input)
    # a flat list: one call over the whole list
    assert ns["_gh_fanout"](mean, {}, {"input": [1, 2, 3]}) == 2
    # a tree (list of lists): one call per branch, as a grafted GH input
    # a tree (list of lists): one call per branch, as a grafted GH input — and
    # each branch's single result stays a branch
    assert ns["_gh_fanout"](mean, {}, {"input": [[1, 3], [10, 20]]}) == [[2], [15]]


def test_branch_fanout_pairs_items_with_their_branch():
    ns = _fanout_fragment()
    scale = lambda geometry, center: (geometry - center) * 2 + center
    pts = [[1, 2], [10, 12]]            # two branches of points
    centres = [[0], [10]]               # one centre per branch (a tree)
    out = ns["_gh_fanout"](scale, {"geometry": pts, "center": centres}, {})
    assert out == [[2, 4], [10, 14]]
    # a flat list next to a tree is ONE branch reused for every branch
    out = ns["_gh_fanout"](scale, {"geometry": pts, "center": 0}, {})
    assert out == [[2, 4], [20, 24]]
    # a FLAT list next to a tree is one branch too: matched item by item in each
    out = ns["_gh_fanout"](scale, {"geometry": pts, "center": [0, 10]}, {})
    assert out == [[2, -6], [20, 14]]


def test_one_item_fanned_is_one_branch_not_a_tree():
    ns = _fanout_fragment()
    pts = lambda curve: [curve, curve + 1]           # a list output
    assert ns["_gh_fanout"](pts, {"curve": [5]}, {}) == [5, 6]
    assert ns["_gh_fanout"](pts, {"curve": [5, 7]}, {}) == [[5, 6], [7, 8]]


def test_a_numeric_param_may_hold_a_domain_and_emits_only_numbers():
    from cad_nodes.transpiler import format_param
    p = catalog.get("GhPlaneSurface").param("x_size")
    assert format_param(p, [-10, 10]) == "[-10.0, 10.0]"
    with pytest.raises((TypeError, ValueError)):
        format_param(p, ["__import__('os')", 1])


def test_graft_puts_every_item_in_its_own_branch():
    from cad_nodes.transpiler import PREAMBLE
    ns = {"ShapeList": type("ShapeList", (list,), {})}
    for name in ("_is_seq", "_gh_graft"):
        exec(re.search(rf"\ndef {name}\(.*?(?=\n\S)", PREAMBLE, re.S).group(0), ns)
    assert ns["_gh_graft"]([1, 2]) == [[1], [2]]
    assert ns["_gh_graft"]([[1, 2], [3]]) == [[[1], [2]], [[3]]]
    assert ns["_gh_graft"](5) == [[5]]


def test_expression_binds_x_and_y_without_a_fan_out():
    """Expression's template used to be the bare `(x + y)`: x and y existed
    only inside a fan-out lambda, so two plain numbers wired in gave 'name not
    defined' (found building the native barattolo)."""
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "a", "type": "NumberInput", "params": {"value": 2}},
        {"id": "b", "type": "NumberInput", "params": {"value": 3}},
        {"id": "e", "type": "Expression", "params": {"expr": "x * y"}},
    ], "connections": [
        {"id": "w1", "from_node": "a", "from_socket": "result", "to_node": "e", "to_socket": "x"},
        {"id": "w2", "from_node": "b", "from_socket": "result", "to_node": "e", "to_socket": "y"},
    ]})
    line = next(l for l in transpile(g).splitlines() if "@node:e " in l)
    ns = {}
    exec(line.split("#")[0].strip().replace(line.split("=")[0].strip(), "r", 1)
         .replace("__out_1", "2").replace("__out_2", "3"), ns)
    assert ns["r"] == 6
