"""
Pure-Python tests for the node size model and automatic layout.

The size half is pinned to REALITY, not to itself: `fixtures/node_sizes.json`
holds sizes read out of a live litegraph in a real browser (regenerate with
`scripts/capture_node_sizes.py`). If `test_matches_real_editor` fails, it is
`cad_nodes/layout.py` that has drifted from the editor — not the fixture.
"""

import json
import pathlib

import pytest

from cad_nodes import catalog, layout
from cad_nodes.graph import Connection, Graph, Node

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "node_sizes.json"


# ── the size model ────────────────────────────────────────────────────────


def test_matches_real_editor():
    """Every registered node type sizes exactly as the browser sizes it."""
    real = json.loads(FIXTURE.read_text())
    assert len(real) > 150, "fixture looks truncated"

    bad = []
    for node_type, (rw, rh) in sorted(real.items()):
        ndef = catalog.get(node_type)
        if ndef is None:
            continue
        mw, mh = layout.node_size(ndef)
        if abs(mw - rw) > 0.5 or abs(mh - rh) > 0.5:
            bad.append(f"{node_type}: editor={rw}x{rh} model={mw}x{mh}")
    assert not bad, "size model drifted from the editor:\n  " + "\n  ".join(bad)


def test_fixture_covers_the_whole_catalog():
    """A newly added node type must be captured too, or it is sized blind."""
    real = json.loads(FIXTURE.read_text())
    missing = sorted(set(catalog.REGISTRY) - set(real))
    assert not missing, (
        f"{len(missing)} types missing from the fixture "
        f"(run scripts/capture_node_sizes.py): {missing[:10]}"
    )


def test_bounded_number_param_makes_two_widgets():
    """A min+max float gets BOTH the cadslider and the ✎ field — the single
    biggest reason a `len(params)` height estimate comes out far too short."""
    bounded = catalog.Param("r", "float", default=1.0, min=0.0, max=10.0)
    unbounded = catalog.Param("r", "float", default=1.0)
    assert layout._param_widget_count(bounded) == 2
    assert layout._param_widget_count(unbounded) == 1


def test_note_param_makes_no_widget():
    """Its text is drawn on the body; a litegraph widget would strip newlines."""
    p = catalog.Param("note", "str", default="", widget="note")
    assert layout._param_widget_count(p) == 0


def test_grow_inputs_each_add_a_toggle():
    """Every fan-out-capable input carries a `＋ name` toggle, which is a widget."""
    ndef = catalog.get("Union")
    grow = layout._grow_inputs(ndef)
    assert grow, "Union's collector input should be grow-capable"
    assert layout.widget_count(ndef) >= len(grow)


def test_node_size_is_never_below_the_floor():
    for node_type in catalog.REGISTRY:
        w, h = layout.node_size(catalog.get(node_type))
        assert w >= layout.MIN_WIDTH, node_type
        assert h > 0, node_type


def test_resized_note_keeps_its_persisted_size():
    """`size` round-trips for a Note, and the model must trust it over the formula."""
    n = Node(id="a", type="Note", size=[400, 300])
    assert layout.node_size(catalog.get("Note"), n) == (400.0, 300.0)


def test_box_includes_the_title_bar():
    """`position` is the BODY's top-left; litegraph draws the title 30px above.
    Layout that ignores this leaves titles visibly colliding."""
    n = Node(id="a", type="Box", position=(100, 200))
    box = layout.node_box(catalog.get("Box"), n)
    assert box.y == 200 - layout.NODE_TITLE_HEIGHT
    assert box.h == layout.node_size(catalog.get("Box"))[1] + layout.NODE_TITLE_HEIGHT


# ── overlap detection ─────────────────────────────────────────────────────


def _chain(n: int = 6) -> Graph:
    """A linear Box -> Move -> Move -> ... chain, all stacked at the origin."""
    nodes = [Node(id="b", type="Box", position=(0, 0))]
    conns = []
    for i in range(n - 1):
        nodes.append(Node(id=f"m{i}", type="Move", position=(0, 0)))
        conns.append(
            Connection(
                id=f"c{i}",
                from_node=nodes[-2].id,
                from_socket="result",
                to_node=nodes[-1].id,
                to_socket="shape",
            )
        )
    return Graph(name="t", nodes=nodes, connections=conns)


def test_overlaps_are_detected():
    g = _chain(4)  # every node at (0,0)
    assert len(layout.overlapping_pairs(g)) == 6  # all pairs collide


def test_arrange_removes_them():
    g = _chain(6)
    r = layout.arrange(g)
    assert r["overlaps"] == 0
    assert layout.overlapping_pairs(g) == []
    assert r["columns"] == 6  # a chain is one node per column


def test_arrange_is_idempotent():
    g = _chain(5)
    layout.arrange(g)
    first = [tuple(n.position) for n in g.nodes]
    layout.arrange(g)
    assert [tuple(n.position) for n in g.nodes] == first


def test_every_wire_points_forward():
    """Longest-path layering, not plain topological order: no node may sit left
    of one of its own producers."""
    g = _chain(4)
    # a second producer feeding the last node from the very start
    g.nodes.append(Node(id="s", type="Sphere", position=(0, 0)))
    g.connections.append(
        Connection(id="cx", from_node="s", from_socket="result",
                   to_node="m2", to_socket="shape")
    )
    layout.arrange(g)
    pos = {n.id: n.position[0] for n in g.nodes}
    for c in g.connections:
        assert pos[c.from_node] < pos[c.to_node], f"{c.from_node} -> {c.to_node}"


def test_arrange_refuses_an_unknown_type():
    g = Graph(name="t", nodes=[Node(id="a", type="NoSuchNode")], connections=[])
    with pytest.raises(ValueError, match="Unknown node type"):
        layout.arrange(g)


def test_empty_graph_is_fine():
    assert layout.arrange(Graph(name="t", nodes=[], connections=[]))["nodes"] == 0


# ── groups ────────────────────────────────────────────────────────────────


def _grouped() -> Graph:
    """Two independent chains, the first one boxed in a group."""
    nodes, conns = [], []
    for tag in ("a", "b"):
        nodes += [
            Node(id=f"{tag}0", type="Box", position=(0, 0)),
            Node(id=f"{tag}1", type="Move", position=(0, 0)),
        ]
        conns.append(
            Connection(id=f"c{tag}", from_node=f"{tag}0", from_socket="result",
                       to_node=f"{tag}1", to_socket="shape")
        )
    g = Graph(name="t", nodes=nodes, connections=conns)
    # Place the 'a' chain apart, then draw a group box around exactly it.
    g.nodes[0].position, g.nodes[1].position = (0, 0), (400, 0)
    g.nodes[2].position, g.nodes[3].position = (0, 2000), (400, 2000)
    g.groups = [{"title": "A", "bounding": [-50, -50, 800, 900], "color": "#333"}]
    return g


def test_group_box_is_refitted_around_its_members():
    g = _grouped()
    layout.arrange(g)
    bx, by, bw, bh = g.groups[0]["bounding"]
    members = [layout.node_box(catalog.get(n.type), n) for n in g.nodes
               if n.id in ("a0", "a1")]
    for m in members:
        assert bx <= m.x and by <= m.y
        assert bx + bw >= m.x + m.w and by + bh >= m.y + m.h


def test_group_box_excludes_non_members():
    """The re-fit must frame the members it HAD, not everything that ends up near."""
    g = _grouped()
    layout.arrange(g)
    bx, by, bw, bh = g.groups[0]["bounding"]
    for n in g.nodes:
        if n.id.startswith("b"):
            m = layout.node_box(catalog.get(n.type), n)
            inside = (bx <= m.x and by <= m.y
                      and bx + bw >= m.x + m.w and by + bh >= m.y + m.h)
            assert not inside, f"{n.id} was swallowed by the group"


def test_group_members_stay_contiguous():
    """Members share a y-band, so the boxes refitted around two groups do not
    cut across each other (they did before the band allocation)."""
    g = _grouped()
    g.groups.append({"title": "B", "bounding": [-50, 1950, 800, 900], "color": "#444"})
    r = layout.arrange(g)
    assert r["group_overlaps"] == 0


# ── the input panel ───────────────────────────────────────────────────────


def _with_sliders(*titles) -> Graph:
    """A Box fed by one slider per title (None = left unnamed)."""
    nodes = [Node(id="box", type="Box")]
    conns = []
    for i, t in enumerate(titles):
        nodes.append(Node(id=f"s{i}", type="NumberSlider", title=t))
        conns.append(
            Connection(id=f"c{i}", from_node=f"s{i}", from_socket="result",
                       to_node="box", to_socket="length")
        )
    return Graph(name="t", nodes=nodes, connections=conns)


def test_naming_a_slider_promotes_it_to_the_panel():
    g = _with_sliders("Altezza", None)
    r = layout.arrange(g)
    assert r["panel"] == ["Altezza"]
    named = next(n for n in g.nodes if n.title == "Altezza")
    plain = next(n for n in g.nodes if n.id == "s1")
    # The panel sits left of everything else, in its own column.
    assert named.position[0] < plain.position[0]


def test_an_unnamed_slider_stays_in_the_flow():
    g = _with_sliders(None, None)
    assert layout.arrange(g)["panel"] == []


def test_the_panel_is_sorted_by_name():
    """Which is also how you order it — prefix the names and they sort that way."""
    g = _with_sliders("Zeta", "Alfa", "Mu")
    assert layout.arrange(g)["panel"] == ["Alfa", "Mu", "Zeta"]
    xs = {n.title: n.position for n in g.nodes if n.title}
    assert xs["Alfa"][1] < xs["Mu"][1] < xs["Zeta"][1]   # stacked in that order
    assert len({p[0] for p in xs.values()}) == 1          # one column


def test_only_parameter_sources_are_promoted():
    """Naming a Box documents it; it must not move the Box into the panel."""
    g = _with_sliders("Altezza")
    next(n for n in g.nodes if n.id == "box").title = "Corpo principale"
    assert layout.arrange(g)["panel"] == ["Altezza"]


def test_a_title_equal_to_the_type_label_is_not_a_name():
    g = _with_sliders(catalog.get("NumberSlider").label)
    assert layout.arrange(g)["panel"] == []


def test_an_explicitly_grouped_slider_is_left_alone():
    """An explicit grouping is a stronger statement than a name."""
    g = _with_sliders("Altezza")
    s = next(n for n in g.nodes if n.title == "Altezza")
    s.position = (0, 0)
    g.groups = [{"title": "G", "bounding": [-40, -40, 300, 300]}]
    assert layout.arrange(g)["panel"] == []


def test_title_round_trips_through_the_graph_dict():
    n = Node(id="a", type="NumberSlider", title="Altezza")
    assert Node.from_dict(n.to_dict()).title == "Altezza"
    assert "title" not in Node(id="a", type="Box").to_dict()


def test_a_renamed_node_is_measured_with_its_own_name():
    """The title feeds litegraph's width; ignoring it would desync the model."""
    ndef = catalog.get("NumberSlider")
    plain = layout.node_size(ndef)
    named = layout.node_size(ndef, Node(id="a", type="NumberSlider",
                                        title="Altezza complessiva del corpo"))
    assert named[0] > plain[0]


def test_a_graph_of_nothing_but_parameters():
    g = Graph(name="t",
              nodes=[Node(id="s", type="NumberSlider", title="Solo")],
              connections=[])
    r = layout.arrange(g)
    assert r["panel"] == ["Solo"] and r["overlaps"] == 0


def test_a_group_with_no_members_keeps_its_box():
    """Dropping it would silently delete the user's annotation."""
    g = _chain(3)
    g.groups = [{"title": "empty", "bounding": [9000, 9000, 100, 100]}]
    layout.arrange(g)
    assert g.groups[0]["bounding"] == [9000, 9000, 100, 100]
