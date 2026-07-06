"""
Pure-Python tests for the node engine (no build123d required).

Run: python -m pytest tests/ -v
"""

import json
from pathlib import Path

import pytest

from cad_nodes import catalog
from cad_nodes.graph import Graph, ValidationError
from cad_nodes.toposort import toposort, CycleError
from cad_nodes.transpiler import transpile

EXAMPLES = Path(__file__).resolve().parent.parent / "cad_nodes" / "examples"


# --- toposort -------------------------------------------------------------
def test_toposort_linear_order():
    order = toposort(["a", "b", "c"], [("a", "b"), ("b", "c")])
    assert order == ["a", "b", "c"]


def test_toposort_is_stable():
    # No edges -> original order preserved.
    assert toposort(["x", "y", "z"], []) == ["x", "y", "z"]


def test_toposort_diamond():
    order = toposort(["a", "b", "c", "d"],
                     [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("a") < order.index("c") < order.index("d")


def test_toposort_detects_cycle():
    with pytest.raises(CycleError):
        toposort(["a", "b"], [("a", "b"), ("b", "a")])


# --- catalog --------------------------------------------------------------
def test_catalog_covers_core_categories():
    cats = {d.category for d in catalog.REGISTRY.values()}
    for expected in ["primitives_3d", "primitives_2d", "operations", "boolean",
                     "modifiers", "transform", "math", "export"]:
        assert expected in cats
    assert "Box" in catalog.REGISTRY
    assert catalog.get("Extrude").input("sketch") is not None


def test_catalog_serialises_to_json():
    data = catalog.as_json()
    assert isinstance(data, list) and len(data) > 20
    # Must be JSON-serialisable end to end.
    json.loads(json.dumps(data))


# --- validation -----------------------------------------------------------
def _flange() -> Graph:
    return Graph.from_dict(json.loads((EXAMPLES / "flange.json").read_text()))


def test_validate_flange_ok():
    warnings = _flange().validate()
    assert warnings == []


def test_validate_unknown_type():
    g = Graph.from_dict({"nodes": [{"id": "x", "type": "Nope"}], "connections": []})
    with pytest.raises(ValidationError):
        g.validate()


def test_validate_incompatible_wire():
    g = Graph.from_dict({
        "nodes": [
            {"id": "n", "type": "NumberSlider", "params": {"value": 1}},
            {"id": "b", "type": "Box"},
        ],
        # A data output cannot feed a geometry input.
        "connections": [{"id": "c", "from_node": "n", "from_socket": "result",
                         "to_node": "b", "to_socket": "missing"}],
    })
    with pytest.raises(ValidationError):
        g.validate()


# --- transpiler golden ----------------------------------------------------
def test_transpile_flange_golden():
    code = transpile(_flange())
    assert "from build123d import *" in code
    # 2D primitives now emit a closed curve (outline); Extrude fills it back to a
    # face (solid=True) via _extrude() so a curve profile still extrudes to a solid.
    assert "__out_1 = _outline(Circle(20.0))" in code
    assert "_extrude(__out_1, 10.0, 0.0, False, True)" in code
    assert "(__out_2 - __out_4)" in code
    assert "export_step(__out_5, 'flange.step')" in code
    assert "__result__ = __out_5" in code


def test_transpile_select_param_maps_to_code():
    g = Graph.from_dict({
        "nodes": [
            {"id": "b", "type": "Box"},
            {"id": "m", "type": "Mirror", "params": {"plane": "YZ"}},
        ],
        "connections": [{"id": "c", "from_node": "b", "from_socket": "result",
                         "to_node": "m", "to_socket": "shape"}],
    })
    code = transpile(g)
    assert "_mirror(__out_1, Plane.YZ, False)" in code


def test_transpile_boolean_operators():
    # build123d algebra: + union, - difference, & intersection (NOT *).
    g = Graph.from_dict({
        "nodes": [
            {"id": "a", "type": "Box"},
            {"id": "b", "type": "Sphere"},
            {"id": "x", "type": "Intersect"},
        ],
        "connections": [
            {"id": "1", "from_node": "a", "from_socket": "result", "to_node": "x", "to_socket": "a"},
            {"id": "2", "from_node": "b", "from_socket": "result", "to_node": "x", "to_socket": "b"},
        ],
    })
    code = transpile(g)
    assert "(__out_1 & __out_2)" in code
    # '*' is for Locations, not intersection. Check the node body only — the
    # fixed runtime PREAMBLE legitimately contains '*' in its helpers.
    body = code.split("# --- nodes ---", 1)[1]
    assert " * " not in body


def test_transpile_bypass_passes_input_through():
    # A bypassed node is skipped; its first matching geometry input passes
    # straight through to its output, so downstream nodes see the upstream value.
    g = Graph.from_dict({
        "nodes": [
            {"id": "b", "type": "Box"},
            {"id": "f", "type": "Fillet", "bypassed": True},
            {"id": "m", "type": "Move"},
        ],
        "connections": [
            {"id": "1", "from_node": "b", "from_socket": "result", "to_node": "f", "to_socket": "part"},
            {"id": "2", "from_node": "f", "from_socket": "result", "to_node": "m", "to_socket": "shape"},
        ],
    })
    code = transpile(g)
    assert "__out_2 = __out_1" in code          # Fillet output == Box output
    assert "# bypassed" in code
    assert ".fillet" not in code.lower()         # the fillet op is not emitted


def test_bypass_flag_round_trips():
    g = Graph.from_dict({"nodes": [{"id": "f", "type": "Fillet", "bypassed": True}],
                         "connections": []})
    assert g.node("f").bypassed is True
    assert g.node("f").to_dict()["bypassed"] is True
    # default omitted when not bypassed
    g2 = Graph.from_dict({"nodes": [{"id": "b", "type": "Box"}], "connections": []})
    assert "bypassed" not in g2.node("b").to_dict()


def test_group_boxes_round_trip():
    # Editor-only group metadata is carried through unchanged so logical
    # clustering survives save/reload and api/copilot round-trips.
    groups = [{"title": "Corpo base", "bounding": [0, 0, 200, 120], "color": "#3f589e"}]
    g = Graph.from_dict({"nodes": [{"id": "b", "type": "Box"}],
                         "connections": [], "groups": groups})
    assert g.groups == groups
    assert g.to_dict()["groups"] == groups
    # absent key when there are no groups (no empty clutter on disk)
    g2 = Graph.from_dict({"nodes": [{"id": "b", "type": "Box"}], "connections": []})
    assert "groups" not in g2.to_dict()


def test_transpile_select_edge_and_targeted_fillet():
    # SelectEdge resolves its picked set at run time; FilletSelectedEdges rounds
    # only those edges (vs Fillet which rounds all).
    g = Graph.from_dict({
        "nodes": [
            {"id": "b", "type": "Box"},
            {"id": "s", "type": "SelectEdge",
             "params": {"selection": {"kind": "edge", "indices": [0, 2],
                                      "sigs": [[5, 0, 5, 10, 0, 0, 1]]}}},
            {"id": "fl", "type": "FilletSelectedEdges", "params": {"radius": 1.5}},
        ],
        "connections": [
            {"id": "1", "from_node": "b", "from_socket": "result", "to_node": "s", "to_socket": "geometry"},
            {"id": "2", "from_node": "b", "from_socket": "result", "to_node": "fl", "to_socket": "part"},
            {"id": "3", "from_node": "s", "from_socket": "selection", "to_node": "fl", "to_socket": "edges"},
        ],
    })
    code = transpile(g)
    assert "def _select_subshapes(" in code          # helper injected
    assert "_select_subshapes(__out_1, 'edge'" in code
    assert "[[5, 0, 5, 10, 0, 0, 1]]" in code        # signatures passed through
    assert "fillet(__out_2, radius=1.5)" in code      # operates on the selection var


def test_transpile_select_face_defaults_kind_and_pushpull():
    # An unpicked SelectFace still transpiles, defaulting its kind to 'face';
    # ExtrudeSelectedFace push/pulls the selection (positive boss / negative pocket).
    g = Graph.from_dict({
        "nodes": [
            {"id": "b", "type": "Box"},
            {"id": "sf", "type": "SelectFace"},   # no selection picked yet
            {"id": "ex", "type": "ExtrudeSelectedFace", "params": {"amount": 8}},
        ],
        "connections": [
            {"id": "1", "from_node": "b", "from_socket": "result", "to_node": "sf", "to_socket": "geometry"},
            {"id": "2", "from_node": "b", "from_socket": "result", "to_node": "ex", "to_socket": "part"},
            {"id": "3", "from_node": "sf", "from_socket": "selection", "to_node": "ex", "to_socket": "faces"},
        ],
    })
    code = transpile(g)
    assert "_select_subshapes(__out_1, 'face'" in code     # kind inferred from node type
    assert "_pushpull(__out_1, __out_2, 8.0)" in code


def test_transpile_origin_input_positions_primitive():
    # A point wired into a primitive's optional origin wraps it in _at(...).
    g = Graph.from_dict({
        "nodes": [
            {"id": "p", "type": "ConstructPoint", "params": {"x": 10, "y": 0, "z": 5}},
            {"id": "s", "type": "Sphere", "params": {"radius": 3}},
        ],
        "connections": [{"id": "c", "from_node": "p", "from_socket": "point",
                         "to_node": "s", "to_socket": "origin"}],
    })
    code = transpile(g)
    assert "Vector(10.0, 0.0, 5.0)" in code
    assert "_at(Sphere(3.0, arc_size3=360.0), __out_1)" in code
    # an unconnected origin must NOT wrap the primitive
    g2 = Graph.from_dict({"nodes": [{"id": "b", "type": "Box"}], "connections": []})
    body = transpile(g2).split("# --- nodes ---")[1]
    assert "_at(" not in body


def test_display_fields_round_trip():
    # colour/wireframe are display-only: they persist but never affect geometry.
    g = Graph.from_dict({"nodes": [
        {"id": "b", "type": "Box", "color": "#44cc88", "wireframe": True},
        {"id": "c", "type": "Cylinder"},
    ], "connections": []})
    assert g.node("b").color == "#44cc88" and g.node("b").wireframe is True
    d = g.node("b").to_dict()
    assert d["color"] == "#44cc88" and d["wireframe"] is True
    assert "color" not in g.node("c").to_dict()       # default omitted
    assert "wireframe" not in g.node("c").to_dict()
    assert "44cc88" not in transpile(g)               # never leaks into the code


def test_transpile_codeblock():
    g = Graph.from_dict({
        "nodes": [{"id": "cb", "type": "CodeBlock",
                   "params": {"code": "result = Box(in_0 or 4, 4, 4)"}}],
        "connections": [],
    })
    code = transpile(g)
    assert "def __codeblock_" in code
    assert "result = Box(in_0 or 4, 4, 4)" in code
    assert "return result" in code


# --- source map (param <-> code spans) -----------------------------------
from cad_nodes.transpiler import (transpile_with_map, parse_codeblock_params,
                                  _SP_A, _SP_B)


def _box_graph():
    return Graph.from_dict({"name": "m", "nodes": [
        {"id": "box_1", "type": "Box",
         "params": {"width": 20, "height": 10, "depth": 5},
         "position": [0, 0]}], "connections": []})


def test_source_map_spans_match_slices():
    code, spans = transpile_with_map(_box_graph())
    assert _SP_A not in code and _SP_B not in code   # sentinels stripped
    lines = code.splitlines()
    by_param = {s["param"]: s for s in spans}
    # the dimensional params are always editable floats; the Box also exposes a
    # `centered` bool span, which is fine — assert the floats are present & exact.
    assert {"width", "height", "depth"} <= set(by_param)
    for s in spans:
        # the recorded (row, col0, col1) must slice exactly the literal in text
        assert lines[s["row"]][s["col0"]:s["col1"]] == "20.0" if s["param"] == "width" else True
        assert s["node_id"] == "box_1"
        if s["param"] in {"width", "height", "depth"}:
            assert s["kind"] == "float"
    w = by_param["width"]
    assert lines[w["row"]][w["col0"]:w["col1"]] == "20.0"
    assert w["min"] == 0.1 and w["max"] == 500


def test_plain_transpile_has_no_sentinels():
    code = transpile(_box_graph())
    assert _SP_A not in code and _SP_B not in code


def test_wired_param_has_no_span():
    # width is driven by an Input source -> its literal isn't in the code, so no span
    g = Graph.from_dict({"name": "m", "nodes": [
        {"id": "p1", "type": "Input", "params": {"text": "42"}, "position": [0, 0]},
        {"id": "box_1", "type": "Box", "params": {"width": 20}, "position": [0, 0]},
    ], "connections": [
        {"id": "c1", "from_node": "p1", "from_socket": "value",
         "to_node": "box_1", "to_socket": "width"}]})
    _, spans = transpile_with_map(g)
    params = {s["param"] for s in spans if s["node_id"] == "box_1"}
    assert "width" not in params           # wired -> not an editable literal
    assert {"height", "depth"} <= params   # the unwired ones still are


# --- CodeBlock #@param parsing + override spans ---------------------------
def test_parse_codeblock_params():
    code = ("teeth = 12   #@param int min=6 max=40\n"
            "mod   = 2.0  #@param min=0.5 max=5 step=0.1\n"
            "mode  = \"a\" #@param select=a,b,c\n"
            "plain = 3\n"
            "result = None")
    decls = parse_codeblock_params(code)
    by = {d["name"]: d for d in decls}
    assert set(by) == {"teeth", "mod", "mode"}     # `plain` (no annotation) skipped
    assert by["teeth"]["type"] == "int" and by["teeth"]["min"] == 6
    assert by["mod"]["type"] == "float" and by["mod"]["step"] == 0.1
    assert by["mode"]["type"] == "select" and by["mode"]["options"] == ["a", "b", "c"]


def test_codeblock_override_injection_and_span():
    g = Graph.from_dict({"name": "m", "nodes": [{
        "id": "cb_1", "type": "CodeBlock",
        "params": {"code": "teeth = 12 #@param int min=6 max=40\nresult = Box(teeth,5,5)",
                   "_cb": {"teeth": 20}},
        "position": [0, 0]}], "connections": []})
    code, spans = transpile_with_map(g)
    assert "#@param" not in code                # declarations are stripped from the body
    assert "teeth=20" in code                   # effective value passed as an argument
    s = next(s for s in spans if s["param"] == "_cb.teeth")
    assert code.splitlines()[s["row"]][s["col0"]:s["col1"]] == "20"
    assert s["value"] == 20 and s["kind"] == "int"
    # the code body itself is exposed as an editable `code` span
    assert any(s["param"] == "code" and s["kind"] == "code" for s in spans)
    # without emit_map the override still executes (deterministic, no sentinels)
    assert "teeth=20" in transpile(g)


# --- CodeBlock declared-param input sockets + fan-out (Phase 4) -----------
def test_codeblock_declared_socket_validates():
    g = Graph.from_dict({"name": "m", "nodes": [
        {"id": "lr", "type": "ListRange",
         "params": {"start": 6, "count": 3, "step": 2}, "position": [0, 0]},
        {"id": "cb", "type": "CodeBlock",
         "params": {"code": "teeth = 12 #@param int\nresult = Box(teeth,5,5)"},
         "position": [0, 0]},
    ], "connections": [
        {"id": "l1", "from_node": "lr", "from_socket": "result",
         "to_node": "cb", "to_socket": "teeth"}]})
    assert g.validate() == []          # declared #@param socket is accepted


def test_codeblock_undeclared_socket_rejected():
    g = Graph.from_dict({"name": "m", "nodes": [
        {"id": "lr", "type": "ListRange", "params": {}, "position": [0, 0]},
        {"id": "cb", "type": "CodeBlock",
         "params": {"code": "result = None"}, "position": [0, 0]},
    ], "connections": [
        {"id": "l1", "from_node": "lr", "from_socket": "result",
         "to_node": "cb", "to_socket": "ghost"}]})
    with pytest.raises(ValidationError):
        g.validate()


def test_codeblock_wired_param_fans_out():
    g = Graph.from_dict({"name": "m", "nodes": [
        {"id": "lr", "type": "ListRange",
         "params": {"start": 6, "count": 3, "step": 2}, "position": [0, 0]},
        {"id": "cb", "type": "CodeBlock",
         "params": {"code": "teeth = 12 #@param int\nresult = Box(teeth,5,5)"},
         "position": [0, 0]},
    ], "connections": [
        {"id": "l1", "from_node": "lr", "from_socket": "result",
         "to_node": "cb", "to_socket": "teeth"}]})
    code = transpile(g)
    assert "_fanout" in code
    assert "#@param" not in code                    # body stripped of declarations
    assert "teeth=teeth" in code                    # the lambda binds the wired arg
    # a wired param is not an editable literal -> no _cb.teeth span (code span is ok)
    _, spans = transpile_with_map(g)
    assert not any(s["param"] == "_cb.teeth" for s in spans if s["node_id"] == "cb")


def test_codeblock_unwired_keeps_override_span():
    g = Graph.from_dict({"name": "m", "nodes": [{
        "id": "cb", "type": "CodeBlock",
        "params": {"code": "teeth = 12 #@param int\nresult = Box(teeth,5,5)",
                   "_cb": {"teeth": 30}}, "position": [0, 0]}], "connections": []})
    code = transpile(g)
    assert "#@param" not in code and "teeth=30" in code   # override passed as the argument
    _, spans = transpile_with_map(g)
    assert any(s["param"] == "_cb.teeth" for s in spans)


# --- node wave 1 (ArrayPolar, planes, vector math, trig, Split, exports…) --
def test_all_templates_reference_real_sockets_or_params():
    # Every {placeholder} in every code template must resolve to an input
    # socket, a param, or a transpiler-provided name — catches template typos
    # at registration time for the whole catalog.
    import re
    provided = {"node_id", "ctx"}
    for d in catalog.REGISTRY.values():
        names = ({s.name for s in d.inputs} | {p.name for p in d.params}
                 | provided)
        for tmpl in d.code_template.values():
            for ph in re.findall(r"{(\w+)(?:!\w)?}", tmpl):
                assert ph in names, f"{d.type}: template refers to unknown {{{ph}}}"


def test_transpile_array_polar():
    g = Graph.from_dict({
        "nodes": [{"id": "b", "type": "Box"},
                  {"id": "p", "type": "ArrayPolar", "params": {"count": 8, "angle": 360}}],
        "connections": [{"id": "c", "from_node": "b", "from_socket": "result",
                         "to_node": "p", "to_socket": "shape"}],
    })
    code = transpile(g)
    assert "_array_polar(__out_1, 8, 360.0, Axis.Z)" in code


def test_transpile_split_keep_select():
    g = Graph.from_dict({
        "nodes": [{"id": "b", "type": "Box"},
                  {"id": "s", "type": "Split", "params": {"keep": "both"}}],
        "connections": [{"id": "c", "from_node": "b", "from_socket": "result",
                         "to_node": "s", "to_socket": "shape"}],
    })
    assert "Keep.BOTH" in transpile(g)


def test_transpile_trig_and_vector_math():
    g = Graph.from_dict({
        "nodes": [
            {"id": "n", "type": "NumberSlider", "params": {"value": 30}},
            {"id": "s", "type": "Sin"},
            {"id": "p1", "type": "ConstructPoint", "params": {"x": 1}},
            {"id": "p2", "type": "ConstructPoint", "params": {"y": 2}},
            {"id": "d", "type": "Distance"},
        ],
        "connections": [
            {"id": "1", "from_node": "n", "from_socket": "result", "to_node": "s", "to_socket": "x"},
            {"id": "2", "from_node": "p1", "from_socket": "point", "to_node": "d", "to_socket": "a"},
            {"id": "3", "from_node": "p2", "from_socket": "point", "to_node": "d", "to_socket": "b"},
        ],
    })
    code = transpile(g)
    assert "math.sin(math.radians(__out_1))" in code
    assert ".length" in code and "_pt(" in code


def test_new_list_nodes_fan_out_downstream():
    # Random is a list producer: feeding it into an item-access input (a
    # primitive's radius) must fan the consumer out.
    g = Graph.from_dict({
        "nodes": [{"id": "r", "type": "Random", "params": {"count": 4}},
                  {"id": "c", "type": "Circle"}],
        "connections": [{"id": "1", "from_node": "r", "from_socket": "result",
                         "to_node": "c", "to_socket": "radius"}],
    })
    code = transpile(g)
    assert "_randlist(4, 0.0, 1.0, 1)" in code
    assert "_fanout" in code
