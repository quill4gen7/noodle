"""
Print physics (PLAN_PRINT_PHYSICS.md) — wire types, params and transpiler emission.

Pure-Python: no build123d, no trimesh. These assert the *shape* of the lane (what may
connect to what, what code comes out), not the geometry — the measurements themselves
run in the worker and are exercised end-to-end by examples/print-orientation.json.
"""

import math

import pytest

from cad_nodes import catalog
from cad_nodes.casts import WIRE_DATA, WIRE_MESH, WIRE_PLANE, WIRE_SOLID, WIRE_VECTOR
from cad_nodes.graph import Graph, ValidationError
from cad_nodes.transpiler import transpile


def _g(nodes, connections):
    return Graph.from_dict({"name": "t", "nodes": nodes, "connections": connections})


def _calls(code: str, helper: str) -> bool:
    # the PREAMBLE *defines* these helpers, so a bare substring match would pass for the
    # wrong reason. A call site is an assignment; a definition is a def.
    return any(f"= {helper}(" in ln for ln in code.splitlines())


PRINT_NODES = ["PlaceOnBed", "Drop", "PrintCheck", "OverhangFaces", "OrientForPrint"]


@pytest.mark.parametrize("ntype", PRINT_NODES)
def test_the_print_nodes_are_registered(ntype):
    assert catalog.get(ntype).category == "print"


def test_place_on_bed_serves_both_lanes():
    # It measures on the mesh and moves the ORIGINAL, so a solid comes out a solid — the
    # same trick that lets one Move node serve both lanes.
    sock = catalog.get("PlaceOnBed").inputs[0]
    assert sock.wire_type == WIRE_SOLID
    assert WIRE_MESH in (sock.accepts or [])
    assert catalog.get("PlaceOnBed").output_follows == "shape"


def test_a_solid_dropped_on_the_bed_stays_a_solid():
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "d", "type": "PlaceOnBed", "params": {}},
         {"id": "f", "type": "Fillet", "params": {}}],   # a B-Rep-only node downstream
        [{"id": "c1", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c2", "from_node": "d", "from_socket": "result",
          "to_node": "f", "to_socket": "part"}],
    )
    g.validate()                                   # output_follows carries `solid` through
    assert _calls(transpile(g), "_bed_drop")


def test_print_check_reports_on_the_data_bus():
    out = catalog.get("PrintCheck").outputs[0]
    assert out.wire_type == WIRE_DATA              # it is text: it goes to a Panel
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "p", "type": "PrintCheck", "params": {}},
         {"id": "d", "type": "Display", "params": {}}],
        [{"id": "c1", "from_node": "b", "from_socket": "result",
          "to_node": "p", "to_socket": "mesh"},    # a solid tessellates at the boundary
         {"id": "c2", "from_node": "p", "from_socket": "report",
          "to_node": "d", "to_socket": "value"}],
    )
    g.validate()
    code = transpile(g)
    # the solid tessellates AT THE BOUNDARY (the solid->mesh cast), so the report is
    # measured on triangles, not on the B-Rep: PrintCheck never sees a solid
    assert "_print_check(_to_mesh(__out_" in code


def test_overhang_faces_stays_on_the_mesh_lane():
    assert catalog.get("OverhangFaces").outputs[0].wire_type == WIRE_MESH


def test_orient_for_print_takes_a_load_vector_and_it_is_optional():
    load = next(s for s in catalog.get("OrientForPrint").inputs if s.name == "load")
    assert load.wire_type == WIRE_VECTOR and not load.required


def test_orient_for_print_has_two_outputs_from_one_search():
    # The mesh and the table that says why it won. Both must come from ONE _orient_plan
    # call: scoring the poses means slicing each of them, and doing that twice because a
    # Panel happens to be wired in would be daft.
    outs = {s.name: s.wire_type for s in catalog.get("OrientForPrint").outputs}
    assert outs == {"result": WIRE_MESH, "report": WIRE_DATA}
    g = _g(
        [{"id": "m", "type": "ImportMesh", "params": {"path": "p.stl"}},
         {"id": "o", "type": "OrientForPrint", "params": {}},
         {"id": "e", "type": "ExportMesh", "params": {"path": "out.stl"}},
         {"id": "d", "type": "Display", "params": {}}],
        [{"id": "c1", "from_node": "m", "from_socket": "result",
          "to_node": "o", "to_socket": "mesh"},
         {"id": "c2", "from_node": "o", "from_socket": "result",
          "to_node": "e", "to_socket": "mesh"},
         {"id": "c3", "from_node": "o", "from_socket": "report",
          "to_node": "d", "to_socket": "value"}],
    )
    g.validate()
    code = transpile(g)
    assert code.count("_orient_plan(__out_") == 1
    assert "['mesh']" in code and "['report']" in code


def test_the_two_outputs_do_not_collapse_onto_one_variable():
    # The bug this guards: without out_var_of, a node's outputs share one var, and the
    # exported STL would silently be the report string.
    g = _g(
        [{"id": "m", "type": "ImportMesh", "params": {"path": "p.stl"}},
         {"id": "o", "type": "OrientForPrint", "params": {}},
         {"id": "e", "type": "ExportMesh", "params": {"path": "out.stl"}},
         {"id": "d", "type": "Display", "params": {}}],
        [{"id": "c1", "from_node": "m", "from_socket": "result",
          "to_node": "o", "to_socket": "mesh"},
         {"id": "c2", "from_node": "o", "from_socket": "result",
          "to_node": "e", "to_socket": "mesh"},
         {"id": "c3", "from_node": "o", "from_socket": "report",
          "to_node": "d", "to_socket": "value"}],
    )
    code = transpile(g)
    export = next(l for l in code.splitlines() if "_mesh_export(" in l and "__out_" in l)
    probe = next(l for l in code.splitlines() if "_probe(" in l and "__out_" in l)
    assert "_rep" not in export       # the STL gets the mesh…
    assert "_rep" in probe            # …and the Panel gets the report


def test_a_mesh_may_not_be_wired_into_the_load():
    # `load` is a direction, not a body. The data bus feeds a vector, a mesh does not.
    g = _g(
        [{"id": "m", "type": "ImportMesh", "params": {"path": "p.stl"}},
         {"id": "o", "type": "OrientForPrint", "params": {}}],
        [{"id": "c", "from_node": "m", "from_socket": "result",
          "to_node": "o", "to_socket": "load"}],
    )
    with pytest.raises(ValidationError):
        g.validate()


def test_support_volume_is_a_body_on_the_mesh_lane():
    # Not a number, a BODY: you can preview it, inspect it, export it. That is the point —
    # `area x height` gestures at the cost; a boolean IS the cost.
    assert catalog.get("SupportVolume").outputs[0].wire_type == WIRE_MESH
    g = _g(
        [{"id": "m", "type": "ImportMesh", "params": {"path": "p.stl"}},
         {"id": "s", "type": "SupportVolume", "params": {}},
         {"id": "i", "type": "MeshInspect", "params": {}}],
        [{"id": "c1", "from_node": "m", "from_socket": "result",
          "to_node": "s", "to_socket": "mesh"},
         {"id": "c2", "from_node": "s", "from_socket": "result",
          "to_node": "i", "to_socket": "mesh"}],
    )
    g.validate()
    assert _calls(transpile(g), "_support_body")


def test_the_search_declares_which_support_number_it_used():
    # All-or-nothing: ranking one pose by real volume and the next by a proxy would compare
    # two different quantities and call it a decision. `exact_below` is the switch, and it
    # reaches _orient_plan as an argument (not a silent default).
    p = {p.name: p for p in catalog.get("OrientForPrint").params}
    assert "exact_below" in p
    g = _g(
        [{"id": "m", "type": "ImportMesh", "params": {"path": "p.stl"}},
         {"id": "o", "type": "OrientForPrint", "params": {"exact_below": 1234}}],
        [{"id": "c", "from_node": "m", "from_socket": "result",
          "to_node": "o", "to_socket": "mesh"}],
    )
    code = transpile(g)
    call = next(l for l in code.splitlines() if "_orient_plan(__out_" in l)
    assert "1234" in call


# --- Drop: Place on Bed as a fall you can scrub ------------------------------

def test_drop_serves_both_lanes_like_place_on_bed():
    # Same trick as PlaceOnBed: measure on the mesh, move the original.
    sock = catalog.get("Drop").inputs[0]
    assert sock.wire_type == WIRE_SOLID
    assert WIRE_MESH in (sock.accepts or [])
    assert catalog.get("Drop").output_follows == "shape"


def test_drop_takes_an_optional_plane_to_fall_toward():
    plane = next(s for s in catalog.get("Drop").inputs if s.name == "plane")
    assert plane.wire_type == WIRE_PLANE and not plane.required


def test_drop_timeline_is_a_normalised_scrub():
    # 0 = where the part is, 1 = at rest. Always the FULL settle: the material
    # changes the shape of the journey, never the reach of the slider.
    t = catalog.get("Drop").param("t")
    assert (t.min, t.max) == (0.0, 1.0)


def test_drop_declares_the_timeline_gizmo():
    # Edit-on-canvas: drag the part along Z to scrub t (pull down = drop, lift
    # = rewind). A wired `t` locks the gizmo — the upstream slider owns time.
    g = catalog.get("Drop").gizmo
    assert g == {"kind": "timeline", "binds": ["t"], "anchor": "preview",
                 "lock": ["t"]}


def test_drop_materials_are_fixed_and_contrasting():
    # Plastic is the default; lead is the promised counterpoint (one dead thud).
    mat = catalog.get("Drop").param("material")
    assert mat.default == "plastic"
    assert "lead" in mat.options and "rubber" in mat.options


def test_drop_settles_by_default_and_the_toggle_reaches_the_call():
    # The topple cascade is the point of the node — on by default; `settle`
    # off must fall back to the translation-only fall (balanced on its edge).
    p = catalog.get("Drop").param("settle")
    assert p.type == "bool" and p.default is True
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {"settle": False}}],
        [{"id": "c", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    call = next(l for l in transpile(g).splitlines() if "= _drop(" in l)
    assert "False" in call


def test_drop_transpiles_with_time_and_material_at_the_call_site():
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {"t": 0.35, "material": "lead"}}],
        [{"id": "c", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    g.validate()
    code = transpile(g)
    assert _calls(code, "_drop")
    call = next(l for l in code.splitlines() if "= _drop(" in l)
    assert "0.35" in call and "'lead'" in call


def test_a_solid_dropped_stays_a_solid():
    # output_follows carries `solid` through, so a B-Rep-only node may follow.
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {}},
         {"id": "f", "type": "Fillet", "params": {}}],
        [{"id": "c1", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c2", "from_node": "d", "from_socket": "result",
          "to_node": "f", "to_socket": "part"}],
    )
    g.validate()


def test_drop_collide_exists_and_is_off_by_default():
    # Collisions cost real compute (rays + sampled rotations): opt-in only.
    p = catalog.get("Drop").param("collide")
    assert p.type == "bool" and p.default is False


def test_collide_unfans_the_shapes_into_one_scene():
    # Two shapes + collide: the transpiler must NOT fan out — the runtime needs
    # the whole list to stack the parts against each other — and the output is
    # a list again so downstream still fans.
    g = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "b", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {"collide": True}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c2", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    g.validate()
    code = transpile(g)
    call = next(l for l in code.splitlines() if "= _drop(" in l)
    assert "_fanout" not in call and "[__out_" in call


def test_without_collide_several_shapes_still_fan_out():
    g = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "b", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c2", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    code = transpile(g)
    call = next(l for l in code.splitlines() if "_drop(" in l and "__out_" in l)
    assert "_fanout" in call


def test_container_unfans_the_shapes_even_without_the_toggle():
    # Parts falling INTO one bowl are one scene by definition — the container
    # implies collide, or there would be nothing for the parts to land in.
    g = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "b", "type": "Box", "params": {}},
         {"id": "bowl", "type": "Sphere", "params": {}},
         {"id": "d", "type": "Drop", "params": {}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c2", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c3", "from_node": "bowl", "from_socket": "result",
          "to_node": "d", "to_socket": "container"}],
    )
    g.validate()
    code = transpile(g)
    call = next(l for l in code.splitlines() if "= _drop(" in l)
    assert "_fanout" not in call and "[__out_" in call


def test_container_is_optional_and_absent_by_default():
    # Nothing wired: the argument must still be passed, as None — the runtime
    # signature takes it positionally.
    g = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    g.validate()
    sock = catalog.get("Drop").input("container")
    assert sock is not None and sock.required is False
    call = next(l for l in transpile(g).splitlines() if "= _drop(" in l)
    args = [a.strip() for a in
            call.split("_drop(", 1)[1].rsplit(")", 1)[0].split(",")]
    # Positional, but pinned by the template rather than by a magic index: adding
    # an argument (motion did exactly this) must not silently move the assertion
    # onto a neighbouring slot.
    slots = catalog.get("Drop").code_template["algebra"]
    slots = slots.split("_drop(", 1)[1].rsplit(")", 1)[0].split(",")
    assert args[[s.strip() for s in slots].index("{container}")] == "None"


def test_grip_reaches_the_runtime_as_an_argument():
    # The scene's friction is a knob, not a constant: at grip 1 a sloped static
    # face grabs a falling part and flings it sideways, which is what turned the
    # Galton board's bell into two lumps against the walls. It must be emitted.
    p = {p.name: p for p in catalog.get("Drop").params}
    assert "grip" in p and p["grip"].default == 1.0
    g = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {"grip": 0.15}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    g.validate()
    call = next(l for l in transpile(g).splitlines() if "= _drop(" in l)
    assert "0.15" in call


# ---------------------------------------------------------------------------
# ContainerMotion — the container stops being furniture (PLAN_PRINT_PHYSICS.md).
# The wiring is asserted here; the FRAME MATH is asserted for real by exec'ing
# the PREAMBLE helpers below, because "world xyz -> bed frame" and the pivot
# correction are exactly the two things that look right and silently are not.
# ---------------------------------------------------------------------------

def _drop_args(call: str) -> list[str]:
    # The emitted line carries a trailing `# @node:… (Drop)` comment, and that
    # comment contains a closing paren — so the call has to be cut at the FIRST
    # one after the arguments, not the last one on the line.
    inner = call.split("_drop(", 1)[1]
    return [a.strip() for a in inner[:inner.index(")")].split(",")]


def _preamble_fns(*names):
    """Lift pure-math helpers out of the PREAMBLE and exec them. They need only
    math + numpy, so they are testable without build123d like everything else
    here — and they are worth testing: a rotation carried into the wrong frame
    still produces a plausible-looking animation."""
    import math as _m

    from cad_nodes.transpiler import PREAMBLE
    src = "\n".join(f"def {n}(" + PREAMBLE.split(f"\ndef {n}(")[1].split("\ndef ")[0]
                    for n in names)
    ns = {"math": _m}
    exec(src, ns)
    return ns


def test_container_motion_is_registered_and_outputs_a_plan():
    d = catalog.get("ContainerMotion")
    assert d.category == "print"
    assert d.outputs[0].wire_type == WIRE_DATA
    # It is driven in world xyz — both a wired vector and the widgets.
    assert d.input("offset").wire_type == WIRE_VECTOR
    assert d.input("pivot").wire_type == WIRE_VECTOR
    assert {p.name for p in d.params} >= {"x", "y", "z", "rx", "ry", "rz",
                                          "cycles", "duration", "delay"}


def test_motion_socket_is_optional_and_reaches_the_runtime():
    # Absent by default: a Drop with no motion must emit None in that slot, or
    # every existing graph starts driving a container that should hold still.
    slots = catalog.get("Drop").code_template["algebra"]
    slots = [s.strip() for s in
             slots.split("_drop(", 1)[1].rsplit(")", 1)[0].split(",")]
    assert "{motion}" in slots
    sock = catalog.get("Drop").input("motion")
    assert sock is not None and sock.required is False and sock.wire_type == WIRE_DATA

    g = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "d", "type": "Drop", "params": {}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"}],
    )
    g.validate()
    call = next(l for l in transpile(g).splitlines() if "= _drop(" in l)
    args = _drop_args(call)
    assert args[slots.index("{motion}")] == "None"

    # Wired, the plan node is emitted and threaded in.
    g2 = _g(
        [{"id": "a", "type": "Box", "params": {}},
         {"id": "b", "type": "Box", "params": {}},
         {"id": "m", "type": "ContainerMotion", "params": {"ry": 90}},
         {"id": "d", "type": "Drop", "params": {}}],
        [{"id": "c1", "from_node": "a", "from_socket": "result",
          "to_node": "d", "to_socket": "shape"},
         {"id": "c2", "from_node": "b", "from_socket": "result",
          "to_node": "d", "to_socket": "container"},
         {"id": "c3", "from_node": "m", "from_socket": "motion",
          "to_node": "d", "to_socket": "motion"}],
    )
    g2.validate()
    code = transpile(g2)
    assert _calls(code, "_container_motion")
    plan_var = next(ln.split(" = ")[0].strip() for ln in code.splitlines()
                    if "= _container_motion(" in ln)
    call = next(ln for ln in code.splitlines() if "= _drop(" in ln)
    assert _drop_args(call)[slots.index("{motion}")] == plan_var


def test_motion_ramp_and_oscillation_are_the_two_shapes():
    ns = _preamble_fns("_mat_quat", "_container_motion", "_motion_driver")
    import numpy as np
    I = np.eye(3)

    # cycles = 0 is a RAMP: nothing before the delay, full displacement at the
    # end, and it STAYS there (a tilted tray does not un-tilt).
    plan = ns["_container_motion"](None, 10.0, 0.0, 0.0, 0, 0, 0, None,
                                   0.0, 2.0, 1.0, "linear")
    pose, end = ns["_motion_driver"](plan, I, np.zeros(3), np.zeros(3))
    assert end == 3.0
    assert np.allclose(pose(0.0)[0], [0, 0, 0])
    assert np.allclose(pose(1.0)[0], [0, 0, 0])          # still waiting
    assert np.allclose(pose(2.0)[0], [5, 0, 0])          # halfway through
    assert np.allclose(pose(3.0)[0], [10, 0, 0])
    assert np.allclose(pose(9.0)[0], [10, 0, 0])         # and stays

    # cycles > 0 OSCILLATES about the start and must come back to it, or the rig
    # would be parked mid-swing forever after the shaking stops.
    plan = ns["_container_motion"](None, 10.0, 0.0, 0.0, 0, 0, 0, None,
                                   2.0, 2.0, 0.0, "linear")
    pose, end = ns["_motion_driver"](plan, I, np.zeros(3), np.zeros(3))
    assert np.allclose(pose(0.0)[0], [0, 0, 0])
    assert np.allclose(pose(0.25)[0], [10, 0, 0])        # first quarter cycle
    assert np.allclose(pose(0.75)[0], [-10, 0, 0], atol=1e-9)
    assert np.allclose(pose(2.0)[0], [0, 0, 0], atol=1e-9)
    assert np.allclose(pose(5.0)[0], [0, 0, 0])          # parked where it began


def test_motion_rotates_about_the_pivot_not_the_bed_origin():
    # bullet poses a body as x -> R x + pos, so turning a bowl about its own
    # centre is ENTIRELY the job of that pos term. If it is wrong the container
    # swings through the scene on an invisible arm instead of turning in place.
    ns = _preamble_fns("_mat_quat", "_container_motion", "_motion_driver")
    import numpy as np
    piv = np.array([30.0, 0.0, 5.0])
    plan = ns["_container_motion"](None, 0, 0, 0, 0, 0, 90.0, None,
                                   0.0, 1.0, 0.0, "linear")
    pose, _ = ns["_motion_driver"](plan, np.eye(3), np.zeros(3), piv)
    pos, q = pose(1.0)
    R = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)   # +90 about z
    assert np.allclose(R @ piv + pos, piv, atol=1e-9)               # pivot fixed
    # a point 20mm out along +y from the pivot swings round to 20mm along -x
    far = piv + np.array([0.0, 20.0, 0.0])
    assert np.allclose(R @ far + pos, piv + np.array([-20.0, 0.0, 0.0]), atol=1e-9)


def test_motion_is_dictated_in_world_and_carried_into_the_bed_frame():
    # The user tilts a tray in WORLD xyz; the colliders live in bed coordinates.
    # On a bed rotated 90 deg about x, a world +z shove must come out as bed +y.
    ns = _preamble_fns("_mat_quat", "_container_motion", "_motion_driver")
    import numpy as np
    B = np.array([[1.0, 0, 0], [0, 0, -1.0], [0, 1.0, 0]])   # bed z = world -y
    plan = ns["_container_motion"](None, 0.0, 0.0, 10.0, 0, 0, 0, None,
                                   0.0, 1.0, 0.0, "linear")
    pose, _ = ns["_motion_driver"](plan, B, np.zeros(3), np.zeros(3))
    assert np.allclose(pose(1.0)[0], B.T @ np.array([0, 0, 10.0]))

    # A world rotation must likewise arrive as R_bed = B^T R_world B.
    plan = ns["_container_motion"](None, 0, 0, 0, 0, 0, 40.0, None,
                                   0.0, 1.0, 0.0, "linear")
    pose, _ = ns["_motion_driver"](plan, B, np.zeros(3), np.zeros(3))
    _, q = pose(1.0)
    c, s = math.cos(math.radians(40)), math.sin(math.radians(40))
    Rw = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])
    qm = ns["_mat_quat"](B.T @ Rw @ B)
    assert np.allclose(q, qm, atol=1e-12)


def test_angular_velocity_reproduces_the_prescribed_turn():
    # resetBaseVelocity is what actually carries the contents (measured: without
    # it a translated tray drags a resting part 1.2% of the way, i.e. not at
    # all). The velocity handed to the solver must match the motion it is paired
    # with, or the friction is a lie in a subtler way.
    ns = _preamble_fns("_mat_quat", "_ang_vel")
    import numpy as np
    dt = 1.0 / 240.0
    for deg in (1.0, 15.0, -30.0):
        a = math.radians(deg)
        c, s = math.cos(a), math.sin(a)
        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])
        w = ns["_ang_vel"](ns["_mat_quat"](np.eye(3)), ns["_mat_quat"](R), dt)
        assert np.allclose(w[:2], [0, 0], atol=1e-9)          # about z only
        assert w[2] == pytest.approx(a / dt, rel=2e-3)        # right rate, right sign


# --- a finish per body: the glass jar that pours steel bolts (§5d-bis) -----
def test_drop_emits_the_container_source_node_id():
    """Only the EMITTER can know which node drew the container — the runtime is
    handed a shape, never a graph. Without this the viewer cannot tell the bowl
    from what falls into it, and the whole scene shares one finish."""
    g = Graph.from_dict({
        "nodes": [{"id": "ball", "type": "Sphere"},
                  {"id": "bowl", "type": "Cylinder"},
                  {"id": "d", "type": "Drop"}],
        "connections": [
            {"id": "c1", "from_node": "ball", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"},
            {"id": "c2", "from_node": "bowl", "from_socket": "result",
             "to_node": "d", "to_socket": "container"}],
    })
    code = transpile(g)
    drop = next(l for l in code.splitlines() if "_drop(" in l and "@node:d" in l)
    assert "['bowl']" in drop, drop


def test_a_drop_with_no_container_still_emits_the_argument():
    """The placeholder is unconditional; an empty list must not become a hole."""
    g = Graph.from_dict({
        "nodes": [{"id": "b", "type": "Box"}, {"id": "d", "type": "Drop"}],
        "connections": [{"id": "c", "from_node": "b", "from_socket": "result",
                         "to_node": "d", "to_socket": "shape"}],
    })
    drop = next(l for l in transpile(g).splitlines()
                if "_drop(" in l and "@node:d" in l)
    assert "[]" in drop, drop


def test_mesh_slots_carry_the_owner():
    """A build123d Shape takes any attribute, so the B-Rep lane works either way
    and only the MESH lane hits the slots wall — where the assignment is swallowed
    by _drop's try/except and the body silently loses its material. Same trap that
    bit `_noodle_anim`."""
    from cad_nodes.transpiler import PREAMBLE
    assert '"_noodle_owner"' in PREAMBLE or "'_noodle_owner'" in PREAMBLE


def test_preview_bodies_carry_owner():
    """view.json is where to look first when a body renders with the wrong
    material: the whole chain fails silently."""
    from cad_nodes import mesh_extractor

    class Fake:                       # duck-typed like the runtime's Mesh
        def __init__(self, owner=None):
            self._noodle_anim = {"kind": "keys", "t": 0.0, "T": 1.0}
            self._noodle_owner = owner
            self._noodle_extra = []

    part, bowl = Fake(), Fake(owner="bowl_node")
    part._noodle_extra = [bowl]
    monkey = mesh_extractor._preview_geom
    mesh_extractor._preview_geom = lambda v, *a, **k: {"mesh": {}, "bbox": None}
    try:
        out = mesh_extractor._preview_of([part])
    finally:
        mesh_extractor._preview_geom = monkey
    assert out["kind"] == "Scene"
    owners = [b.get("owner") for b in out["bodies"]]
    assert owners == [None, "bowl_node"], owners   # the fallers keep the node look


def test_viewer_resolves_finish_per_body():
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "webui" / "viewer.js").read_text()
    assert "b.owner" in src and "opts.finishOf(own)" in src
    # an emissive container must still reach the glow layer
    assert "finishOf(b.owner) === 'emissive'" in src


def test_body_owner_is_resolved_across_the_id_namespaces():
    """`body.owner` is an ON-DISK graph id; the editor's colour/finish lookups are
    keyed by its own litegraph-derived ids, and the two coincide only by luck.
    nodeByGraphId is the bridge — without it resolveColor fell through to
    `order.indexOf` on an undefined `order` and took the whole render down."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent / "webui" / "nodes.html").read_text()
    assert "function nodeFor(id){ return nodeByGraphId[id] || nodeIndex[id] || null; }" in src
    assert "const i = (order || []).indexOf(id);" in src      # order is optional for a body
    # colorOf's second argument must survive the trip to the per-body call
    viewer = (pathlib.Path(__file__).parent.parent / "webui" / "viewer.js").read_text()
    assert "opts.colorOf(own, opts.order)" in viewer


def test_each_wired_shape_carries_its_own_owner():
    """Five bolts wired into one Drop are usually five NODES, not one node fanning
    out — so they can be styled apart exactly like the container. Only a single
    node producing many pieces genuinely shares an owner."""
    g = Graph.from_dict({
        "nodes": [{"id": "a", "type": "Sphere"}, {"id": "b", "type": "Sphere"},
                  {"id": "bowl", "type": "Cylinder"}, {"id": "d", "type": "Drop"}],
        "connections": [
            {"id": "c1", "from_node": "a", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"},
            {"id": "c2", "from_node": "b", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"},
            {"id": "c3", "from_node": "bowl", "from_socket": "result",
             "to_node": "d", "to_socket": "container"}],
    })
    drop = next(l for l in transpile(g).splitlines()
                if "_drop(" in l and "@node:d" in l)
    assert "['bowl']" in drop and "['a', 'b']" in drop, drop


def test_an_explicit_finish_is_what_overrides_a_body():
    """Now that EVERY falling body names an owner, treating "unset" as an
    override would silently stop the Drop's own finish reaching the parts it
    pours — which is what every existing graph relies on."""
    import pathlib
    viewer = (pathlib.Path(__file__).parent.parent / "webui" / "viewer.js").read_text()
    assert "const f = opts.finishOf(own); if (f) bfinish = f;" in viewer


# ---------------------------------------------------------------------------
# Animate — the same motion, with nothing simulated.
#
# Drop asks "what would happen"; Animate says "do this". They share the Motion
# node and the keyframe replay in the browser, and that sharing is the point:
# the tests below pin what must stay COMMON (the plan, the "keys" format, the
# timeline gizmo) and what must stay DIFFERENT (no un-fan, no container, no
# solver).
# ---------------------------------------------------------------------------

def test_animate_is_registered_and_takes_a_motion_plan():
    d = catalog.get("Animate")
    assert d.category == "print"
    assert d.input("motion").wire_type == WIRE_DATA
    assert d.input("motion").required is False        # unwired = pass the shape through
    # Both lanes, exactly like Drop and PlaceOnBed: it moves the ORIGINAL.
    sh = d.input("shape")
    assert sh.wire_type == WIRE_SOLID and WIRE_MESH in (sh.accepts or [])
    assert d.output_follows == "shape"


def test_animate_declares_the_same_timeline_gizmo_as_drop():
    # One `t` slider commonly drives both, so they must feel like one control.
    a, drop = catalog.get("Animate").gizmo, catalog.get("Drop").gizmo
    assert a == drop == {"kind": "timeline", "binds": ["t"],
                         "anchor": "preview", "lock": ["t"]}
    p = catalog.get("Animate").param("t")
    assert (p.default, p.min, p.max) == (1.0, 0.0, 1.0)


def test_hold_pads_the_timeline_so_one_slider_can_drive_two_clocks():
    """The live 60fps scrub only follows a wire that runs STRAIGHT into `t`
    (applyDropTargets walks direct links), so lining a 1.2s unscrew up with an
    8s pour must not be done by rescaling t through a Remap. `hold` pads the
    shorter timeline instead and keeps the wire direct."""
    p = catalog.get("Animate").param("hold")
    assert p.default == 0.0 and p.min == 0.0
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "m", "type": "ContainerMotion", "params": {"z": 10}},
         {"id": "a", "type": "Animate", "params": {"hold": 6.8}}],
        [{"id": "c1", "from_node": "b", "from_socket": "result",
          "to_node": "a", "to_socket": "shape"},
         {"id": "c2", "from_node": "m", "from_socket": "motion",
          "to_node": "a", "to_socket": "motion"}],
    )
    g.validate()
    call = next(l for l in transpile(g).splitlines() if "= _animate(" in l)
    assert "6.8" in call


def test_animate_transpiles_with_the_shape_the_plan_and_t():
    g = _g(
        [{"id": "b", "type": "Box", "params": {}},
         {"id": "m", "type": "ContainerMotion", "params": {"z": 12, "rz": 720}},
         {"id": "a", "type": "Animate", "params": {"t": 0.4}}],
        [{"id": "c1", "from_node": "b", "from_socket": "result",
          "to_node": "a", "to_socket": "shape"},
         {"id": "c2", "from_node": "m", "from_socket": "motion",
          "to_node": "a", "to_socket": "motion"}],
    )
    g.validate()
    code = transpile(g)
    assert _calls(code, "_animate")
    call = next(l for l in code.splitlines() if "= _animate(" in l)
    assert "0.4" in call and "__out_" in call


def test_animate_without_a_motion_still_emits_the_argument():
    # A half-wired node must not become a syntax error: `motion` arrives as None
    # and the runtime returns the shape untouched.
    g = _g([{"id": "b", "type": "Box", "params": {}},
            {"id": "a", "type": "Animate", "params": {}}],
           [{"id": "c1", "from_node": "b", "from_socket": "result",
             "to_node": "a", "to_socket": "shape"}])
    g.validate()
    call = next(l for l in transpile(g).splitlines() if "= _animate(" in l)
    assert "None" in call


def test_animate_fans_out_over_several_shapes():
    """The one place it must NOT copy Drop. A Drop un-fans its shapes into one
    scene because they have to collide with each other; nothing here interacts,
    so five lids wired in are five independent movements — the ordinary rule."""
    g = _g(
        [{"id": "x", "type": "Box", "params": {}},
         {"id": "y", "type": "Box", "params": {}},
         {"id": "m", "type": "ContainerMotion", "params": {}},
         {"id": "a", "type": "Animate", "params": {}}],
        [{"id": "c1", "from_node": "x", "from_socket": "result",
          "to_node": "a", "to_socket": "shape"},
         {"id": "c2", "from_node": "y", "from_socket": "result",
          "to_node": "a", "to_socket": "shape"},
         {"id": "c3", "from_node": "m", "from_socket": "motion",
          "to_node": "a", "to_socket": "motion"}],
    )
    g.validate()
    call = next(l for l in transpile(g).splitlines() if "_animate(" in l and "__out_" in l)
    assert "_fanout" in call


def test_animate_has_no_container_and_no_solver_switches():
    # Everything that costs compute belongs to Drop. If one of these ever turns
    # up here, the node has stopped being a displacement.
    d = catalog.get("Animate")
    names = {s.name for s in d.inputs} | {p.name for p in d.params}
    assert not (names & {"container", "collide", "grip", "material", "settle", "plane"})


def test_a_screw_is_one_phase_driving_move_and_rotation_together():
    """Why a lid unscrewing needs no new vocabulary: the plan advances rotation
    and translation on the SAME phase, so `move z` + `rotate z` is a helix. If
    they ever ran on separate clocks the cap would rise and turn out of step."""
    ns = _preamble_fns("_mat_quat", "_container_motion", "_motion_driver")
    import numpy as np
    plan = ns["_container_motion"](None, 0, 0, 12.0, 0, 0, 720.0, None,
                                   0.0, 1.0, 0.0, "linear")
    pose, end = ns["_motion_driver"](plan, np.eye(3), np.zeros(3), np.zeros(3))
    assert end == 1.0
    for f in (0.25, 0.5, 0.75, 1.0):
        pos, q = pose(f)
        assert pos[2] == pytest.approx(12.0 * f)          # risen proportionally…
        # …and turned by exactly the matching fraction of the 720 deg (read back
        # as the quaternion's half-angle, unwrapped by the rise).
        ang = 2.0 * math.atan2(math.hypot(*q[:3]), q[3])
        turned = math.radians(720.0 * f) % (2 * math.pi)
        if turned > math.pi:
            turned = 2 * math.pi - turned
        assert abs(ang - turned) < 1e-9


def test_the_editor_replays_both_timeline_nodes():
    """The live 60fps scrub is shared: one set names who ships a timeline, and
    both the node's own `t` and a slider wired into it consult it. Hard-coding
    'Drop' in either place leaves Animate correct but silently un-scrubbable."""
    import pathlib
    js = (pathlib.Path(__file__).parent.parent / "webui" / "nodes.html").read_text()
    assert "const TIMELINE_NODES = new Set(['Drop', 'Animate']);" in js
    assert "if (TIMELINE_NODES.has(node.cadType)) return applyDropAnim(" in js
    assert "if (!tn || !TIMELINE_NODES.has(tn.cadType)) continue;" in js


def test_the_live_replay_resolves_meshes_by_ON_DISK_id():
    """The bug this pins cost a whole recording, and it failed SILENTLY.

    `previewMeshes` is keyed by the on-disk graph id (viewer.js keys it by the
    view.previews key); a litegraph node carries a separate runtime id. They
    coincide only for a graph the editor saved and reloaded in one go — which is
    why every earlier test of the scrub passed. Measured on jar-pour-film: the
    Drop (runtime 30) looked up 'n30' while its mesh sat under 'n29', and a
    Number Slider (runtime 31) resolved 'n31' — the TORUS's mesh. So the failure
    mode is not only a lost 60fps scrub, it is a node handed ANOTHER node's
    geometry to transform. nodeByGraphId is the bridge and every lookup must
    cross it.
    """
    import pathlib
    js = (pathlib.Path(__file__).parent.parent / "webui" / "nodes.html").read_text()
    # The inverse of nodeFor() exists and is the single place the key is built.
    assert "function graphIdOf(node){" in js
    assert "function previewMeshOf(node){" in js
    assert "function previewAnimOf(node){" in js
    # and NO caller hand-builds the key any more.
    assert "previewMeshes['n'+node.id]" not in js
    assert "previewAnims['n'+node.id]" not in js
    assert "previewMeshes['n'+n.id]" not in js
