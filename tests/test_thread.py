"""Thread — the size tables, the profile arithmetic, and the wiring contract.

Pure-Python: no build123d, no manifold3d. What is pinned here is everything that
can be got wrong silently — the standards tables (a wrong pitch is a thread that
looks perfect and fits nothing), the male/female root truncation (it is the ONLY
difference between the two, so a copy-paste slip makes both the same thread), and
the drift between the catalog's size dropdown and the table the helper resolves
against. The geometry itself — watertight ribs, the mating pair, the booleans —
runs in the worker and was measured there (PLAN_THREADS.md).
"""

import math

import pytest

from cad_nodes import catalog
from cad_nodes.casts import WIRE_MESH, WIRE_SOLID, WIRE_SURFACE, WIRE_VECTOR
from cad_nodes.graph import Graph
from cad_nodes.transpiler import PREAMBLE, transpile


def _emit(params=None, connections=(), nodes=()):
    g = Graph.from_dict({
        "name": "t",
        "nodes": [{"id": "n1", "type": "Thread", "params": params or {}}] + list(nodes),
        "connections": list(connections),
    })
    return [ln for ln in transpile(g).splitlines() if "_thread(" in ln and "def " not in ln][0]


def _fragment():
    """The size tables and the profile maths, lifted out of the PREAMBLE. They
    are pure arithmetic over `math`, so they can be exercised here rather than
    only inside the worker."""
    start = PREAMBLE.index("_IN_MM = 25.4")
    end = PREAMBLE.index("def _thread(")
    src = PREAMBLE[start:end].replace('\\"\\"\\"', '"""')
    ns = {"math": math}
    exec(compile(src, "<preamble:thread>", "exec"), ns)
    return ns


# --- catalog contract ------------------------------------------------------
def test_registered_as_a_fastener_producing_mesh():
    d = catalog.get("Thread")
    assert d.category == "fastener"
    assert [s.wire_type for s in d.outputs] == [WIRE_MESH]


def test_shape_is_optional_and_takes_either_lane():
    """Wiring `shape` is the convenience, not the contract: unwired the node
    hands back the bare thread."""
    s = catalog.get("Thread").input("shape")
    assert not s.required
    assert s.wire_type == WIRE_SOLID
    assert WIRE_MESH in s.accepts and WIRE_SURFACE in s.accepts


def test_placement_socket_is_at_not_origin():
    """An `origin` socket is wrapped by the emitter around the node's WHOLE
    result, so with `shape` wired it would move the finished assembly and a
    tapped hole could never leave the axis. `at` is taken by the helper and
    applied to the thread before the boolean instead."""
    d = catalog.get("Thread")
    assert d.input("at").wire_type == WIRE_VECTOR
    assert not d.input("at").required
    assert d.input("origin") is None

    body = PREAMBLE.split("def _thread(")[1].split("\ndef ")[0]
    assert body.index("_at(out, _at_pt)") < body.index('_mesh_bool("subtract"')


def test_answers_to_the_words_people_actually_type():
    a = catalog.get("Thread").aliases
    for word in ("filetto", "vite", "bullone", "screw", "bolt", "nut", "tap"):
        assert word in a


# --- emission --------------------------------------------------------------
def test_arguments_reach_the_helper_in_its_own_order():
    """The template is positional, so a reordered signature would bind `length`
    to `clearance` and still run."""
    sig = PREAMBLE.split("def _thread(")[1].split(")")[0]
    names = [p.strip().split("=")[0].strip() for p in sig.replace("\n", " ").split(",")]
    assert names[:13] == [
        "_size", "_length", "_internal", "_clearance", "_starts", "_lefthand",
        "_lead_in", "_segments", "_profile", "_diameter", "_pitch", "_shape",
        "_at_pt"]

    a = _args(_emit({"size": "M8", "length": 20.0, "clearance": 0.25, "starts": 2}))
    assert a[0] == "'M8'" and a[1] == "20.0"
    assert a[3] == "0.25" and a[4] == "2"


def test_kind_becomes_the_internal_flag():
    assert "'internal' == 'internal'" in _emit({"kind": "internal"})
    assert "'external' == 'internal'" in _emit({"kind": "external"})


def _args(line):
    """The helper's argument list, with the trailing `# @node:...` comment (which
    carries parentheses of its own) stripped off first."""
    call = line.split("_thread(")[1].split("#")[0].strip()
    return [a.strip() for a in call.rstrip().rstrip(")").split(",")]


def test_unwired_shape_and_at_are_none_so_the_bare_thread_comes_back():
    assert _args(_emit())[-2:] == ["None", "None"]


def test_wired_shape_reaches_the_helper_leaving_at_alone():
    line = _emit(
        nodes=[{"id": "n0", "type": "Box", "params": {}}],
        connections=[{"id": "c1", "from_node": "n0", "from_socket": "result",
                      "to_node": "n1", "to_socket": "shape"}],
    )
    shape, at = _args(line)[-2:]
    assert shape.startswith("__out_") and at == "None"


def test_a_tapped_hole_can_be_placed_and_still_cut():
    """Both wired at once is the case the `at`/`origin` split exists for."""
    line = _emit(
        nodes=[{"id": "n0", "type": "Box", "params": {}},
               {"id": "np", "type": "Vector", "params": {"x": 5, "y": 5, "z": 0}}],
        connections=[{"id": "c1", "from_node": "n0", "from_socket": "result",
                      "to_node": "n1", "to_socket": "shape"},
                     {"id": "c2", "from_node": "np", "from_socket": "vector",
                      "to_node": "n1", "to_socket": "at"}],
    )
    shape, at = _args(line)[-2:]
    assert shape.startswith("__out_") and at.startswith("__out_")


# --- the size tables -------------------------------------------------------
def test_no_drift_between_the_dropdown_and_the_table():
    """The catalog offers a flat list of names; the helper resolves them against
    its own dict. Nothing links the two but this test."""
    ns = _fragment()
    known = {n for table in ns["_THREAD_SIZES"].values() for n in table}
    offered = set(catalog.get("Thread").param("size").options) - {"custom"}
    assert offered == known


def test_metric_coarse_pitches():
    ns = _fragment()
    iso = ns["_THREAD_SIZES"]["ISO metric"]
    for name, (d, p) in [("M3", (3.0, 0.5)), ("M6", (6.0, 1.0)),
                         ("M10", (10.0, 1.5)), ("M20", (20.0, 2.5))]:
        assert iso[name] == (d, p)


def test_inch_families_are_converted_not_transcribed():
    ns = _fragment()
    d, p = ns["_THREAD_SIZES"]["UNC"]["1/4-20 UNC"]
    assert d == pytest.approx(6.35)          # 0.250"
    assert p == pytest.approx(1.27)          # 25.4 / 20 tpi
    d, p = ns["_THREAD_SIZES"]["NPT"]["1/2 NPT"]
    assert d == pytest.approx(21.336)        # 0.840"
    assert p == pytest.approx(25.4 / 14)


def test_every_family_declares_a_profile():
    ns = _fragment()
    assert set(ns["_THREAD_SIZES"]) <= set(ns["_THREAD_FAMILY"])


# --- the profile -----------------------------------------------------------
def test_male_and_female_differ_only_in_the_root_truncation():
    """The whole of male vs female: 17H/24 against 15H/24. Get them equal and
    the pair binds; swap them and the nut is loose."""
    ns = _fragment()
    p = 1.0
    h_ext, _, _ = ns["_thread_section"]("ISO metric", p, False)
    h_int, _, _ = ns["_thread_section"]("ISO metric", p, True)
    assert 2 * h_ext == pytest.approx(1.2269 * p, abs=1e-4)   # ISO minor d3
    assert 2 * h_int == pytest.approx(1.0825 * p, abs=1e-4)   # ISO basic D1
    assert h_ext > h_int


def test_iso_flanks_really_are_sixty_degrees():
    """Reconstruct the included angle from the returned widths."""
    ns = _fragment()
    p = 2.0
    h, c, hw = ns["_thread_section"]("ISO metric", p, False)
    assert c == pytest.approx(p / 8)
    assert math.degrees(math.atan((hw - c / 2) / h)) == pytest.approx(30.0)


def test_trapezoidal_and_acme_have_their_own_angles():
    ns = _fragment()
    for fam, angle in [("Trapezoidal", 15.0), ("ACME", 14.5), ("NPT", 30.0)]:
        h, c, hw = ns["_thread_section"](fam, 4.0, False)
        assert math.degrees(math.atan((hw - c / 2) / h)) == pytest.approx(angle)


def test_npt_is_the_only_tapered_family():
    ns = _fragment()
    tapered = [f for f, (_, _, t) in ns["_THREAD_FAMILY"].items() if t]
    assert tapered == ["NPT"]
    assert ns["_THREAD_FAMILY"]["NPT"][2] == pytest.approx(1 / 32)  # 1:16 on Ø


def test_every_catalog_size_leaves_room_between_its_turns():
    """The flanks must meet the root before they meet each other. Sweeping the
    whole dropdown is what catches a mistyped pitch in a table nobody rereads."""
    ns = _fragment()
    for name in catalog.get("Thread").param("size").options:
        if name == "custom":
            continue
        d, p, fam = ns["_thread_spec"](name)
        for internal in (False, True):
            h, c, hw = ns["_thread_section"](fam, p, internal)
            assert 2 * hw < p, f"{name}: flanks collide"
            assert 0 < h < d / 2, f"{name}: root past the axis"


# --- overrides -------------------------------------------------------------
def test_pitch_override_is_how_a_fine_thread_is_asked_for():
    ns = _fragment()
    assert ns["_thread_spec"]("M8") == (8.0, 1.25, "ISO metric")
    assert ns["_thread_spec"]("M8", _pitch=1.0) == (8.0, 1.0, "ISO metric")


def test_custom_takes_its_family_from_the_profile_param():
    ns = _fragment()
    assert ns["_thread_spec"]("custom", "ACME", 20.0, 3.0) == (20.0, 3.0, "ACME")


def test_custom_without_numbers_says_so():
    ns = _fragment()
    with pytest.raises(ValueError, match="diameter"):
        ns["_thread_spec"]("custom")


# --- the traps -------------------------------------------------------------
def test_the_winding_reversal_is_still_there():
    """An inverted winding makes manifold3d read the rib as NEGATIVE volume and
    subtract it — silently, and the result stays watertight. Measured: the M6
    rod came back at 180.5mm3 against a bare core of 227.9."""
    body = PREAMBLE.split("def _thread(")[1].split("\ndef ")[0]
    assert "[:, ::-1]" in body
    assert "NEGATIVE volume" in body


def test_the_section_reaches_into_the_core():
    """Abutting the core instead of overlapping it is what made OCCT fail with
    StdFail_NotDone; manifold3d wants the overlap too."""
    body = PREAMBLE.split("def _thread(")[1].split("\ndef ")[0]
    assert "base = root - 0.15 * p" in body


def test_the_boolean_direction_is_not_left_to_the_user():
    body = PREAMBLE.split("def _thread(")[1].split("\ndef ")[0]
    assert '_mesh_bool("subtract" if _internal else "union", _shape, out)' in body


def test_the_description_warns_about_double_clearance():
    d = catalog.get("Thread").description
    assert "ONE half" in d
