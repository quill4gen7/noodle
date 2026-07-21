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
    assert "export_step(__out_5, _out('flange.step'))" in code  # exports/ sandbox
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
    # operates on the selection var, against the part EXPLICITLY: build123d would
    # otherwise infer the target from the picks' stale topo_parent (see _reanchor)
    assert "_round(__out_1, __out_2, 'fillet', 1.5)" in code


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


def test_transpile_trace_image_bakes_frozen_contours():
    # TraceImage has no code_template: the transpiler special-cases it, inlining
    # the contours + mm/pixel scale FROZEN into params["trace"] (like SelectEdge's
    # picked sigs) so the graph re-runs from fixed data with no image processing.
    g = Graph.from_dict({
        "nodes": [
            {"id": "t", "type": "TraceImage",
             "params": {"path": "assets/foo.png",
                        "trace": {"scale": 0.5, "imgH": 160,
                                  "contours": [
                                      {"pts": [[0, 0], [100, 0], [100, 80]],
                                       "closed": True, "hole": False}]}}},
        ],
        "connections": [],
    })
    code = transpile(g)
    assert "def _trace_curves(" in code                 # helper injected
    assert "_trace_curves([{'pts': [[0, 0], [100, 0], [100, 80]]" in code
    assert "0.5, 160)" in code                           # scale + image height passed through
    # the node registers as a curve producer with an empty template
    tdef = catalog.get("TraceImage")
    assert tdef.outputs[0].wire_type == "curve"
    assert tdef.code_template.get("algebra") == ""


def test_transpile_ref_image_is_editor_only():
    # RefImage is a viewport-only reference (like Note): it never appears in the
    # generated program, and a graph containing one still transpiles cleanly.
    g = Graph.from_dict({
        "nodes": [
            {"id": "b", "type": "Box"},
            {"id": "r", "type": "RefImage",
             "params": {"path": "assets/blueprint.png", "plane": "XZ", "width": 200}},
        ],
        "connections": [],
    })
    code = transpile(g)
    assert "blueprint.png" not in code       # the reference never leaks into the code
    assert "(RefImage)" not in code          # the node is never emitted (no @node annotation)
    assert "Box(" in code                     # the real geometry still transpiles
    rdef = catalog.get("RefImage")
    assert rdef.outputs == [] and rdef.inputs == []


def test_transpile_center_of_mass_two_outputs():
    # CenterOfMass is universal (casts from curves/faces/points) and special-cased
    # into TWO outputs — `center` (a vector) and `volume` (a number) must resolve to
    # DIFFERENT downstream vars via the per-socket out_var_of map.
    cdef = catalog.get("CenterOfMass")
    assert [o.name for o in cdef.outputs] == ["center", "volume"]
    assert cdef.code_template.get("algebra") == ""
    assert "curve" in (cdef.inputs[0].accepts or [])   # a circle/curve may feed it
    g = Graph.from_dict({"nodes": [
        {"id": "c", "type": "Circle", "params": {"radius": 5}},
        {"id": "m", "type": "CenterOfMass", "params": {}},
        {"id": "dc", "type": "Display", "params": {}},
        {"id": "dv", "type": "Display", "params": {}},
    ], "connections": [
        {"id": "l1", "from_node": "c", "from_socket": "result", "to_node": "m", "to_socket": "shape"},
        {"id": "l2", "from_node": "m", "from_socket": "center", "to_node": "dc", "to_socket": "value"},
        {"id": "l3", "from_node": "m", "from_socket": "volume", "to_node": "dv", "to_socket": "value"},
    ]})
    assert g.validate() == []                          # curve -> center-of-mass casts fine
    code = transpile(g)
    assert "_center_of(" in code and "_volume_of(" in code
    import re
    cvar = re.search(r"(\w+) = _center_of", code).group(1)
    assert f"_probe('dc', {cvar})" in code             # center consumer
    assert f"_probe('dv', {cvar}_vol)" in code         # volume consumer -> distinct var


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
    # Names the EMITTER supplies rather than the node declaring them:
    # container_ids is the graph-level answer to "which node drew the Drop's
    # container", which the runtime cannot know (it only ever sees a shape).
    provided = {"node_id", "ctx", "container_ids", "shape_ids"}
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


# --- Rotate: pivot + about (world / part / group) --------------------------
def _rot_graph(about=None, extra_nodes=(), extra_conns=()):
    params = {"angle": 45, "axis": "X"}
    if about is not None:
        params["about"] = about
    return Graph.from_dict({"name": "t", "nodes": [
        {"id": "b", "type": "Box", "params": {}},
        {"id": "r", "type": "Rotate", "params": params},
        *extra_nodes,
    ], "connections": [
        {"id": "c1", "from_node": "b", "from_socket": "result",
         "to_node": "r", "to_socket": "shape"},
        *extra_conns,
    ]})


def test_rotate_takes_an_optional_pivot_point():
    piv = next(s for s in catalog.get("Rotate").inputs if s.name == "pivot")
    assert piv.wire_type == "vector" and not piv.required


def test_rotate_about_defaults_to_world_so_old_graphs_are_untouched():
    # Backward compatibility is the contract: a graph saved before `about`
    # existed carries no such param, and must keep orbiting the global axis.
    p = catalog.get("Rotate").param("about")
    assert p.default == "world"
    assert p.options == ["world", "part", "group"]
    g = _rot_graph()                       # no `about` in params at all
    g.validate()
    code = transpile(g)
    call = next(l for l in code.splitlines() if "= _rotate(" in l)
    assert "'world'" in call and "__pivot_" not in call


def test_rotate_about_group_hoists_one_shared_centre_out_of_the_fanout():
    # Two shapes multi-wired into `shape` fan out; with about=group each item
    # must pivot about the ONE collective centre, so the emitter hoists
    # _pivot_of([...]) before the lambda — else each piece spins about itself.
    g = _rot_graph("group",
                   extra_nodes=[{"id": "b2", "type": "Box", "params": {}}],
                   extra_conns=[{"id": "c2", "from_node": "b2",
                                 "from_socket": "result",
                                 "to_node": "r", "to_socket": "shape"}])
    g.validate()
    code = transpile(g)
    assert any(l.strip().startswith("__pivot_") and "_pivot_of(" in l
               for l in code.splitlines())
    call = next(l for l in code.splitlines() if "_fanout(" in l and "_rotate(" in l)
    assert "__pivot_" in call               # the hoisted centre reaches every item


def test_rotate_about_part_does_not_hoist():
    # Per-part centres live in the helper: each fanned item measures its own.
    g = _rot_graph("part",
                   extra_nodes=[{"id": "b2", "type": "Box", "params": {}}],
                   extra_conns=[{"id": "c2", "from_node": "b2",
                                 "from_socket": "result",
                                 "to_node": "r", "to_socket": "shape"}])
    code = transpile(g)
    assert "__pivot_" not in code


def test_a_wired_pivot_wins_over_the_hoist():
    # An explicit pivot point is the user naming the centre — group mode must
    # not overwrite it.
    g = _rot_graph("group",
                   extra_nodes=[{"id": "b2", "type": "Box", "params": {}},
                                {"id": "p", "type": "ConstructPoint",
                                 "params": {"x": 5}}],
                   extra_conns=[{"id": "c2", "from_node": "b2",
                                 "from_socket": "result",
                                 "to_node": "r", "to_socket": "shape"},
                                {"id": "c3", "from_node": "p",
                                 "from_socket": "point",
                                 "to_node": "r", "to_socket": "pivot"}])
    g.validate()
    assert "__pivot_" not in transpile(g)


# --- fillet/chamfer target (feedback 20260718-164854) ----------------------
# build123d's algebra-mode fillet()/chamfer() take no target: they read
# `objects[0].topo_parent`, which after a boolean still names the PRE-boolean
# operand. Rounding a corner of a Union therefore returned that operand alone
# and silently dropped the rest (a 6402mm2 fused sketch came back as 3512).
# Every rounding node must hand the runtime the part EXPLICITLY.

def _round_graph(node_type, params, part_socket, sel_socket=None):
    nodes = [{"id": "a", "type": "Box", "params": {}},
             {"id": "b", "type": "Box", "params": {}},
             {"id": "u", "type": "Union", "params": {}},
             {"id": "r", "type": node_type, "params": params}]
    conns = [{"id": "1", "from_node": "a", "from_socket": "result",
              "to_node": "u", "to_socket": "shapes"},
             {"id": "2", "from_node": "b", "from_socket": "result",
              "to_node": "u", "to_socket": "shapes"},
             {"id": "3", "from_node": "u", "from_socket": "result",
              "to_node": "r", "to_socket": part_socket}]
    if sel_socket:
        nodes.append({"id": "s", "type": "SelectEdge",
                      "params": {"selection": {"kind": "edge", "indices": [0]}}})
        conns += [{"id": "4", "from_node": "u", "from_socket": "result",
                   "to_node": "s", "to_socket": "geometry"},
                  {"id": "5", "from_node": "s", "from_socket": "selection",
                   "to_node": "r", "to_socket": sel_socket}]
    g = Graph.from_dict({"nodes": nodes, "connections": conns})
    g.validate()
    return g


# --- the preview eye reaches every emit path ------------------------------
# The eye was wired to nothing on the five special-cased emit paths: they never
# called _previewed(), so switching it on for a selector, CenterOfMass,
# TraceImage, OrientForPrint or a bypassed node silently did nothing. Only
# _emit_simple/_emit_group/_emit_codeblock honoured it.

def _eye_graph(node_type, params=None, extra=None, conns=None, **flags):
    nodes = [{"id": "b", "type": "Box", "params": {}},
             dict({"id": "e", "type": node_type, "params": params or {}}, **flags)]
    nodes += extra or []
    g = Graph.from_dict({"nodes": nodes, "connections": conns or []})
    g.validate()
    return g


def test_selected_rounding_passes_the_part_not_just_the_picks():
    code = transpile(_round_graph("FilletChamferSelected",
                                  {"mode": "chamfer", "size": 15},
                                  "part", "selection"))
    call = next(l for l in code.splitlines() if "= _round(" in l)
    # the union var must be the FIRST argument — the target to chamfer
    assert "_round(__out_3, __out_4, 'chamfer', 15.0)" in call
    # and the bare build123d call, which infers a stale target, must be gone
    assert "chamfer(__out_4" not in code


def test_corner_rounding_fills_the_face_once():
    # _round_corners must call _face() a single time: two calls make two
    # distinct Faces, and picks off one never match the other.
    for node_type, params in (("FilletChamferCorners", {"mode": "fillet", "size": 2}),
                              ("Fillet2D", {"radius": 2}),
                              ("Chamfer2D", {"length": 2})):
        g = Graph.from_dict({
            "nodes": [{"id": "c", "type": "Circle", "params": {"radius": 5}},
                      {"id": "r", "type": node_type, "params": params}],
            "connections": [{"id": "1", "from_node": "c", "from_socket": "result",
                             "to_node": "r", "to_socket": "shape"}]})
        g.validate()
        # the node body only — the fixed PREAMBLE legitimately calls .vertices()
        body = transpile(g).split("# --- nodes ---", 1)[1]
        assert "_round_corners(" in body, node_type
        assert ".vertices()" not in body, node_type      # no double _face()


def test_round_all_still_takes_the_part_itself():
    code = transpile(_round_graph("FilletChamfer", {"mode": "fillet", "size": 1},
                                  "part"))
    assert "_round_all(__out_3, 'fillet', 1.0)" in code
def _previews(code):
    import re
    return set(re.findall(r"__previews__\['(\w+)'\]", code))


def test_eye_on_a_selector_draws_the_picked_set():
    # A selector's output wire is `selection`, which says how it may be WIRED,
    # not what it holds — at run time it is drawable sub-shapes.
    for t in ("SelectEdge", "SelectFace", "SelectVertex", "SelectShape"):
        sock = "shapes" if t == "SelectShape" else "geometry"
        g = _eye_graph(t, {"selection": {"kind": "edge", "indices": [0]}},
                       conns=[{"id": "1", "from_node": "b", "from_socket": "result",
                               "to_node": "e", "to_socket": sock}],
                       preview=True)
        assert "e" in _previews(transpile(g)), t


def test_a_selector_never_draws_itself_unasked():
    # Opt-in only: a real graph is full of wired selectors and auto-drawing them
    # all would bury the part under its own picks.
    for eye in (None, False):
        g = _eye_graph("SelectEdge", {"selection": {"kind": "edge", "indices": [0]}},
                       conns=[{"id": "1", "from_node": "b", "from_socket": "result",
                               "to_node": "e", "to_socket": "geometry"}],
                       **({"preview": eye} if eye is not None else {}))
        assert "e" not in _previews(transpile(g)), eye


def test_eye_on_the_special_emit_paths():
    # Each of these has a previewable output and its own emit path that used to
    # skip the hook entirely.
    for t, params in (("CenterOfMass", {}), ("TraceImage", {}),
                      ("OrientForPrint", {})):
        sock = {"CenterOfMass": "shape", "OrientForPrint": "mesh"}.get(t)
        conns = ([{"id": "1", "from_node": "b", "from_socket": "result",
                   "to_node": "e", "to_socket": sock}] if sock else [])
        g = _eye_graph(t, params, conns=conns, preview=True)
        assert "e" in _previews(transpile(g)), t


def test_eye_off_still_wins_on_the_special_paths():
    g = _eye_graph("CenterOfMass", conns=[{"id": "1", "from_node": "b",
                                           "from_socket": "result",
                                           "to_node": "e", "to_socket": "shape"}],
                   preview=False)
    assert "e" not in _previews(transpile(g))


def test_bypassing_a_node_does_not_un_draw_it():
    # A bypassed node still carries a value (its upstream's), so it still
    # answers to the eye.
    g = _eye_graph("Move", conns=[{"id": "1", "from_node": "b",
                                   "from_socket": "result",
                                   "to_node": "e", "to_socket": "shape"}],
                   preview=True, bypassed=True)
    code = transpile(g)
    assert "# bypassed" in code and "e" in _previews(code)
