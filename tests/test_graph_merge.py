"""
Three-way merge of the editor's canvas with a graph an agent wrote meanwhile
(cad_nodes/graph_merge.py). The contract: non-conflicting edits from both sides
survive, a true conflict keeps the HUMAN's value and is reported, and nothing
is ever silently lost.
"""

import copy

from cad_nodes.graph_merge import merge3, rebase


def _n(nid, typ="Box", **params):
    return {"id": nid, "type": typ, "params": dict(params), "position": [0, 0]}


def _c(a, b, fs="result", ts="shape"):
    return {"from_node": a, "from_socket": fs, "to_node": b, "to_socket": ts}


def _base():
    return {"name": "p",
            "nodes": [_n("n1", length=10, width=10), _n("n2", "Move", x=0), _n("n3", "Display")],
            "connections": [_c("n1", "n2")]}


def _node(g, nid):
    return next((n for n in g["nodes"] if n["id"] == nid), None)


def test_disjoint_edits_both_survive():
    base = _base()
    mine = copy.deepcopy(base)
    _node(mine, "n1")["params"]["length"] = 20        # human: length
    theirs = copy.deepcopy(base)
    _node(theirs, "n1")["params"]["width"] = 33       # agent: width, same node
    _node(theirs, "n2")["params"]["x"] = 5            # agent: another node
    r = merge3(base, mine, base, theirs)
    assert r["conflicts"] == []
    assert _node(r["graph"], "n1")["params"] == {"length": 20, "width": 33}
    assert _node(r["graph"], "n2")["params"] == {"x": 5}
    assert set(r["changed"]) == {"n1", "n2"}


def test_a_true_conflict_keeps_mine_and_is_reported():
    base = _base()
    mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
    _node(mine, "n1")["params"]["length"] = 20
    _node(theirs, "n1")["params"]["length"] = 99
    r = merge3(base, mine, base, theirs)
    assert _node(r["graph"], "n1")["params"]["length"] == 20
    (c,) = r["conflicts"]
    assert c["id"] == "n1" and c["kind"] == "fields"
    assert c["fields"] == [{"field": "params.length", "mine": 20, "theirs": 99}]
    assert c["theirs_node"]["params"]["length"] == 99
    # and it is not replayed onto the undo snapshots either
    assert not any(o.get("field") == "params.length" for o in r["ops"])


def test_the_same_change_on_both_sides_is_not_a_conflict():
    base = _base()
    mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
    _node(mine, "n1")["params"]["length"] = 20
    _node(theirs, "n1")["params"]["length"] = 20.0
    assert merge3(base, mine, base, theirs)["conflicts"] == []


def test_moving_the_same_node_is_quiet_and_mine_wins():
    base = _base()
    mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
    _node(mine, "n1")["position"] = [100, 100]
    _node(theirs, "n1")["position"] = [500, 500]
    r = merge3(base, mine, base, theirs)
    assert r["conflicts"] == [] and _node(r["graph"], "n1")["position"] == [100, 100]


def test_agent_added_nodes_and_wires_arrive():
    base = _base()
    mine = copy.deepcopy(base)
    theirs = copy.deepcopy(base)
    theirs["nodes"].append(_n("n4", "Sphere", radius=3))
    theirs["connections"].append(_c("n4", "n3", ts="value"))
    r = merge3(base, mine, base, theirs)
    assert _node(r["graph"], "n4")["params"] == {"radius": 3}
    assert _c("n4", "n3", ts="value") in r["graph"]["connections"]
    assert {o["op"] for o in r["ops"]} == {"node_add", "conn_add"}


def test_the_same_new_id_on_both_sides_renames_mine():
    """Theirs is on disk and an agent may be holding that id; the human's
    unsaved node is the one that can move without anybody noticing."""
    base = _base()
    mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
    mine["nodes"].append(_n("n4", "Cylinder"))
    mine["connections"].append(_c("n4", "n2"))
    theirs["nodes"].append(_n("n4", "Sphere"))
    r = merge3(base, mine, base, theirs)
    assert _node(r["graph"], "n4")["type"] == "Sphere"
    new = r["renamed"]["n4"]
    assert _node(r["graph"], new)["type"] == "Cylinder"
    assert _c(new, "n2") in r["graph"]["connections"]


def test_agent_delete_applies_unless_the_human_edited_that_node():
    base = _base()
    theirs = copy.deepcopy(base)
    theirs["nodes"] = [n for n in theirs["nodes"] if n["id"] != "n2"]
    theirs["connections"] = []
    r = merge3(base, copy.deepcopy(base), base, theirs)
    assert _node(r["graph"], "n2") is None and r["graph"]["connections"] == []

    mine = copy.deepcopy(base)
    _node(mine, "n2")["params"]["x"] = 7
    r = merge3(base, mine, base, theirs)
    assert _node(r["graph"], "n2")["params"]["x"] == 7           # the edit is kept
    assert r["conflicts"][0]["kind"] == "deleted_by_them"


def test_human_delete_of_a_node_the_agent_edited_is_asked():
    base = _base()
    mine = copy.deepcopy(base)
    mine["nodes"] = [n for n in mine["nodes"] if n["id"] != "n3"]
    theirs = copy.deepcopy(base)
    _node(theirs, "n3")["title"] = "Peso"
    r = merge3(base, mine, base, theirs)
    assert _node(r["graph"], "n3") is None
    (c,) = r["conflicts"]
    assert c["kind"] == "deleted_by_you" and c["theirs_node"]["title"] == "Peso"


def test_rewiring_a_single_input_on_both_sides_is_a_conflict():
    base = _base()
    base["nodes"] += [_n("a", "Sphere"), _n("b", "Cylinder")]
    base["connections"] = []
    mine, theirs = copy.deepcopy(base), copy.deepcopy(base)
    mine["connections"].append(_c("a", "n2"))
    theirs["connections"].append(_c("b", "n2"))
    r = merge3(base, mine, base, theirs)
    assert r["graph"]["connections"] == [_c("a", "n2")]
    assert r["conflicts"][0]["kind"] == "wire"


def test_each_side_is_diffed_against_its_own_spelling_of_the_base():
    """The editor writes every widget value; an agent's file may omit defaults.
    A param the agent's graph never names is not an agent edit."""
    base_theirs = {"name": "p", "nodes": [_n("n1", length=10)], "connections": []}
    base_mine = {"name": "p", "nodes": [_n("n1", length=10, width=10, height=10)], "connections": []}
    mine = copy.deepcopy(base_mine)
    _node(mine, "n1")["params"]["height"] = 4
    theirs = copy.deepcopy(base_theirs)
    _node(theirs, "n1")["params"]["length"] = 12
    r = merge3(base_mine, mine, base_theirs, theirs)
    assert r["conflicts"] == []
    assert _node(r["graph"], "n1")["params"] == {"length": 12, "width": 10, "height": 4}


def test_groups_follow_theirs_only_when_mine_left_them_alone():
    base = _base()
    theirs = copy.deepcopy(base)
    theirs["groups"] = [{"title": "Parametri", "bounding": [0, 0, 10, 10]}]
    r = merge3(base, copy.deepcopy(base), base, theirs)
    assert r["graph"]["groups"][0]["title"] == "Parametri"
    mine = copy.deepcopy(base)
    mine["groups"] = [{"title": "Mio", "bounding": [0, 0, 5, 5]}]
    assert merge3(base, mine, base, theirs)["graph"]["groups"][0]["title"] == "Mio"


def test_rebase_is_theirs_in_the_editors_spelling():
    base = _base()
    theirs = copy.deepcopy(base)
    _node(theirs, "n1")["params"]["length"] = 42
    nxt = rebase(base, base, theirs)
    assert _node(nxt, "n1")["params"]["length"] == 42
    assert merge3(nxt, nxt, theirs, theirs)["ops"] == []


def test_title_cleared_by_the_agent_is_removed():
    base = _base()
    _node(base, "n1")["title"] = "Corpo"
    theirs = copy.deepcopy(base)
    del _node(theirs, "n1")["title"]
    r = merge3(base, copy.deepcopy(base), base, theirs)
    assert "title" not in _node(r["graph"], "n1")
