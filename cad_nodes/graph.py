"""
Graph model: Node, Connection, Graph + JSON (de)serialisation + validation.

Graph JSON shape:
{
  "name": "flange",
  "nodes": [
    {"id": "n1", "type": "Circle", "params": {"radius": 20}, "position": [0, 0],
     "parent": null},
    ...
  ],
  "connections": [
    {"id": "c1", "from_node": "n1", "from_socket": "result",
     "to_node": "n2", "to_socket": "sketch"},
    ...
  ]
}

`parent` (optional) links a node to a group node (BuildPart/BuildSketch) for
builder-mode nesting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import catalog
from .catalog import wires_compatible


@dataclass
class Node:
    id: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    position: tuple[float, float] = (0.0, 0.0)
    parent: Optional[str] = None  # id of a group node, if nested
    # Per-node preview eye: None=auto (show terminals only), True=on, False=off.
    preview: Optional[bool] = None
    # Bypassed (Grasshopper/ComfyUI style): node is skipped, its first matching
    # geometry input passes straight through to its output.
    bypassed: bool = False
    # Display-only (don't affect geometry): preview colour (hex) and wireframe.
    color: Optional[str] = None
    wireframe: bool = False
    # UX-only: names of inputs whose "multi" (+) toggle is on, so the editor
    # restores their extra/spare connection slots on reload. Ignored by the
    # engine (several connections to one socket already fan out by themselves).
    multi: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "params": self.params,
            "position": list(self.position),
            "parent": self.parent,
        }
        if self.preview is not None:   # only serialise explicit on/off
            d["preview"] = self.preview
        if self.bypassed:
            d["bypassed"] = True
        if self.color:
            d["color"] = self.color
        if self.wireframe:
            d["wireframe"] = True
        if self.multi:
            d["multi"] = list(self.multi)
        return d

    @staticmethod
    def from_dict(d: dict) -> "Node":
        pos = d.get("position", [0, 0])
        return Node(
            id=d["id"],
            type=d["type"],
            params=dict(d.get("params", {})),
            position=(pos[0], pos[1]) if pos else (0.0, 0.0),
            parent=d.get("parent"),
            preview=d.get("preview"),
            bypassed=bool(d.get("bypassed", False)),
            color=d.get("color"),
            wireframe=bool(d.get("wireframe", False)),
            multi=list(d.get("multi", [])),
        )


@dataclass
class Connection:
    id: str
    from_node: str
    from_socket: str
    to_node: str
    to_socket: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_node": self.from_node,
            "from_socket": self.from_socket,
            "to_node": self.to_node,
            "to_socket": self.to_socket,
        }

    @staticmethod
    def from_dict(d: dict) -> "Connection":
        return Connection(
            id=d["id"],
            from_node=d["from_node"],
            from_socket=d["from_socket"],
            to_node=d["to_node"],
            to_socket=d["to_socket"],
        )


class ValidationError(Exception):
    pass


@dataclass
class Graph:
    name: str = "untitled"
    nodes: list[Node] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    # Opaque editor-only metadata: LiteGraph group boxes (title/bounding/color)
    # that visually cluster nodes. The engine never reads them; they are carried
    # through so logical grouping survives save/reload and api/copilot edits.
    groups: list[dict] = field(default_factory=list)

    # -- lookups -----------------------------------------------------------
    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"No node {node_id!r} in graph")

    def edges(self) -> list[tuple[str, str]]:
        return [(c.from_node, c.to_node) for c in self.connections]

    def inputs_of(self, node_id: str) -> dict[str, list[tuple[str, str]]]:
        """socket_name -> list of (from_node_id, from_socket) feeding it."""
        result: dict[str, list[tuple[str, str]]] = {}
        for c in self.connections:
            if c.to_node == node_id:
                result.setdefault(c.to_socket, []).append((c.from_node, c.from_socket))
        return result

    def children_of(self, group_id: str) -> list[Node]:
        return [n for n in self.nodes if n.parent == group_id]

    def effective_output_type(self, node_id: str, socket_name: str | None = None,
                              _seen: set | None = None) -> str:
        """The runtime wire type of a node's output, resolving type-preserving
        nodes (`NodeDef.output_follows`) up the chain — e.g. a curve moved/rotated
        stays a curve, so Move's first output mirrors its `shape` input. Non-first
        outputs and non-following nodes return their static socket type."""
        try:
            node = self.node(node_id)
            ndef = catalog.get(node.type)
        except (KeyError, Exception):
            return catalog.WIRE_DATA
        out = ndef.output(socket_name) if socket_name else (
            ndef.outputs[0] if ndef.outputs else None)
        static = out.wire_type if out else catalog.WIRE_DATA
        first = ndef.outputs[0].name if ndef.outputs else None
        follows = getattr(ndef, "output_follows", None)
        if follows and out is not None and (socket_name is None or socket_name == first):
            _seen = _seen or set()
            if node_id in _seen:        # cycle guard (graph should be a DAG anyway)
                return static
            _seen.add(node_id)
            for c in self.connections:  # the source feeding the followed input
                if c.to_node == node_id and c.to_socket == follows:
                    return self.effective_output_type(c.from_node, c.from_socket, _seen)
            return static               # followed input unconnected -> static
        return static

    def effective_output_subtype(self, node_id: str, socket_name: str | None = None,
                                 _seen: set | None = None) -> str:
        """The advisory sub-type tag of a node's output (number/integer/list/
        line/spline…), resolving pass-throughs that preserve the tag
        (`NodeDef.subtype_follows`, defaulting to `output_follows`) up the chain —
        e.g. a Spline through a Curve container or a Move stays a `spline`. Legend
        only; never gates a connection. Returns '' when untagged."""
        try:
            node = self.node(node_id)
            ndef = catalog.get(node.type)
        except (KeyError, Exception):
            return ""
        out = ndef.output(socket_name) if socket_name else (
            ndef.outputs[0] if ndef.outputs else None)
        static = (out.subtype if out else "") or ""
        if static:
            return static
        first = ndef.outputs[0].name if ndef.outputs else None
        if socket_name is not None and socket_name != first:
            return ""                   # only the first output follows
        # a container in `transform` mode emits freshly-extracted shapes — a new
        # kind, so drop the inherited tag (filter keeps it, as a pass-through).
        if ndef.category == "container" and (node.params or {}).get("mode") == "transform":
            return ""
        follows = getattr(ndef, "subtype_follows", None) or getattr(ndef, "output_follows", None)
        if not follows:
            return ""
        _seen = _seen or set()
        if node_id in _seen:            # cycle guard (graph should be a DAG anyway)
            return ""
        _seen.add(node_id)
        for c in self.connections:      # the source feeding the followed input
            if c.to_node == node_id and c.to_socket == follows:
                return self.effective_output_subtype(c.from_node, c.from_socket, _seen)
        return ""                       # followed input unconnected -> untagged

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "nodes": [n.to_dict() for n in self.nodes],
            "connections": [c.to_dict() for c in self.connections],
        }
        if self.groups:            # only serialise when present
            d["groups"] = self.groups
        return d

    @staticmethod
    def from_dict(d: dict) -> "Graph":
        return Graph(
            name=d.get("name", "untitled"),
            nodes=[Node.from_dict(n) for n in d.get("nodes", [])],
            connections=[Connection.from_dict(c) for c in d.get("connections", [])],
            groups=list(d.get("groups", [])),
        )

    # -- validation --------------------------------------------------------
    def validate(self) -> list[str]:
        """
        Raise ValidationError on hard problems (unknown type/socket, type
        mismatch, duplicate ids). Return a list of soft warnings (e.g. missing
        required inputs) that don't prevent transpilation.
        """
        warnings: list[str] = []

        seen_ids: set[str] = set()
        for n in self.nodes:
            if n.id in seen_ids:
                raise ValidationError(f"Duplicate node id: {n.id}")
            seen_ids.add(n.id)
            if n.type not in catalog.REGISTRY:
                raise ValidationError(f"Unknown node type {n.type!r} (node {n.id})")
            if n.parent is not None and n.parent not in {x.id for x in self.nodes}:
                raise ValidationError(f"Node {n.id} has unknown parent {n.parent!r}")

        node_by_id = {n.id: n for n in self.nodes}

        for c in self.connections:
            if c.from_node not in node_by_id:
                raise ValidationError(f"Connection {c.id}: unknown from_node {c.from_node!r}")
            if c.to_node not in node_by_id:
                raise ValidationError(f"Connection {c.id}: unknown to_node {c.to_node!r}")

            src_def = catalog.get(node_by_id[c.from_node].type)
            dst_def = catalog.get(node_by_id[c.to_node].type)

            out = src_def.output(c.from_socket)
            inp = dst_def.input(c.to_socket)
            if out is None:
                raise ValidationError(
                    f"Connection {c.id}: node {c.from_node} ({src_def.type}) "
                    f"has no output {c.from_socket!r}")
            if inp is None:
                # A CodeBlock exposes a per-instance input socket for each of its
                # declared `#@param`s (params-as-inputs); these aren't in the
                # static catalog def, so accept them as universal `data` sinks.
                if dst_def.type == "CodeBlock":
                    from .transpiler import parse_codeblock_params  # lazy: avoid cycle
                    decls = parse_codeblock_params(
                        node_by_id[c.to_node].params.get("code", ""))
                    if any(d["name"] == c.to_socket for d in decls):
                        continue
                raise ValidationError(
                    f"Connection {c.id}: node {c.to_node} ({dst_def.type}) "
                    f"has no input {c.to_socket!r}")
            src_type = self.effective_output_type(c.from_node, c.from_socket)
            if (not wires_compatible(src_type, inp.wire_type)
                    and src_type not in (inp.accepts or [])):
                raise ValidationError(
                    f"Connection {c.id}: incompatible wire types "
                    f"{src_type} -> {inp.wire_type}")

        # Soft: required inputs that are unconnected.
        for n in self.nodes:
            ndef = catalog.get(n.type)
            connected = set(self.inputs_of(n.id).keys())
            for s in ndef.inputs:
                if s.required and s.name not in connected:
                    warnings.append(
                        f"Node {n.id} ({n.type}): required input {s.name!r} is unconnected")

        return warnings
