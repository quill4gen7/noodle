"""Grasshopper .gh reader — pure Python, no Rhino, no build123d.

The archives here are written by a tiny GH_IO writer below, so every case says
exactly which bytes it is about. The format notes live in cad_nodes/gh/archive.py.
"""
import struct
import uuid
import zlib

import pytest

from cad_nodes.gh import archive, definition, geometry


# ---------------------------------------------------------------- GH_IO writer
def _s(text: str) -> bytes:
    b = text.encode()
    n, out = len(b), bytearray()
    while True:
        c = n & 0x7F
        n >>= 7
        out.append(c | (0x80 if n else 0))
        if not n:
            return bytes(out) + b


def _item(name, code, value, index=-1) -> bytes:
    head = _s(name) + struct.pack("<ii", index, code)
    if code == 1:
        return head + bytes([1 if value else 0])
    if code == 3:
        return head + struct.pack("<i", value)
    if code == 6:
        return head + struct.pack("<d", value)
    if code == 9:
        return head + uuid.UUID(value).bytes_le
    if code == 10:
        return head + _s(value)
    if code == 20:
        return head + struct.pack("<i", len(value)) + value
    if code == 31:
        return head + struct.pack("<2f", *value)
    if code == 80:
        return head + struct.pack("<3i", *value)
    raise AssertionError(code)


def _chunk(name, items=(), chunks=(), index=-1) -> bytes:
    return (_s(name) + struct.pack("<iii", index, len(items), len(chunks))
            + b"".join(items) + b"".join(chunks))


G = {k: str(uuid.uuid4()) for k in
     ("slider", "extrude", "ext_in", "ext_out", "add", "add_a", "add_b", "add_out", "gone")}


def _archive(compress=True) -> bytes:
    slider = _chunk("Object", [
        _item("GUID", 9, str(uuid.uuid4())), _item("Name", 10, "Number Slider")], [
        _chunk("Container", [
            _item("InstanceGuid", 9, G["slider"]), _item("NickName", 10, "len"),
            _item("SourceCount", 3, 0)], [
            _chunk("Attributes", [_item("Pivot", 31, [10.0, 20.0])]),
            _chunk("Slider", [_item("Value", 6, 50.0), _item("Min", 6, 0.0),
                              _item("Max", 6, 500.0)])])], index=0)
    extrude = _chunk("Object", [
        _item("GUID", 9, str(uuid.uuid4())), _item("Name", 10, "Extrude")], [
        _chunk("Container", [
            _item("InstanceGuid", 9, G["extrude"]), _item("NickName", 10, "Extr"),
            _item("Hidden", 1, True)], [
            _chunk("param_input", [
                _item("InstanceGuid", 9, G["ext_in"]), _item("Name", 10, "Direction"),
                _item("NickName", 10, "D"), _item("Source", 9, G["slider"], 0),
                _item("Source", 9, G["gone"], 1),          # upstream deleted
                _item("SourceCount", 3, 2), _item("Mapping", 3, 1)], index=0),
            _chunk("param_output", [
                _item("InstanceGuid", 9, G["ext_out"]), _item("Name", 10, "Extrusion")],
                index=0)])], index=1)
    # a variable-parameter component: inputs under ParameterData/InputParam
    add = _chunk("Object", [
        _item("GUID", 9, str(uuid.uuid4())), _item("Name", 10, "Addition")], [
        _chunk("Container", [
            _item("InstanceGuid", 9, G["add"]), _item("NickName", 10, "A+B")], [
            _chunk("ParameterData", [_item("InputCount", 3, 2)], [
                _chunk("InputParam", [
                    _item("InstanceGuid", 9, G["add_a"]), _item("Name", 10, "A"),
                    _item("NickName", 10, "A"), _item("Source", 9, G["slider"], 0)], index=0),
                _chunk("InputParam", [
                    _item("InstanceGuid", 9, G["add_b"]), _item("Name", 10, "B"),
                    _item("NickName", 10, "B")], [
                    _chunk("PersistentData", [_item("Count", 3, 1)], [
                        _chunk("Branch", [_item("Count", 3, 1), _item("Path", 10, "{0}")], [
                            _chunk("Item", [_item("number", 6, 7.0)], index=0)], index=0)])],
                    index=1),
                _chunk("OutputParam", [
                    _item("InstanceGuid", 9, G["add_out"]), _item("Name", 10, "Result")],
                    index=0)])])], index=2)
    root = _chunk("Root", [_item("ArchiveVersion", 80, [0, 2, 2])], [
        _chunk("Definition", [_item("plugin_version", 80, [1, 0, 7])], [
            _chunk("DefinitionProperties", [_item("Name", 10, "probe.gh")]),
            _chunk("GHALibraries", [_item("Count", 3, 2)], [
                _chunk("Library", [_item("Name", 10, "Pufferfish")], index=0),
                _chunk("Library", [_item("Name", 10, "Pufferfish")], index=1)]),
            _chunk("DefinitionObjects", [_item("ObjectCount", 3, 3)],
                   [slider, extrude, add])])])
    if not compress:
        return root
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(root) + c.flush()


# ---------------------------------------------------------------- archive
def test_parses_raw_deflate_and_plain_alike():
    for raw in (_archive(True), _archive(False)):
        root = archive.parse(raw)
        assert root.name == "Root"
        assert root.item("ArchiveVersion") == [0, 2, 2]


def test_rejects_what_is_not_a_gh():
    with pytest.raises(archive.ArchiveError):
        archive.parse(b"PK\x03\x04 a zip, not a definition")


def test_unknown_type_code_says_where_instead_of_desyncing():
    bad = _chunk("Root", [_s("x") + struct.pack("<ii", -1, 99)])
    with pytest.raises(archive.ArchiveError, match="type code 99 at byte"):
        archive.parse(bad)


def test_trailing_bytes_are_an_error():
    with pytest.raises(archive.ArchiveError, match="trailing"):
        archive.parse(_archive(False) + b"\x00")


# ---------------------------------------------------------------- definition
def test_objects_values_and_wires():
    d = definition.read_bytes(_archive())
    assert d.name == "probe.gh"
    assert d.libraries == ["Pufferfish"]                 # deduplicated
    slider, extrude, add = d.objects
    assert slider.floating and slider.slider["value"] == 50.0
    assert slider.pos == [10.0, 20.0]
    assert extrude.hidden and extrude.inputs[0].flags == ["flatten"]
    # the deleted upstream is dropped, as Grasshopper drops it on load
    assert extrude.inputs[0].wires == [(0, 0)]


def test_variable_parameter_inputs_are_read_from_ParameterData():
    add = definition.read_bytes(_archive()).objects[2]
    assert [p.name for p in add.inputs] == ["A", "B"]
    assert add.inputs[0].wires == [(0, 0)]
    assert add.inputs[1].data == [7.0]
    assert [p.name for p in add.outputs] == ["Result"]


def test_shown_is_enabled_and_not_hidden():
    d = definition.read_bytes(_archive())
    # the slider never draws; the Extrude is hidden; Addition is a number
    assert d.shown() == []


def test_report_reads_top_to_bottom():
    text = definition.report(definition.read_bytes(_archive()))
    assert "#0 Number Slider 'len' = 50 [0..500]" in text
    assert "D <- #0." in text and "[flatten]" in text
    assert "B = 7" in text


# ---------------------------------------------------------------- geometry
def test_rhino_knots_become_occt_knots_and_multiplicities():
    # Rhino keeps n+degree-1 knots: a clamped cubic with 6 points has 8
    vals, mults = geometry.occ_knots([0, 0, 0, 1, 2, 3, 3, 3], 3)
    assert vals == [0, 1, 2, 3] and mults == [4, 1, 1, 4]
    assert sum(mults) == 6 + 3 + 1
    # a polyline segment: 2 points, degree 1
    assert geometry.occ_knots([5.0, 9.0], 1) == ([5.0, 9.0], [2, 2])


def _binaryformatter_blob(payload: bytes) -> bytes:
    """The shape of an ON_Data record: deflate(BinaryFormatter(ISerializable
    RhinoCommon object with version/archive3dm/opennurbs/data))."""
    body = (b"\x00\x01\x00\x00\x00\xff\xff\xff\xff\x01\x00\x00\x00\x00\x00\x00\x00"
            b"\x0c\x02\x00\x00\x00" + _s("RhinoCommon, Version=8.2") +
            b"\x05\x01\x00\x00\x00" + _s("Rhino.Geometry.PolyCurve") +
            struct.pack("<i", 4) + _s("version") + _s("archive3dm") + _s("opennurbs") +
            _s("data") + bytes([0, 0, 0, 7, 8, 8, 8, 2]) + struct.pack("<i", 2) +
            struct.pack("<iii", 10000, 50, 202312128) +
            b"\x09" + struct.pack("<i", 3) +
            b"\x0f" + struct.pack("<i", 3) + struct.pack("<i", len(payload)) + b"\x02" +
            payload + b"\x0b")
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(body) + c.flush()


def test_on_data_unwraps_to_what_rhino3dm_decodes():
    cls, dd = geometry.unwrap(_binaryformatter_blob(b"\x01\x02openNURBS"))
    assert cls == "PolyCurve"
    assert dd == {"version": 10000, "archive3dm": 50, "opennurbs": 202312128,
                  "data": b"\x01\x02openNURBS"}


def test_curve_code_carries_every_digit_it_was_given():
    rec = {"kind": "curve", "segments": [
        {"degree": 3, "points": [[0, -219.375, 50], [0, -216.2234, 57.00841],
                                 [0, -210.873, 62.9], [0, -110, 189.9588165]],
         "weights": None, "knots": [0, 0, 0, 103.8904821, 103.8904821, 103.8904821]}]}
    code = geometry.curve_code([rec], "probe")
    assert "103.8904821" in code and "189.9588165" in code
    assert "[0, 103.8904821]" in code and "[4, 4]" in code
    compile(code, "<codeblock>", "exec")


# ---------------------------------------------------------------- the preview
def test_a_solid_whose_coarse_tessellation_fails_is_retried_finer():
    """A 300mm rail with a 15mm B-spline hook: the size-scaled deflection
    (6mm) leaves the hook face untriangulated, tessellate() raises, and the
    preview used to fall back to bare edges."""
    from cad_nodes import mesh_extractor as mx

    class V:
        def __init__(self, x): self.X = self.Y = self.Z = x

    class Rail:
        asked = []
        def tessellate(self, tol, ang):
            self.asked.append(tol)
            if tol > 0.2:
                raise AttributeError("'NoneType' object has no attribute 'NbNodes'")
            return [V(0), V(1), V(2)], [(0, 1, 2)]
        def edges(self):
            raise AssertionError("must not fall back to edges")

    entry = mx._preview_geom(Rail())
    assert entry["mesh"]["triangles"] == [[0, 1, 2]]
    assert len(Rail.asked) >= 2 and Rail.asked[-1] < Rail.asked[0]


def test_rational_control_points_are_dehomogenised():
    """rhino3dm gives a rational curve's CVs as (x*w, y*w, z*w, w)."""
    class P:
        def __init__(self, x, y, z, w): self.X, self.Y, self.Z, self.W = x, y, z, w
    class N:
        Degree, IsPeriodic, IsRational = 2, False, True
        Points = [P(0, 0, 0, 1), P(0.7071, 0.7071, 0, 0.7071), P(0, 1, 0, 1)]
        Knots = [0, 0, 1, 1]
        def ToNurbsCurve(self): return self
    rec = geometry._nurbs(N())
    assert rec["points"][1] == [1.0, 1.0, 0.0]
    assert rec["weights"] == [1, 0.7071, 1]
