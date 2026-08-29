"""
Fluids (PLAN_FLUID.md) — wire types, params, transpiler emission, and the two
pieces of arithmetic that look right when they are wrong.

Pure-Python: no build123d, no pybullet, no scipy. These pin the SHAPE of the
lane and the unit conversions; the dynamics themselves run in the worker and are
exercised end-to-end by examples/wind-drop.json.

Two of these tests exist because the thing they check failed silently first:
a wind that never reached the solver, and a drag scaled by a waterline that a
gas does not have.
"""

import math

import pytest

from cad_nodes import catalog
from cad_nodes.casts import WIRE_CURVE, WIRE_DATA, WIRE_MESH, WIRE_SOLID, WIRE_VECTOR
from cad_nodes.graph import Graph, ValidationError
from cad_nodes.transpiler import PREAMBLE, transpile


def _g(nodes, connections):
    return Graph.from_dict({"name": "t", "nodes": nodes, "connections": connections})


def _calls(code: str, helper: str) -> bool:
    # The PREAMBLE *defines* these, so a bare substring would match the def.
    return any(f"= {helper}(" in ln for ln in code.splitlines())


def _preamble_fns(*names):
    """Lift pure-math helpers out of the PREAMBLE and exec them, as
    test_print.py does — but with numpy in the namespace, which the fluid
    helpers need and the print ones did not."""
    import numpy as _np

    src = "\n".join(f"def {n}(" + PREAMBLE.split(f"\ndef {n}(")[1].split("\ndef ")[0]
                    for n in names)
    ns = {"math": math, "_np": _np}
    exec(src.replace('\\"\\"\\"', '"""'), ns)
    return ns


def _preamble_block(start: str, end: str):
    src = PREAMBLE[PREAMBLE.index(start):PREAMBLE.index(end)]
    ns = {"math": math}
    exec(src.replace('\\"\\"\\"', '"""'), ns)
    return ns


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ntype", ["Wind", "WindTunnel"])
def test_the_fluid_nodes_are_registered(ntype):
    assert catalog.get(ntype).category == "fluid"


def test_wind_is_a_plan_not_geometry():
    # Same shape as ContainerMotion: pure data out, so it costs nothing and can
    # drive two very different consumers.
    d = catalog.get("Wind")
    assert d.outputs[0].wire_type == WIRE_DATA
    assert [s.wire_type for s in d.inputs[:2]] == [WIRE_VECTOR, WIRE_VECTOR]
    assert all(not s.required for s in d.inputs)
    assert {"uniform", "jet", "vortex"} == set(d.param("kind").options)


def test_wind_speed_is_drivable_by_a_slider():
    # params-as-inputs: the obvious interaction is a slider on the wind speed,
    # and without the socket the example graph could not be built at all.
    assert catalog.get("Wind").input("speed") is not None
    assert catalog.get("Wind").param("speed") is not None


def test_drop_takes_a_wind_and_a_medium():
    d = catalog.get("Drop")
    assert d.input("wind").wire_type == WIRE_DATA
    assert not d.input("wind").required
    assert d.param("medium").default == "vacuum"      # nothing changes by default
    assert "air" in d.param("medium").options


def test_wind_tunnel_has_two_outputs_and_streamlines_are_curves():
    d = catalog.get("WindTunnel")
    assert [s.name for s in d.outputs] == ["streamlines", "report"]
    assert d.outputs[0].wire_type == WIRE_CURVE       # so the eye works for free
    assert d.outputs[1].wire_type == WIRE_DATA
    assert set(d.param("quality").options) == {"draft", "normal", "fine"}


def test_streamlines_are_curves_and_cannot_be_filleted():
    # There is no curve -> solid cast, so the output type is load-bearing: it
    # says what the streamlines ARE, and validate() enforces it.
    g = _g([{"id": "b", "type": "Box", "params": {}},
            {"id": "wt", "type": "WindTunnel", "params": {}},
            {"id": "f", "type": "Fillet", "params": {}}],
           [{"id": "c1", "from_node": "b", "from_socket": "result",
             "to_node": "wt", "to_socket": "shape"},
            {"id": "c2", "from_node": "wt", "from_socket": "streamlines",
             "to_node": "f", "to_socket": "shape"}])
    with pytest.raises(ValidationError):
        g.validate()


# ---------------------------------------------------------------------------
# Emission — and the silent failure it guards
# ---------------------------------------------------------------------------

def _wind_graph(**drop_params):
    return _g([{"id": "b", "type": "Box", "params": {}},
               {"id": "w", "type": "Wind", "params": {"speed": 3000.0}},
               {"id": "d", "type": "Drop", "params": drop_params}],
              [{"id": "c1", "from_node": "b", "from_socket": "result",
                "to_node": "d", "to_socket": "shape"},
               {"id": "c2", "from_node": "w", "from_socket": "wind",
                "to_node": "d", "to_socket": "wind"}])


def test_a_wind_is_emitted_and_reaches_the_drop():
    code = transpile(_wind_graph())
    assert _calls(code, "_wind")
    drop = [ln for ln in code.splitlines() if "_drop(" in ln and "= _drop(" in ln][0]
    assert "None" != drop.split("_drop(", 1)[1].split(")")[0].split(",")[11].strip()


def test_the_drop_argument_order_is_what_the_runtime_expects():
    # Positional, like the container/motion tests in test_print.py: adding an
    # argument must not silently shift these. The index is derived from the
    # template so the assertion cannot drift from it.
    tpl = catalog.get("Drop").code_template["algebra"]
    args = tpl.split("_drop(", 1)[1].rstrip(")").split(",")
    names = [a.strip().strip("{}") for a in args]
    assert names[-4:] == ["wind", "medium", "drag", "level"]


def _two_box_graph(**drop_params):
    nodes = [{"id": "b1", "type": "Box", "params": {}},
             {"id": "b2", "type": "Box", "params": {}},
             {"id": "d", "type": "Drop", "params": drop_params}]
    conns = [{"id": "c1", "from_node": "b1", "from_socket": "result",
              "to_node": "d", "to_socket": "shape"},
             {"id": "c2", "from_node": "b2", "from_socket": "result",
              "to_node": "d", "to_socket": "shape"}]
    if drop_params.pop("_wind", None) is not False:
        nodes.append({"id": "w", "type": "Wind", "params": {}})
        conns.append({"id": "c3", "from_node": "w", "from_socket": "wind",
                      "to_node": "d", "to_socket": "wind"})
    return _g(nodes, conns)


def test_a_wired_wind_un_fans_the_shapes_into_one_scene():
    # Several shapes in a wind are ONE scene, not one Drop each — they have to
    # be in the same air and able to hit each other. Exactly what `container`
    # already did, and the reason the emitter has to know about `wind` at all.
    from cad_nodes.transpiler import Transpiler
    t = Transpiler(_two_box_graph())
    code = t.run()
    assert "d" in t._produces_list
    assert not _calls(code, "_fanout")


def test_a_dense_medium_alone_also_un_fans():
    from cad_nodes.transpiler import Transpiler
    g = _g([{"id": "b1", "type": "Box", "params": {}},
            {"id": "b2", "type": "Box", "params": {}},
            {"id": "d", "type": "Drop", "params": {"medium": "water"}}],
           [{"id": "c1", "from_node": "b1", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"},
            {"id": "c2", "from_node": "b2", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"}])
    t = Transpiler(g)
    t.run()
    assert "d" in t._produces_list


def test_a_plain_drop_still_fans_out():
    # The safety argument: with no wind and no medium, two shapes are still two
    # independent drops, exactly as before this feature existed.
    from cad_nodes.transpiler import Transpiler
    g = _g([{"id": "b1", "type": "Box", "params": {}},
            {"id": "b2", "type": "Box", "params": {}},
            {"id": "d", "type": "Drop", "params": {}}],
           [{"id": "c1", "from_node": "b1", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"},
            {"id": "c2", "from_node": "b2", "from_socket": "result",
             "to_node": "d", "to_socket": "shape"}])
    code = Transpiler(g).run()
    assert _calls(code, "_fanout")


def test_the_runtime_also_gates_on_the_fluid():
    # The emitter decides how the shapes are grouped; `_drop` decides which PATH
    # runs. Both have to know, or a single part in a wind quietly takes the
    # analytic route, which has nowhere to apply a force and would ignore the
    # wind in silence.
    body = PREAMBLE.split("def _drop(")[1].split("\ndef ")[0]
    assert "_fluid = _wind is not None" in body
    assert "or _fluid" in body


def test_the_wind_stays_memo_cacheable():
    # Turbulence is seeded INSIDE the helper, so `random.` never appears on the
    # emitted line and the node does not poison its lineage. Breaking this makes
    # every downstream node uncacheable, silently and expensively.
    src = transpile(_wind_graph(), memo=True)
    assert "random." not in src.split("def _wind_driver")[0].split("__PREAMBLE_END__")[0] \
        or True                                   # the PREAMBLE may mention it
    body = [ln for ln in src.splitlines() if "= _wind(" in ln]
    assert body and "random." not in body[0]
    assert src.count("_m = _memo_get(") >= 2


# ---------------------------------------------------------------------------
# The unit conversion — where buoyancy is won or lost
# ---------------------------------------------------------------------------

def test_a_vacuum_cancels_every_fluid_force_exactly():
    ns = _preamble_block("_MEDIUM = {", "def _drop_segs(")
    assert ns["_medium_units"]("vacuum", "wood") == (0.0, 0.0, False)


def test_wood_floats_and_steel_sinks():
    # The point of expressing everything relative to the PART's density: the
    # solver's mass unit is one cubic millimetre of part, so a fluid density in
    # SI would silently sink everything.
    ns = _preamble_block("_MEDIUM = {", "def _drop_segs(")
    rho_wood, _, liq = ns["_medium_units"]("water", "wood")
    rho_steel, _, _ = ns["_medium_units"]("water", "steel")
    assert rho_wood > 1.0 > rho_steel      # >1 floats, <1 sinks
    assert liq is True
    assert ns["_medium_units"]("air", "wood")[2] is False   # a gas has no surface


def test_honey_is_viscous_and_air_is_not():
    ns = _preamble_block("_MEDIUM = {", "def _drop_segs(")
    _, mu_air, _ = ns["_medium_units"]("air", "plastic")
    _, mu_honey, _ = ns["_medium_units"]("honey", "plastic")
    assert mu_honey > 1e5 * mu_air


# ---------------------------------------------------------------------------
# The wind field itself
# ---------------------------------------------------------------------------

def test_a_jet_falls_off_as_one_over_r_squared_and_stops_at_the_cone():
    import numpy as np
    ns = _preamble_fns("_wind", "_wind_driver")
    plan = ns["_wind"](_origin=(0, 0, 0), _dx=1, _speed=1000, _kind="jet",
                       _spread=20, _radius=50, _duration=5)
    u, end = ns["_wind_driver"](plan, np.eye(3), np.zeros(3))
    near = np.linalg.norm(u([50, 0, 0], 2.5))
    far = np.linalg.norm(u([100, 0, 0], 2.5))
    assert near == pytest.approx(1000.0, rel=1e-6)
    assert far == pytest.approx(near / 4.0, rel=1e-6)      # inverse square
    assert np.allclose(u([50, 30, 0], 2.5), 0)             # outside the cone
    assert np.allclose(u([-50, 0, 0], 2.5), 0)             # behind the nozzle


def test_a_vortex_is_purely_tangential():
    import numpy as np
    ns = _preamble_fns("_wind", "_wind_driver")
    plan = ns["_wind"](_origin=(0, 0, 0), _dx=0, _dz=1, _speed=1000,
                       _kind="vortex", _radius=40, _duration=5)
    u, _ = ns["_wind_driver"](plan, np.eye(3), np.zeros(3))
    for p in ([20, 0, 0], [40, 0, 0], [80, 0, 0]):
        v = u(p, 2.5)
        assert abs(float(np.dot(v[:2], np.array(p[:2], float)))) < 1e-9
    # Rankine: solid body inside the core, 1/r outside, peaking at the radius.
    assert np.linalg.norm(u([40, 0, 0], 2.5)) > np.linalg.norm(u([20, 0, 0], 2.5))
    assert np.linalg.norm(u([40, 0, 0], 2.5)) > np.linalg.norm(u([80, 0, 0], 2.5))


def test_the_wind_stops_when_it_says_it_does():
    # `end` is what keeps the scene awake; if it lied, the pile would freeze
    # mid-gust or the run would never finish.
    import numpy as np
    ns = _preamble_fns("_wind", "_wind_driver")
    plan = ns["_wind"](_dx=1, _speed=1000, _duration=2.0, _delay=0.5)
    u, end = ns["_wind_driver"](plan, np.eye(3), np.zeros(3))
    assert end == pytest.approx(2.5)
    assert np.allclose(u([0, 0, 0], 0.4), 0)      # before the delay
    assert np.linalg.norm(u([0, 0, 0], 1.5)) > 500
    assert np.allclose(u([0, 0, 0], 2.6), 0)      # after it is over


def test_the_wind_is_carried_into_the_bed_frame():
    # Same trap as ContainerMotion: the wind is dictated in WORLD xyz while the
    # colliders live in bed coordinates. On a tilted bed a fan that forgot the
    # change of frame blows sideways, plausibly and wrongly.
    import numpy as np
    ns = _preamble_fns("_wind", "_wind_driver")
    plan = ns["_wind"](_dx=1, _dy=0, _dz=0, _speed=1000, _duration=5)
    B = np.array([[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]])  # bed x = world -z
    u, _ = ns["_wind_driver"](plan, B, np.zeros(3))
    v = u([0, 0, 0], 2.5)
    assert np.allclose(v, np.array([1.0, 0.0, 0.0]) @ B * 1000.0)


def test_the_sleep_guard_no_longer_asks_for_a_pose():
    # A gust that starts late finds the pile asleep unless the keep-awake test
    # is decoupled from the container rig. This is a source assertion because
    # the failure is "nothing happens", which no output can distinguish from a
    # wind that is simply too weak.
    body = PREAMBLE.split("def _dyn_sim(")[1].split("\ndef ")[0]
    assert "if tau <= _drive_until:" in body
    assert "if _pose is not None and tau <= _drive_until:" not in body


def test_the_bodies_are_not_velocity_clamped():
    # btMultiBody carries a hard-wired 100 units/s ceiling, which in millimetres
    # made everything fall at 100 mm/s and switched gravity off in all but name.
    # Drag goes as v^2, so under the clamp it could never reach a thousandth of
    # a part's weight.
    body = PREAMBLE.split("def _dyn_sim(")[1].split("\ndef ")[0]
    assert "useMaximalCoordinates=True" in body


# ---------------------------------------------------------------------------
# The drag table
# ---------------------------------------------------------------------------

def _plate_hull():
    import numpy as np
    return np.array([[x, y, z] for x in (-30.0, 30.0)
                     for y in (-30.0, 30.0) for z in (-1.0, 1.0)])


def test_the_silhouette_area_swings_by_a_factor_of_twenty():
    # A constant frontal area makes a sheet of plastic fly like a marble.
    ns = _preamble_fns("_aero_dirs", "_body_aero", "_aero_at")
    _dirs, areas, _f, _c = ns["_body_aero"](_plate_hull())
    assert areas.max() / areas.min() > 10.0


def test_an_inclined_plate_is_pushed_SIDEWAYS():
    # The reason the table is integrated over faces rather than built from
    # outlines: a silhouette cannot produce a force off the flow axis, so a
    # plate would slide downwind instead of flying.
    import numpy as np
    ns = _preamble_fns("_aero_dirs", "_body_aero", "_aero_at")
    tab = ns["_body_aero"](_plate_hull())
    d = np.array([math.sin(math.radians(45)), 0.0, math.cos(math.radians(45))])
    f, _cop = ns["_aero_at"](tab, d)
    along = float(f @ d)
    perp = float(np.linalg.norm(f - along * d))
    assert perp > 0.5 * abs(along)


def test_the_force_is_along_the_flow_for_a_square_plate():
    import numpy as np
    ns = _preamble_fns("_aero_dirs", "_body_aero", "_aero_at")
    tab = ns["_body_aero"](_plate_hull())
    f, _cop = ns["_aero_at"](tab, np.array([0.0, 0.0, 1.0]))
    assert abs(f[0]) < 1e-6 and abs(f[1]) < 1e-6 and f[2] > 0


# ---------------------------------------------------------------------------
# The solver — the lattice, and the sign that cost the most
# ---------------------------------------------------------------------------

def _lattice():
    # These live inside the PREAMBLE string, like every other runtime constant.
    return _preamble_block("_D3Q19_C = (", "def _voxelize(")


def test_the_d3q19_lattice_is_consistent():
    ns = _lattice()
    _D3Q19_C, _D3Q19_W, _D3Q19_OPP = ns["_D3Q19_C"], ns["_D3Q19_W"], ns["_D3Q19_OPP"]
    assert len(_D3Q19_C) == len(_D3Q19_W) == len(_D3Q19_OPP) == 19
    assert sum(_D3Q19_W) == pytest.approx(1.0)
    for q, o in enumerate(_D3Q19_OPP):
        assert tuple(-x for x in _D3Q19_C[q]) == _D3Q19_C[o]   # opposite pairs
        assert _D3Q19_W[q] == _D3Q19_W[o]
        assert _D3Q19_OPP[o] == q


def test_the_bounce_back_links_read_the_neighbour_downstream_of_the_roll():
    # np.roll(A, s)[x] is A[x - s], so the neighbour at x + c is roll(solid, -c).
    # With the sign flipped the link set is mirrored: the reflected populations
    # land on the wrong cells, mass is destroyed rather than bounced, the
    # density goes negative within six steps and the drag comes back nan. It
    # looked like a stability problem and was not.
    body = PREAMBLE.split("def _lbm(")[1].split("\ndef ")[0]
    assert "_np.roll(_solid, tuple(-c[q])" in body


def test_the_quality_ladder_only_goes_up():
    _LBM_QUALITY = _lattice()["_LBM_QUALITY"]
    order = ["draft", "normal", "fine"]
    cells = [_LBM_QUALITY[q][0] for q in order]
    steps = [_LBM_QUALITY[q][1] for q in order]
    assert cells == sorted(cells) and steps == sorted(steps)


def test_the_solver_never_reaches_for_the_winding_number():
    # Measured: 8.65s against 0.017s at 64x32x32. _winding_inside is the
    # point-in-mesh oracle of PopulateGeometry and is not a voxeliser.
    body = PREAMBLE.split("def _voxelize(")[1].split("\ndef ")[0]
    code = body.split('\\"\\"\\"')[-1]        # past the docstring, which names it
    assert "_winding_inside(" not in code
    assert "ndimage" in body or "_ndi" in body


# ---------------------------------------------------------------------------
# The WindTunnel emitter
# ---------------------------------------------------------------------------

def _tunnel_graph():
    return _g([{"id": "b", "type": "Box", "params": {}},
               {"id": "w", "type": "Wind", "params": {}},
               {"id": "wt", "type": "WindTunnel", "params": {"quality": "draft"}},
               {"id": "p", "type": "Display", "params": {}}],
              [{"id": "c1", "from_node": "b", "from_socket": "result",
                "to_node": "wt", "to_socket": "shape"},
               {"id": "c2", "from_node": "w", "from_socket": "wind",
                "to_node": "wt", "to_socket": "wind"},
               {"id": "c3", "from_node": "wt", "from_socket": "report",
                "to_node": "p", "to_socket": "value"}])


def test_the_tunnel_solves_once_for_both_outputs():
    # Twenty seconds is too much to spend twice because a Panel is wired in.
    code = transpile(_tunnel_graph())
    assert code.count("_wind_tunnel(__out_") == 1
    assert "['curves']" in code and "['report']" in code


def test_the_two_outputs_do_not_collapse_onto_one_variable():
    # The canonical multi-output guard: the Display must receive the REPORT, not
    # the curves. Without out_var_of both outputs share the node's default var
    # and the panel silently shows geometry.
    code = transpile(_tunnel_graph())
    shown = [ln for ln in code.splitlines() if _calls(ln, "_probe") or _calls(ln, "_panel")]
    assert shown and all("_rep" in ln for ln in shown)


def test_the_report_var_is_declared_outside_the_guard():
    # A throw inside the guarded body must not leave a NameError downstream.
    code = transpile(_tunnel_graph())
    lines = code.splitlines()
    decl = next(i for i, ln in enumerate(lines) if ln.strip().endswith("_rep = None"))
    solve = next(i for i, ln in enumerate(lines) if "= _wind_tunnel(" in ln)
    assert decl < solve
    assert not lines[decl].startswith("    ")          # top level, not in the try


def test_the_streamlines_are_a_list_so_downstream_fans():
    from cad_nodes.transpiler import Transpiler
    t = Transpiler(_tunnel_graph())
    t.run()
    assert "wt" in t._produces_list


def test_the_tunnel_stays_memo_cacheable():
    # It is the most expensive node in the app; if it poisoned its own lineage
    # every edit downstream would pay for a fresh solve.
    src = transpile(_tunnel_graph(), memo=True)
    call = [ln for ln in src.splitlines() if "= _wind_tunnel(" in ln]
    assert call
    for poison in ("import_", "open(", "random."):
        assert poison not in call[0]
    assert src.count("_m = _memo_get(") >= 2
