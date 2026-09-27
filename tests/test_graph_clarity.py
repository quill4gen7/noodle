"""
Graph readability: collapsed nodes, auto-groups, lineage, derived titles,
minimap and the stale-graph notice.

The layout half is pure Python; the editor half is source assertions on
nodes.html, like the other frontend contracts in tests/.
"""

import pathlib

from cad_nodes import catalog, layout
from cad_nodes.graph import Graph, Node

NODES = (pathlib.Path(__file__).resolve().parent.parent / "webui" / "nodes.html").read_text()


# ── collapsed state ───────────────────────────────────────────────────────


def test_collapsed_round_trips_through_the_graph_dict():
    n = Node(id="a", type="Box", collapsed=True)
    assert n.to_dict()["collapsed"] is True
    assert Node.from_dict(n.to_dict()).collapsed is True
    assert "collapsed" not in Node(id="a", type="Box").to_dict()


def test_a_collapsed_node_is_measured_as_its_title_bar():
    ndef = catalog.get("Box")
    w, h = layout.node_size(ndef, Node(id="a", type="Box", collapsed=True))
    assert h == 0.0 and w == layout.node_size(ndef)[0]
    box = layout.node_box(ndef, Node(id="a", type="Box", collapsed=True))
    assert box.h == layout.NODE_TITLE_HEIGHT


def test_arrange_packs_collapsed_nodes_tightly():
    def column(collapsed):
        return Graph(name="t", nodes=[
            Node(id=f"b{i}", type="Box", collapsed=collapsed) for i in range(4)
        ], connections=[])
    open_, shut = column(False), column(True)
    layout.arrange(open_)
    layout.arrange(shut)
    span = lambda g: max(n.position[1] for n in g.nodes) - min(n.position[1] for n in g.nodes)  # noqa: E731
    assert span(shut) < span(open_) / 2


def test_the_editor_saves_and_restores_collapsed():
    assert "if (n.flags && n.flags.collapsed) nd.collapsed = true;" in NODES
    assert "if (nd.collapsed) node.flags.collapsed = true;" in NODES


# ── stable node ids ───────────────────────────────────────────────────────


def test_the_editor_keeps_on_disk_node_ids():
    """litegraph numbers nodes 1..N in load order and the save used to write
    those back, so n51 became n49 then n48 across saves and an agent holding
    `n51` edited the wrong node. The loader now pins the runtime id to the disk
    id and the serializer goes through graphIdOf."""
    load = NODES[NODES.index("function fromGraphJSON("):NODES.index("function refreshPolyType(")]
    assert "if (m) node.id = +m[1];" in load
    assert "node._gid = nd.id" in load
    assert "const id = graphIdOf(n); idMap[n.id] = id;" in NODES
    assert "return node._gid || ('n' + node.id);" in NODES


def test_no_caller_rebuilds_a_graph_id_by_hand():
    import re
    code = "\n".join(line for line in NODES.splitlines() if not line.lstrip().startswith("//"))
    assert not re.search(r"'n'\s*\+\s*\w+\.id\b(?!\))", code.replace("('n' + node.id)", ""))


# ── live sync (editor side; the server half is test_graph_version/merge) ──


def _fn(name, until):
    i = NODES.index(name)
    return NODES[i:NODES.index(until, i)]


def test_every_save_names_its_base_and_a_409_merges():
    save = _fn("async function persistToServer(", "window.saveGraph")
    assert "${syncBaseQuery()}" in save
    assert "if (res.status === 409) return syncOnStaleSave(res, _retry || 0);" in save
    assert "syncSaved(payload," in save
    assert "if (!syncMaySave()) return false;" in save     # held while conflicts are open


def test_open_reads_graph_and_version_in_one_go():
    op = _fn("window.openGraph = async function", "window.showTab")
    assert "/version?graph=1" in op
    assert op.index("syncLoaded(") < op.index("fromGraphJSON(data)")


def test_copilot_and_import_merge_instead_of_a_noop_reload():
    """openGraph(currentName) returns early for the open graph, so the copilot's
    edits never reached the canvas and the next save reverted them."""
    assert "await openGraph(currentName)" not in NODES
    assert NODES.count("await syncPullNow();") == 2


def test_autosave_is_armed_once_per_change():
    """checkDirty runs every second; re-arming the 2.5s debounce each time meant
    an idle dirty graph never autosaved."""
    cd = _fn("function checkDirty(){", "function scheduleServerSave(){")
    assert "if (cur !== _dirtySeen){" in cd


def test_external_edits_never_become_undo_steps():
    sync = _fn("Live sync — BEGIN", "Live sync — END")
    assert "rebaseHistory(m.ops);" in sync
    assert "histBase = histSnapshot();" in sync
    # a bounded number is two widgets: patch both or the ✎ field wins on save
    assert "for (const w of (node.widgets||[])) if (w.cadParam === k) w.value = v;" in sync


def test_the_screenshot_page_never_polls():
    sync = _fn("Live sync — BEGIN", "Live sync — END")
    assert "if (!window.__noodleShot){\n    setInterval(syncPoll, 1500);" in sync


# ── layout: straighter wires, auto groups ────────────────────────────────


def _fan():
    """A hub feeding three ListItem -> Move chains, each Move named, plus a
    slider feeding the hub; one chain's node appended far below the rest."""
    from cad_nodes.graph import Connection
    nodes = [Node(id="s", type="NumberSlider"), Node(id="hub", type="CodeBlock", title="Progetto")]
    conns = [Connection(id="c0", from_node="s", from_socket="result", to_node="hub", to_socket="in_0")]
    for i, name in enumerate(("Gamba", "Cassa", None)):
        nodes += [Node(id=f"li{i}", type="ListItem", params={"index": i}),
                  Node(id=f"mv{i}", type="Move", title=name, position=(0, 9000 + i))]
        conns += [Connection(id=f"a{i}", from_node="hub", from_socket="result", to_node=f"li{i}", to_socket="list"),
                  Connection(id=f"b{i}", from_node=f"li{i}", from_socket="result", to_node=f"mv{i}", to_socket="shape")]
    return Graph(name="t", nodes=nodes, connections=conns)


def test_chains_come_out_as_straight_rows():
    g = _fan()
    layout.arrange(g)
    pos = {n.id: n.position for n in g.nodes}
    mid = {n.id: n.position[1] + layout.node_size(catalog.REGISTRY[n.type], n)[1] / 2 for n in g.nodes}
    for i in range(3):   # each ListItem sits level with its own Move
        assert abs(mid[f"li{i}"] - mid[f"mv{i}"]) < 120
    assert mid["li0"] < mid["li1"] < mid["li2"]
    assert max(p[1] for p in pos.values()) < 3000   # the far-away one is pulled in


def test_propose_groups_finds_params_hub_and_chains():
    props = layout.propose_groups(_fan())
    by_kind = {}
    for p in props:
        by_kind.setdefault(p["kind"], []).append(p)
    assert by_kind["params"][0]["members"] == ["s"]
    assert by_kind["hub"][0]["title"] == "Progetto"
    titles = sorted(p["title"] for p in by_kind["chain"])
    assert titles == ["Cassa", "Gamba", "Progetto[2]"]


def test_arrange_auto_groups_is_opt_in_and_idempotent():
    g = _fan()
    layout.arrange(g)
    assert not g.groups                       # a plain arrange invents nothing
    r = layout.arrange(g, groups="auto")
    assert len(r["groups_created"]) == 5 and r["group_overlaps"] == 0
    first = [tuple(n.position) for n in g.nodes]
    r2 = layout.arrange(g, groups="auto")      # everything is grouped now
    assert r2["groups_created"] == [] and len(g.groups) == 5
    assert [tuple(n.position) for n in g.nodes] == first


# ── editor readability aids (source contracts) ────────────────────────────


def test_derived_titles_are_display_only():
    block = _fn("Graph clarity — BEGIN", "Graph clarity — END")
    assert "LGraphNode.prototype.getTitle = function(){ return derivedTitle(this) || _gt.call(this); };" in block
    # the serializer reads node.title, never getTitle(): nothing derived is saved
    ser = _fn("function toGraphJSON(", "function createNodeFromData(")
    assert "getTitle" not in ser


def test_lineage_minimap_and_auto_groups_are_reachable():
    block = _fn("Graph clarity — BEGIN", "Graph clarity — END")
    assert "drawLineage(ctx, this);" in block
    assert "lcanvas.onDrawOverlay = function(ctx)" in block
    assert "arrangeGraph('auto')" in block
    assert "...clarityCommands()," in NODES                      # ⌘K palette
    assert "lcanvas.getExtraMenuOptions = () =>" in block          # canvas right-click
    assert "?groups=auto" in _fn("async function arrangeGraph(", "function undo(")
