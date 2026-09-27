"""
Three-way merge of two edits of one graph — the editor's and an agent's.

`graph_version` makes a stale write FAIL instead of overwriting; this module is
what the loser does next. The editor holds its unsaved canvas (`mine`) and the
graph it was edited from (`base_mine`); the server holds the graph somebody
else wrote (`theirs`) and the version both started from (`base_theirs`). The
two bases are the SAME version of the file, just in two spellings: the editor
serialises every widget's value, a hand-written or API graph may omit
defaults. So each side is diffed only against its own base, never across.

Rules, field by field (a param key is a field):

- changed by one side only  -> that side's value;
- changed by both, to the same value -> no conflict;
- changed by both, differently -> a CONFLICT. The merge keeps MINE (a human's
  work is never silently dropped) and reports it, so the editor can ask
  "keep mine / take theirs" per node. Where a node sits, whether it is folded
  and its size are "quiet": both moving a node is not worth a question, mine
  wins without one.
- a node deleted by one side and edited by the other is a conflict too; the
  default keeps whatever the human did (their edit, or their delete).
- wires merge as sets: theirs' additions and removals apply to mine, except a
  second wire into a single-input socket both sides rewired (a conflict,
  mine kept).
- a node both sides ADDED under the same id: theirs is on disk already and an
  agent may be holding that id, so MINE is renamed.

Besides the merged graph it returns `ops`: theirs' applied changes as a flat
list, which the editor replays onto its undo snapshots so Ctrl+Z never takes an
agent's edit back out; and `base_next`, the editor's own base with theirs
applied, i.e. what "saved" now means for its dirty tracking.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from . import catalog

# Node-level fields that are compared (params are handled key by key).
NODE_FIELDS = ("type", "title", "position", "preview", "bypassed", "color",
               "finish", "wireframe", "multi", "collapsed", "size")
# Both sides changing these is not worth asking about: mine wins, silently.
QUIET_FIELDS = {"position", "collapsed", "size"}

_ABSENT = object()


def _norm(nd: dict, field: str) -> Any:
    v = nd.get(field)
    if field in ("position", "size"):
        return [round(float(x)) for x in v] if v else None
    if field in ("bypassed", "wireframe", "collapsed"):
        return bool(v)
    if field == "multi":
        return sorted(v or [])
    if field in ("title", "color", "finish"):
        return v or None
    return v


def _pnorm(v: Any) -> Any:
    """A param value in comparable form (5 == 5.0, dict key order ignored)."""
    if v is _ABSENT:
        return v
    return json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else v


def _conn_key(c: dict) -> tuple:
    return (c.get("from_node"), c.get("from_socket"), c.get("to_node"), c.get("to_socket"))


def _node_changed(a: dict, b: dict) -> bool:
    if any(_norm(a, f) != _norm(b, f) for f in NODE_FIELDS if f not in QUIET_FIELDS):
        return True
    pa, pb = a.get("params") or {}, b.get("params") or {}
    return any(_pnorm(pa.get(k, _ABSENT)) != _pnorm(pb.get(k, _ABSENT)) for k in set(pa) | set(pb))


def _single_input(graph_nodes: dict, node_id: str, socket: str) -> bool:
    nd = graph_nodes.get(node_id)
    ndef = catalog.REGISTRY.get(nd.get("type")) if nd else None
    if ndef is None:
        return False
    for s in ndef.inputs or []:
        if s.name == socket:
            return not s.multiple
    return False     # dynamic (CodeBlock) or unknown socket: do not second-guess


def _label(nd: dict) -> str:
    if nd.get("title"):
        return nd["title"]
    ndef = catalog.REGISTRY.get(nd.get("type"))
    return (ndef.label if ndef else nd.get("type")) or nd.get("id")


def _fresh_id(taken: set) -> str:
    k = 1 + max((int(m.group(1)) for i in taken
                 if (m := re.fullmatch(r"n([1-9]\d*)", str(i)))), default=0)
    return f"n{k}"


def _rename(graph: dict, old: str, new: str) -> None:
    for n in graph.get("nodes", []):
        if n.get("id") == old:
            n["id"] = new
    for c in graph.get("connections", []):
        if c.get("from_node") == old:
            c["from_node"] = new
        if c.get("to_node") == old:
            c["to_node"] = new


def _merge_node(bm: dict, m: dict, bt: dict, t: dict, ops: list, conflict_fields: list) -> dict:
    out = copy.deepcopy(m)
    nid = m["id"]
    for f in NODE_FIELDS:
        tchg = _norm(bt, f) != _norm(t, f)
        if not tchg:
            continue
        mchg = _norm(bm, f) != _norm(m, f)
        if not mchg:
            if f in t and t[f] is not None:
                out[f] = copy.deepcopy(t[f])
            else:
                out.pop(f, None)
            ops.append({"op": "node_set", "id": nid, "field": f, "value": t.get(f)})
        elif _norm(t, f) != _norm(m, f) and f not in QUIET_FIELDS:
            conflict_fields.append({"field": f, "mine": m.get(f), "theirs": t.get(f)})

    pbm, pm, pbt, pt = (x.get("params") or {} for x in (bm, m, bt, t))
    params = out.setdefault("params", {})
    for k in sorted(set(pbt) | set(pt)):
        tv, btv = pt.get(k, _ABSENT), pbt.get(k, _ABSENT)
        if _pnorm(tv) == _pnorm(btv):
            continue
        mv, bmv = pm.get(k, _ABSENT), pbm.get(k, _ABSENT)
        if _pnorm(mv) == _pnorm(bmv):
            if tv is _ABSENT:
                params.pop(k, None)
            else:
                params[k] = copy.deepcopy(tv)
            ops.append({"op": "node_set", "id": nid, "field": "params." + k,
                        "value": None if tv is _ABSENT else tv, "absent": tv is _ABSENT})
        elif _pnorm(mv) != _pnorm(tv):
            conflict_fields.append({"field": "params." + k,
                                    "mine": None if mv is _ABSENT else mv,
                                    "theirs": None if tv is _ABSENT else tv})
    return out


def merge3(base_mine: dict, mine: dict, base_theirs: dict, theirs: dict) -> dict:
    """
    Merge `theirs` (base_theirs -> theirs) into `mine` (base_mine -> mine).

    Returns {graph, conflicts, changed, ops, renamed, base_next}. `graph` keeps
    mine's name and node order, with theirs' new nodes appended.
    """
    mine = copy.deepcopy(mine)
    idx = lambda g: {n["id"]: n for n in g.get("nodes", []) if "id" in n}  # noqa: E731
    bm, bt, t = idx(base_mine), idx(base_theirs), idx(theirs)

    ops: list[dict] = []
    renamed: dict[str, str] = {}
    # Both sides added the same id: theirs is on disk (an agent may hold it), so
    # MINE moves. Must happen before anything is compared by id.
    taken = set(idx(mine)) | set(t) | set(bm) | set(bt)
    for nid in list(idx(mine)):
        if nid not in bm and nid in t and nid not in bt:
            new = _fresh_id(taken)
            taken.add(new)
            _rename(mine, nid, new)
            renamed[nid] = new
            ops.append({"op": "rename", "from": nid, "to": new})
    m = idx(mine)

    conflicts: list[dict] = []
    changed: list[str] = []
    out_nodes: list[dict] = []

    for nid, mn in m.items():
        if nid in bt and nid not in t:                       # theirs deleted it
            if nid in bm and _node_changed(bm[nid], mn):
                conflicts.append({"id": nid, "kind": "deleted_by_them", "label": _label(mn),
                                  "fields": [], "theirs_node": None})
                out_nodes.append(copy.deepcopy(mn))           # keep the human's edit
            else:
                ops.append({"op": "node_del", "id": nid})
                changed.append(nid)
            continue
        if nid in bm and nid in bt and nid in t:
            cf: list[dict] = []
            n_ops = len(ops)
            out_nodes.append(_merge_node(bm[nid], mn, bt[nid], t[nid], ops, cf))
            if len(ops) > n_ops:
                changed.append(nid)
            if cf:
                conflicts.append({"id": nid, "kind": "fields", "label": _label(mn),
                                  "fields": cf, "theirs_node": copy.deepcopy(t[nid])})
            continue
        out_nodes.append(copy.deepcopy(mn))                   # mine added it

    # Nodes the human deleted that theirs edited meanwhile: stays deleted, asked.
    for nid in bm:
        if nid not in m and nid in bt and nid in t and _node_changed(bt[nid], t[nid]):
            conflicts.append({"id": nid, "kind": "deleted_by_you", "label": _label(t[nid]),
                              "fields": [], "theirs_node": copy.deepcopy(t[nid])})

    for nid, tn in t.items():                                  # theirs added
        if nid not in bt and nid not in m:
            out_nodes.append(copy.deepcopy(tn))
            ops.append({"op": "node_add", "node": copy.deepcopy(tn)})
            changed.append(nid)

    # ── wires ──
    live = {n["id"] for n in out_nodes}
    bt_keys = {_conn_key(c) for c in base_theirs.get("connections", [])}
    t_keys = {_conn_key(c) for c in theirs.get("connections", [])}
    bm_keys = {_conn_key(c) for c in base_mine.get("connections", [])}
    removed_t = bt_keys - t_keys
    conns = [copy.deepcopy(c) for c in mine.get("connections", [])
             if _conn_key(c) not in removed_t]
    for k in removed_t:
        ops.append({"op": "conn_del", "key": list(k)})
        if k[2] in live:
            changed.append(k[2])
    have = {_conn_key(c) for c in conns}
    mine_added_into = {(k[2], k[3]) for k in have - bm_keys}
    by_id = {n["id"]: n for n in out_nodes}
    for c in theirs.get("connections", []):
        k = _conn_key(c)
        if k in bt_keys or k in have:
            continue
        if (k[2], k[3]) in mine_added_into and _single_input(by_id, k[2], k[3]):
            conflicts.append({"id": k[2], "kind": "wire", "label": _label(by_id[k[2]]),
                              "fields": [{"field": "input." + k[3], "mine": "your wire",
                                          "theirs": f"{k[0]}.{k[1]}"}],
                              "theirs_node": None, "theirs_connection": copy.deepcopy(c)})
            continue
        conns.append(copy.deepcopy(c))
        have.add(k)
        ops.append({"op": "conn_add", "conn": copy.deepcopy(c)})
        changed.append(k[2])
    conns = [c for c in conns if c.get("from_node") in live and c.get("to_node") in live]

    # ── group boxes (editor-only): theirs only if the human left them alone ──
    groups = mine.get("groups") or []
    tg, btg = theirs.get("groups") or [], base_theirs.get("groups") or []
    if json.dumps(tg, sort_keys=True) != json.dumps(btg, sort_keys=True):
        if json.dumps(groups, sort_keys=True) == json.dumps(base_mine.get("groups") or [], sort_keys=True):
            groups = copy.deepcopy(tg)
            ops.append({"op": "groups", "value": copy.deepcopy(tg)})

    graph = {k: v for k, v in mine.items() if k not in ("nodes", "connections", "groups")}
    graph["nodes"] = out_nodes
    graph["connections"] = conns
    if groups:
        graph["groups"] = groups

    out = {
        "graph": graph,
        "conflicts": conflicts,
        "changed": sorted(set(changed) & live),
        "ops": ops,
        "renamed": renamed,
    }
    return out


def rebase(base_mine: dict, base_theirs: dict, theirs: dict) -> dict:
    """The editor's base with theirs applied — what 'saved' means after a merge."""
    return merge3(base_mine, base_mine, base_theirs, theirs)["graph"]
