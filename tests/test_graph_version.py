"""
Optimistic concurrency on graph.json: a write that names the version it edited
is refused (409 / StaleGraphError) when someone else wrote in between, instead
of silently overwriting their work. Writes without a base keep working.
"""

import json

import pytest

from cad_nodes import api
from cad_nodes.graph import Graph, Node
from cad_nodes.graph_version import StaleGraphError, read_versioned, version_of_bytes
from cad_nodes.store import GraphStore


def _g(name="p", **params):
    return Graph(name=name, nodes=[Node(id="n1", type="Box", params=dict(params))])


def test_version_is_a_hash_of_exactly_what_was_written(tmp_path):
    store = GraphStore(tmp_path)
    v = store.save("p", _g(length=5))
    assert v == version_of_bytes((tmp_path / "p" / "graph.json").read_bytes())
    assert store.version("p") == v
    assert store.version("nope") is None


def test_same_size_edit_changes_the_version(tmp_path):
    """The reason it is a hash and not mtime+size: 5 -> 6 keeps the size."""
    store = GraphStore(tmp_path)
    v5 = store.save("p", _g(length=5))
    v6 = store.save("p", _g(length=6))
    assert v5 != v6


def test_a_write_on_the_current_base_goes_through(tmp_path):
    store = GraphStore(tmp_path)
    v1 = store.save("p", _g(length=5))
    v2 = store.save("p", _g(length=7), base_version=v1)
    assert v2 != v1 and store.version("p") == v2


def test_a_write_on_a_stale_base_is_refused_and_nothing_changes(tmp_path):
    store = GraphStore(tmp_path)
    v1 = store.save("p", _g(length=5))
    v2 = store.save("p", _g(length=7))              # someone else, no base
    with pytest.raises(StaleGraphError) as e:
        store.save("p", _g(length=9), base_version=v1)
    assert e.value.current == v2
    assert e.value.graph["nodes"][0]["params"] == {"length": 7}
    assert store.load("p").nodes[0].params == {"length": 7}   # untouched
    d = e.value.detail()
    assert d["error"] == "stale_graph" and d["version"] == v2


def test_no_base_still_overwrites(tmp_path):
    """Backward compatible: every existing caller passes no base."""
    store = GraphStore(tmp_path)
    store.save("p", _g(length=5))
    store.save("p", _g(length=6))
    assert store.load("p").nodes[0].params == {"length": 6}


def test_api_round_trip(tmp_path):
    store = GraphStore(tmp_path)
    store.save("p", _g(length=5))
    cur = api.read_versioned_graph(store, "p")
    assert cur["graph"]["nodes"][0]["params"] == {"length": 5}
    g = cur["graph"]
    g["nodes"][0]["params"]["length"] = 8
    new = api.write_graph(store, "p", g, base_version=cur["version"])
    assert new["version"] == store.version("p")
    with pytest.raises(StaleGraphError):
        api.write_graph(store, "p", g, base_version=cur["version"])


def test_read_versioned_hashes_the_bytes_it_parsed(tmp_path):
    f = tmp_path / "graph.json"
    f.write_text(json.dumps({"a": 1}))
    v, d = read_versioned(f)
    assert d == {"a": 1} and v == version_of_bytes(f.read_bytes())
    assert read_versioned(tmp_path / "missing.json") == (None, None)


# ── the HTTP routes (skipped where the server cannot be imported) ────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    try:
        import server
        from fastapi.testclient import TestClient
    except Exception as e:  # CI without /app/webui, or without httpx
        pytest.skip(f"server not importable here: {e}")
    monkeypatch.setattr(server, "PROJECTS_DIR", tmp_path)
    return TestClient(server.app)


def _doc(length):
    return {"name": "p", "nodes": [{"id": "n1", "type": "Box", "params": {"length": length},
                                    "position": [0, 0]}], "connections": []}


def test_http_save_returns_the_version_and_409s_on_a_stale_base(client, tmp_path):
    r = client.post("/api/graph/p", json=_doc(5))
    assert r.status_code == 200
    v1 = r.json()["version"]
    assert client.get("/api/graph/p/version").json() == {"version": v1}

    # an agent writes in between (no base: the old behaviour)
    v2 = client.post("/api/graph/p", json=_doc(7)).json()["version"]

    # the editor's save still names v1: refused, with what to merge against
    r = client.post(f"/api/graph/p?base_version={v1}", json=_doc(9))
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["version"] == v2 and d["graph"]["nodes"][0]["params"] == {"length": 7}
    assert json.loads((tmp_path / "p" / "graph.json").read_text())["nodes"][0]["params"] == {"length": 7}

    # on the right base it goes through; base_version may also ride in the body
    body = _doc(9) | {"base_version": v2}
    r = client.post("/api/graph/p", json=body)
    assert r.status_code == 200
    assert "base_version" not in json.loads((tmp_path / "p" / "graph.json").read_text())


def test_http_version_can_carry_the_graph(client):
    client.post("/api/graph/p", json=_doc(5))
    r = client.get("/api/graph/p/version?graph=1").json()
    assert r["graph"]["nodes"][0]["params"] == {"length": 5}
    assert client.get("/api/graph/nope/version").status_code == 404


def test_http_merge_is_stateless_and_returns_the_rebased_base(client, tmp_path):
    base = _doc(5)
    mine = _doc(5)
    mine["nodes"][0]["position"] = [50, 50]
    theirs = _doc(8)
    r = client.post("/api/graph/p/merge", json={"base_mine": base, "mine": mine,
                                                "base_theirs": base, "theirs": theirs})
    assert r.status_code == 200
    d = r.json()
    assert d["graph"]["nodes"][0]["params"] == {"length": 8}
    assert d["graph"]["nodes"][0]["position"] == [50, 50]
    assert d["base_next"]["nodes"][0]["position"] == [0, 0]
    assert d["changed"] == ["n1"] and d["conflicts"] == []
    assert not (tmp_path / "p").exists()
