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
