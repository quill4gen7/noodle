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
