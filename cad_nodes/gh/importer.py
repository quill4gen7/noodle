"""A Grasshopper definition translated 1:1 into a noodle graph.

One GH component -> one noodle node, following the table in `components.py`
(`COMPONENTS: {type_guid: GhMap}`). This module owns only the TRANSLATION
rules — what a kind means, how a wire, a persistent value or a flag crosses
over — and never guesses a mapping: a component missing from the table, a
noodle type missing from the catalog, an output or a socket the map does not
name, all end up in the report and the graph is still built around them.

Kinds (GhMap.kind):

- `alias` / `new`  a node of type `GhMap.noodle`. `inputs` / `outputs` map the
  GH param's FULL name ("Start Point") to a noodle socket; `params` maps the GH
  inputs that are widgets in noodle. A persistent value on an unwired input goes
  to its param (via `params`, else the same-named param of the socket); when
  there is no such param it becomes an `Input` node wired into the socket.
- `slider`     NumberSlider / IntegerSlider (digits 0), the GH range as its
  drag window, the nickname as its title — so it lands in the input panel.
- `panel`      wired in -> `Display`; not wired -> `Input` with the text.
- `container`  a floating Curve / Brep / Number… param. Wired: it vanishes and
  downstream is wired straight to its sources. Holding values: an `Input`
  (numbers, points, text) or a CodeBlock of exact B-splines (Rhino curves).
- `skip`       nothing (Group, Scribble are read separately: groups / Note).

Wires: `Source` on the receiving side names an upstream OUTPUT; its GH name
goes through the upstream map's `outputs`. Several sources into one input stay
several connections (noodle's multi-connect = GH's fan-in).

Flags on an input: `flatten` -> a ListFlatten in between, `reverse` -> a
ListReverse, `degrees` -> the node's `degrees` param, `reparameterize` -> its
`reparameterize` param. `graft` is noodle's default fan-out and adds nothing;
anything with no counterpart is reported.

Ids: GH object #k becomes node `n<k>` (the `#k` of gh_report), inserted helper
nodes `n<k>_<socket>_<what>`, so a line of the report and a node of the graph
name each other. Every node carries `params._gh = {k, guid, type, nick}`.
Disabled (Locked) components are left out, as GH computes nothing for them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .definition import Definition, GhObject, GhValue, Param

# GH canvas units are px of a much denser canvas than litegraph's; this keeps
# the GH picture recognisable. `arrange()` is the real layout.
POS_SCALE = 2.5


@dataclass
class ImportResult:
    graph: dict
    issues: list[str] = field(default_factory=list)
    unmapped: dict[str, list[int]] = field(default_factory=dict)  # "Name guid" -> [k]

    def report(self) -> str:
        out = [f"{len(self.graph['nodes'])} nodes, {len(self.graph['connections'])} wires, "
               f"{len(self.issues)} issues"]
        if self.unmapped:
            out.append("not in the table:")
            out += [f"  {key}: " + " ".join(f"#{k}" for k in ks)
                    for key, ks in sorted(self.unmapped.items())]
        out += [f"- {i}" for i in self.issues]
        return "\n".join(out)


def _get(m: Any, key: str, default=None):
    if isinstance(m, dict):
        return m.get(key, default)
    return getattr(m, key, default)


def _snake(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def _catalog():
    from cad_nodes import catalog
    return catalog.REGISTRY


def _default_components():
    try:
        from .components import COMPONENTS
    except ImportError:
        return {}
    return COMPONENTS


# A decoder: (object, i, value) -> geometry record or None, where `value` is
# the i-th internalised geometry of the object that carries cached ON_Data.

def decode_with_rhino3dm(o: GhObject, i: int, value: dict) -> Optional[dict]:
    from . import geometry
    if not value.get("on_data") or not geometry.have_rhino3dm():
        return None
    return geometry.decode(value["on_data"])


def decode_from_report(path) -> Callable[[GhObject, int, dict], Optional[dict]]:
    """A decoder over `gh_report.py FILE --geometry OUT.json` — decode once
    where rhino3dm runs (the container), import anywhere."""
    import json
    with open(path) as f:
        cache = json.load(f)

    def decode(o: GhObject, i: int, value: dict) -> Optional[dict]:
        recs = (cache.get(str(o.k)) or {}).get("records") or []
        return recs[i] if i < len(recs) else None
    return decode


def _text_of(v: Any) -> str:
    """A persistent value as one line of an `Input` node (friendly mode, which
    tries JSON first, then "x,y,z" -> Vector)."""
    import json
    if isinstance(v, GhValue) and v.kind == "domain":
        return json.dumps([float(x) for x in v])          # [a, b], not a point
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.10g}"
    if isinstance(v, (list, tuple)):
        return ",".join(_text_of(x) for x in v)
    return str(v)


def _plane_code(v: GhValue) -> str:
    o, x, y = v[0:3], v[3:6], v[6:9]
    z = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
    t = lambda w: "(" + ", ".join(f"{c:.10g}" for c in w) + ")"
    return f"Plane(origin={t(o)}, x_dir={t(x)}, z_dir={t(z)})"


def world_plane(v: GhValue) -> Optional[str]:
    """XY / XZ / YZ when the plane is a world plane through the origin."""
    if any(abs(c) > 1e-9 for c in v[:3]):
        return None
    x, y = v[3:6], v[6:9]
    n = [x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]]
    axis = max(range(3), key=lambda i: abs(n[i]))
    return {0: "YZ", 1: "XZ", 2: "XY"}[axis] if abs(abs(n[axis]) - 1) < 1e-9 else None


def _encode(values: list) -> tuple[str, str]:
    """Values -> (text, mode) of an `Input` node. Planes need build123d mode."""
    if any(isinstance(v, GhValue) and v.kind == "plane" for v in values):
        return "\n".join(_plane_code(v) if isinstance(v, GhValue) and v.kind == "plane"
                         else repr(v) for v in values), "build123d"
    return "\n".join(_text_of(v) for v in values), "friendly"


def _param_value(v: Any, pdef) -> Any:
    """A persistent value as a widget value of `pdef`."""
    if isinstance(v, GhValue):
        if v.kind == "plane" and pdef.type == "select":
            return world_plane(v)
        return [float(x) for x in v]
    return v


class _Importer:
    def __init__(self, d: Definition, components, decode, registry):
        self.d = d
        self.table = components
        self.decode = decode
        self.reg = registry
        self.nodes: dict[str, dict] = {}
        self.conns: list[dict] = []
        self.issues: list[str] = []
        self.unmapped: dict[str, list[int]] = {}
        # (k, output j) -> [(node id, socket)] : where that GH output lives in noodle.
        # A list because a wired container stands for ALL its sources.
        self.out_at: dict[tuple[int, int], list[tuple[str, str]]] = {}
        self.pending: list[tuple[GhObject, str]] = []   # (object, node id) to wire
        self.shown = d.shown()
        self.consumed = {a for o in d.objects for p in o.inputs for a, _b in p.wires}
        self.dead: list[int] = []
        self.claimed: dict[str, set[str]] = {}
        # (node id, GH input name) -> (node id, socket): where that input really goes
        self.redirect: dict[tuple[str, str], tuple[str, str]] = {}  # node id -> GH inputs a per-type rule took

    # ------------------------------------------------------------ helpers
    def issue(self, o: GhObject | None, msg: str):
        self.issues.append((f"{o.label}: " if o else "") + msg)

    def node(self, nid: str, ntype: str, o: GhObject | None, params=None, title=None,
             dx: float = 0.0) -> dict:
        pos = (o.pos if o and o.pos else [0.0, 0.0])
        n = {"id": nid, "type": ntype, "params": dict(params or {}),
             "position": [pos[0] * POS_SCALE + dx, pos[1] * POS_SCALE], "parent": None}
        if o is not None:
            n["params"]["_gh"] = {"k": o.k, "guid": o.guid, "type": o.type, "nick": o.nick}
        if title:
            n["title"] = title
        self.nodes[nid] = n
        return n

    def wire(self, src: tuple[str, str], dst: tuple[str, str]):
        self.conns.append({"id": f"l{len(self.conns) + 1}", "from_node": src[0],
                           "from_socket": src[1], "to_node": dst[0], "to_socket": dst[1]})

    def first_output(self, ntype: str) -> str:
        nd = self.reg.get(ntype)
        return nd.outputs[0].name if nd and nd.outputs else "result"

    def input_node(self, nid: str, o: GhObject, values: list, title=None) -> tuple[str, str]:
        text, mode = _encode(values)
        self.node(nid, "Input", o, {"text": text, "mode": mode}, title=title, dx=-260)
        return (nid, self.first_output("Input"))

    # ------------------------------------------------------------ objects
    def run(self) -> ImportResult:
        objs = self.d.objects
        for o in objs:
            if o.type == "Group":
                continue
            if o.type == "Scribble":
                if o.text:
                    self.node(f"n{o.k}", "Note", o, {"text": o.text})
                continue
            if o.locked:
                self.issue(o, "disabled in GH, left out (downstream gets nothing)")
                continue
            m = self.table.get(o.type_guid)
            if m is None:
                self.unmapped.setdefault(f"{o.type} {o.type_guid}", []).append(o.k)
                continue
            getattr(self, "_" + _get(m, "kind", "skip"), self._unknown_kind)(o, m)
        for o, nid in self.pending:
            self._wire_inputs(o, nid, self.table[o.type_guid])
        self._show_sources()
        self._custom_previews()
        self._check_wires()
        if self.dead:
            self.issues.append("left out, hidden and read by nobody: "
                               + " ".join(f"#{k}" for k in self.dead))
        return ImportResult(
            graph={"name": self.d.name.rsplit(".", 1)[0] or "gh-import",
                   "nodes": list(self.nodes.values()), "connections": self.conns,
                   "groups": self._groups()},
            issues=self.issues, unmapped=self.unmapped)

    def _unknown_kind(self, o, m):
        self.issue(o, f"unknown kind {_get(m, 'kind')!r} in the table")

    def _skip(self, o, m):
        pass

    def _slider(self, o, m):
        s = o.slider or {}
        integer = s.get("digits") == 0 or s.get("interval") in (1, 2, 3)  # int / even / odd
        ntype = _get(m, "noodle") or ("IntegerSlider" if integer else "NumberSlider")
        lo, hi, v = s.get("min"), s.get("max"), s.get("value")
        params = {"value": int(round(v)) if integer and v is not None else v}
        if lo is not None and hi is not None:
            params["_ui"] = {"value": {"min": lo, "max": hi}}
        nid = f"n{o.k}"
        self.node(nid, ntype, o, params, title=o.nick or None)
        self.out_at[(o.k, 0)] = [(nid, self.first_output(ntype))]

    def _panel(self, o, m):
        nid = f"n{o.k}"
        if o.inputs and o.inputs[0].wires:
            self.node(nid, "Display", o, title=o.nick if o.nick != "Panel" else None)
            self.pending.append((o, nid))
            self.out_at[(o.k, 0)] = [(nid, self.first_output("Display"))]
        else:
            lines = (o.text or "").splitlines() or [""]
            self.out_at[(o.k, 0)] = [self.input_node(nid, o, lines,
                                                     title=o.nick if o.nick != "Panel" else None)]
            self.nodes[nid]["position"][0] += 260

    def _container(self, o, m):
        p = o.inputs[0] if o.inputs else None
        if o.hidden and o.k not in self.consumed:
            self.dead.append(o.k)        # scaffolding: drawn by nobody, read by nobody
            return
        if p is not None and p.wires:
            # resolved lazily: the sources may come later in the file
            self.out_at[(o.k, 0)] = [("@", f"{a}:{b}") for a, b in p.wires]
            return
        if p is None or not p.data:
            self.issue(o, "empty parameter (no wire, no value)")
            return
        nid = f"n{o.k}"
        geo = [v for v in p.data if isinstance(v, dict) and "geometry" in v]
        if not geo:
            self.out_at[(o.k, 0)] = [self.input_node(nid, o, p.data, title=o.nick or None)]
            self.nodes[nid]["position"][0] += 260
            return
        cached = [v for v in geo if v.get("on_data")]
        recs = [self.decode(o, i, v) for i, v in enumerate(cached)]
        recs += [None] * (len(geo) - len(cached))     # a reference with no cache
        curves = [r for r in recs if r and r.get("kind") == "curve"]
        if len(curves) != len(geo):
            kinds = sorted({(r or {}).get("kind", "undecoded") for r in recs})
            self.issue(o, f"Rhino geometry not rebuilt ({', '.join(kinds)}): "
                          "only curves are, and they need rhino3dm")
            if not curves:
                return
        from .geometry import curve_code
        self.node(nid, "CodeBlock", o, {"code": curve_code(curves, o.nick or o.type)},
                  title=o.nick or o.type)
        self.nodes[nid]["preview"] = o in self.shown
        self.out_at[(o.k, 0)] = [(nid, "result")]

    def _alias(self, o, m):
        ntype = _get(m, "noodle")
        nd = self.reg.get(ntype)
        if nd is None:
            self.issue(o, f"noodle type {ntype!r} is not in the catalog")
            return
        nid = f"n{o.k}"
        n = self.node(nid, ntype, o, title=o.nick if o.nick and o.nick != o.type else None)
        self._eye(o, n)
        outs = _get(m, "outputs") or {}
        for j, p in enumerate(o.outputs):
            sock = outs.get(p.name)
            if sock is None:
                continue
            if nd.output(sock) is None:
                self.issue(o, f"output {p.name!r} -> {sock!r}: no such socket on {ntype}")
                continue
            self.out_at[(o.k, j)] = [(nid, sock)]
        self.pending.append((o, nid))
        extra = _EXTRAS.get(o.type)
        if extra is not None:
            self.claimed[nid] = extra(self, o, n) or set()

    _new = _alias

    def _eye(self, o: GhObject, n: dict):
        """GH draws every enabled, non-hidden object that holds geometry
        (Definition.shown); everything else is drawn by nobody."""
        nd = self.reg.get(n["type"])
        if nd is None or all(s.wire_type == "data" for s in nd.outputs):
            return
        n["preview"] = o in self.shown

    def _show_sources(self):
        """A wired container GH draws vanished on import: draw its sources."""
        for o in self.shown:
            m = self.table.get(o.type_guid)
            if m is None or _get(m, "kind") != "container" or (o.k, 0) not in self.out_at:
                continue
            for nid, _s in self.sources_of(o.k, 0) or []:
                if nid in self.nodes:
                    self.nodes[nid]["preview"] = True

    # ------------------------------------------------------------ wires
    def sources_of(self, k: int, j: int, seen=()) -> list[tuple[str, str]] | None:
        at = self.out_at.get((k, j))
        if at is None:
            return None
        res = []
        for nid, sock in at:
            if nid != "@":
                res.append((nid, sock))
                continue
            a, b = map(int, sock.split(":"))
            if (a, b) in seen:
                continue
            sub = self.sources_of(a, b, seen + ((k, j),))
            if sub is None:
                self._missing_output(a, b)
            else:
                res += sub
        return res

    def _missing_output(self, a: int, b: int):
        up = self.d.objects[a]
        if up.locked or up.type_guid not in self.table:
            return              # already reported once, as disabled / unmapped
        if _get(self.table[up.type_guid], "kind") in ("container", "skip"):
            return              # an empty param (reported) / skipped on purpose
        name = up.outputs[b].name if b < len(up.outputs) else f"#{b}"
        self.issue(up, f"output {name!r} is used downstream but not mapped")

    def _wire_inputs(self, o: GhObject, nid: str, m):
        n = self.nodes[nid]
        nd = self.reg.get(n["type"])
        ins = _get(m, "inputs") or {}
        pmap = _get(m, "params") or {}
        if _get(m, "kind") == "panel":
            ins = {o.inputs[0].name: nd.inputs[0].name}
        for p in o.inputs:
            sock = ins.get(p.name)
            tnid, tnd = nid, nd
            if (nid, p.name) in self.redirect:      # a per-type rule put a node in front
                tnid, sock = self.redirect[(nid, p.name)]
                tnd = self.reg[self.nodes[tnid]["type"]]
            if p.wires:
                if sock is None or tnd.input(sock) is None:
                    self.issue(o, f"input {p.name!r} is wired but "
                                  + ("not mapped" if sock is None
                                     else f"{self.nodes[tnid]['type']} has no socket {sock!r}"))
                    continue
                srcs = []
                for a, b in p.wires:
                    s = self.sources_of(a, b)
                    if s is None:
                        self._missing_output(a, b)
                    else:
                        srcs += s
                for s in self._through_flags(o, tnid, p, sock, srcs, tnd):
                    self.wire(s, (tnid, sock))
            elif p.data and p.name not in self.claimed.get(nid, ()):
                self._persistent(o, nid, p, sock, pmap.get(p.name), nd)
            self._param_flags(o, n, p, nd)

    def _merge(self, nid, sock, srcs):
        """Several GH sources into one input are ONE list in GH. noodle keeps
        that only on an item-access input (fan-out) or a `multiple` one: a
        plain list_access input reads its first wire and drops the rest
        silently. So those get a ListCreate collecting every source."""
        hid = f"{nid}_{sock}_merge"
        self.node(hid, "ListCreate", None)
        x, y = self.nodes[nid]["position"]
        self.nodes[hid]["position"] = [x - 220, y + 40]
        for s in srcs:
            self.wire(s, (hid, "items"))
        return [(hid, self.first_output("ListCreate"))]

    def _through_flags(self, o, nid, p, sock, srcs, nd):
        target = nd.input(sock)
        many = len(srcs) > 1
        if many and (("flatten" in p.flags or "reverse" in p.flags)
                     or (target.list_access and not target.multiple)):
            srcs = self._merge(nid, sock, srcs)       # ListCreate also flattens
        for flag, helper in (("flatten", "ListFlatten"), ("reverse", "ListReverse")):
            if flag == "flatten" and many:
                continue
            if flag in p.flags and srcs:
                hid = f"{nid}_{sock}_{flag}"
                self.node(hid, helper, None)
                self.nodes[hid]["position"] = [self.nodes[nid]["position"][0] - 200,
                                               self.nodes[nid]["position"][1]]
                hin = self.reg[helper].inputs[0].name
                for s in srcs:
                    self.wire(s, (hid, hin))
                srcs = [(hid, self.first_output(helper))]
        return srcs

    def _persistent(self, o, nid, p, sock, param, nd):
        n = self.nodes[nid]
        target = param or (sock if sock and nd.param(sock) else None)
        values = [v for v in p.data if not (isinstance(v, dict) and "geometry" in v)]
        if len(values) != len(p.data):
            self.issue(o, f"input {p.name!r}: internalised Rhino geometry is not imported")
            return
        if target and len(values) == 1:
            if nd.param(target) is None:
                self.issue(o, f"input {p.name!r} -> param {target!r}: not on {n['type']}")
                return
            v = _param_value(values[0], nd.param(target))
            if v is None:
                self.issue(o, f"input {p.name!r}: plane {_text_of(values[0])} is not a world "
                              f"plane, {n['type']}.{target} cannot hold it")
                return
            n["params"][target] = v
            return
        if sock and nd.input(sock) is not None:
            src = self.input_node(f"{nid}_{sock}_value", None, values)
            self.nodes[src[0]]["position"] = [n["position"][0] - 260, n["position"][1]]
            self.wire(src, (nid, sock))
            return
        if param is None and sock is None:
            self.issue(o, f"input {p.name!r} = {_text_of(values[0])} is not mapped")
            return
        self.issue(o, f"input {p.name!r}: {len(values)} values, nowhere to put them")

    def _param_flags(self, o, n, p, nd):
        for flag in p.flags:
            if flag in ("flatten", "reverse", "graft"):
                continue
            if flag in ("degrees", "reparameterize") and nd.param(flag) is not None:
                n["params"][flag] = True
            elif flag == "invert" and nd.param("invert") is not None:
                n["params"]["invert"] = True
            else:
                self.issue(o, f"input {p.name!r}: flag [{flag}] has no counterpart")

    def _check_wires(self):
        """Drop — and report — every wire noodle's types refuse, so the graph
        always loads: a GH wire is untyped, a noodle one is not, and one bad
        wire makes validate() reject the whole graph."""
        if self.reg is not _catalog():
            return              # a stand-in registry: validate() knows nothing of it
        from cad_nodes.graph import Graph
        from cad_nodes.casts import wires_compatible
        while True:
            g = Graph.from_dict({"name": "x", "nodes": list(self.nodes.values()),
                                 "connections": self.conns})
            bad = None
            for c in g.connections:
                inp = self.reg[self.nodes[c.to_node]["type"]].input(c.to_socket)
                if inp is None:
                    continue
                t = g.effective_output_type(c.from_node, c.from_socket)
                if not wires_compatible(t, inp.wire_type) and t not in (inp.accepts or []):
                    bad = (c, t, inp.wire_type)
                    break
            if bad is None:
                return
            c, t, w = bad
            self.conns = [x for x in self.conns if x["id"] != c.id]
            src, dst = self.nodes[c.from_node], self.nodes[c.to_node]
            k = lambda n: f"#{n['params']['_gh']['k']}" if "_gh" in n["params"] else n["id"]
            self.issues.append(f"wire {k(src)} {src['type']}.{c.from_socket} ({t}) -> "
                               f"{k(dst)} {dst['type']}.{c.to_socket} ({w}): "
                               "types refused by noodle, dropped")

    # ------------------------------------------------------------ display
    def _custom_previews(self):
        """Custom Preview -> the eye on what it draws, coloured if the colour
        is a plain value. It is a display in GH, not a node in noodle."""
        for o in self.d.objects:
            if o.type != "Custom Preview" or o.locked:
                continue
            m = self.table.get(o.type_guid)
            if m is not None and _get(m, "kind") != "skip":
                continue           # the table gave it a node of its own
            geo = next((p for p in o.inputs if p.name == "Geometry"), None)
            mat = next((p for p in o.inputs if p.name in ("Material", "Shader")), None)
            colour = None
            data = mat.data if mat is not None else None
            if mat is not None and mat.wires:
                # a wire wins over the (stale) value under it; a Shader / Colour
                # param upstream holds the value itself
                up = self.d.objects[mat.wires[0][0]]
                data = up.inputs[0].data if up.floating and up.inputs else None
            if data:
                c = data[0]
                c = c.get("diffuse") if isinstance(c, dict) else c   # a material, or a colour
                if isinstance(c, int) and not isinstance(c, bool):
                    colour = f"#{c & 0xFFFFFF:06x}"
            for a, b in (geo.wires if geo else []):
                for nid, _s in self.sources_of(a, b) or []:
                    self.nodes[nid]["preview"] = True
                    if colour:
                        self.nodes[nid]["color"] = colour
            if mat is not None and mat.wires and colour is None:
                self.issue(o, "colour comes from a computed wire: set it by hand (🎨 Aspetto)")

    def _groups(self) -> list[dict]:
        try:
            from cad_nodes.graph import Node
            from cad_nodes.layout import node_box
        except Exception:
            return []
        guid_node = {}
        for n in self.nodes.values():
            g = n["params"].get("_gh")
            if g:
                guid_node[g["guid"]] = n
        out = []
        for o in self.d.objects:
            if o.type != "Group":
                continue
            members = [guid_node[g] for g in o.members if g in guid_node]
            if not members:
                continue
            boxes = [node_box(self.reg[n["type"]], Node.from_dict(n)) for n in members
                     if n["type"] in self.reg]
            if not boxes:
                continue
            x0 = min(b.x for b in boxes) - 24
            y0 = min(b.y for b in boxes) - 50
            x1 = max(b.x + b.w for b in boxes) + 24
            y1 = max(b.y + b.h for b in boxes) + 24
            colour = f"#{(o.colour or 0) & 0xFFFFFF:06x}" if o.colour else "#3f589e"
            out.append({"title": o.nick or "Group", "bounding": [x0, y0, x1 - x0, y1 - y0],
                        "color": colour})
        return out


# ---------------------------------------------------------------- per type
# What a component keeps OUTSIDE its params (a graph mapper's curve) or means
# by an unwired input (Mirror's plane) — rules of GH, not of the table.

def _graph_mapper(imp: _Importer, o: GhObject, n: dict):
    """The curve lives in Container/LocalGraph, its points NORMALISED to the
    graph's Domain (x0..x1 -> y0..y1). noodle's GraphMapper works in [0,1], so
    a domain other than the unit square becomes a Remap on each side:
    Values: Source (if wired, else x0..x1) -> [0,1], then [0,1] -> Target (if
    wired, else y0..y1). That Source/Target REPLACE the domain is a reading of
    the plugin (RichedGraphMapper), to be checked against Grasshopper.

    Curves: Conic (quadratic Bezier (0,0) -> C -> (1,1), degree-elevated: the
    handles sit 2/3 of the way to C), Bezier (4 control points), Linear,
    Polyline (its points, linear). Others are reported."""
    c = o.chunk.sub("Container") if o.chunk else None
    lg = c.sub("LocalGraph") if c else None
    g = lg.sub("Graph") if lg else None
    dom = lg.sub("Domain") if lg else None
    x0, x1, y0, y1 = ([float(dom.item(k)) for k in ("x0", "x1", "y0", "y1")]
                      if dom is not None else [0.0, 1.0, 0.0, 1.0])
    kind = g.item("container_name") if g else None
    P = n["params"]
    if kind == "Conic":
        cx, cy = g.item("Cx"), g.item("Cy")
        P["mode"] = "bezier"
        P["curve"] = [
            {"p": [0.0, 0.0], "hi": None, "ho": [2 * cx / 3, 2 * cy / 3]},
            {"p": [1.0, 1.0], "hi": [1 + 2 * (cx - 1) / 3, 1 + 2 * (cy - 1) / 3], "ho": None}]
    elif kind == "Bezier":
        xs, ys = g.all("cpx"), g.all("cpy")
        P["mode"] = "bezier"
        P["curve"] = [{"p": [xs[0], ys[0]], "hi": None, "ho": [xs[1], ys[1]]},
                      {"p": [xs[3], ys[3]], "hi": [xs[2], ys[2]], "ho": None}]
    elif kind == "Linear":
        P["mode"] = "linear"
    elif kind == "Polyline":
        P["mode"] = "linear"
        P["curve"] = [{"p": [pt[0], pt[1]], "hi": None, "ho": None} for pt in g.all("pt")]
        imp.issue(o, "Polyline graph: points read as normalised to the domain — check")
    else:
        imp.issue(o, f"graph type {kind!r} not translated (Conic, Bezier, Linear, Polyline are)")

    ins = {p.name: p for p in o.inputs}
    nid = n["id"]
    x, y = n["position"]

    def side(name, lo, hi, where):
        p = ins.get(name)
        wired = p is not None and bool(p.wires)
        if not wired and (lo, hi) == (0.0, 1.0):
            return None
        rid = f"{nid}_{where}"
        params = ({"source_min": lo, "source_max": hi, "target_min": 0.0, "target_max": 1.0}
                  if where == "in" else
                  {"source_min": 0.0, "source_max": 1.0, "target_min": lo, "target_max": hi})
        imp.node(rid, "Remap", None, params, title=f"{o.nick or o.type} {name}")
        imp.nodes[rid]["position"] = [x + (-240 if where == "in" else 240), y]
        if wired:
            imp.redirect[(nid, name)] = (rid, "source" if where == "in" else "target")
        return rid

    rin = side("Source", x0, x1, "in")
    rout = side("Target", y0, y1, "out")
    if rin:
        imp.redirect[(nid, "Values")] = (rin, "value")
        imp.wire((rin, "result"), (nid, "t"))
    if rout:
        imp.wire((nid, "result"), (rout, "value"))
        imp.out_at[(o.k, 0)] = [(rout, "result")]
    return {"Source", "Target"}


def _mirror(imp: _Importer, o: GhObject, n: dict):
    """GH mirrors in the YZ plane when P is unwired; noodle's default is XZ.
    An internalised plane is read by its normal."""
    p = next((p for p in o.inputs if p.name == "Plane"), None)
    if p is None or p.wires:
        return {"Plane"}
    name = "YZ"
    if p.data:
        name = world_plane(p.data[0])
        if name is None:
            imp.issue(o, "internalised plane is not a world plane through the origin")
            return {"Plane"}
    n["params"]["plane"] = name
    return {"Plane"}


_EXTRAS = {"Rich Graph Mapper": _graph_mapper, "Mirror": _mirror}


def import_definition(d: Definition, components=None,
                      decode: Callable[[GhObject, int, dict], Optional[dict]] = decode_with_rhino3dm,
                      registry=None) -> ImportResult:
    """Translate `d` with the GH->noodle table (default: components.COMPONENTS).

    `decode` turns an internalised geometry value into a geometry record; the
    default needs rhino3dm, `decode_from_report()` reads one decoded earlier.
    """
    return _Importer(d, _default_components() if components is None else components,
                     decode, _catalog() if registry is None else registry).run()
