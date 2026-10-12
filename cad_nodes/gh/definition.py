"""A Grasshopper definition as a flat graph: components, their values, the wires.

`archive.py` gives the raw chunk tree; this module reads it the way Grasshopper
means it:

- `DefinitionObjects/Object#k` is one canvas object. Its `GUID` is the
  component TYPE (the same for every Number Slider in the world), its
  `Container/InstanceGuid` is THIS instance.
- A component's inputs/outputs are `param_input#i` / `param_output#i`, except
  for variable-parameter components (List Item, Addition…), which keep them
  under `ParameterData/InputParam#i` / `OutputParam#i`. Missing that second
  form leaves those components with no inputs and their wires dangling.
- A floating parameter (Number Slider, Panel, a Curve/Brep container…) is its
  own single input AND output: the Container carries `Source[i]` and the
  instance guid is what downstream wires point at.
- A wire is stored on the RECEIVING side only: `Source[i]` = the InstanceGuid of
  the upstream output parameter. A source naming nothing (the upstream was
  deleted) is dropped, as Grasshopper drops it on load.
- `Hidden` = preview off, `Locked` = disabled. What Grasshopper DRAWS is every
  enabled, non-hidden object with geometry — that is how you tell the parts of a
  definition that matter from the scaffolding.
- Geometry referenced from Rhino (`RefID`) is cached in the file as `ON_Data`,
  so a definition is readable without its .3dm. `geometry.py` decodes it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .archive import Chunk, load, parse

MAPPING = {0: None, 1: "flatten", 2: "graft"}


@dataclass
class Param:
    name: str
    nick: str
    guid: str
    sources: list[str] = field(default_factory=list)       # upstream param guids
    wires: list[tuple[int, int]] = field(default_factory=list)  # (object k, output j)
    data: list[Any] | None = None       # persistent (internalised) values, flattened
    paths: list[str] = field(default_factory=list)          # their tree branch paths
    flags: list[str] = field(default_factory=list)          # flatten/graft/reverse/simplify
    expression: str | None = None


@dataclass
class GhObject:
    k: int
    type: str                  # "Number Slider", "Extrude"… (component Name)
    type_guid: str
    nick: str
    guid: str                  # instance guid
    pos: list[float] | None
    hidden: bool
    locked: bool
    inputs: list[Param] = field(default_factory=list)
    outputs: list[Param] = field(default_factory=list)
    floating: bool = False
    slider: dict | None = None
    text: str | None = None            # Panel / Scribble text
    members: list[str] = field(default_factory=list)   # Group: instance guids
    colour: int | None = None
    chunk: Chunk | None = None

    @property
    def label(self) -> str:
        nk = self.nick if self.nick and self.nick != self.type else ""
        return f"#{self.k} {self.type}" + (f" '{nk}'" if nk else "")


@dataclass
class Definition:
    name: str
    plugin_version: list[int] | None
    libraries: list[str]
    objects: list[GhObject]

    def by_guid(self) -> dict[str, GhObject]:
        return {o.guid: o for o in self.objects}

    def shown(self) -> list[GhObject]:
        """What Grasshopper previews: enabled, not hidden, carrying geometry."""
        return [o for o in self.objects
                if not o.hidden and not o.locked and o.type not in _NO_GEOMETRY]


# Objects that never draw anything, whatever their Hidden flag says.
_NO_GEOMETRY = {
    "Group", "Scribble", "Panel", "Number Slider", "Boolean Toggle", "Number",
    "Integer", "Construct Domain", "Deconstruct Domain", "Construct Domain²",
    "Deconstruct Domain²", "Addition", "Subtraction", "Multiplication",
    "Division", "Negative", "Radians", "Average", "Maximum", "Minimum",
    "Length", "Area", "Volume", "Bounds", "Remap Numbers", "List Item",
    "List Length", "Sort List", "Dispatch", "Series", "Range", "Rich Graph Mapper",
    "Graph Mapper", "Custom Preview", "Unit X", "Unit Y", "Unit Z", "Vector XYZ",
}


def _param(c: Chunk) -> Param:
    p = Param(name=c.item("Name", default=""), nick=c.item("NickName", default=""),
              guid=c.item("InstanceGuid", default=""), sources=c.all("Source"))
    m = MAPPING.get(c.item("Mapping", default=0))
    if m:
        p.flags.append(m)
    for flag in ("Reverse", "Simplify"):
        if c.item(flag):
            p.flags.append(flag.lower())
    p.expression = c.item("InternalExpression") or None
    pd = c.sub("PersistentData")
    if pd is not None:
        p.data = []
        for br in pd.subs("Branch"):
            for it in br.subs("Item"):
                p.paths.append(br.item("Path", default="{0}"))
                p.data.append(_item_value(it))
    return p


def _item_value(it: Chunk) -> Any:
    """One persistent-data item: a plain value, or a geometry record."""
    blob = it.item("ON_Data")
    if blob is not None or it.item("RefID") is not None:
        return {"geometry": it.item("TypeName"), "ref": it.item("RefID"),
                "on_data": blob}
    for n, _i, _t, v in it.items:
        return v
    return None


def from_chunk(root: Chunk) -> Definition:
    dfn = root.sub("Definition")
    libs = [lib.item("Name") for lib in (dfn.sub("GHALibraries") or Chunk("")).subs("Library")]
    props = dfn.sub("DefinitionProperties") or Chunk("")
    objects: list[GhObject] = []
    owner: dict[str, tuple[int, int]] = {}
    for k, ob in enumerate((dfn.sub("DefinitionObjects") or Chunk("")).subs("Object")):
        c = ob.sub("Container") or Chunk("Container")
        att = c.sub("Attributes")
        o = GhObject(k=k, type=ob.item("Name", default="?"), type_guid=ob.item("GUID", default=""),
                     nick=c.item("NickName", default=""), guid=c.item("InstanceGuid", default=""),
                     pos=att.item("Pivot") if att else None,
                     hidden=bool(c.item("Hidden", default=False)),
                     locked=bool(c.item("Locked", default=False)), chunk=ob)
        pdat = c.sub("ParameterData")
        ins = c.subs("param_input") + (pdat.subs("InputParam") if pdat else [])
        outs = c.subs("param_output") + (pdat.subs("OutputParam") if pdat else [])
        if not ins and not outs and c.item("SourceCount") is not None:
            o.floating = True
            ins = outs = [c]
        o.inputs = [_param(p) for p in ins]
        o.outputs = [_param(p) for p in outs]
        for j, p in enumerate(o.outputs):
            owner[p.guid] = (k, j)
        owner.setdefault(o.guid, (k, 0))
        s = c.sub("Slider")
        if s is not None:
            o.slider = {"value": s.item("Value"), "min": s.item("Min"), "max": s.item("Max"),
                        "digits": s.item("Digits"), "interval": s.item("Interval")}
        if o.type == "Panel":
            o.text = c.item("UserText")
        elif o.type == "Scribble":
            o.text = c.item("Text")
        elif o.type == "Group":
            o.members = c.all("ID")
            o.colour = c.item("Colour")
        objects.append(o)
    for o in objects:
        for p in o.inputs:
            p.wires = [owner[s] for s in p.sources if s in owner]
    return Definition(name=props.item("Name", default=""),
                      plugin_version=dfn.item("plugin_version"),
                      libraries=list(dict.fromkeys(libs)), objects=objects)


def read(path) -> Definition:
    return from_chunk(load(path))


def read_bytes(raw: bytes) -> Definition:
    return from_chunk(parse(raw))


def _short(v: Any) -> str:
    if isinstance(v, dict) and "geometry" in v:
        n = len(v["on_data"]) if v.get("on_data") else 0
        return f"<{v.get('geometry') or 'geometry'} ref, {n}B cached>"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, list):
        return "[" + ", ".join(_short(x) for x in v) + "]"
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)}B>"
    return repr(v)


def report(d: Definition, skip=("Group", "Scribble")) -> str:
    """The definition as text an agent (or a human) can read top to bottom:
    one block per object, each input with where it comes from or its value."""
    out = [f"{d.name}: {len(d.objects)} objects, plugins: {', '.join(d.libraries) or '-'}"]
    objs = d.objects
    for o in objs:
        if o.type in skip:
            continue
        line = o.label
        if o.slider:
            s = o.slider
            line += f" = {_short(s['value'])} [{_short(s['min'])}..{_short(s['max'])}]"
        if o.text is not None:
            line += f" text={o.text!r}"
        if o.locked:
            line += "  (DISABLED)"
        elif not o.hidden and o.type not in _NO_GEOMETRY:
            line += "  (shown)"
        out.append(line)
        for p in o.inputs:
            src = ", ".join(f"#{a}.{objs[a].outputs[b].name if objs[a].outputs else ''}"
                            for a, b in p.wires)
            flags = "".join(f" [{f}]" for f in p.flags)
            if p.expression:
                flags += f" expr={p.expression!r}"
            if o.floating:
                if src or p.data:
                    out.append(f"    <- {src or _short(p.data)}{flags}")
            elif src:
                out.append(f"    {p.nick} <- {src}{flags}")
            elif p.data is not None:
                out.append(f"    {p.nick} = {_short(p.data if len(p.data) != 1 else p.data[0])}{flags}")
    groups = [o for o in objs if o.type == "Group" and o.nick]
    if groups:
        guid_k = {o.guid: o.k for o in objs}
        out.append("groups:")
        for g in groups:
            out.append(f"  {g.nick!r}: " + " ".join(f"#{guid_k[m]}" for m in g.members if m in guid_k))
    notes = [o for o in objs if o.type == "Scribble" and o.text]
    if notes:
        out.append("scribbles: " + " | ".join(repr(o.text) for o in notes))
    return "\n".join(out)
