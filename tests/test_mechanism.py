"""Mechanism — gears that find each other, a gearbox that shifts, and the wiring.

Pure-Python: no build123d, no manifold3d. The kinematics (`_keyframes`,
`_scalar_track`, `_joint`, `_mechanism`) are lifted out of the PREAMBLE and run
on stand-in shapes that carry the same `_noodle_gear` data a SpurGear attaches,
with the collision check off (it is manifold3d's, and was measured in the
worker — PLAN_MECHANISM.md). What is pinned here is what goes wrong SILENTLY:
a wrong ratio or a sign flip still animates smoothly, and an animation that is
wrong is worse than none when it is used to look for interferences.
"""

import json
import math
from pathlib import Path

from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from cad_nodes import catalog
from cad_nodes.casts import WIRE_DATA, WIRE_MESH, WIRE_SOLID
from cad_nodes.graph import Graph
from cad_nodes.transpiler import PREAMBLE, transpile

EXAMPLE = Path(__file__).resolve().parents[1] / "cad_nodes/examples/gearbox-2-speed.json"


class _Shape:
    """Stands in for a build123d shape: takes attributes, copies on moved()."""
    def __init__(self, gear=None):
        if gear:
            self._noodle_gear = gear

    def moved(self, _loc):
        return _Shape()


def _ns():
    def cut(a, b):
        return PREAMBLE[PREAMBLE.index(a):PREAMBLE.index(b)]
    src = cut("def _mat_quat(", "def _container_motion(") + \
        cut("def _gear_profile(", "def _aero_dirs(")
    ns = {"math": math,
          "_drop_apply": lambda s, B, o, ops: _Shape(),
          "_is_mesh": lambda s: False,
          "Location": lambda *a: None,
          "_pt": lambda v: None if v is None else SimpleNamespace(X=v[0], Y=v[1], Z=v[2])}
    exec(compile(src, "<preamble:mechanism>", "exec"), ns)
    return ns


NS = _ns()


def gear(z, m=2.0, width=8.0, center=(0, 0, 0), axis=(0, 0, 1), xref=(1, 0, 0)):
    return _Shape({"z": z, "m": m, "width": width, "pa": 20.0,
                   "center": list(center), "axis": list(axis), "xref": list(xref),
                   "loc": np.eye(4).tolist()})


def joint(shapes, name, **kw):
    return NS["_joint"](shapes, _id=name, _name=name, **kw)


def turns(keys):
    return NS["_keyframes"](keys)


def solve(joints, t=1.0, engage="clash", **kw):
    return NS["_mechanism"](joints, t, 0.0, 0.0, engage, 0, **kw)


def at(trace, name, tau):
    """A body's turn (degrees) at the frame nearest tau seconds."""
    k = trace["names"].index(name)
    i = min(range(len(trace["times"])), key=lambda i: abs(trace["times"][i] - tau))
    return trace["turn"][k][i]


# --- catalog contract ------------------------------------------------------
def test_registered_in_the_mechanism_category():
    for t in ("SpurGear", "Joint", "KeyframeMotion", "Mechanism"):
        assert catalog.get(t).category == "mechanism"


def test_mechanism_has_three_outputs_and_a_timeline():
    d = catalog.get("Mechanism")
    assert [(s.name, s.wire_type) for s in d.outputs] == [
        ("result", WIRE_SOLID), ("report", WIRE_DATA), ("clash", WIRE_MESH)]
    assert d.input("joints").multiple
    assert d.gizmo["kind"] == "timeline"


def test_spur_gear_places_itself_with_at_not_origin():
    """Like Thread: the gear's data rides on its Location, so the node must
    place the gear itself — an emitter-wrapped `origin` would not be recorded."""
    names = [s.name for s in catalog.get("SpurGear").inputs]
    assert "at" in names and "origin" not in names


def test_the_editor_scrubs_a_mechanism_live():
    html = (Path(__file__).resolve().parents[1] / "webui/nodes.html").read_text()
    line = next(l for l in html.splitlines() if "const TIMELINE_NODES" in l)
    assert "'Mechanism'" in line


# --- emission --------------------------------------------------------------
def test_example_transpiles_with_owners_and_one_solve():
    g = Graph.from_dict(json.loads(EXAMPLE.read_text()))
    g.validate()
    src = transpile(g)
    j_in = next(l for l in src.splitlines() if "@node:j_in " in l)
    # every shape of the body names the node that drew it, in wiring order
    assert "['in20', 'in30', 'sh_in'], 'j_in', 'Primario')" in j_in
    calls = [l for l in src.splitlines() if "_mechanism(" in l and "def " not in l]
    assert len(calls) == 1                     # report and clash read the same plan
    assert "'clash'" in calls[0]


# --- the gear --------------------------------------------------------------
def test_gear_profile_spans_root_to_tip():
    z, m = 20, 2.0
    pts = NS["_gear_profile"](z, m)
    r = [math.hypot(x, y) for x, y in pts]
    assert max(r) == pytest.approx(m * z / 2 + m)
    assert min(r) == pytest.approx(m * z / 2 - 1.25 * m)
    # rotationally periodic: as many points per tooth for every tooth
    assert len(pts) % z == 0


def test_backlash_thins_the_tooth_by_the_right_angle():
    """Backlash comes off the tooth, not out of thin air: tooth 0's angular
    width at the tip shrinks by backlash / pitch radius (half per flank)."""
    z, m = 20, 2.0
    ra = m * z / 2 + m

    def tip_width(b):
        pts = NS["_gear_profile"](z, m, _backlash=b)
        tooth0 = pts[:len(pts) // z]
        tip = [math.atan2(y, x) for x, y in tooth0
               if abs(math.hypot(x, y) - ra) < 1e-9]
        return max(tip) - min(tip)
    assert tip_width(0.0) - tip_width(0.2) == pytest.approx(0.2 / (m * z / 2))


# --- keyframes -------------------------------------------------------------
def test_keyframes_parse_sort_and_interpolate():
    plan = NS["_keyframes"]("2:10; 0:0\n1:4")
    assert plan["times"] == [0.0, 1.0, 2.0]
    f, end = NS["_scalar_track"](plan, [0, 0, 1], "turn")
    assert end == 2.0
    assert f(-1) == 0 and f(0.5) == pytest.approx(2.0) and f(1.5) == pytest.approx(7.0)
    assert f(99) == 10
    step, _ = NS["_scalar_track"](NS["_keyframes"]("0:0, 1:5", _interp="step"),
                                  [0, 0, 1], "turn")
    assert step(0.99) == 0 and step(1.0) == 5


def test_keyframes_from_wired_lists_and_their_errors():
    plan = NS["_keyframes"]("ignored", [0, 1], [3, 4])
    assert plan["values"] == [3.0, 4.0]
    with pytest.raises(ValueError, match="same length"):
        NS["_keyframes"]("", [0, 1], [3])
    with pytest.raises(ValueError, match="time:value"):
        NS["_keyframes"]("0:0, 7")


def test_a_motion_drives_a_joint_along_its_own_axis():
    """A Motion node's rotation is read ABOUT the joint's axis: rotate z 90 is
    90° for a z joint and nothing at all for an x joint."""
    plan = {"move": [0, 0, 5], "rot": [0, 0, 90], "pivot": None, "cycles": 0.0,
            "duration": 1.0, "delay": 0.0, "easing": "linear"}
    f, end = NS["_scalar_track"](plan, [0, 0, 1], "turn")
    assert end == 1.0 and f(1.0) == pytest.approx(90) and f(0.5) == pytest.approx(45)
    fx, _ = NS["_scalar_track"](plan, [1, 0, 0], "turn")
    assert fx(1.0) == 0
    fs, _ = NS["_scalar_track"](plan, [0, 0, 1], "slide")
    assert fs(1.0) == pytest.approx(5)


# --- the train -------------------------------------------------------------
def _train(third_angle_deg=60.0):
    m = 2.0
    a3 = math.radians(third_angle_deg)
    c2 = (m * (20 + 30) / 2, 0, 0)
    d = m * (30 + 15) / 2
    c3 = (c2[0] + d * math.cos(a3), d * math.sin(a3), 0)
    js = [joint([gear(20)], "A", _turn=turns("0:0, 1:360")),
          joint([gear(30, center=c2)], "B"),
          joint([gear(15, center=c3)], "C")]
    return js, {"A": (20, (0, 0)), "B": (30, c2), "C": (15, c3)}


def test_a_gear_train_turns_at_the_tooth_ratio():
    out = solve(_train()[0])
    tr = out["trace"]
    for name, ratio in (("B", -20 / 30), ("C", 20 / 15)):
        moved = at(tr, name, 1.0) - at(tr, name, 0.0)
        assert moved == pytest.approx(360 * ratio, abs=1e-6), name
    assert not tr["conflicts"]
    assert "ratio -0.6667" in out["report"]


def test_teeth_interleave_whatever_the_angle_of_the_line_of_centres():
    """The meshing condition, stated physically rather than in the solver's
    algebra: measure where the pitch point falls within a pitch of each gear
    (u = 0 on a tooth's centre line). A tooth of one must face a SPACE of the
    other, and rolling keeps it so: u_a + u_b = 1/2 (mod 1) at every frame."""
    for ang in (0.0, 37.0, 60.0, 135.0):
        js, geo = _train(ang)
        tr = solve(js)["trace"]
        for na, nb in (("A", "B"), ("B", "C")):
            (za, ca), (zb, cb) = geo[na], geo[nb]
            phi = math.atan2(cb[1] - ca[1], cb[0] - ca[0])
            for i in (0, 7, 33, 60):
                tha = math.radians(tr["turn"][tr["names"].index(na)][i])
                thb = math.radians(tr["turn"][tr["names"].index(nb)][i])
                ua = ((phi - tha) / (2 * math.pi / za)) % 1.0
                ub = ((phi + math.pi - thb) / (2 * math.pi / zb)) % 1.0
                r = (ua + ub - 0.5) % 1.0
                assert min(r, 1.0 - r) == pytest.approx(0.0, abs=1e-6), (ang, na, nb, i)


def test_antiparallel_axes_flip_the_sign_of_the_body_angle():
    """B's gear (and joint) points along −z: the same physical rotation reads as
    the opposite body angle. Getting this wrong mirrors the motion silently."""
    js = [joint([gear(20)], "A", _turn=turns("0:0, 1:360")),
          joint([gear(30, center=(50, 0, 0), axis=(0, 0, -1))], "B")]
    tr = solve(js)["trace"]
    assert at(tr, "B", 1.0) - at(tr, "B", 0.0) == pytest.approx(+240.0, abs=1e-6)


def test_follows_ties_two_bodies_with_a_ratio():
    js = [joint([gear(20)], "A", _turn=turns("0:0, 1:90"))]
    js.append(joint([_Shape()], "belt", _axis="z", _pivot=(0, 0, 0), _follows=js[0],
                    _ratio=-0.5))
    tr = solve(js)["trace"]
    assert at(tr, "belt", 1.0) == pytest.approx(-45.0)


def test_rpm_drives_with_no_plan_and_defaults_to_four_seconds():
    tr = solve([joint([gear(20)], "A", _rpm=60.0)])["trace"]
    assert tr["times"][-1] == pytest.approx(4.0)
    assert at(tr, "A", 1.0) == pytest.approx(360.0)


def test_a_locked_loop_is_reported_not_hidden():
    """Three equal gears whose centres form an equilateral triangle all mesh
    with each other — an odd loop, which cannot turn. Say so."""
    m, z = 2.0, 20
    d = m * z
    cs = [(0, 0, 0), (d, 0, 0), (d / 2, d * math.sqrt(3) / 2, 0)]
    js = [joint([gear(z, center=cs[0])], "A", _turn=turns("0:0, 1:90")),
          joint([gear(z, center=cs[1])], "B"),
          joint([gear(z, center=cs[2])], "C")]
    out = solve(js)
    assert out["trace"]["conflicts"]
    assert "CONFLICT" in out["report"]


def test_a_gear_off_its_joint_axis_is_an_error():
    with pytest.raises(ValueError, match="not on the joint's axis"):
        joint([gear(20), gear(20, center=(5, 0, 0))], "bad")


# --- the gearbox -----------------------------------------------------------
def _gearbox():
    """The example's geometry: input 20t (z 0) + 30t (z 30), sliding cluster
    40t (z 0) + 30t (z 12) on the output, centre distance 60 for both pairs."""
    k_in = turns("0:0, 8:720")
    fork = NS["_keyframes"]("0:0, 2.8:0, 3.6:9, 4.4:9, 5.2:18, 8:18", _interp="smooth")
    j_in = joint([gear(20), gear(30, center=(0, 0, 30))], "in", _turn=k_in)
    j_out = joint([_Shape()], "out", _axis="z", _pivot=(60, 0, 15))
    j_clu = joint([gear(40, center=(60, 0, 0)), gear(30, center=(60, 0, 12))],
                  "cluster", _slide=fork, _follows=j_out)
    return [j_in, j_clu, j_out]


@pytest.mark.parametrize("engage", ["clash", "snap"])
def test_the_gearbox_shifts_first_neutral_second(engage):
    tr = solve(_gearbox(), engage=engage)["trace"]
    rate = lambda a, b: -(at(tr, "out", b) - at(tr, "out", a)) / (90.0 * (b - a))
    assert rate(0.5, 2.5) == pytest.approx(0.5, abs=1e-6)        # 1st, 2:1
    assert at(tr, "out", 4.3) == pytest.approx(at(tr, "out", 3.7), abs=1e-9)  # N
    assert rate(5.5, 7.5) == pytest.approx(1.0, abs=1e-6)        # 2nd, 1:1
    # the cluster rides its splined shaft through all of it
    assert tr["turn"][tr["names"].index("cluster")] == tr["turn"][tr["names"].index("out")]


def test_clash_engages_continuously_and_snap_jumps():
    """The same shift, two answers, read on the frame where 2nd comes into
    mesh. snap: the output jumps by the whole phase error there. clash: it does
    not — the error is recovered over the entry stroke instead."""
    clash = solve(_gearbox(), engage="clash")["trace"]
    snap = solve(_gearbox(), engage="snap")["trace"]
    assert len(clash["events"]) == 1 and len(snap["events"]) == 1
    tau, _a, _b, err = snap["events"][0]
    assert 0.0 < abs(err) <= 360.0 / 30 / 2 + 1e-9          # at most half a pitch
    i = snap["times"].index(tau)
    k = snap["names"].index("out")
    assert snap["turn"][k][i] - snap["turn"][k][i - 1] == pytest.approx(err, abs=1e-6)
    assert abs(clash["turn"][k][i] - clash["turn"][k][i - 1]) < abs(err) / 2
    # and once fully engaged the two agree to the tooth: same mesh, same phase
    end_c, end_s = clash["turn"][k][-1], snap["turn"][k][-1]
    r = ((end_c - end_s) / 12.0) % 1.0
    assert min(r, 1.0 - r) == pytest.approx(0.0, abs=1e-6)


def test_the_baked_pose_is_the_trace_at_t():
    out = solve(_gearbox(), t=0.25)
    assert "t=0.250 (2.00 s)" in out["report"]
    assert all(getattr(b, "_noodle_anim", {}).get("kind") == "keys" for b in out["bodies"])
    assert len(out["bodies"]) == 5      # 2 + 2 gears + the output shaft


def test_shapes_wired_into_a_joint_are_not_auto_drawn_at_rest():
    """A Joint's output is a data wire, so by the plain auto rule every gear
    feeding it looked terminal and was drawn at its REST pose, under the posed
    copy the Mechanism draws — a ghost of each sliding body (seen at t=0.9 on
    gearbox-2-speed: the cluster twice). Only the Mechanism draws them."""
    src = transpile(Graph.from_dict(json.loads(EXAMPLE.read_text())))
    drawn = {l.split("[")[1].split("]")[0].strip("'")
             for l in src.splitlines() if l.strip().startswith("__previews__[")}
    assert drawn == {"mech"}
