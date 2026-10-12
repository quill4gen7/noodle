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
    assert gh <= mapped, sorted(gh - mapped)


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
    assert re.search(rf"Line\(_pt\({start}\), _pt\({end}\)\)", src)


def test_an_unwired_list_access_input_falls_back_to_its_param():
    g = Graph.from_dict({"name": "t", "nodes": [
        {"id": "r", "type": "GhRectangle", "params": {"x_size": 20, "y_size": 7}},
    ], "connections": []})
    assert "_gh_rectangle(None, 20.0, 7.0, 0.0)" in transpile(g)


def test_mirror_takes_a_wired_plane_and_keeps_the_curve_type():
    d = catalog.get("Mirror")
    assert d.input("plane") is not None and d.output_follows == "shape"


@pytest.mark.skipif(not (SAMPLES / "ikeaLack.gh").exists(), reason="no gh-samples")
def test_ikealack_is_fully_mapped():
    from cad_nodes.gh import definition
    d = definition.read(SAMPLES / "ikeaLack.gh")
    missing = {(o.type, o.type_guid) for o in d.objects if o.type_guid not in COMPONENTS}
    assert not missing, sorted(missing)
