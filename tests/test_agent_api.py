"""
The agent-facing editing surface in cad_nodes.api: node addressing by title,
strict param validation, small edits (edit_code), atomic batches (apply_ops),
compact reads (graph / catalog / node def), lean execute summaries, execute
overrides and the screenshot guards. No build123d: the executor and the browser
are stubbed where an op would reach them.
"""

import asyncio
import json

import pytest

from cad_nodes import api, layout
from cad_nodes.graph import ValidationError
from cad_nodes.store import GraphStore


@pytest.fixture
def store(tmp_path):
    s = GraphStore(tmp_path)
    api.create_graph(s, "g")
    return s


def _raw(store, gid="g"):
    return (store.dir(gid) / "graph.json").read_bytes()


# --- addressing ------------------------------------------------------------
def test_set_param_addresses_a_node_by_title(store):
    api.add_node(store, "g", "Box", title="Scocca")
    out = api.set_param(store, "g", "Scocca", {"width": 42})
    assert out["node"] == "box_1"
    assert store.load("g").node("box_1").params["width"] == 42


def test_an_ambiguous_title_is_an_error_naming_the_candidates(store):
    api.add_node(store, "g", "Box", title="Twin")
    api.add_node(store, "g", "Box", title="Twin")
    with pytest.raises(KeyError, match="box_1, box_2"):
        api.set_param(store, "g", "Twin", {"width": 1})


def test_an_id_wins_over_a_title(store):
    api.add_node(store, "g", "Box")                    # box_1
    api.add_node(store, "g", "Box", title="box_1")     # titled like the first's id
    api.set_param(store, "g", "box_1", {"width": 7})
    assert store.load("g").node("box_1").params["width"] == 7


# --- param validation ------------------------------------------------------
def test_unknown_param_lists_the_valid_ones(store):
    api.add_node(store, "g", "Box")
    with pytest.raises(ValueError) as e:
        api.set_param(store, "g", "box_1", {"lenght": 5})
    msg = str(e.value)
    assert "box_1" in msg and "'lenght'" in msg
    for name in ("width", "height", "depth"):
        assert name in msg


def test_bad_type_names_node_and_param(store):
    api.add_node(store, "g", "Box")
    with pytest.raises(ValueError, match=r"box_1\.width expects float"):
        api.set_param(store, "g", "box_1", {"width": "wide"})


def test_rejected_set_param_saves_nothing(store):
    api.add_node(store, "g", "Box")
    before = _raw(store)
    with pytest.raises(ValueError):
        api.set_param(store, "g", "box_1", {"width": 3, "nope": 1})
    assert _raw(store) == before


def test_numbers_are_coerced_and_clamps_are_reported(store):
    api.add_node(store, "g", "Box")
    out = api.set_param(store, "g", "box_1", {"width": "12.5", "height": 1e9})
    assert out["params"]["width"] == 12.5
    assert out["params"]["height"] == 500          # the catalog's hard max
    assert any("box_1.height clamped" in n for n in out["notes"])




def test_an_input_slider_widens_its_window_instead_of_clamping(store):
    # The catalog's 0..100 is only a slider's default drag window; a real graph
    # (tars-pet) keeps a leg angle at 180 with a ⚙-widened window.
    api.add_node(store, "g", "NumberSlider", title="Angolo")
    out = api.set_param(store, "g", "Angolo", {"value": 180})
    assert out["params"]["value"] == 180
    node = store.load("g").node(out["node"])
    assert node.params["_ui"]["value"] == {"min": 0, "max": 180}
    assert any("widened" in n for n in out["notes"])
    out = api.set_param(store, "g", "Angolo", {"value": 90})   # inside: untouched
    assert not out.get("notes")
    assert store.load("g").node(out["node"]).params["_ui"]["value"]["max"] == 180


def test_bool_and_select_are_checked(store):
    api.add_node(store, "g", "Box")
    api.set_param(store, "g", "box_1", {"centered": "false"})
    assert store.load("g").node("box_1").params["centered"] is False
    with pytest.raises(ValueError, match="bool"):
        api.set_param(store, "g", "box_1", {"centered": "maybe"})
    api.add_node(store, "g", "FilletChamfer")
    with pytest.raises(ValueError, match="not one of"):
        api.set_param(store, "g", "filletchamfer_1", {"mode": "bevel"})


def test_add_node_validates_its_params(store):
    with pytest.raises(ValueError, match="Valid params"):
        api.add_node(store, "g", "Box", {"size": 3})
    assert store.load("g").nodes == []


def test_editor_state_keys_are_accepted(store):
    api.add_node(store, "g", "NumberSlider")
    api.set_param(store, "g", "numberslider_1",
                  {"_ui": {"value": {"min": 0, "max": 5, "step": 1}}})
    assert "_ui" in store.load("g").node("numberslider_1").params


def test_codeblock_param_by_bare_name_lands_in_the_override(store):
    code = "teeth = 12  #@param min=6 max=40\nresult = None\n"
    api.add_node(store, "g", "CodeBlock", {"code": code, "teeth": 20})
    node = store.load("g").node("codeblock_1")
    assert node.params["_cb"] == {"teeth": 20}
    assert node.params["code"] == code             # source untouched
    with pytest.raises(ValueError, match="teeth"):  # listed among valid names
        api.set_param(store, "g", "codeblock_1", {"teth": 3})


def test_patch_param_keeps_its_contract(store):
    api.add_node(store, "g", "Box")
    assert api.patch_param(store, "g", "box_1", "width", "7") == 7.0
    with pytest.raises(ValueError, match="no param"):
        api.patch_param(store, "g", "box_1", "nope", 1)


def test_check_params_audits_a_hand_edited_graph(store):
    api.add_node(store, "g", "Box")
    g = store.load("g")
    g.node("box_1").params.update({"bogus": 1, "width": 9999})
    issues = api.check_params(g)
    assert any("'bogus'" in i for i in issues)
    assert any("out of range" in i and "box_1.width" in i for i in issues)


# --- wiring ----------------------------------------------------------------
def test_a_bad_socket_lists_the_real_ones(store):
    api.add_node(store, "g", "Circle")
    api.add_node(store, "g", "Extrude")
    with pytest.raises(ValidationError) as e:
        api.connect(store, "g", "circle_1", "result", "extrude_1", "skech")
    assert "Valid inputs of extrude_1" in str(e.value)
    assert "sketch" in str(e.value)
    with pytest.raises(ValidationError, match="Valid outputs of circle_1"):
        api.connect(store, "g", "circle_1", "out", "extrude_1", "sketch")


def test_connect_checked_returns_warnings_for_its_nodes(store):
    api.add_node(store, "g", "Circle")
    api.add_node(store, "g", "Extrude")
    api.add_node(store, "g", "Fillet")               # unrelated, also unwired
    out = api.connect_checked(store, "g", "circle_1", "result", "extrude_1", "sketch")
    assert out["connection_id"] == "c1"
    assert all("fillet_1" not in w for w in out["warnings"])


# --- auto placement --------------------------------------------------------
def test_new_nodes_are_not_stacked_at_the_origin(store):
    for _ in range(4):
        api.add_node(store, "g", "Box")
    g = store.load("g")
    assert layout.overlapping_pairs(g) == []
    assert len({tuple(n.position) for n in g.nodes}) == 4


def test_an_explicit_position_is_kept(store):
    api.add_node(store, "g", "Box", position=(10, 20))
    assert tuple(store.load("g").node("box_1").position) == (10, 20)


def test_batch_places_new_nodes_right_of_their_upstream(store):
    api.add_node(store, "g", "Circle", position=(100, 300))
    out = api.apply_ops(store, "g", [
        {"op": "add_node", "type": "Extrude"},
        {"op": "connect", "from": "circle_1.result", "to": "$0.sketch"},
    ])
    g = store.load("g")
    ext = g.node(out["created"]["$0"])
    assert ext.position[0] > 100 and ext.position[1] == 300
    assert layout.overlapping_pairs(g) == []


# --- edit_code -------------------------------------------------------------
def test_edit_code_replaces_exactly_one_match(store):
    api.add_node(store, "g", "CodeBlock", {"code": "a = 1\nb = 2\nresult = a + b\n"})
    out = api.edit_code(store, "g", "codeblock_1", "b = 2", "b = 3")
    assert out["line"] == 2
    assert "b = 3" in store.load("g").node("codeblock_1").params["code"]


def test_edit_code_refuses_zero_or_many_matches(store):
    api.add_node(store, "g", "CodeBlock", {"code": "x = 1\nx = 1\n"})
    before = _raw(store)
    with pytest.raises(ValueError, match="not found"):
        api.edit_code(store, "g", "codeblock_1", "y = 1", "y = 2")
    with pytest.raises(ValueError, match="matches 2 times"):
        api.edit_code(store, "g", "codeblock_1", "x = 1", "x = 2")
    assert _raw(store) == before


# --- apply_ops -------------------------------------------------------------
def test_apply_ops_is_all_or_nothing(store):
    api.add_node(store, "g", "Box")
    before = _raw(store)
    with pytest.raises(ValueError, match=r"op #2 .*nothing saved"):
        api.apply_ops(store, "g", [
            {"op": "set_param", "node": "box_1", "params": {"width": 3}},
            {"op": "add_node", "type": "Sphere"},
            {"op": "connect", "from": "box_1.result", "to": "$1.nope"},
        ])
    assert _raw(store) == before


def test_apply_ops_full_roundtrip(store):
    api.add_node(store, "g", "Box", position=(0, 0))
    out = api.apply_ops(store, "g", [
        {"op": "add_node", "type": "Fillet", "params": {"radius": 1}, "id": "round"},
        {"op": "connect", "from": "box_1.result", "to": "round.part"},
        {"op": "set_node", "node": "round", "title": "Rounded", "preview": True},
        {"op": "set_param", "node": "Rounded", "params": {"radius": 2}},
        {"op": "add_node", "type": "CodeBlock", "params": {"code": "r = 1\n"}},
        {"op": "edit_code", "node": "$4", "old": "r = 1", "new": "r = 2"},
        {"op": "disconnect", "from": "box_1.result", "to": "round.part"},
        {"op": "remove", "node": "$4"},
    ])
    assert out["ok"] and "version" in out
    g = store.load("g")
    assert g.node("round").params["radius"] == 2
    assert g.node("round").title == "Rounded" and g.node("round").preview is True
    assert g.connections == []
    assert [n.id for n in g.nodes] == ["box_1", "round"]


def test_apply_ops_never_renumbers_existing_ids(store):
    api.add_node(store, "g", "Box")
    api.add_node(store, "g", "Box")
    api.delete_node(store, "g", "box_1")
    out = api.apply_ops(store, "g", [{"op": "add_node", "type": "Box"}])
    assert out["created"]["$0"] == "box_1"             # reuses a free id…
    assert {n.id for n in store.load("g").nodes} == {"box_1", "box_2"}  # …box_2 kept


def test_apply_ops_rejects_an_unknown_op(store):
    with pytest.raises(ValueError, match="unknown op 'explode'"):
        api.apply_ops(store, "g", [{"op": "explode"}])


# --- versions (hook only on this branch) -----------------------------------
class _VersionedStore(GraphStore):
    def version(self, graph_id):
        return 7


def test_a_stale_base_version_is_refused(tmp_path):
    s = _VersionedStore(tmp_path)
    api.create_graph(s, "g")
    api.add_node(s, "g", "Box")
    with pytest.raises(api.StaleGraphError):
        api.set_param(s, "g", "box_1", {"width": 3}, base_version=6)
    assert api.set_param(s, "g", "box_1", {"width": 3}, base_version=7)["version"] == 7


def test_without_versions_the_hook_is_a_no_op(store):
    api.add_node(store, "g", "Box")
    out = api.set_param(store, "g", "box_1", {"width": 3}, base_version="anything")
    assert out["version"] is None


# --- compact reads ---------------------------------------------------------
def test_compact_graph_is_small_and_elides_long_code(store):
    long_code = "\n".join(f"x{i} = {i}" for i in range(200))
    api.add_node(store, "g", "CodeBlock", {"code": long_code}, title="Big")
    api.add_node(store, "g", "Box")
    api.set_param(store, "g", "box_1", {"_ui": {"width": {"min": 0, "max": 9}}})
    c = api.get_graph_compact(store, "g")
    cb = next(n for n in c["nodes"] if n["id"] == "codeblock_1")
    assert cb["title"] == "Big"
    assert cb["params"]["code"].startswith("<200 lines")
    assert all("position" not in n for n in c["nodes"])
    assert all("_ui" not in n["params"] for n in c["nodes"])
    full = api.get_graph_compact(store, "g", node="Big")
    assert full["nodes"][0]["params"]["code"] == long_code
    assert "position" in full["nodes"][0]


def test_compact_catalog_filters_by_query():
    all_lines = api.compact_catalog().splitlines()
    assert any(ln.startswith("Box [") for ln in all_lines)
    hits = api.compact_catalog(query="fillet").splitlines()
    assert hits and len(hits) < len(all_lines)
    assert any(ln.startswith("FilletChamfer [") for ln in hits)
    box = next(ln for ln in all_lines if ln.startswith("Box ["))
    assert "width=" in box                           # defaults shown


def test_node_def_for_agent_drops_codegen_internals():
    d = api.node_def_for_agent("Box")
    assert "code_template" not in d and "imports" not in d
    assert {p["name"] for p in d["params"]} >= {"width", "height", "depth"}
    assert d["outputs"][0] == {"name": "result", "wire_type": "solid"}


# --- lean execute -----------------------------------------------------------
_RESULT = {
    "success": True, "code": "x" * 10000, "stdout": "hi", "errors": None,
    "warnings": [], "node_errors": {}, "node_cached": ["a"],
    "node_timings": {"a": 0.0001, "b": 1.23456},
    "view": {"success": True, "kind": "Solid", "volume": 35742.95185338583,
             "bbox": {"min": [-38.000005, 0.0, 1e-9], "max": [38.00000500000001, 1, 2]},
             "mesh": {"vertices": [[0, 0, 0]] * 100},
             "node_timings": {"a": 0.0001}, "stl": "/app/projects/g/output.stl",
             "previews": {"n1": {"kind": "Solid", "volume": 1.0000001,
                                 "mesh": {"triangles": [[0, 1, 2]]}},
                          "n2": {"kind": "Points", "points": [[0, 0, 0]] * 50}}},
}


def test_summarize_execute_drops_code_and_meshes_and_rounds():
    s = api.summarize_execute(_RESULT)
    assert "code" not in s and "stdout" not in s
    v = s["view"]
    assert "mesh" not in v and "stl" not in v and "node_timings" not in v
    assert v["bbox"]["min"] == [-38.0, 0.0, 0.0]
    assert v["volume"] == 35743.0
    assert v["previews"]["n1"] == {"kind": "Solid", "volume": 1.0}
    assert v["previews"]["n2"] == {"kind": "Points"}
    assert s["slowest"] == {"b": 1.235}
    assert len(json.dumps(s)) < 600


def test_summarize_execute_code_is_opt_in():
    s = api.summarize_execute(_RESULT, include_code=True)
    assert s["code"] == _RESULT["code"] and s["stdout"] == "hi"


def test_execute_overrides_run_without_saving(store, monkeypatch):
    api.add_node(store, "g", "Box", title="Body")
    before = _raw(store)
    seen = {}

    def fake_execute_graph(graph, workdir, timeout=120):
        seen["width"] = graph.node("box_1").params["width"]
        return {"success": True, "view": {}}
    monkeypatch.setattr(api, "execute_graph", fake_execute_graph)
    res = api.execute(store, "g", overrides={"Body": {"width": 33}})
    assert seen["width"] == 33
    assert res["overrides"] == {"box_1": {"width": 33.0}}
    assert _raw(store) == before
    with pytest.raises(ValueError, match="Valid params"):
        api.execute(store, "g", overrides={"Body": {"wdth": 1}})


# --- screenshots -------------------------------------------------------------
_PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 400


def _fake_render(calls, png=_PNG):
    async def render(graph_id, **opts):
        calls.append(opts)
        return png, {"ran": True, "width": 1, "height": 1, "bytes": len(png)}
    return render


def test_a_tiny_or_non_png_capture_is_an_error(store, monkeypatch):
    from cad_nodes import screenshot as shot
    api.add_node(store, "g", "Box")
    for bad in (b'{"detail":"no preview for node x"}', b"\x89PNG\r\n\x1a\nshort"):
        monkeypatch.setattr(shot, "render", _fake_render([], bad))
        with pytest.raises(api.ScreenshotFailed):
            asyncio.run(api.screenshot(store, "g"))


def test_isolating_an_undrawn_node_turns_its_eye_on_then_restores_it(store, monkeypatch):
    from cad_nodes import screenshot as shot
    api.add_node(store, "g", "Box", title="Inner")
    api.add_node(store, "g", "Fillet")
    api.connect(store, "g", "box_1", "result", "fillet_1", "part")
    calls, eyes = [], []
    render = _fake_render(calls)

    async def spying(graph_id, **opts):
        eyes.append(store.load("g").node("box_1").preview)
        return await render(graph_id, **opts)
    monkeypatch.setattr(shot, "render", spying)
    asyncio.run(api.screenshot(store, "g", node="Inner", isolate=True, run=False))
    assert eyes == [True]                          # drawn for the shot
    assert calls[0]["node"] == "box_1" and calls[0]["run"] is True
    assert store.load("g").node("box_1").preview is None   # and restored


def test_screenshot_of_a_non_geometry_node_says_why(store, monkeypatch):
    from cad_nodes import screenshot as shot
    api.add_node(store, "g", "NumberSlider")
    monkeypatch.setattr(shot, "render", _fake_render([]))
    with pytest.raises(ValueError, match="no drawable output"):
        asyncio.run(api.screenshot(store, "g", node="numberslider_1"))


# --- offline CLI -------------------------------------------------------------
def test_cli_validate_and_arrange_work_offline(store, tmp_path, capsys):
    from cad_nodes import cli
    api.add_node(store, "g", "Box", position=(0, 0))
    api.add_node(store, "g", "Box", position=(0, 0))       # stacked on purpose
    path = store.dir("g") / "graph.json"
    assert cli.main(["validate", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    out = tmp_path / "arranged.json"
    assert cli.main(["arrange", str(path), "-o", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["overlaps"] == 0
    assert layout.overlapping_pairs(api.Graph.from_dict(json.loads(out.read_text()))) == []
    data = json.loads(path.read_text())
    data["nodes"][0]["params"]["bogus"] = 1
    path.write_text(json.dumps(data))
    assert cli.main(["validate", str(path)]) == 1
    assert cli.main(["catalog", "--query", "polar"]) == 0
    assert "ArrayPolar [" in capsys.readouterr().out


# --- the guide ---------------------------------------------------------------
def test_help_topics_are_served_on_demand_only():
    core = api.agent_help()
    assert "## topic:" not in core
    assert set(api.help_topics()) >= {"screenshots", "retroeng", "print",
                                      "threads", "fluid"}
    for t in api.help_topics():
        assert t in core                          # the core guide lists them
        assert api.agent_help(t).startswith(f"## topic: {t}")
    assert "section_outline" in api.agent_help("retroeng")
    with pytest.raises(ValueError, match="screenshots"):
        api.agent_help("nope")


def test_every_tool_the_guide_names_exists():
    import re
    from pathlib import Path
    root = Path(api.__file__).resolve().parent.parent
    mcp_src = (root / "mcp_server.py").read_text()
    text = (root / "cad_nodes" / "AGENT_HELP.md").read_text()
    named = set(re.findall(r"\b(cad_[a-z_]+)\(?", text))
    missing = [t for t in named if f"def {t}(" not in mcp_src]
    assert not missing, missing


def test_isolating_an_already_drawn_node_does_not_touch_the_graph(store, monkeypatch):
    """A terminal node is drawn already: no eye flip, no save — so the cheap
    run=0 path for extra angles stays cheap."""
    from cad_nodes import screenshot as shot
    api.add_node(store, "g", "Box")
    before = _raw(store)
    calls = []
    monkeypatch.setattr(shot, "render", _fake_render(calls))
    asyncio.run(api.screenshot(store, "g", node="box_1", run=False))
    assert calls[0]["run"] is False
    assert _raw(store) == before


def test_summarize_execute_passes_an_api_error_through():
    assert api.summarize_execute({"error": "KeyError: 'nope'"}) == {"error": "KeyError: 'nope'"}


def test_agent_writes_are_versioned_like_the_editors_saves(store):
    # The agent tools and the editor share ONE version: graph.json's hash.
    api.add_node(store, "g", "Box", title="Scocca")
    v0 = store.version("g")
    out = api.set_param(store, "g", "Scocca", {"width": 30}, base_version=v0)
    assert out["version"] == store.version("g") != v0
    with pytest.raises(api.StaleGraphError):          # someone wrote meanwhile
        api.set_param(store, "g", "Scocca", {"width": 31}, base_version=v0)
    assert store.load("g").node("box_1").params["width"] == 30
