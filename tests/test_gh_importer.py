"""The 1:1 translator: GH definition + components table -> noodle graph.

Pure Python. The archive is written with test_gh_import's GH_IO writer; the
table here is a stand-in for components.py, mapping onto nodes that already
exist in the catalog, so the test pins the TRANSLATION rules and not the table.
"""
import uuid

from cad_nodes import catalog
from cad_nodes.graph import Graph
from cad_nodes.gh import definition, importer
from tests.test_gh_import import _chunk, _item

T = {k: str(uuid.uuid4()) for k in
     ("slider", "number", "extrude", "add", "panel", "custom", "group", "mystery")}
I = {k: str(uuid.uuid4()) for k in
     ("slider", "num", "ext", "ext_base", "ext_dir", "ext_out", "add", "add_a", "add_b",
      "add_out", "panel", "custom", "custom_g", "custom_m", "locked", "locked_out",
      "mystery", "mystery_out", "group")}

TABLE = {
    T["slider"]: {"kind": "slider"},
    T["number"]: {"kind": "container"},
    T["panel"]: {"kind": "panel"},
    T["custom"]: {"kind": "skip"},
    T["extrude"]: {"kind": "alias", "noodle": "Extrude",
                   "inputs": {"Base": "sketch", "Direction": "amount"},
                   "outputs": {"Extrusion": "result"}},
    T["add"]: {"kind": "alias", "noodle": "Add",
               "inputs": {"A": "a", "B": "b"}, "outputs": {"Result": "result"}},
}


def _obj(k, type_guid, name, container_items, subs=()):
    return _chunk("Object", [_item("GUID", 9, type_guid), _item("Name", 10, name)],
                  [_chunk("Container", container_items, list(subs))], index=k)


def _pivot(x, y):
    return _chunk("Attributes", [_item("Pivot", 31, [x, y])])


def _definition():
    objs = [
        _obj(0, T["slider"], "Number Slider",
             [_item("InstanceGuid", 9, I["slider"]), _item("NickName", 10, "A Larghezza"),
              _item("SourceCount", 3, 0)],
             [_pivot(0, 0), _chunk("Slider", [_item("Value", 6, 42.0), _item("Min", 6, 0.0),
                                              _item("Max", 6, 900.0), _item("Digits", 3, 1)])]),
        # a floating Number param wired from the slider: must vanish
        _obj(1, T["number"], "Number",
             [_item("InstanceGuid", 9, I["num"]), _item("NickName", 10, "Num"),
              _item("SourceCount", 3, 1), _item("Source", 9, I["slider"], 0)], [_pivot(50, 0)]),
        _obj(2, T["add"], "Addition",
             [_item("InstanceGuid", 9, I["add"]), _item("NickName", 10, "Addition")],
             [_pivot(100, 0), _chunk("ParameterData", [], [
                 _chunk("InputParam", [_item("InstanceGuid", 9, I["add_a"]),
                                       _item("Name", 10, "A"), _item("NickName", 10, "A"),
                                       _item("Source", 9, I["num"], 0),
                                       _item("Mapping", 3, 1)], index=0),
                 _chunk("InputParam", [_item("InstanceGuid", 9, I["add_b"]),
                                       _item("Name", 10, "B"), _item("NickName", 10, "B")], [
                     _chunk("PersistentData", [], [_chunk("Branch", [_item("Path", 10, "{0}")], [
                         _chunk("Item", [_item("number", 6, 8.0)], index=0)], index=0)])],
                     index=1),
                 _chunk("OutputParam", [_item("InstanceGuid", 9, I["add_out"]),
                                        _item("Name", 10, "Result")], index=0)])]),
        _obj(3, T["extrude"], "Extrude",
             [_item("InstanceGuid", 9, I["ext"]), _item("NickName", 10, "Extrude")],
             [_pivot(200, 0),
              _chunk("param_input", [_item("InstanceGuid", 9, I["ext_base"]),
                                     _item("Name", 10, "Base"), _item("NickName", 10, "B")],
                     index=0),
              _chunk("param_input", [_item("InstanceGuid", 9, I["ext_dir"]),
                                     _item("Name", 10, "Direction"), _item("NickName", 10, "D"),
                                     _item("Source", 9, I["add_out"], 0)], index=1),
              _chunk("param_output", [_item("InstanceGuid", 9, I["ext_out"]),
                                      _item("Name", 10, "Extrusion")], index=0)]),
        # a panel reading the addition
        _obj(4, T["panel"], "Panel",
             [_item("InstanceGuid", 9, I["panel"]), _item("NickName", 10, "Panel"),
              _item("SourceCount", 3, 1), _item("Source", 9, I["add_out"], 0)], [_pivot(150, 80)]),
        _obj(5, T["custom"], "Custom Preview",
             [_item("InstanceGuid", 9, I["custom"]), _item("NickName", 10, "Preview")],
             [_pivot(300, 0),
              _chunk("param_input", [_item("InstanceGuid", 9, I["custom_g"]),
                                     _item("Name", 10, "Geometry"),
                                     _item("Source", 9, I["ext_out"], 0)], index=0),
              _chunk("param_input", [_item("InstanceGuid", 9, I["custom_m"]),
                                     _item("Name", 10, "Material")], [
                  _chunk("PersistentData", [], [_chunk("Branch", [_item("Path", 10, "{0}")], [
                      _chunk("Item", [_item("colour", 3, 0x00CC2200)], index=0)], index=0)])],
                     index=1)]),
        _obj(6, T["extrude"], "Extrude",
             [_item("InstanceGuid", 9, I["locked"]), _item("NickName", 10, "Extrude"),
              _item("Locked", 1, True)],
             [_chunk("param_output", [_item("InstanceGuid", 9, I["locked_out"]),
                                      _item("Name", 10, "Extrusion")], index=0)]),
        _obj(7, T["mystery"], "Pufferfish Thing",
             [_item("InstanceGuid", 9, I["mystery"]), _item("NickName", 10, "PT")],
             [_chunk("param_output", [_item("InstanceGuid", 9, I["mystery_out"]),
                                      _item("Name", 10, "Out")], index=0)]),
        _obj(8, T["group"], "Group",
             [_item("InstanceGuid", 9, I["group"]), _item("NickName", 10, "Somma"),
              _item("ID", 9, I["add"]), _item("ID", 9, I["slider"])]),
    ]
    root = _chunk("Root", [], [_chunk("Definition", [], [
        _chunk("DefinitionProperties", [_item("Name", 10, "probe.gh")]),
        _chunk("DefinitionObjects", [], objs)])])
    return definition.read_bytes(root)


def _run():
    return importer.import_definition(_definition(), TABLE, decode=lambda o, i, v: None)


def _by_id(res):
    return {n["id"]: n for n in res.graph["nodes"]}


def _wires(res):
    return {(c["from_node"], c["from_socket"], c["to_node"], c["to_socket"])
            for c in res.graph["connections"]}


def test_ids_follow_the_report_and_carry_provenance():
    nodes = _by_id(_run())
    assert nodes["n0"]["type"] == "NumberSlider"
    assert nodes["n2"]["type"] == "Add" and nodes["n3"]["type"] == "Extrude"
    assert nodes["n3"]["params"]["_gh"] == {"k": 3, "guid": I["ext"], "type": "Extrude",
                                           "nick": "Extrude"}


def test_slider_keeps_its_name_and_its_range():
    s = _by_id(_run())["n0"]
    assert s["title"] == "A Larghezza" and s["params"]["value"] == 42.0
    assert s["params"]["_ui"] == {"value": {"min": 0.0, "max": 900.0}}


def test_a_wired_container_vanishes_and_flags_insert_helpers():
    res = _run()
    nodes, wires = _by_id(res), _wires(res)
    assert "n1" not in nodes
    # A was [flatten]: slider -> ListFlatten -> Add.a
    assert nodes["n2_a_flatten"]["type"] == "ListFlatten"
    assert ("n0", "result", "n2_a_flatten", "list") in wires
    assert ("n2_a_flatten", "result", "n2", "a") in wires


def test_persistent_value_without_a_param_becomes_an_input_node():
    # Add has sockets a/b and no same-named params: B = 8 needs a source
    assert catalog.get("Add").param("b") is None
    res = _run()
    nodes = _by_id(res)
    assert nodes["n2_b_value"]["type"] == "Input"
    assert nodes["n2_b_value"]["params"]["text"] == "8"
    assert ("n2_b_value", "value", "n2", "b") in _wires(res)


def test_wired_panel_is_a_display_and_preview_follows_custom_preview():
    res = _run()
    nodes, wires = _by_id(res), _wires(res)
    assert nodes["n4"]["type"] == "Display" and ("n2", "result", "n4", "value") in wires
    assert "n5" not in nodes                       # Custom Preview is not a node…
    assert nodes["n3"]["preview"] is True          # …it is the eye and the colour
    assert nodes["n3"]["color"] == "#cc2200"


def test_what_cannot_be_translated_is_reported_not_guessed():
    res = _run()
    assert "n6" not in _by_id(res) and "n7" not in _by_id(res)
    assert res.unmapped == {f"Pufferfish Thing {T['mystery']}": [7]}
    text = res.report()
    assert "#6 Extrude: disabled in GH" in text


def test_groups_frame_their_members_and_the_graph_loads():
    res = _run()
    (g,) = res.graph["groups"]
    assert g["title"] == "Somma"
    x, y, w, h = g["bounding"]
    for nid in ("n0", "n2"):
        px, py = _by_id(res)[nid]["position"]
        assert x <= px <= x + w and y <= py <= y + h
    Graph.from_dict(res.graph).validate()


def test_typename_is_never_the_value_and_values_are_typed():
    from cad_nodes.gh.archive import Chunk
    from cad_nodes.gh.definition import GhValue, _item_value
    it = Chunk("Item", items=[("TypeName", -1, 10, "Grasshopper.Kernel.Types.GH_Integer"),
                              ("number", -1, 3, -2)])
    assert _item_value(it) == -2
    dom = _item_value(Chunk("Item", items=[("Interval", -1, 60, [0.0, 1.0])]))
    assert isinstance(dom, GhValue) and dom.kind == "domain"
    assert importer._text_of(GhValue("domain", [0.0, 1.0])) == "[0.0, 1.0]"   # not a point
    assert importer.world_plane(GhValue("plane", [0, 0, 0, 0, 1, 0, 0, 0, 1])) == "YZ"
    assert importer.world_plane(GhValue("plane", [0, 5, 0, 1, 0, 0, 0, 1, 0])) is None


def test_several_sources_into_a_list_input_are_collected_not_dropped():
    # ListReverse.list is list_access and not `multiple`: noodle would read
    # only the first wire, so both go through a ListCreate
    table = dict(TABLE)
    table[T["add"]] = {"kind": "alias", "noodle": "ListReverse",
                       "inputs": {"A": "list"}, "outputs": {"Result": "result"}}
    d = _definition()
    d.objects[2].inputs[0].wires = [(0, 0), (0, 0)]
    d.objects[2].inputs[0].flags = []
    res = importer.import_definition(d, table, decode=lambda o, i, v: None)
    wires = _wires(res)
    merge = _by_id(res)["n2_list_merge"]
    # GH merges sources branch by branch: GhMerge under the branch rules (_gh)
    assert merge["type"] == "GhMerge" and "_gh" in merge["params"]
    assert {("n0", "result", "n2_list_merge", "data_1"),
            ("n0", "result", "n2_list_merge", "data_2")} <= wires
    assert ("n2_list_merge", "result", "n2", "list") in wires
    assert sum(1 for w in wires if w[2] == "n2" and w[3] == "list") == 1


def test_a_wired_mirror_plane_stays_wired():
    # Mirror's rule only fills in an UNWIRED plane; claiming the wired one
    # dropped it and every mirror fell back to a plane through the origin
    table = dict(TABLE)
    table[T["add"]] = {"kind": "alias", "noodle": "Mirror",
                       "inputs": {"A": "shape", "B": "plane"}, "outputs": {"Result": "result"}}
    d = _definition()
    o = d.objects[2]
    o.type = "Mirror"
    o.inputs[1].name = "Plane"
    o.inputs[1].wires = [(0, 0)]
    table[T["add"]]["inputs"] = {"A": "shape", "Plane": "plane"}
    res = importer.import_definition(d, table, decode=lambda o, i, v: None)
    assert ("n0", "result", "n2", "plane") in _wires(res)
