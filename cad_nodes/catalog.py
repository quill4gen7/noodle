"""
Node catalog — declarative definitions of every node type.

Each node is a `NodeDef`: its sockets (inputs/outputs), parameters (widgets +
defaults) and a `code_template` that the transpiler turns into build123d code.

The catalog is data, not logic: adding the rest of the ~150 nodes from
PLAN_NODE_CAD.md is pure data-entry against this same shape. The subset here
covers every category so the engine is exercisable end-to-end.

Template substitution (see transpiler.py):
  - {socket_name}  -> the variable holding the upstream node's output
  - {param_name}   -> the formatted parameter value

Group nodes (BuildPart / BuildSketch / BuildLine) are flagged with
`is_group=True` and handled specially by the transpiler (nested `with` blocks).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Wire types + compatibility — the single source of truth is cads_nodes/casts.py.
# Re-exported here so existing `catalog.WIRE_*` / `wires_compatible` keep working;
# WIRE_COMPATIBLE is now DERIVED from the cast registry (no hand-maintained copy).
# ---------------------------------------------------------------------------
from .casts import (  # noqa: E402,F401
    WIRE_SOLID, WIRE_SURFACE, WIRE_CURVE, WIRE_DATA, WIRE_TREE,
    WIRE_PLANE, WIRE_VECTOR, WIRE_SELECTION, WIRE_MESH, WIRE_TYPES,
    wires_compatible, cast_helper, build_compatible, build_input_accepts,
)

WIRE_COMPATIBLE: dict[str, set[str]] = build_compatible()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class Socket:
    name: str
    wire_type: str = WIRE_DATA
    required: bool = True
    multiple: bool = False  # accepts many connections (e.g. ListCreate, Loft)
    # Grasshopper-style data access. Item-access inputs FAN OUT: a list on the
    # wire (from several connections, or a list-producing upstream) runs the node
    # once per item -> a list output. list_access inputs consume the whole list
    # as one value (List/Sort/Item… and every `multiple` collector).
    list_access: bool = False
    # Per-socket widening: extra OUTPUT wire types this input accepts, BEYOND what
    # the global WIRE_COMPATIBLE table allows. Lets one polymorphic input (e.g. a
    # transform / Select that works on any shape at runtime) take a curve without
    # globally permitting curve -> every geometry input. See PLAN_DATA_PROTOCOL.md.
    accepts: list[str] = field(default_factory=list)
    # raw=True: the NODE handles any type coercion itself (e.g. Extrude/Loft pick
    # face vs outline by their `solid` flag), so the transpiler must NOT auto-apply
    # a boundary cast to this input — it would strip the info the node needs.
    raw: bool = False
    # Advisory sub-type within a wire type, for legend/disambiguation only (NOT
    # validation — connections still gate on wire_type). Splits the opaque `data`
    # bus (number/integer/boolean/text/list/domain) and the `curve` family
    # (line/polyline/spline/arc). See PLAN_DATA_PROTOCOL.md §4d (tag, don't split).
    subtype: str = ""


@dataclass
class Param:
    name: str
    type: str = "float"  # float | int | bool | str | select
    label: str = ""
    default: Any = 0.0
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    widget: str = "input"  # input | slider | checkbox | select | color | text
    options: list[str] = field(default_factory=list)
    # Map a select value -> raw code expression (e.g. "Z" -> "Axis.Z").
    code_map: dict[str, str] = field(default_factory=dict)
    # Render the value verbatim (no quoting) — for params that are code.
    raw: bool = False
    optional: bool = False
    # Soft cap: the slider drags only up to soft_max (a sane default range), while
    # a typed value can still reach the hard `max`. Guards against an accidental
    # scrub blowing up fan-out (e.g. ListRange count) without limiting power use.
    soft_max: Optional[float] = None


@dataclass
class NodeDef:
    type: str
    category: str
    label: str = ""
    inputs: list[Socket] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    outputs: list[Socket] = field(default_factory=list)
    # code_template["algebra"] is required; ["builder"] used inside group nodes.
    code_template: dict[str, str] = field(default_factory=dict)
    imports: list[str] = field(default_factory=lambda: ["from build123d import *"])
    description: str = ""
    # Hidden from the editor's add-node search/menu (frontend sets litegraph's
    # skip_list), but still registered so older graphs referencing it load & run.
    hidden: bool = False
    # Extra names the add-node search should find this node under — the words a
    # user coming from another CAD would type ("cut"/"trim" for Split). Matched by
    # nodes.html's own search (nodeSearchRows; litegraph's searchbox_extras can't
    # do it — see CLAUDE.md §6). Search entries ONLY, never node types, so nothing
    # else in the app (graph.json, the transpiler, MCP) ever sees them: `type`
    # stays the id. The user's own additions live in projects/_aliases.json and
    # get promoted HERE by hand once they've earned it.
    aliases: list[str] = field(default_factory=list)
    is_group: bool = False
    # For group nodes: how to read the result out of the context manager var.
    group_kind: Optional[str] = None  # "part" | "sketch" | "line"
    # On-canvas direct manipulation (PLAN_NODE_CAD.md §E). When set, the editor
    # shows an "Edit on canvas" toggle that drives a viewport gizmo bound to the
    # named params. Shape:
    #   {"kind": "translate", "binds": ["x","y","z"],
    #    "anchor": "params"|"preview", "lock": [<socket names>]}
    # binds  = the three params the gizmo writes (X/Y/Z order).
    # anchor = where the handle rests: "params" (the bound values ARE the point,
    #          e.g. ConstructPoint) or "preview" (the node's result bbox centre,
    #          e.g. Move — its params are a delta, not a world position).
    # lock   = input sockets that, when wired, make the value upstream-computed →
    #          the gizmo is read-only (you edit the producer node instead).
    gizmo: Optional[dict] = None
    # output_follows: name of the INPUT socket whose effective wire type this
    # node's FIRST output mirrors (type-preserving / polymorphic nodes — e.g. a
    # transform: move a curve -> get a curve back, not a generic geometry). The
    # graph validator and the editor resolve it up the chain; the runtime already
    # preserves the kind. See PLAN_DATA_PROTOCOL.md.
    output_follows: Optional[str] = None
    # subtype_follows: name of the INPUT socket whose effective *subtype* this
    # node's FIRST output inherits when its own subtype is unset — i.e. a
    # pass-through that preserves the advisory tag (a Curve/Panel container, a
    # transform). Defaults to `output_follows` when None. Advisory only (legend);
    # never gates a connection. See PLAN_DATA_PROTOCOL.md §4d.
    subtype_follows: Optional[str] = None

    def param(self, name: str) -> Optional[Param]:
        for p in self.params:
            if p.name == name:
                return p
        return None

    def input(self, name: str) -> Optional[Socket]:
        for s in self.inputs:
            if s.name == name:
                return s
        return None

    def output(self, name: str) -> Optional[Socket]:
        for s in self.outputs:
            if s.name == name:
                return s
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
REGISTRY: dict[str, NodeDef] = {}


def register(node: NodeDef) -> NodeDef:
    if node.type in REGISTRY:
        raise ValueError(f"Duplicate node type: {node.type}")
    if not node.label:
        node.label = node.type
    REGISTRY[node.type] = node
    return node


def get(node_type: str) -> NodeDef:
    try:
        return REGISTRY[node_type]
    except KeyError:
        raise KeyError(f"Unknown node type: {node_type!r}") from None


def as_json() -> list[dict]:
    """Serialise the whole catalog (for `cad://nodes` / frontend registry)."""
    return [asdict(REGISTRY[t]) for t in sorted(REGISTRY)]


# Convenience shorthands for terse definitions below ------------------------
def _geo(name="result"):
    return [Socket(name, WIRE_SOLID)]


def _sk(name="result"):
    return [Socket(name, WIRE_SURFACE)]


def _data(name="result"):
    return [Socket(name, WIRE_DATA)]


def _cv(name="result"):
    return [Socket(name, WIRE_CURVE)]


def _mesh(name="result"):
    return [Socket(name, WIRE_MESH)]


def _origin_in():
    # optional point input; when wired, positions the primitive there (default 0,0,0)
    return [Socket("origin", WIRE_VECTOR, required=False)]


def _pin(*names):
    """Optional data inputs named like params: when wired, the socket overrides
    the widget (params-as-inputs); feed a list to fan the node out (one copy per
    value). Unwired, the node falls back to the param widget. Zero transpiler
    cost — `_emit_simple` already resolves a same-named param/socket."""
    return [Socket(n, WIRE_DATA, required=False) for n in names]


def _f(name, default=0.0, lo=None, hi=None, step=0.5, label="", widget="slider",
       soft_max=None):
    return Param(name, "float", label or name, default, lo, hi, step, widget,
                 soft_max=soft_max)


def _i(name, default=0, lo=None, hi=None, label="", widget="slider",
       soft_max=None):
    return Param(name, "int", label or name, default, lo, hi, 1, widget,
                 soft_max=soft_max)


# ===========================================================================
# 1. Primitives 3D
# ===========================================================================
register(NodeDef("Box", "primitives_3d", "Box",
    inputs=_origin_in() + _pin("width", "height", "depth"),
    params=[_f("width", 10, 0.1, 500), _f("height", 10, 0.1, 500), _f("depth", 10, 0.1, 500),
            Param("centered", "bool", "centered", True, widget="checkbox")],
    outputs=_geo(),
    code_template={"algebra": "Box({width}, {height}, {depth}, align=_al({centered}))"},
    description="Solid box of given width, height, depth. `centered` aligns it on "
                "its centre; turn it off to seat its corner at the origin."))

register(NodeDef("Cylinder", "primitives_3d", "Cylinder",
    inputs=_origin_in() + _pin("radius", "height"),
    params=[_f("radius", 5, 0.1, 500), _f("height", 20, 0.1, 500),
            _f("arc", 360, 1, 360, label="arc"),
            Param("centered", "bool", "centered", True, widget="checkbox")],
    outputs=_geo(),
    code_template={"algebra": "Cylinder({radius}, {height}, arc_size={arc}, align=_al({centered}))"},
    description="Solid cylinder. `arc` < 360 makes a wedge / pie slice. `centered` "
                "aligns it on its centre; off seats its base at the origin."))

register(NodeDef("Sphere", "primitives_3d", "Sphere",
    inputs=_origin_in() + _pin("radius"),
    params=[_f("radius", 10, 0.1, 500), _f("arc", 360, 1, 360, label="arc")],
    outputs=_geo(),
    code_template={"algebra": "Sphere({radius}, arc_size3={arc})"},
    description="Solid sphere. `arc` < 360 sweeps only part of the way around "
                "(a wedge)."))

register(NodeDef("Cone", "primitives_3d", "Cone",
    inputs=_origin_in() + _pin("bottom_radius", "top_radius", "height"),
    params=[_f("bottom_radius", 5, 0, 500), _f("top_radius", 0, 0, 500), _f("height", 15, 0.1, 500)],
    outputs=_geo(),
    code_template={"algebra": "Cone({bottom_radius}, {top_radius}, {height})"},
    description="Truncated cone / frustum."))

register(NodeDef("Torus", "primitives_3d", "Torus",
    inputs=_origin_in() + _pin("major_radius", "minor_radius"),
    params=[_f("major_radius", 15, 0.1, 500), _f("minor_radius", 3, 0.1, 500),
            _f("arc", 360, 1, 360, label="arc")],
    outputs=_geo(),
    code_template={"algebra": "Torus({major_radius}, {minor_radius}, major_angle={arc})"},
    description="Torus / ring. `arc` < 360 makes a partial ring (a C-shape)."))

register(NodeDef("Wedge", "primitives_3d", "Wedge",
    inputs=_origin_in() + _pin("xsize", "ysize", "zsize"),
    params=[_f("xsize", 10, 0.1, 500), _f("ysize", 10, 0.1, 500), _f("zsize", 10, 0.1, 500),
            _f("xmin", 2, 0, 500), _f("zmin", 2, 0, 500),
            _f("xmax", 8, 0, 500), _f("zmax", 8, 0, 500)],
    outputs=_geo(),
    code_template={"algebra": "Wedge({xsize}, {ysize}, {zsize}, {xmin}, {zmin}, {xmax}, {zmax})"},
    description="Wedge: a box whose top face shrinks to the rectangle "
                "[xmin..xmax] x [zmin..zmax] (ramps, tapered blocks). Keep "
                "0 <= xmin < xmax <= xsize and likewise for z."))

register(NodeDef("Polyhedron", "primitives_3d", "Polyhedron",
    inputs=_origin_in() + _pin("faces", "size"),
    params=[Param("kind", "select", "kind", "platonic", widget="select",
                  options=["platonic", "prism", "antiprism", "bipyramid", "sphere"]),
            _i("faces", 6, 4, 200, label="faces"),
            _f("size", 10, 0.1, 500, label="size (circumradius)"),
            Param("as_mesh", "bool", "mesh output", False, widget="checkbox")],
    outputs=_geo(),
    aliases=["poliedro", "platonic", "tetrahedron", "cube", "octahedron",
             "dodecahedron", "icosahedron", "prism", "antiprism", "bipyramid",
             "dice", "d20", "geodesic"],
    code_template={"algebra": "_polyhedron({kind}, {faces}, {size}, {as_mesh})"},
    description="A polyhedron with the face count you ask for: the BIGGEST one of "
                "its family that fits in a sphere of radius `size` (every vertex "
                "sits on that sphere). `kind` picks the family — platonic (4/6/8/"
                "12/20: tetra, cube, octa, dodeca, icosa), prism (k+2 faces), "
                "antiprism (2k+2), bipyramid (2k), or sphere (any even count: "
                "near-uniform points hulled, F = 2V-4 — a geodesic-ish ball). "
                "`faces` SNAPS to the nearest count the family can build, so the "
                "slider always lands on a real solid. Beware the honest limit: "
                "'most volume per unit SURFACE for N faces' is an open problem "
                "past a few counts and its answers surprise (the best 5-faced "
                "solid is a triangular prism, not a square pyramid; the best "
                "8-faced one is not the regular octahedron), so this maximises "
                "within a family inscribed in a sphere, which is exact. Output is "
                "a real B-Rep solid with planar faces — Fillet/Chamfer/booleans "
                "all work. `mesh output` returns the same hull on the mesh lane "
                "instead (for Mesh Union & co.); leave it off unless you need it, "
                "as a mesh will not go into a Fillet."))

register(NodeDef("ConstructPoint", "vector", "Construct Point",
    inputs=[Socket("x", WIRE_DATA, required=False),
            Socket("y", WIRE_DATA, required=False),
            Socket("z", WIRE_DATA, required=False)],
    params=[_f("x", 0, label="x"), _f("y", 0, label="y"), _f("z", 0, label="z")],
    outputs=[Socket("point", WIRE_VECTOR)],
    gizmo={"kind": "translate", "binds": ["x", "y", "z"],
           "anchor": "params", "lock": ["x", "y", "z"]},
    code_template={"algebra": "Vector({x}, {y}, {z})"},
    description="A point (vector) from X, Y, Z. Each coordinate can be a widget "
                "OR an input — wire a list of numbers (e.g. from Range) into x to "
                "build a list of points. Feed `point` into a primitive's origin."))

# ===========================================================================
# 2. Primitives 2D (sketch / curve)
# ===========================================================================
register(NodeDef("Rectangle", "primitives_2d", "Rectangle",
    inputs=_origin_in() + _pin("width", "height"),
    params=[_f("width", 30, 0.1, 500), _f("height", 20, 0.1, 500)],
    outputs=_cv(),
    code_template={"algebra": "_outline(Rectangle({width}, {height}))"},
    description="2D rectangle as a closed curve. Feed a Surface (Make Face) node "
                "to fill it into a face."))

register(NodeDef("RoundedRectangle", "primitives_2d", "Rounded Rectangle",
    inputs=_origin_in() + _pin("width", "height", "radius"),
    params=[_f("width", 30, 0.1, 500), _f("height", 20, 0.1, 500), _f("radius", 3, 0, 250)],
    outputs=_cv(),
    code_template={"algebra": "_outline(RectangleRounded({width}, {height}, {radius}))"},
    description="Rectangle with filleted corners, as a closed curve. Feed a Surface "
                "(Make Face) node to fill it into a face."))

register(NodeDef("Circle", "primitives_2d", "Circle",
    inputs=_origin_in() + _pin("radius"),
    params=[_f("radius", 10, 0.1, 500)],
    outputs=_cv(),
    code_template={"algebra": "_outline(Circle({radius}))"},
    description="2D circle as a closed curve. Feed a Surface (Make Face) node to "
                "fill it into a face."))

register(NodeDef("Ellipse", "primitives_2d", "Ellipse",
    inputs=_origin_in() + _pin("x_radius", "y_radius"),
    params=[_f("x_radius", 8, 0.1, 500), _f("y_radius", 5, 0.1, 500)],
    outputs=_cv(),
    code_template={"algebra": "_outline(Ellipse({x_radius}, {y_radius}))"},
    description="2D ellipse as a closed curve. Feed a Surface (Make Face) node to "
                "fill it into a face."))

register(NodeDef("Polygon", "primitives_2d", "Regular Polygon",
    inputs=_origin_in() + _pin("radius", "sides"),
    params=[_f("radius", 10, 0.1, 500), _i("sides", 6, 3, 64),
            _f("rotation", 0, -360, 360, label="rotation"),
            Param("major", "bool", "to vertices", True, widget="checkbox")],
    outputs=_cv(),
    code_template={"algebra": "_outline(RegularPolygon({radius}, {sides}, major_radius={major}, rotation={rotation}))"},
    description="Regular N-sided polygon, as a closed curve. `to vertices` (default) "
                "measures `radius` to the corners (circumscribed); turn it off to "
                "measure to the edge midpoints (inscribed). `rotation` spins it. "
                "Feed a Surface (Make Face) node to fill it into a face."))

register(NodeDef("Star", "primitives_2d", "Star",
    inputs=_origin_in() + _pin("outer_radius", "inner_radius", "points"),
    params=[_f("outer_radius", 10, 0.1, 500), _f("inner_radius", 5, 0.1, 500),
            _i("points", 5, 2, 64)],
    outputs=_cv(),
    code_template={"algebra": "_star({outer_radius}, {inner_radius}, {points})"},
    description="Star with `points` spikes alternating between the outer and "
                "inner radius, as a closed curve. Feed a Surface (Make Face) node "
                "to fill it into a face."))

register(NodeDef("Slot", "primitives_2d", "Slot",
    inputs=_origin_in() + _pin("separation", "height"),
    params=[_f("separation", 20, 0.1, 500, label="separation"),
            _f("height", 6, 0.1, 500),
            _f("rotation", 0, -360, 360, label="rotation")],
    outputs=_cv(),
    code_template={"algebra": "_outline(SlotCenterToCenter({separation}, {height}, rotation={rotation}))"},
    description="Slot (oblong hole): two half-circles of diameter `height` whose "
                "centres sit `separation` apart, as a closed curve. Feed a Surface "
                "(Make Face) node, then Extrude/Subtract for the classic slot cut."))

register(NodeDef("Text", "primitives_2d", "Text",
    inputs=[Socket("plane", WIRE_PLANE, required=False)] + _origin_in(),
    params=[Param("text", "str", "text", "Hello", widget="input"),
            _f("font_size", 10, 0.1, 500),
            Param("font", "str", "font", "Arial", widget="font"),
            Param("style", "select", "style", "regular", widget="select",
                  options=["regular", "bold", "italic", "bolditalic"],
                  code_map={"regular": "FontStyle.REGULAR", "bold": "FontStyle.BOLD",
                            "italic": "FontStyle.ITALIC", "bolditalic": "FontStyle.BOLDITALIC"}),
            Param("align", "select", "align", "center", widget="select",
                  options=["left", "center", "right"],
                  code_map={"left": "(TextAlign.LEFT, TextAlign.CENTER)",
                            "center": "(TextAlign.CENTER, TextAlign.CENTER)",
                            "right": "(TextAlign.RIGHT, TextAlign.CENTER)"}),
            Param("plane", "select", "plane", "XY", widget="select",
                  options=["XY", "XZ", "YZ"],
                  code_map={"XY": "Plane.XY", "XZ": "Plane.XZ", "YZ": "Plane.YZ"})],
    outputs=_sk(),
    code_template={"algebra": "_to_plane(Text({text}, font_size={font_size}, **_font({font}), font_style={style}, text_align={align}), {plane})"},
    description="Text as a 2D sketch, seated on `plane`: pick XY/XZ/YZ, or wire in "
                "any Plane — or a flat face (Select Face / Faces By Normal), which "
                "casts to the plane it lies in — to place AND orient the text on it. "
                "Wiring `plane` overrides the XY/XZ/YZ picker. `font` picks a system "
                "family OR a custom font you upload (⬆) — used without installing it "
                "system-wide. Choose `style` (bold / italic) and horizontal `align`."))

# ===========================================================================
# 2b. Curves (WIRE_CURVE producers) — lines, arcs, splines along which profiles
# are placed / lofted. Point inputs are vectors and fan out.
# ===========================================================================
register(NodeDef("Line", "curves", "Line",
    inputs=[Socket("start", WIRE_VECTOR, required=False),
            Socket("end", WIRE_VECTOR, required=False)],
    outputs=_cv(),
    code_template={"algebra": "Line(_pt({start}), _pt({end}))"},
    description="Straight line between two points. Wire ConstructPoint/Vector into "
                "start and end; feed lists to fan out (one line per pair)."))

register(NodeDef("Polyline", "curves", "Polyline",
    inputs=[Socket("points", WIRE_VECTOR, multiple=True)],
    params=[Param("closed", "bool", "closed", False, widget="checkbox")],
    outputs=_cv(),
    code_template={"algebra": "Polyline(*_curve_points([{points}]), close={closed})"},
    description="Open or closed polyline through a sequence of points. Wire "
                "several points and/or a single list of points."))

register(NodeDef("Arc3pt", "curves", "Arc (3 point)",
    inputs=[Socket("start", WIRE_VECTOR, required=False),
            Socket("mid", WIRE_VECTOR, required=False),
            Socket("end", WIRE_VECTOR, required=False)],
    outputs=_cv(),
    code_template={"algebra": "ThreePointArc(_pt({start}), _pt({mid}), _pt({end}))"},
    description="Circular arc through three points (start, a mid point, end)."))

register(NodeDef("ArcCenter", "curves", "Arc (center)",
    inputs=[Socket("center", WIRE_VECTOR, required=False)] + _pin("radius", "start_angle", "end_angle"),
    params=[_f("radius", 10, 0.1, 500), _f("start_angle", 0, -360, 360),
            _f("end_angle", 90, -360, 360)],
    outputs=_cv(),
    code_template={"algebra": "CenterArc(_pt({center}) or (0, 0, 0), {radius}, {start_angle}, {end_angle})"},
    description="Circular arc by centre point, radius and start/end angle "
                "(degrees, CCW). Centre defaults to the origin if unwired."))

register(NodeDef("Spline", "curves", "Spline",
    inputs=[Socket("points", WIRE_VECTOR, multiple=True)],
    outputs=_cv(),
    code_template={"algebra": "Spline(*_curve_points([{points}]))"},
    description="Smooth spline (B-spline) through a sequence of points. Wire "
                "several points and/or a single list of points."))

register(NodeDef("Bezier", "curves", "Bezier",
    inputs=[Socket("points", WIRE_VECTOR, multiple=True)],
    outputs=_cv(),
    code_template={"algebra": "Bezier(*_curve_points([{points}]))"},
    description="Bezier curve from control points: it starts at the first point, "
                "ends at the last, and is pulled toward (without touching) the "
                "ones in between. Unlike Spline it does NOT pass through them."))

register(NodeDef("Helix", "curves", "Helix",
    inputs=_pin("pitch", "height", "radius"),
    params=[_f("pitch", 5, 0.1, 500), _f("height", 25, 0.1, 500),
            _f("radius", 8, 0.1, 500),
            _f("cone_angle", 0, -60, 60, label="cone angle"),
            Param("lefthand", "bool", "left-handed", False, widget="checkbox")],
    outputs=_cv(),
    code_template={"algebra": "Helix({pitch}, {height}, {radius}, cone_angle={cone_angle}, lefthand={lefthand})"},
    description="Helix around the Z axis: `pitch` is the climb per turn, `height` "
                "the total height. `cone angle` tapers it (a spiral on a cone). "
                "Feed into Sweep.path for springs and thread-like features."))

register(NodeDef("CurveOnPlane", "curves", "Curve on Plane",
    inputs=[Socket("plane", WIRE_PLANE, required=False)],
    params=[Param("plane", "select", "plane", "XY", widget="select",
                  options=["XY", "XZ", "YZ"],
                  code_map={"XY": "Plane.XY", "XZ": "Plane.XZ", "YZ": "Plane.YZ"}),
            Param("mode", "select", "mode", "polyline", widget="select",
                  options=["polyline", "spline"]),
            Param("closed", "bool", "closed", False, widget="checkbox"),
            Param("points", "curve3d", "points", [], widget="curve3d", raw=True)],
    outputs=_cv(),
    code_template={"algebra": "_curve_draw({points}, {mode}, {closed}, {plane})"},
    description="Draw a curve by clicking points directly in the 3D viewport "
                "(✎ Draw button). Points are authored flat in the plane's local "
                "(u, v) and re-seated by `plane`: pick XY/XZ/YZ, or wire in any "
                "Plane (Plane Origin, Bounding Plane, Divide Curve frames…) to "
                "place/orient the curve dynamically. Wiring `plane` locks the "
                "XY/XZ/YZ picker (on the node and in ✎ Draw) since the wire now "
                "drives it."))

# --- curve evaluation & division (the frames layer) ---
register(NodeDef("DivideCurve", "curves", "Divide Curve",
    inputs=[Socket("curve", WIRE_CURVE)] + _pin("count"),
    params=[_i("count", 8, 1, 500, label="count")],
    outputs=[Socket("frames", WIRE_PLANE)],
    code_template={"algebra": "_curve_frames({curve}, {count})"},
    description="Divide a curve into N evenly spaced frames (planes) whose Z axis "
                "is the curve tangent. Feed `frames` into ToPlane to seat profiles "
                "for a Loft, or into Plane Origin to get the points. List output — "
                "downstream fans out (one profile/shape per frame)."))

register(NodeDef("EvaluateCurve", "curves", "Evaluate Curve",
    inputs=[Socket("curve", WIRE_CURVE)] + _pin("t"),
    params=[_f("t", 0.0, 0.0, 1.0, 0.01, label="t")],
    outputs=[Socket("frame", WIRE_PLANE)],
    code_template={"algebra": "_eval_frame({curve}, {t})"},
    description="The frame (plane) at parameter t in [0,1] along a curve, Z aligned "
                "to the tangent. Wire a list of t values (e.g. from Range) to get "
                "many frames (fans out)."))

register(NodeDef("CurveEndpoints", "curves", "Curve Endpoints",
    inputs=[Socket("curve", WIRE_CURVE)],
    outputs=[Socket("points", WIRE_VECTOR)],
    code_template={"algebra": "_curve_endpoints({curve})"},
    description="The start and end of a curve as a 2-item list [start, end] of "
                "points."))

register(NodeDef("CurveLength", "curves", "Curve Length",
    inputs=[Socket("curve", WIRE_CURVE)],
    outputs=_data("length"),
    code_template={"algebra": "_curve_length({curve})"},
    description="Total length of a curve (a number)."))

# ===========================================================================
# 3. Operations 2D -> 3D
# ===========================================================================
register(NodeDef("Extrude", "operations", "Extrude",
    inputs=[Socket("sketch", WIRE_SURFACE, raw=True)] + _pin("amount", "taper"),
    params=[_f("amount", 10, -500, 500), _f("taper", 0, -45, 45),
            Param("both", "bool", "both", False, widget="checkbox"),
            Param("solid", "bool", "solid", True, widget="checkbox")],
    outputs=_geo(),
    code_template={"algebra": "_extrude({sketch}, {amount}, {taper}, {both}, {solid})",
                   "builder": "extrude(amount={amount}, taper={taper}, both={both})"},
    description="Extrude a 2D profile along its normal. A negative `amount` "
                "extrudes the opposite way. `both` extrudes symmetrically in "
                "both directions, fusing into a single solid. `solid` (default) "
                "fills the profile for a solid; turn it off to extrude just the "
                "outline into an open surface (wall / ribbon)."))

register(NodeDef("Revolve", "operations", "Revolve",
    inputs=[Socket("sketch", WIRE_SURFACE, raw=True)] + _pin("angle"),
    params=[_f("angle", 360, 1, 360),
            Param("axis", "select", "axis", "Y", widget="select",
                  options=["X", "Y", "Z"],
                  code_map={"X": "Axis.X", "Y": "Axis.Y", "Z": "Axis.Z"}),
            Param("solid", "bool", "solid", True, widget="checkbox")],
    outputs=_geo(),
    code_template={"algebra": "_revolve({sketch}, {axis}, {angle}, {solid})",
                   "builder": "revolve(axis={axis}, revolution_arc={angle})"},
    description="Revolve a profile around an in-plane axis (X or Y). The profile "
                "must sit off the axis (use a Move node), like a lathe — a "
                "profile crossing the axis is invalid geometry. `solid` (default) "
                "fills the profile for a solid of revolution; turn it off to "
                "revolve just the outline into an open surface."))

register(NodeDef("Loft", "operations", "Loft",
    inputs=[Socket("sections", WIRE_SURFACE, multiple=True, raw=True),
            Socket("start_point", WIRE_VECTOR, required=False),
            Socket("end_point", WIRE_VECTOR, required=False)],
    params=[Param("ruled", "bool", "ruled", False, widget="checkbox"),
            Param("solid", "bool", "solid", True, widget="checkbox"),
            Param("smoothing", "bool", "smoothing", False, widget="checkbox"),
            Param("continuity", "select", "continuity", "C2", widget="select",
                  options=["C0", "C1", "C2", "G1", "G2"]),
            Param("parametrization", "select", "parametrization", "chord",
                  widget="select", options=["uniform", "chord", "centripetal"]),
            Param("max_degree", "int", "max degree", 8, 1, 25, widget="slider")],
    outputs=_geo(),
    code_template={"algebra": "_loft([{sections}], {ruled}, {solid}, {smoothing}, "
                              "{continuity}, {parametrization}, {max_degree}, "
                              "{start_point}, {end_point})",
                   "builder": "loft(ruled={ruled})"},
    description="Loft / skin through an ordered list of sections. Wire several "
                "sketches OR a single list (e.g. ToPlane over Divide Curve frames). "
                "`ruled` = straight skin vs a smooth surface; `solid` caps the ends "
                "(off = open shell of the outlines). Smooth controls: `smoothing` "
                "uses the approximating algorithm, `continuity` (C0–G2) and "
                "`max_degree` shape its quality, `parametrization` "
                "(uniform/chord/centripetal) changes the bulge between sections. "
                "Optional `start_point`/`end_point` loft to a tip (a cone cap)."))

register(NodeDef("Sweep", "operations", "Sweep",
    inputs=[Socket("section", WIRE_SURFACE, raw=True), Socket("path", WIRE_CURVE)],
    params=[Param("is_frenet", "bool", "is_frenet", False, widget="checkbox"),
            Param("solid", "bool", "solid", True, widget="checkbox")],
    outputs=_geo(),
    code_template={"algebra": "_sweep({section}, {path}, {is_frenet}, {solid})"},
    description="Sweep a profile (section) along a path curve. The profile is "
                "auto-seated perpendicular to the path start. `is_frenet` uses the "
                "curve's natural frame (for twisting paths). `solid` (default) "
                "fills the profile for a solid; turn it off to sweep just the "
                "outline into an open surface (a tube wall)."))

register(NodeDef("Thicken", "operations", "Thicken",
    inputs=[Socket("sketch", WIRE_SURFACE, raw=True)],
    params=[_f("thickness", 2.5, 0.1, 100)],
    outputs=_geo(),
    code_template={"algebra": "thicken(_face({sketch}), {thickness})"},
    description="Give a surface/sketch a thickness."))

register(NodeDef("MakeFace", "operations", "Surface (Make Face)",
    inputs=[Socket("edges", WIRE_CURVE)],
    outputs=_sk(),
    code_template={"algebra": "make_face({edges})", "builder": "make_face()"},
    description="Patch / boundary surface: fill a closed curve (Circle, Rectangle, "
                "Polyline, …) into a 2D face, ready for Extrude / Revolve / Loft. "
                "Fans out over a list of curves (one face each)."))

register(NodeDef("PopulateGeometry", "operations", "Populate",
    inputs=[Socket("region", WIRE_SURFACE, required=False, raw=True,
                   accepts=[WIRE_SOLID, WIRE_MESH])],
    params=[_i("count", 40, 1, 5000, label="count"),
            _i("seed", 1, 0, 100000, label="seed", widget="input"),
            _f("width", 100, 1, 5000, label="width", widget="input"),
            _f("height", 100, 1, 5000, label="height", widget="input")],
    outputs=[Socket("points", WIRE_VECTOR)],
    code_template={"algebra": "_populate({count}, {seed}, {width}, {height}, {region})"},
    description="Scatter `count` random points, deterministic per `seed` — and "
                "WHERE they land depends on what you wire into `region`: an OPEN "
                "curve scatters ALONG it (uniform by arc length), a closed curve "
                "or flat sketch INSIDE the region (really inside, not its "
                "bounding box), a curved surface ON it (uniform by area), a "
                "solid or watertight mesh INSIDE the volume. Nothing wired: a "
                "width x height box at z=0. Feed Voronoi2D from a flat fill, "
                "Voronoi3D from a volume fill, or any origin/Move to scatter."))

register(NodeDef("Voronoi2D", "operations", "Voronoi 2D",
    inputs=[Socket("points", WIRE_VECTOR, list_access=True),
            Socket("boundary", WIRE_SURFACE, required=False)],
    params=[_f("scale", 0.9, 0.05, 1.0, 0.05, label="scale")],
    outputs=[Socket("cells", WIRE_SURFACE)],
    code_template={"algebra": "_voronoi2d({points}, {boundary}, {scale})"},
    description="Polygonal Voronoi cells from a set of `points`, clipped to the "
                "`boundary` rectangle (a Rectangle sketch). `scale` shrinks each "
                "cell toward its centre to leave a frame (1.0 = cells share "
                "edges). Fan out downstream (Extrude / MapToSurface) per cell."))

register(NodeDef("DivideSurface", "operations", "Divide Surface",
    inputs=[Socket("surface", WIRE_SOLID)],
    params=[_i("u_count", 6, 1, 500, label="u"), _i("v_count", 6, 1, 500, label="v")],
    outputs=[Socket("points", WIRE_VECTOR)],
    code_template={"algebra": "_divide_surface({surface}, {u_count}, {v_count})"},
    description="Sample a u x v grid of points on a surface (the largest face of "
                "the input). Feed `points` into a primitive's origin or Move to "
                "scatter geometry across the surface."))

register(NodeDef("MapToSurface", "operations", "Map To Surface",
    inputs=[Socket("shapes", WIRE_SURFACE, list_access=True),
            Socket("surface", WIRE_SOLID),
            Socket("boundary", WIRE_SURFACE, required=False)],
    params=[_f("width", 100, 1, 5000, label="width", widget="input"),
            _f("height", 100, 1, 5000, label="height", widget="input")],
    outputs=[Socket("mapped", WIRE_SURFACE)],
    code_template={"algebra": "_map_to_surface({shapes}, {surface}, {boundary}, {width}, {height})"},
    description="Wrap flat 2D shapes onto a surface: each shape's centroid maps "
                "from the 2D domain (the `boundary` rectangle, else width x "
                "height) to the surface's UV, and is re-seated on the tangent "
                "plane there. Then Extrude (along the normal) for radial "
                "cutters/bosses. Use the SAME boundary as Voronoi2D."))

# ===========================================================================
# 4. Booleans (CSG)
# ===========================================================================
register(NodeDef("Union", "boolean", "Union",
    # ONE collector input: shift-drag several wires into it, or feed a
    # list-producing node (e.g. a fanned MakeFace) — every shape flows in and is
    # fused into ONE result. Works for 2D faces/sketches AND 3D solids. (Legacy
    # graphs wired `a`/`b` are remapped to `shapes` on load — see graph.py /
    # fromGraphJSON.)
    inputs=[Socket("shapes", WIRE_SOLID, multiple=True)],
    outputs=_geo(),
    # type-preserving: fusing 2D faces yields a surface (feeds Extrude), fusing
    # solids yields a solid — so the output mirrors what flows in.
    output_follows="shapes",
    code_template={"algebra": "_union({shapes})"},
    aliases=["fuse", "boolean union", "unione", "add", "merge", "combine"],
    description="Boolean union — fuses everything wired in into one shape. Feed "
                "a whole list or many wires into the single socket; 3D solids "
                "fuse into one part, and COPLANAR 2D faces into one region. It "
                "is a boolean, not a sew: faces on different planes only touch, "
                "they do not overlap, so Union refuses them and points you at "
                "Join — which stitches them into a shell."))

register(NodeDef("Join", "boolean", "Join",
    # The complement of Union, and the reason Union no longer pretends: a boolean
    # fuses OVERLAPPING material, Join sews pieces that merely SHARE A BORDER.
    # One collector, same shift-drag / list idiom as Union. Declared `surface`
    # (the common case) but widened to curves and solids — the helper dispatches
    # on what really arrives. A `multiple` collector skips boundary casts
    # (CLAUDE.md §5c), so a CLOSED curve reaches the node as a curve rather than
    # being filled into a face on the way in — which is what lets Join chain it.
    inputs=[Socket("shapes", WIRE_SURFACE, multiple=True,
                   accepts=[WIRE_CURVE, WIRE_SOLID])],
    params=[_f("tolerance", 0.001, 0.0, 10, step=0.001, widget="input"),
            Param("make_solid", "bool", "close into a solid", True,
                  widget="checkbox")],
    outputs=_sk(),
    output_follows="shapes",
    aliases=["sew", "stitch", "weld", "unisci", "cuci", "shell", "wire",
             "combine", "knit", "merge faces"],
    # The collector SPREADS its wires (`a, b`), so bracket them: `_join` takes the
    # pieces as one list and keeps its tolerance/close args positional after it.
    code_template={"algebra": "_join([{shapes}], {tolerance}, {make_solid})"},
    description="Sew touching SURFACES into one shell, or chain touching CURVES "
                "into one wire. Six faces of a box come back as a closed shell "
                "and — with `close into a solid` on — as a real solid you can "
                "fillet and boolean; five come back as an open surface. Pieces "
                "that do not share their border edges are an error, not a silent "
                "bag of parts: raise `tolerance` (curves only — faces sew on "
                "OCCT's own tolerance) or check they really meet. Use Union "
                "instead when the shapes OVERLAP and you want them fused."))

register(NodeDef("Subtract", "boolean", "Subtract",
    # `b` is list-access: a LIST of tools (e.g. a fanned set of cutters) is
    # subtracted wholesale (A - [b0, b1, …]), not fanned. `a` still fans.
    inputs=[Socket("a", WIRE_SOLID), Socket("b", WIRE_SOLID, list_access=True)],
    outputs=_geo(),
    code_template={"algebra": "({a} - {b})"},
    description="Boolean difference A - B. B may be a single shape or a whole "
                "list of tools (all subtracted)."))

register(NodeDef("Intersect", "boolean", "Intersect",
    inputs=[Socket("a", WIRE_SOLID), Socket("b", WIRE_SOLID)],
    outputs=_geo(),
    code_template={"algebra": "({a} & {b})"},
    description="Boolean intersection A & B."))

register(NodeDef("BooleanMulti", "boolean", "Union (legacy)",
    # Deprecated: folded into Union, which now handles N inputs / lists itself.
    # Kept (hidden) so older graphs that reference "BooleanMulti" still load & run.
    inputs=[Socket("shapes", WIRE_SOLID, multiple=True)],
    outputs=_geo(),
    code_template={"algebra": "_union({shapes})"},
    hidden=True,
    description="Deprecated alias of Union (kept so older graphs load). "
                "Use Union — it fuses any number of shapes on its own."))

# ===========================================================================
# 5. Modifiers
# ===========================================================================
# A single fillet/chamfer node with a `mode` dropdown, replacing the old
# separate Fillet + Chamfer (3D edges) and Fillet2D + Chamfer2D (2D corners) —
# those stay registered but `hidden` so older graphs keep loading & running.
def _mode_param():
    return Param("mode", "select", "mode", "fillet",
                 widget="select", options=["fillet", "chamfer"])

register(NodeDef("FilletChamfer", "modifiers", "Fillet / Chamfer",
    inputs=[Socket("part", WIRE_SOLID)] + _pin("size"),
    params=[_mode_param(), _f("size", 2, 0.01, 100, step=0.05)],
    outputs=_geo(),
    code_template={"algebra": "_round_all({part}, {mode}, {size})"},
    description="Round (fillet) or bevel (chamfer) ALL edges of a part — pick "
                "which with `mode`. `size` is the radius / bevel length. If `size` "
                "is too big for the geometry (e.g. thin extruded-text strokes) it "
                "is auto-clamped to the largest that fits, so it never fails "
                "wholesale on one thin spot."))

register(NodeDef("FilletChamferCorners", "modifiers", "Fillet / Chamfer Corners",
    # The 2D version: rounds/bevels the CORNERS (vertices) of a face/sketch and
    # returns the OUTLINE curve. A closed curve auto-casts to a face via _face.
    inputs=[Socket("shape", WIRE_SURFACE, raw=True)] + _pin("size"),
    params=[_mode_param(), _f("size", 2, 0.01, 100, step=0.05)],
    outputs=_cv(),
    code_template={"algebra": "_outline(_round_corners({shape}, {mode}, {size}))"},
    description="Round (fillet) or bevel (chamfer) all corners of a 2D "
                "face/sketch — `mode` picks which. Feed a closed curve or a Make "
                "Face result; outputs the rounded outline as a curve (fill it "
                "with Make Face, or feed straight into Extrude)."))

# Deprecated singles — hidden, kept so older graphs load & run.
register(NodeDef("Fillet", "modifiers", "Fillet",
    inputs=[Socket("part", WIRE_SOLID)] + _pin("radius"),
    params=[_f("radius", 2, 0.01, 100, step=0.05)], outputs=_geo(), hidden=True,
    code_template={"algebra": "_round_all({part}, 'fillet', {radius})"},
    description="Deprecated: use Fillet / Chamfer (mode=fillet). Round all edges."))

register(NodeDef("Chamfer", "modifiers", "Chamfer",
    inputs=[Socket("part", WIRE_SOLID)] + _pin("length"),
    params=[_f("length", 1.5, 0.01, 100, step=0.05)], outputs=_geo(), hidden=True,
    code_template={"algebra": "_round_all({part}, 'chamfer', {length})"},
    description="Deprecated: use Fillet / Chamfer (mode=chamfer). Bevel all edges."))

register(NodeDef("Fillet2D", "modifiers", "Fillet Corners",
    inputs=[Socket("shape", WIRE_SURFACE, raw=True)] + _pin("radius"),
    params=[_f("radius", 2, 0.01, 100, step=0.05)], outputs=_sk(), hidden=True,
    code_template={"algebra": "_round_corners({shape}, 'fillet', {radius})"},
    description="Deprecated: use Fillet / Chamfer Corners. Round 2D corners."))

register(NodeDef("Chamfer2D", "modifiers", "Chamfer Corners",
    inputs=[Socket("shape", WIRE_SURFACE, raw=True)] + _pin("length"),
    params=[_f("length", 1.5, 0.01, 100, step=0.05)], outputs=_sk(), hidden=True,
    code_template={"algebra": "_round_corners({shape}, 'chamfer', {length})"},
    description="Deprecated: use Fillet / Chamfer Corners. Bevel 2D corners."))

# ===========================================================================
# 5b. Sub-shape selection (pick edges/faces in the 3D picker, operate on them)
# ===========================================================================
# SelectEdge carries the picked set in params["selection"] = {kind, indices,
# sigs}; the transpiler special-cases it (see _emit_select) and resolves the
# set at run time by nearest-anchor matching, so it survives param tweaks.
# Every selector has TWO outputs: `selection` (drives a targeted op like Fillet /
# Push-Pull, consumed whole) and a geometry output that materialises the picked
# sub-shapes as usable geometry — edges→curve, faces→surface, vertices→point —
# so you can loft the picked edges, extrude a picked face, scatter on vertices,
# etc. Both resolve to the same ShapeList; the geometry output fans out.
register(NodeDef("SelectEdge", "select", "Select Edge",
    inputs=[Socket("geometry", WIRE_SOLID, accepts=[WIRE_CURVE])],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("edges", WIRE_CURVE)],
    code_template={"algebra": ""},  # handled by the transpiler, not a template
    description="Pick specific edges of a shape in the 3D picker. `selection` "
                "drives a targeted op; `edges` gives the picked edges as curves."))

register(NodeDef("SelectFace", "select", "Select Face",
    inputs=[Socket("geometry", WIRE_SOLID, accepts=[WIRE_CURVE])],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("faces", WIRE_SURFACE)],
    code_template={"algebra": ""},  # handled by the transpiler, not a template
    description="Pick specific faces of a shape in the 3D picker. `selection` "
                "drives a targeted op; `faces` gives the picked faces as surfaces."))

register(NodeDef("SelectVertex", "select", "Select Vertex",
    inputs=[Socket("geometry", WIRE_SOLID, accepts=[WIRE_CURVE])],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("points", WIRE_VECTOR)],
    code_template={"algebra": ""},  # handled by the transpiler, not a template
    description="Pick specific vertices of a shape in the 3D picker. `selection` "
                "drives a targeted op; `points` gives the picked vertices as points."))

register(NodeDef("SelectShape", "select", "Select Shape",
    # Picks WHOLE objects from a LIST (not sub-shapes of one object). Universal:
    # the input accepts any shape type and the output mirrors it (type-preserving).
    # Handled by the transpiler (_emit_select) like the other Select* nodes.
    inputs=[Socket("shapes", WIRE_DATA, list_access=True)],
    outputs=[Socket("shapes", WIRE_DATA)],
    output_follows="shapes",
    code_template={"algebra": ""},
    description="Pick WHOLE objects from a list in the 3D picker — universal, any "
                "shape type (solids, faces, curves, Voronoi cells, array copies). "
                "Outputs the selected objects; they fan out downstream."))

# --- Predicate (semantic) selectors -----------------------------------------
# Unlike the pick-by-click Select* nodes (positional anchors that break when a
# feature moves), these describe the selection by INTENT and re-evaluate against
# the current geometry, so they survive parameter changes. Ordinary catalog
# nodes (a PREAMBLE helper does the work) — one atom per criterion, composable.
def _choice(name, default, options, label=None):
    """A dropdown param whose string value is emitted as a quoted literal
    (select falls back to repr(value) when it has no code_map)."""
    return Param(name, "select", label or name, default, widget="select", options=options)

register(NodeDef("FacesByNormal", "select", "Faces By Normal",
    inputs=[Socket("geometry", WIRE_SOLID)],
    params=[_choice("axis", "Z", ["X", "Y", "Z"]),
            _choice("sign", "+", ["+", "-", "both"]),
            _f("tol", 0.1, 0.001, 1.0)],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("faces", WIRE_SURFACE)],
    code_template={"algebra": "_faces_by_normal({geometry}, {axis}, {sign}, {tol})"},
    description="Select faces whose normal points along an axis (+Z = the top "
                "face). Predicate-based: survives parameter changes that move the "
                "face — unlike a hand-picked selection."))

register(NodeDef("EdgesByType", "select", "Edges By Type",
    inputs=[Socket("geometry", WIRE_SOLID, accepts=[WIRE_CURVE])],
    params=[_choice("type", "circle", ["circle", "line", "ellipse", "spline"])],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("edges", WIRE_CURVE)],
    code_template={"algebra": "_edges_by_type({geometry}, {type})"},
    description="Select all edges of a geometry type — 'circle' grabs every hole / "
                "round edge (e.g. to fillet them all). Stable under param changes."))

register(NodeDef("FacesByType", "select", "Faces By Type",
    inputs=[Socket("geometry", WIRE_SOLID)],
    params=[_choice("type", "plane", ["plane", "cylinder", "sphere", "cone", "torus"])],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("faces", WIRE_SURFACE)],
    code_template={"algebra": "_faces_by_type({geometry}, {type})"},
    description="Select all faces of a surface type — e.g. every cylindrical wall "
                "(bore/boss). Stable under param changes."))

register(NodeDef("FacesByArea", "select", "Faces By Area",
    inputs=[Socket("geometry", WIRE_SOLID)],
    params=[_choice("pick", "largest", ["largest", "smallest"]), _i("n", 1, 1, 100)],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("faces", WIRE_SURFACE)],
    code_template={"algebra": "_by_size({geometry}, 'face', 'area', {pick}, {n})"},
    description="Select the N largest (or smallest) faces by area."))

register(NodeDef("EdgesByLength", "select", "Edges By Length",
    inputs=[Socket("geometry", WIRE_SOLID, accepts=[WIRE_CURVE])],
    params=[_choice("pick", "longest", ["longest", "shortest"]), _i("n", 1, 1, 100)],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("edges", WIRE_CURVE)],
    code_template={"algebra": "_by_size({geometry}, 'edge', 'length', {pick}, {n})"},
    description="Select the N longest (or shortest) edges by length."))

register(NodeDef("SubshapesByPosition", "select", "Subshapes By Position",
    inputs=[Socket("geometry", WIRE_SOLID, accepts=[WIRE_CURVE])],
    params=[_choice("kind", "face", ["face", "edge", "vertex"]),
            _choice("axis", "Z", ["X", "Y", "Z"]),
            _choice("pick", "max", ["max", "min"]), _i("n", 1, 1, 100)],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("shapes", WIRE_DATA)],
    code_template={"algebra": "_by_position({geometry}, {kind}, {axis}, {pick}, {n})"},
    description="Select the N extreme sub-shapes along an axis — the topmost face "
                "(Z/max), the leftmost edges (X/min), and so on."))

register(NodeDef("CombineSelection", "select", "Combine Selection",
    inputs=[Socket("a", WIRE_SELECTION, list_access=True), Socket("b", WIRE_SELECTION, list_access=True)],
    params=[_choice("mode", "or", ["or", "and", "subtract"])],
    outputs=[Socket("selection", WIRE_SELECTION), Socket("shapes", WIRE_DATA)],
    code_template={"algebra": "_combine_sel({a}, {b}, {mode})"},
    description="Boolean-combine two selections: or (union), and (in both), "
                "subtract (in A but not B). Compose predicate selectors."))

register(NodeDef("FilletChamferSelected", "modifiers", "Fillet / Chamfer (selected)",
    # Applies to whatever the selection holds: edges (3D) OR vertices (2D
    # corners) — so this also covers "fillet selected corners" via Select Vertex.
    inputs=[Socket("part", WIRE_SOLID), Socket("selection", WIRE_SELECTION, list_access=True)],
    params=[_mode_param(), _f("size", 2, 0.01, 100, step=0.05)],
    outputs=_geo(),
    code_template={"algebra": "_round({part}, {selection}, {mode}, {size})"},
    description="Round (fillet) or bevel (chamfer) only the sub-shapes chosen by "
                "a Select node — edges (3D) or corners/vertices (2D). `mode` picks "
                "which, `size` is the radius / bevel length."))

# Deprecated singles — hidden, kept so older graphs load & run.
register(NodeDef("FilletSelectedEdges", "modifiers", "Fillet Selected Edges",
    inputs=[Socket("part", WIRE_SOLID), Socket("edges", WIRE_SELECTION, list_access=True)],
    params=[_f("radius", 2, 0.01, 100, step=0.05)], outputs=_geo(), hidden=True,
    code_template={"algebra": "_round({part}, {edges}, 'fillet', {radius})"},
    description="Deprecated: use Fillet / Chamfer (selected). Round chosen edges."))

register(NodeDef("ChamferSelectedEdges", "modifiers", "Chamfer Selected Edges",
    inputs=[Socket("part", WIRE_SOLID), Socket("edges", WIRE_SELECTION, list_access=True)],
    params=[_f("length", 1.5, 0.01, 100, step=0.05)], outputs=_geo(), hidden=True,
    code_template={"algebra": "_round({part}, {edges}, 'chamfer', {length})"},
    description="Deprecated: use Fillet / Chamfer (selected). Bevel chosen edges."))

register(NodeDef("ExtrudeSelectedFace", "modifiers", "Push / Pull Face",
    inputs=[Socket("part", WIRE_SOLID), Socket("faces", WIRE_SELECTION, list_access=True)],
    params=[_f("amount", 5, -200, 200)],
    outputs=_geo(),
    code_template={"algebra": "_pushpull({part}, {faces}, {amount})"},
    description="Push/pull the faces chosen by a Select Face node along their "
                "normal: positive grows a boss, negative carves a pocket."))

register(NodeDef("Shell", "modifiers", "Shell",
    inputs=[Socket("part", WIRE_SOLID),
            Socket("thickness", WIRE_DATA, required=False)],
    params=[_f("thickness", 1, 0.05, 100)],
    outputs=_geo(),
    code_template={"algebra": "_shell({part}, {thickness})"},
    description="Hollow out a solid with the given wall thickness, leaving the top "
                "(+Z) face open. An open surface (e.g. a non-solid Loft) is thickened "
                "into a solid wall instead."))

register(NodeDef("ShellByFaces", "modifiers", "Shell By Faces",
    inputs=[Socket("part", WIRE_SOLID), Socket("faces", WIRE_SELECTION, list_access=True),
            Socket("thickness", WIRE_DATA, required=False)],
    params=[_f("thickness", 1, 0.05, 100)],
    outputs=_geo(),
    code_template={"algebra": "_shell_faces({part}, {faces}, {thickness})"},
    description="Hollow a solid to a wall of `thickness`, leaving the SELECTED "
                "faces open. Unlike Shell (which always opens the top), you choose "
                "the openings with a face selector — Faces By Normal, Faces By "
                "Type, or Combine Selection for several."))

register(NodeDef("Offset", "modifiers", "Offset",
    inputs=[Socket("shape", WIRE_SOLID)] + _pin("amount"),
    params=[_f("amount", 2, -100, 100),
            Param("kind", "select", "corners", "arc", widget="select",
                  options=["arc", "intersection", "tangent"],
                  code_map={"arc": "Kind.ARC", "intersection": "Kind.INTERSECTION",
                            "tangent": "Kind.TANGENT"})],
    outputs=_geo(),
    code_template={"algebra": "offset({shape}, amount={amount}, kind={kind})"},
    description="Offset a shape outward/inward. `corners` sets how convex corners "
                "are filled: arc (rounded), intersection (sharp), or tangent."))

register(NodeDef("Section", "modifiers", "Section",
    inputs=[Socket("shape", WIRE_SOLID),
            Socket("plane", WIRE_PLANE, required=False)],
    outputs=_sk(),
    code_template={"algebra": "_section({shape}, {plane})"},
    description="Planar cross-section of a shape, cut by the Plane wired into "
                "`plane` (e.g. from a Bounding Plane node, which sets both "
                "orientation and position). Defaults to the XY plane through the "
                "global origin when nothing is wired."))

register(NodeDef("Split", "modifiers", "Split",
    # raw=True: the tool must arrive AS WIRED. Without it the surface->plane cast
    # (_as_plane) would flatten a curved cutting surface into the plane it roughly
    # lies in, and the whole point of accepting a surface would be lost.
    inputs=[Socket("shape", WIRE_SOLID),
            Socket("plane", WIRE_PLANE, required=False, raw=True)],
    params=[Param("keep", "select", "keep", "top", widget="select",
                  options=["top", "bottom", "both"],
                  code_map={"top": "Keep.TOP", "bottom": "Keep.BOTTOM",
                            "both": "Keep.BOTH"}),
            Param("solid", "bool", "solid", True, widget="checkbox")],
    outputs=_geo(),
    aliases=["Cut", "Trim"],
    code_template={"algebra": "_split({shape}, {plane}, {keep}, {solid})"},
    description="Cut a shape in two and keep the side above the tool (`top`), "
                "below it (`bottom`) or `both` halves. The tool is whatever is "
                "wired into `plane`: a Plane (Bounding Plane / Plane node), a "
                "SURFACE — the cut follows it curve and all — or another SOLID, "
                "which cuts along its skin. Defaults to XY through the origin. "
                "`solid` off leaves the cut OPEN: the faces the tool created are "
                "dropped and you get a shell, not a capped solid. Unlike Section "
                "(a 2D slice), Split keeps the geometry it cut."))

# ===========================================================================
# 6. Transforms
# ===========================================================================
# The `shape` socket of every transform also accepts WIRE_MESH: a Move/Rotate/
# Scale/Mirror on triangles is the same operation as on a B-Rep, just expressed
# as a 4x4 on the vertex array instead of a Location (see the PREAMBLE's
# _mesh_matrix). `output_follows="shape"` then carries the mesh type back out, so
# the mesh lane reuses these nodes instead of duplicating them. PLAN_MESH_LANE.md §3.
register(NodeDef("Move", "transform", "Move",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE, WIRE_MESH]),
            Socket("offset", WIRE_VECTOR, required=False)],
    params=[_f("x", 0, -500, 500), _f("y", 0, -500, 500), _f("z", 0, -500, 500)],
    outputs=_geo(),
    gizmo={"kind": "translate", "binds": ["x", "y", "z"],
           "anchor": "preview", "lock": ["offset"]},
    code_template={"algebra": "_move({shape}, {offset}, {x}, {y}, {z})"},
    output_follows="shape",
    description="Translate a shape (or a plane). Wire a vector into `offset` to "
                "drive the position; feed a LIST of vectors to scatter the shape "
                "to each position (one moved copy per vector)."))

register(NodeDef("Rotate", "transform", "Rotate",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE, WIRE_MESH]),
            Socket("pivot", WIRE_VECTOR, required=False)] + _pin("angle"),
    params=[_f("angle", 90, -360, 360),
            Param("axis", "select", "axis", "Z", widget="select",
                  options=["X", "Y", "Z"],
                  code_map={"X": "Axis.X", "Y": "Axis.Y", "Z": "Axis.Z"}),
            Param("about", "select", "about", "world", widget="select",
                  options=["world", "part", "group"])],
    outputs=_geo(),
    gizmo={"kind": "rotate", "binds": ["angle"], "axisParam": "axis",
           "anchor": "origin", "lock": ["angle"]},
    code_template={"algebra": "_rotate({shape}, {axis}, {angle}, {pivot}, {about})"},
    output_follows="shape",
    description="Rotate a shape (or a plane, or a mesh). `about` picks the centre: "
                "`world` turns about the global axis (a part away from the origin "
                "ORBITS — the old behaviour), `part` about its own bbox centre (it "
                "turns in place), `group` about the collective bbox centre when a "
                "list is wired in — the ensemble turns as one rigid body. Wire a "
                "point into `pivot` to name the centre exactly; it overrides the "
                "dropdown. The centre is measured on the tessellation (the fast "
                "OCCT box is oversized — same reason Place on Bed does)."))

register(NodeDef("Scale", "transform", "Scale",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE, WIRE_MESH])] + _pin("factor"),
    params=[_f("factor", 2, 0.01, 100),
            _f("x", 1, 0.01, 100, label="x"), _f("y", 1, 0.01, 100, label="y"),
            _f("z", 1, 0.01, 100, label="z")],
    outputs=_geo(),
    gizmo={"kind": "scale", "binds": ["factor"], "anchor": "preview", "lock": ["factor"]},
    code_template={"algebra": "_scale({shape}, {factor}, {x}, {y}, {z})"},
    output_follows="shape",
    description="Scale a shape. `factor` is the uniform multiplier; set `x`/`y`/`z` "
                "(default 1) for a non-uniform scale on top of it."))

register(NodeDef("ToPlane", "transform", "To Plane",
    inputs=[Socket("shape", WIRE_SURFACE, raw=True), Socket("plane", WIRE_PLANE)],
    outputs=_sk(),
    code_template={"algebra": "_to_plane({shape}, {plane})"},
    description="Re-seat a 2D profile onto a plane/frame (its local XY comes to lie "
                "in the plane). Wire a list of frames (Divide Curve) — and optionally "
                "a list of profiles — and it zips one profile per frame, oriented "
                "perpendicular to the curve, ready to Loft."))

register(NodeDef("Mirror", "transform", "Mirror",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE, WIRE_MESH])],
    params=[Param("plane", "select", "plane", "XZ", widget="select",
                  options=["XY", "XZ", "YZ"],
                  code_map={"XY": "Plane.XY", "XZ": "Plane.XZ", "YZ": "Plane.YZ"}),
            Param("copy", "bool", "copy", False, widget="checkbox")],
    outputs=_geo(),
    code_template={"algebra": "_mirror({shape}, {plane}, {copy})"},
    description="Mirror across a plane. `copy` keeps the original too, so the "
                "result is symmetric (original + reflection)."))

register(NodeDef("ArrayLinear", "transform", "Linear Array",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE])],
    params=[_i("count", 3, 1, 200), _f("dx", 20, -500, 500),
            _f("dy", 0, -500, 500), _f("dz", 0, -500, 500)],
    outputs=[Socket("result", WIRE_SOLID)],
    code_template={"algebra": "[Pos({dx}*i, {dy}*i, {dz}*i) * {shape} for i in range({count})]"},
    description="Repeat a shape along a vector -> list."))

register(NodeDef("ArrayPolar", "transform", "Polar Array",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE])] + _pin("count", "angle"),
    params=[_i("count", 6, 1, 200), _f("angle", 360, -360, 360),
            Param("axis", "select", "axis", "Z", widget="select",
                  options=["X", "Y", "Z"],
                  code_map={"X": "Axis.X", "Y": "Axis.Y", "Z": "Axis.Z"})],
    outputs=[Socket("result", WIRE_SOLID)],
    code_template={"algebra": "_array_polar({shape}, {count}, {angle}, {axis})"},
    description="Repeat a shape around a global axis -> list (bolt circles, "
                "spokes). A full 360 spaces `count` copies evenly; a partial "
                "`angle` fans them between 0 and angle. Rotation is about the "
                "axis through the origin — Move the shape off-axis first."))

register(NodeDef("Align", "transform", "Align",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_CURVE]),
            Socket("ref", WIRE_VECTOR, required=False),
            Socket("target", WIRE_VECTOR, required=False)],
    outputs=_geo(),
    code_template={"algebra": "_align({shape}, {ref}, {target})"},
    output_follows="shape",
    description="Translate a shape so `ref` (default: its bounding-box centre) "
                "lands on `target` (default: the origin). Unwired it just centres "
                "the shape; wire points to snap a feature onto a location."))

# ===========================================================================
# 7. Planes
# ===========================================================================
register(NodeDef("PlaneOrigin", "plane", "Plane (origin)",
    params=[Param("plane", "select", "plane", "XY", widget="select",
                  options=["XY", "XZ", "YZ"],
                  code_map={"XY": "Plane.XY", "XZ": "Plane.XZ", "YZ": "Plane.YZ"})],
    outputs=[Socket("plane", WIRE_PLANE)],
    code_template={"algebra": "{plane}"},
    description="A base plane."))

register(NodeDef("BoundingPlane", "plane", "Bounding Plane",
    inputs=[Socket("shape", WIRE_SOLID)],
    params=[Param("orientation", "select", "orientation", "XY", widget="select",
                  options=["XY", "XZ", "YZ"],
                  code_map={"XY": "Plane.XY", "XZ": "Plane.XZ", "YZ": "Plane.YZ"}),
            _f("position", 0.5, 0.0, 1.0, 0.01, label="position")],
    outputs=[Socket("plane", WIRE_PLANE)],
    code_template={"algebra": "_bbox_plane({shape}, {orientation}, {position})"},
    description="A Plane parallel to the chosen base plane (XY/XZ/YZ), centred on "
                "the input geometry's bounding box and slid along its normal by "
                "position (0=min, 0.5=centre, 1=max). A real plane on the wire — "
                "feed it to Section or any plane input; works for any geometry."))

register(NodeDef("PlaneNormal", "plane", "Plane (normal)",
    inputs=[Socket("origin", WIRE_VECTOR, required=False),
            Socket("normal", WIRE_VECTOR, required=False)],
    outputs=[Socket("plane", WIRE_PLANE)],
    code_template={"algebra": "_plane_normal({origin}, {normal})"},
    description="A Plane from an origin point and a normal (z) direction — the "
                "free-form way to orient work planes. Defaults: origin (0,0,0), "
                "normal +Z. Wire lists to get many planes (fans out)."))

register(NodeDef("MovePlane", "plane", "Move Plane",
    inputs=[Socket("plane", WIRE_PLANE)] + _pin("distance"),
    params=[_f("distance", 5, -500, 500)],
    outputs=[Socket("plane", WIRE_PLANE)],
    code_template={"algebra": "_plane_offset({plane}, {distance})"},
    description="Slide a plane along its own normal by `distance`. Wire a list "
                "of distances (Range) to get a stack of parallel planes (fans "
                "out) — e.g. many Sections through a part."))

register(NodeDef("DeconstructPlane", "plane", "Plane Origin",
    inputs=[Socket("plane", WIRE_PLANE)],
    outputs=[Socket("point", WIRE_VECTOR)],
    code_template={"algebra": "_plane_origin({plane})"},
    description="The origin point of a plane/frame. Wire a list of frames (e.g. "
                "from Divide Curve) to get the points along the curve (fans out)."))

register(NodeDef("Deconstruct", "vector", "Deconstruct (points)",
    inputs=[Socket("shape", WIRE_SOLID, list_access=True,
                   accepts=[WIRE_CURVE, WIRE_SELECTION, WIRE_VECTOR, WIRE_PLANE])],
    outputs=[Socket("points", WIRE_VECTOR)],
    code_template={"algebra": "_deconstruct({shape})"},
    description="Explode anything into its points — the one coherent point "
                "extractor: a solid/surface/curve gives ALL its vertices; a "
                "plane/frame its origin; a selection the picked points; a point "
                "itself. Outputs a list (fans out). Pairs with Divide Curve "
                "(frames) and Select Vertex (interactive pick)."))

register(NodeDef("DeconstructEdges", "vector", "Deconstruct Edges",
    inputs=[Socket("shape", WIRE_SOLID, list_access=True,
                   accepts=[WIRE_SURFACE, WIRE_CURVE, WIRE_SELECTION])],
    outputs=[Socket("edges", WIRE_CURVE)],
    code_template={"algebra": "_explode({shape}, 'edge')"},
    description="Explode a shape into its edges (curves) — the companion of "
                "Deconstruct (points): a solid/surface/curve gives ALL its "
                "constituent edges as a list (fans out). Feed into Sweep.path, "
                "Move, or any curve input."))

register(NodeDef("DeconstructFaces", "vector", "Deconstruct Faces",
    inputs=[Socket("shape", WIRE_SOLID, list_access=True,
                   accepts=[WIRE_SURFACE, WIRE_SELECTION])],
    outputs=[Socket("faces", WIRE_SURFACE)],
    code_template={"algebra": "_explode({shape}, 'face')"},
    description="Explode a shape into its faces (surfaces): a solid/surface "
                "gives ALL its constituent faces as a list (fans out). Feed into "
                "Extrude, Thicken, or any surface input."))

# ===========================================================================
# 8. Vectors & points
# ===========================================================================
register(NodeDef("Vector", "vector", "Vector",
    inputs=[Socket("x", WIRE_DATA, required=False),
            Socket("y", WIRE_DATA, required=False),
            Socket("z", WIRE_DATA, required=False)],
    params=[_f("x", 0, -1000, 1000, widget="input"),
            _f("y", 0, -1000, 1000, widget="input"),
            _f("z", 0, -1000, 1000, widget="input")],
    outputs=[Socket("vector", WIRE_VECTOR)],
    gizmo={"kind": "translate", "binds": ["x", "y", "z"],
           "anchor": "params", "lock": ["x", "y", "z"]},
    code_template={"algebra": "Vector({x}, {y}, {z})"},
    description="A 3D vector. Each component is a widget OR an input; wire a list "
                "of numbers into a component to build a list of vectors."))

register(NodeDef("CrossProduct", "vector", "Cross Product",
    inputs=[Socket("a", WIRE_VECTOR), Socket("b", WIRE_VECTOR)],
    outputs=[Socket("vector", WIRE_VECTOR)],
    code_template={"algebra": "_pt({a}).cross(_pt({b}))"},
    description="Cross product a x b: the vector perpendicular to both. Feed "
                "into Plane (normal) to build a plane from two directions."))

register(NodeDef("DotProduct", "vector", "Dot Product",
    inputs=[Socket("a", WIRE_VECTOR), Socket("b", WIRE_VECTOR)],
    outputs=_data(),
    code_template={"algebra": "_pt({a}).dot(_pt({b}))"},
    description="Dot product a . b (a number). Zero = perpendicular; negative = "
                "pointing away from each other."))

register(NodeDef("Normalize", "vector", "Normalize",
    inputs=[Socket("vector", WIRE_VECTOR)],
    outputs=[Socket("vector", WIRE_VECTOR)],
    code_template={"algebra": "_pt({vector}).normalized()"},
    description="The unit vector (length 1) in the same direction."))

register(NodeDef("VectorLength", "vector", "Vector Length",
    inputs=[Socket("vector", WIRE_VECTOR)],
    outputs=_data("length"),
    code_template={"algebra": "_pt({vector}).length"},
    description="The length (magnitude) of a vector — also the distance of a "
                "point from the origin."))

register(NodeDef("Distance", "vector", "Distance",
    inputs=[Socket("a", WIRE_VECTOR), Socket("b", WIRE_VECTOR)],
    outputs=_data(),
    code_template={"algebra": "(_pt({a}) - _pt({b})).length"},
    description="Distance between two points (a number). Fans out over lists — "
                "e.g. distances from a grid of points to an attractor, into "
                "Remap, into a radius."))

register(NodeDef("Midpoint", "vector", "Midpoint",
    inputs=[Socket("a", WIRE_VECTOR), Socket("b", WIRE_VECTOR)],
    outputs=[Socket("point", WIRE_VECTOR)],
    code_template={"algebra": "((_pt({a}) + _pt({b})) / 2)"},
    description="The point halfway between two points."))

# ===========================================================================
# 9. Lists / data trees (subset)
# ===========================================================================
register(NodeDef("ListCreate", "data", "List",
    inputs=[Socket("items", WIRE_DATA, required=False, multiple=True)],
    outputs=_data(),
    # Flatten so a MIX of lists and single items merges into ONE flat list
    # (wire a list + loose pieces → one unified list), like the containers.
    code_template={"algebra": "_flatten([{items}])"},
    description="Collect inputs into ONE flat list — mixes lists and single "
                "items (each wired list is merged in, not nested)."))

register(NodeDef("ListRange", "data", "Range",
    params=[_f("start", 0, widget="input"),
            _i("count", 5, 0, 100000, soft_max=200),
            _f("step", 1, widget="input")],
    outputs=_data(),
    code_template={"algebra": "[{start} + i*{step} for i in range({count})]"},
    description="Numeric series start, start+step, ..."))

register(NodeDef("ListItem", "data", "List Item",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    params=[_i("index", 0, 0, 100000, widget="input")],
    outputs=_data(),
    code_template={"algebra": "{list}[{index}]"},
    description="Pick an item by index (negative counts from the end)."))

register(NodeDef("ListLength", "data", "List Length",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "len({list})"},
    description="Number of items in a list."))

register(NodeDef("ListFirst", "data", "List First",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "{list}[0]"},
    description="The first item."))

register(NodeDef("ListLast", "data", "List Last",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "{list}[-1]"},
    description="The last item."))

register(NodeDef("ListReverse", "data", "List Reverse",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "list(reversed({list}))"},
    description="Reverse the order of a list."))

register(NodeDef("ListSlice", "data", "List Slice",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    params=[_i("start", 0, -100000, 100000, widget="input"),
            _i("stop", 0, -100000, 100000, widget="input"),
            _i("step", 1, -100000, 100000, widget="input")],
    outputs=_data(),
    code_template={"algebra": "_slice({list}, {start}, {stop}, {step})"},
    description="Sub-list list[start:stop:step]. stop=0 means 'to the end'."))

register(NodeDef("ListFlatten", "data", "List Flatten",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "_flatten({list})"},
    description="Flatten nested lists into one flat list."))

register(NodeDef("ListSort", "data", "List Sort",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    params=[Param("by", "select", "by", "X", widget="select",
                  options=["X", "Y", "Z", "length", "area", "volume", "radius", "value"])],
    outputs=_data(),
    code_template={"algebra": "_sort({list}, {by})"},
    description="Sort a list. Shapes sort by position (X/Y/Z) or metric "
                "(length/area/volume/radius); points by component; numbers by value."))

register(NodeDef("Concat", "data", "Concat",
    inputs=[Socket("a", WIRE_DATA, list_access=True),
            Socket("b", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "(list({a}) + list({b}))"},
    description="Join two lists end to end."))

register(NodeDef("ListRepeat", "data", "List Repeat",
    inputs=[Socket("value", WIRE_DATA, list_access=True)] + _pin("count"),
    params=[_i("count", 3, 0, 100000, soft_max=200)],
    outputs=_data(),
    code_template={"algebra": "_repeat({value}, {count})"},
    description="Repeat a value `count` times -> list (a whole list is "
                "concatenated `count` times). The fan-out multiplier."))

register(NodeDef("ListShift", "data", "List Shift",
    inputs=[Socket("list", WIRE_DATA, list_access=True)] + _pin("offset"),
    params=[_i("offset", 1, -100000, 100000, widget="input"),
            Param("wrap", "bool", "wrap", True, widget="checkbox")],
    outputs=_data(),
    code_template={"algebra": "_shift({list}, {offset}, {wrap})"},
    description="Shift a list by `offset` positions. `wrap` (default) cycles "
                "items around; off drops them from the end instead. Shift a copy "
                "against the original to pair each item with its neighbour."))

register(NodeDef("ListFilter", "data", "List Filter",
    inputs=[Socket("list", WIRE_DATA, list_access=True),
            Socket("pattern", WIRE_DATA, required=False, list_access=True)],
    params=[Param("invert", "bool", "invert", False, widget="checkbox")],
    outputs=_data(),
    code_template={"algebra": "_dispatch({list}, {pattern}, {invert})"},
    description="Keep the items where the boolean `pattern` is True (the "
                "pattern cycles: [True, False] keeps every other item — the "
                "Grasshopper Dispatch/Cull idiom). `invert` keeps the False side."))

register(NodeDef("ListUnique", "data", "List Unique",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data(),
    code_template={"algebra": "_unique({list})"},
    description="Drop duplicate items, keeping the first occurrence of each."))

register(NodeDef("Random", "data", "Random",
    inputs=_pin("count", "min", "max", "seed"),
    params=[_i("count", 10, 0, 100000, soft_max=200),
            _f("min", 0, widget="input"), _f("max", 1, widget="input"),
            _i("seed", 1, 0, 100000, widget="input")],
    outputs=_data(),
    code_template={"algebra": "_randlist({count}, {min}, {max}, {seed})"},
    description="`count` uniform random numbers in [min, max], deterministic "
                "per `seed` — jitter for radii/heights/rotations (fans out)."))

# --- domains, series, remap (parametric glue) ---
register(NodeDef("ConstructDomain", "data", "Domain",
    inputs=_pin("min", "max"),
    params=[_f("min", 0, widget="input"), _f("max", 1, widget="input")],
    outputs=_data("domain"),
    code_template={"algebra": "[{min}, {max}]"},
    description="A numeric domain [min, max] (a 2-item list). Feed into Remap or "
                "Divide Domain."))

register(NodeDef("Series", "data", "Series",
    inputs=_pin("start", "step", "count"),
    params=[_f("start", 0, widget="input"), _f("step", 1, widget="input"),
            _i("count", 10, 0, 100000)],
    outputs=_data(),
    code_template={"algebra": "[{start} + i*{step} for i in range(int({count}))]"},
    description="Arithmetic series: start, start+step, … (count items). Drives "
                "params downstream (e.g. into Circle.radius) — fans out."))

register(NodeDef("DivideDomain", "data", "Divide Domain",
    inputs=[Socket("domain", WIRE_DATA, list_access=True)] + _pin("count"),
    params=[_i("count", 10, 1, 100000)],
    outputs=_data(),
    code_template={"algebra": "_divide_domain({domain}, {count})"},
    description="N values evenly spanning a domain [min, max], endpoints "
                "inclusive. Fans out downstream."))

register(NodeDef("Remap", "data", "Remap",
    inputs=[Socket("value", WIRE_DATA),
            Socket("source", WIRE_DATA, required=False, list_access=True),
            Socket("target", WIRE_DATA, required=False, list_access=True)],
    params=[_f("source_min", 0, widget="input"), _f("source_max", 1, widget="input"),
            _f("target_min", 0, widget="input"), _f("target_max", 10, widget="input")],
    outputs=_data(),
    code_template={"algebra": "_remap({value}, {source}, {target}, {source_min}, "
                              "{source_max}, {target_min}, {target_max})"},
    description="Remap value(s) from a source range to a target range. Wire a "
                "[min,max] domain into source/target to override the widgets. Fans "
                "out over a list of values (Series → Remap → radius)."))

register(NodeDef("Bounds", "data", "Bounds",
    inputs=[Socket("list", WIRE_DATA, list_access=True)],
    outputs=_data("domain"),
    code_template={"algebra": "_bounds({list})"},
    description="The [min, max] domain spanning a list of numbers."))

register(NodeDef("GraphMapper", "data", "Graph Mapper",
    inputs=_pin("t"),
    params=[_f("t", 0.0, 0.0, 1.0, 0.01, label="t"),
            Param("mode", "select", "mode", "smooth", widget="select",
                  options=["smooth", "linear", "bezier"]),
            Param("curve", "curve", "curve",
                  [{"p": [0.0, 0.0], "hi": None, "ho": None},
                   {"p": [1.0, 1.0], "hi": None, "ho": None}],
                  widget="curve", raw=True)],
    outputs=_data(),
    code_template={"algebra": "_graphmap({t}, {curve}, {mode})"},
    description="An editable function curve f(x)->y drawn on the node. Wire a t "
                "(or a list/Range of t) into it to sample the curve — smooth "
                "(monotone, no overshoot), linear, or bezier with tangent handles. "
                "Fans out over a list of t (Divide Curve t → radii → variable "
                "loft). Pipe the output through Remap to reach a target range."))

# ===========================================================================
# 10. Math
# ===========================================================================
def _binop(type_, label, op):
    register(NodeDef(type_, "math", label,
        inputs=[Socket("a", WIRE_DATA), Socket("b", WIRE_DATA)],
        outputs=_data(),
        code_template={"algebra": f"({{a}} {op} {{b}})"},
        description=f"{label}."))


_binop("Add", "Add", "+")
_binop("SubtractNum", "Subtract (num)", "-")
_binop("Multiply", "Multiply", "*")
_binop("Divide", "Divide", "/")
_binop("Power", "Power", "**")

register(NodeDef("Clamp", "math", "Clamp",
    inputs=[Socket("x", WIRE_DATA)],
    params=[_f("min", 0, widget="input"), _f("max", 10, widget="input")],
    outputs=_data(),
    code_template={"algebra": "max({min}, min({x}, {max}))"},
    description="Clamp x to [min, max]."))

register(NodeDef("Expression", "math", "Expression",
    inputs=[Socket("x", WIRE_DATA, required=False), Socket("y", WIRE_DATA, required=False)],
    params=[Param("expr", "str", "expr", "x + y", widget="input", raw=True)],
    outputs=_data(),
    code_template={"algebra": "({expr})"},
    description="Evaluate an expression of inputs x, y."))


def _mathfn(type_, label, expr, desc):
    """A one-input math function node; fans out over a list of x."""
    register(NodeDef(type_, "math", label,
        inputs=[Socket("x", WIRE_DATA)],
        outputs=_data(),
        code_template={"algebra": expr},
        description=desc))


_mathfn("Sin", "Sin", "math.sin(math.radians({x}))", "Sine of x (degrees).")
_mathfn("Cos", "Cos", "math.cos(math.radians({x}))", "Cosine of x (degrees).")
_mathfn("Tan", "Tan", "math.tan(math.radians({x}))", "Tangent of x (degrees).")
_mathfn("ArcSin", "ArcSin", "math.degrees(math.asin({x}))",
        "Inverse sine -> degrees (x in [-1, 1]).")
_mathfn("ArcCos", "ArcCos", "math.degrees(math.acos({x}))",
        "Inverse cosine -> degrees (x in [-1, 1]).")
_mathfn("ArcTan", "ArcTan", "math.degrees(math.atan({x}))",
        "Inverse tangent -> degrees.")
_mathfn("Sqrt", "Sqrt", "math.sqrt({x})", "Square root of x.")
_mathfn("Abs", "Abs", "abs({x})", "Absolute value of x.")
_mathfn("Floor", "Floor", "math.floor({x})", "Round x down to an integer.")
_mathfn("Ceil", "Ceil", "math.ceil({x})", "Round x up to an integer.")

register(NodeDef("Round", "math", "Round",
    inputs=[Socket("x", WIRE_DATA)],
    params=[_i("decimals", 0, 0, 12, widget="input")],
    outputs=_data(),
    code_template={"algebra": "round({x}, {decimals})"},
    description="Round x to `decimals` decimal places."))

_binop("Modulo", "Modulo", "%")

register(NodeDef("Min", "math", "Min",
    inputs=[Socket("a", WIRE_DATA), Socket("b", WIRE_DATA)],
    outputs=_data(),
    code_template={"algebra": "min({a}, {b})"},
    description="The smaller of a and b. For a whole list use Bounds (its "
                "domain is [min, max])."))

register(NodeDef("Max", "math", "Max",
    inputs=[Socket("a", WIRE_DATA), Socket("b", WIRE_DATA)],
    outputs=_data(),
    code_template={"algebra": "max({a}, {b})"},
    description="The larger of a and b. For a whole list use Bounds (its "
                "domain is [min, max])."))

# ===========================================================================
# 11. Panels / inspection
# ===========================================================================
register(NodeDef("ToAgent", "data", "To Agent",
    inputs=[Socket("value", WIRE_DATA, required=False, list_access=True)],
    params=[Param("label", "str", "label", "", widget="input"),
            Param("date", "str", "date", "", widget="input")],
    outputs=_data("value"),
    subtype_follows="value",
    code_template={"algebra": "{value}"},
    description="Provenance tag for the AI agent/copilot. Whatever you wire in "
                "passes through UNCHANGED, but the node registers label + date + "
                "location (workflow, node id, upstream source) in the agent's "
                "index, so you can say 'retro-engineer part X in workflow Y'. "
                "Tag an ImportSTEP/ImportSTL to hand the agent a file; `date` "
                "is stamped automatically when the graph is saved."))

register(NodeDef("Note", "panel", "Note",
    inputs=[], outputs=[],
    params=[Param("text", "str", "text", "", widget="note")],
    code_template={"algebra": "None"},   # never emitted — the transpiler skips Note
    description="A sticky note — free-text annotation and instructions pinned to "
                "the canvas, in the spirit of ComfyUI's Note node. Multi-line text "
                "that keeps its formatting; it has no sockets and no effect on the "
                "model. Double-click to edit."))

# The old dual-mode Panel is gone, split into two single-responsibility nodes:
# Input (text -> value SOURCE) and Display (inspect + route a wired value). Both
# live in the `panel` category so the add-node search still finds them by typing
# "panel" (litegraph matches the category/type path). Legacy Panel nodes are
# remapped on load (see nodes.html::fromGraphJSON).
register(NodeDef("Input", "panel", "Input",
    params=[Param("text", "str", "text", "", widget="note"),
            Param("mode", "select", "mode", "friendly", widget="select",
                  options=["friendly", "json", "build123d"])],
    outputs=_data("value"),
    code_template={"algebra": "_panel({node_id!r}, None, {text}, {mode})"},
    description="Text -> data SOURCE. Type values, one per line (several lines = "
                "a list that fans out downstream). `mode` picks the syntax: "
                "friendly ('0,0,0' -> Vector), json, or build123d (eval). No "
                "input — a pure value source on the wire. Double-click to edit."))

register(NodeDef("Display", "panel", "Display",
    inputs=[Socket("value", WIRE_DATA, required=False, list_access=True)],
    outputs=_data("value"),
    subtype_follows="value",   # pass-through: keep the upstream tag
    code_template={"algebra": "_probe({node_id!r}, {value})"},
    description="Display + routing. Wire a value in to SEE it — shown on the node "
                "itself AND in the Panels tab — and passed through unchanged, so "
                "you can tap a wire without altering the data."))

register(NodeDef("BoundingBox", "panel", "Bounding Box",
    inputs=[Socket("shape", WIRE_SOLID)],
    outputs=[Socket("box", WIRE_SOLID)],
    code_template={"algebra": "_bbox_solid({shape})"},
    description="Axis-aligned bounding box of a shape, as a real solid Box you "
                "can render, subtract or extrude from. A flat 2D input (e.g. "
                "Text) yields a thin slab (its zero-thickness side gets a tiny "
                "proportional thickness so it stays a valid solid)."))

register(NodeDef("Volume", "panel", "Volume",
    inputs=[Socket("shape", WIRE_SOLID)],
    outputs=_data("volume"),
    code_template={"algebra": "{shape}.volume"},
    description="Volume of a solid (model units cubed). Wire into a Panel to "
                "read it."))

register(NodeDef("Area", "panel", "Area",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_SURFACE])],
    outputs=_data("area"),
    code_template={"algebra": "{shape}.area"},
    description="Total surface area of a shape (for a solid: all its faces)."))

register(NodeDef("CenterOfMass", "panel", "Center of Mass",
    # Universal + casts from anything: solids, faces, curves, points/vertices. The
    # whole input is consumed as one (list_access), so a point cloud / a set of
    # shapes gives ONE aggregate centre. Special-cased by the transpiler
    # (_emit_center) into TWO outputs — see out_var_of.
    inputs=[Socket("shape", WIRE_SOLID, list_access=True,
                   accepts=[WIRE_SURFACE, WIRE_CURVE, WIRE_VECTOR, WIRE_PLANE, WIRE_SELECTION])],
    outputs=[Socket("center", WIRE_VECTOR), Socket("volume", WIRE_DATA)],
    code_template={"algebra": ""},   # handled by the transpiler, not a template
    description="Centre + volume of whatever you feed it. Closed solids -> centre "
                "of mass + volume; faces -> area centroid; open/closed curves -> "
                "curve centroid (a straight line gives its midpoint, a circle its "
                "centre); a point cloud -> the mean point. `center` is a vector "
                "(wire into Align.ref / Move.offset); `volume` reads in a Panel."))

# ===========================================================================
# 11b. Containers / legend — one typed pass-through per wire type. They colour
# the wire, label the graph (a legend) and inspect the value (Panels tab) without
# changing it. A cast/convert mode is planned (PLAN_DATA_PROTOCOL.md).
# ===========================================================================
# What a gated container (filter/transform) extracts in transform mode, for the
# node description. The runtime rules live in transpiler `_gate`.
_GATE_DESC = {
    "surface":   "fills closed curves and pulls the PLANAR faces out of solids",
    "curve":     "outlines surfaces, pulls the edges out of solids and joins points",
    "point":     "explodes any shape into its points (vertices, a plane's origin, …)",
    "solid":     "explodes a compound into its individual solids",
    "plane":     "reads the Plane of each planar face of a surface or solid",
    "selection": "also pulls a solid's faces/edges/vertices into the selection",
}


def _container(type_: str, label: str, wire: str,
               gate: str | None = None,
               accepts: list[str] | None = None) -> NodeDef:
    if gate:
        # filter/transform gate targeting the container's OWN type. `filter` lets
        # only values already of that type through; `transform` also coerces /
        # extracts everything compatible into it. Output is always a list (it may
        # explode a shape), so downstream fans out. Input is a `multiple` collector
        # widened (via `accepts`) to take the coercible source types.
        return register(NodeDef(type_, "container", label,
            inputs=[Socket("value", wire, required=False, multiple=True,
                           accepts=accepts or [])],
            params=[Param("mode", "select", "mode", "filter",
                          widget="select", options=["filter", "transform"])],
            outputs=[Socket("value", wire)],
            subtype_follows="value",   # filter keeps the upstream tag; transform drops it
            code_template={"algebra": f"_gate({{node_id!r}}, [{{value}}], '{gate}', {{mode}})"},
            description=f"{label} container — a typed filter / transformer. "
                        f"`filter` (default) passes only values that are already "
                        f"{label.lower()}s; `transform` also coerces what's "
                        f"compatible ({_GATE_DESC.get(gate, '')}). Always outputs a "
                        f"list (fans out) and shows the result in the Panels tab."))
    return register(NodeDef(type_, "container", label,
        inputs=[Socket("value", wire, required=False, list_access=True)],
        outputs=[Socket("value", wire)],
        subtype_follows="value",   # legend pass-through: keep the upstream tag
        code_template={"algebra": "_probe({node_id!r}, {value})"},
        description=f"{label} container / legend: a typed pass-through. Wire a "
                    f"{label.lower()} through it to colour the wire, label the "
                    f"graph and inspect the value in the Panels tab — unchanged."))

_container("Geometry", "Geometry", WIRE_SOLID, gate="solid")
_container("Surface", "Surface", WIRE_SURFACE, gate="surface",
           accepts=[WIRE_SOLID, WIRE_CURVE])
_container("Curve", "Curve", WIRE_CURVE, gate="curve",
           accepts=[WIRE_SOLID, WIRE_SURFACE, WIRE_VECTOR])
_container("Point", "Point", WIRE_VECTOR, gate="point",
           accepts=[WIRE_SOLID, WIRE_SURFACE, WIRE_CURVE, WIRE_SELECTION, WIRE_PLANE])
_container("Plane", "Plane", WIRE_PLANE, gate="plane",
           accepts=[WIRE_SURFACE, WIRE_SOLID])
_container("Selection", "Selection", WIRE_SELECTION, gate="selection",
           accepts=[WIRE_CURVE, WIRE_SURFACE, WIRE_SOLID, WIRE_VECTOR])

# ===========================================================================
# 12. Inputs / parameters
# ===========================================================================
register(NodeDef("NumberSlider", "input", "Number Slider",
    params=[_f("value", 5, 0, 100, widget="slider")],
    outputs=_data(),
    code_template={"algebra": "{value}"},
    description="A float value."))

register(NodeDef("IntegerSlider", "input", "Integer Slider",
    params=[_i("value", 5, 0, 100, widget="slider")],
    outputs=_data(),
    code_template={"algebra": "{value}"},
    description="An int value."))

register(NodeDef("NumberInput", "input", "Number",
    params=[_f("value", 10, widget="input")],
    outputs=_data(),
    code_template={"algebra": "{value}"},
    description="A typed float value."))

register(NodeDef("BooleanToggle", "input", "Boolean",
    params=[Param("value", "bool", "value", True, widget="checkbox")],
    outputs=_data(),
    code_template={"algebra": "{value}"},
    description="A boolean value."))

register(NodeDef("StringInput", "input", "String",
    params=[Param("value", "str", "value", "label", widget="input")],
    outputs=_data(),
    code_template={"algebra": "{value}"},
    description="A string value."))

# ===========================================================================
# 12b. Import / IO
# ===========================================================================
# Load an external model file into geometry. `path` is an "asset" picker: it lists
# the files already imported into the project's library (projects/<name>/assets/),
# filtered by the extensions in `options`, and stores a project-relative path
# (e.g. "assets/part.step") — which resolves because the worker runs with the
# project dir as cwd. Files get there via the UI upload (POST .../asset|import).
def _asset(name, exts):
    return Param("path", "str", "file", "", widget="asset", options=exts)

register(NodeDef("ImportSTEP", "import", "Import STEP",
    params=[_asset("path", [".step", ".stp"])],
    outputs=_geo(),
    code_template={"algebra": "import_step({path})"},
    description="Load a STEP file as solid geometry (Compound)."))

register(NodeDef("ImportSTL", "import", "Import STL",
    params=[_asset("path", [".stl"])],
    outputs=_geo(),
    code_template={"algebra": "import_stl({path})"},
    description="Load an STL mesh as a Face (good for reference; limited for solid ops)."))

register(NodeDef("ImportSVG", "import", "Import SVG",
    params=[_asset("path", [".svg"])],
    outputs=_cv(),
    code_template={"algebra": "import_svg({path})"},
    description="Load 2D profiles from an SVG file (wires/faces) to extrude or build on."))

register(NodeDef("ImportDXF", "import", "Import DXF",
    params=[_asset("path", [".dxf"])],
    outputs=_cv(),
    code_template={"algebra": "import_dxf({path})"},
    description="Load 2D profiles from a DXF file (wires) to extrude or build on."))

# TraceImage vectorizes a PNG/JPG into curves. Like SelectEdge it has an EMPTY
# code_template — the transpiler special-cases it (see _emit_vectorize). Its
# ✎ edit-mode (rembg + magic-wand + pen + 2-point scale) freezes the traced
# contours + mm/pixel scale into params["trace"], so the graph re-runs from
# fixed data with no image processing at run time. See PLAN_TRACE_IMAGE.md.
register(NodeDef("TraceImage", "import", "Trace Image",
    params=[_asset("path", [".png", ".jpg", ".jpeg"])],
    outputs=_cv(),
    code_template={"algebra": ""},   # handled by the transpiler, not a template
    description="Vectorize a PNG/JPG into curves. Open ✎ Edit to remove the "
                "background, trace the contours and set a real scale (mm) — the "
                "traced outline is frozen into the node so the graph re-runs with "
                "fixed data (no image processing at run time)."))

# A modelling reference: shows an image on a plane in the 3D viewport (a
# blueprint underlay to trace/align over). Editor-only, like Note — the
# transpiler skips it and it never becomes geometry; nodes.html draws a textured
# quad in the shared viewer scene from these params.
register(NodeDef("RefImage", "import", "Reference Image",
    inputs=[], outputs=[],
    params=[_asset("path", [".png", ".jpg", ".jpeg"]),
            Param("align_to_trace", "bool", "align to trace", True, widget="checkbox"),
            Param("plane", "select", "plane", "XY", widget="select", options=["XY", "XZ", "YZ"]),
            _f("width", 100, 1, 2000, label="width"),
            _f("x", 0, -1000, 1000), _f("y", 0, -1000, 1000), _f("z", 0, -1000, 1000),
            _f("opacity", 0.6, 0, 1, 0.05, label="opacity")],
    code_template={"algebra": "None"},   # editor-only — the transpiler skips it (like Note)
    description="Show an image on a plane in the 3D viewport as a modelling "
                "reference (blueprint underlay). Editor-only: it draws a textured "
                "quad and never becomes geometry. With 'align to trace' on (default), "
                "if a Trace Image node uses the same file the quad snaps to that "
                "trace's scale/position so the reference and the traced curves match "
                "1:1; otherwise place it manually via plane, width (mm), centre (x,y,z)."))

# ===========================================================================
# 12b. The mesh lane (PLAN_MESH_LANE.md)
#
# Triangles are their own kind of geometry, not a degenerate B-Rep. build123d
# cannot model them: `import_stl` gives a Face with no surface (booleans on it
# are refused), and `Mesher.read` sews every triangle into a planar B-Rep face —
# 300s to open a 147k-triangle part, 81s per boolean. These nodes run on trimesh
# instead: 0.16s to open the same file, 0.31s to repair it.
#
# Transforms are NOT duplicated here — Move/Rotate/Scale/Mirror take a mesh
# directly (§6 above). The bridge back to the B-Rep lane (MeshToSolid) and the
# booleans (manifold3d) land in phase 2.
# ===========================================================================
register(NodeDef("ImportMesh", "mesh", "Import Mesh",
    params=[_asset("path", [".stl", ".obj", ".ply", ".3mf"])],
    outputs=_mesh(),
    code_template={"algebra": "_mesh_load({path})"},
    description="Load an STL/OBJ/PLY/3MF as a real triangle mesh you can repair "
                "and operate on. Prefer this over Import STL, which hands the mesh "
                "to the B-Rep kernel as a surface with no volume."))

register(NodeDef("ToMesh", "mesh", "To Mesh",
    inputs=[Socket("shape", WIRE_SOLID)],
    params=[_f("tolerance", 0.1, 0.001, 5.0, 0.01, label="tolerance")],
    outputs=_mesh(),
    code_template={"algebra": "_to_mesh({shape}, {tolerance})"},
    description="Tessellate a solid/surface into a mesh. `tolerance` is the max "
                "deviation in mm — smaller means more triangles. (Wiring a solid "
                "straight into a mesh input does this for you.)"))

register(NodeDef("MeshFix", "mesh", "Mesh Fix",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[_i("min_body", 16, 0, 5000, label="min body (tris)"),
            Param("fill_holes", "bool", "fill holes", True, widget="checkbox")],
    outputs=_mesh(),
    code_template={"algebra": "_mesh_fix({mesh}, {min_body}, {fill_holes})"},
    description="Repair a mesh so boolean ops will accept it: merge duplicate "
                "vertices, drop duplicate/degenerate faces, discard stray shards "
                "smaller than `min body`, fill holes, fix normals. Most STLs that "
                "'fail' need only this."))

register(NodeDef("MeshInspect", "mesh", "Mesh Inspect",
    inputs=[Socket("mesh", WIRE_MESH)],
    outputs=_data("report"),
    code_template={"algebra": "_mesh_inspect({mesh})"},
    description="Report a mesh's health as text — triangles, watertight, bodies, "
                "boundary and non-manifold edges, euler, volume, area. Wire it into "
                "a Panel to see WHY a mesh misbehaves before you operate on it."))

register(NodeDef("ExportMesh", "mesh", "Export Mesh",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[Param("path", "str", "path", "output.stl", widget="input")],
    outputs=[],
    code_template={"algebra": "_mesh_export({mesh}, _out({path}))"},
    description="Write a mesh to STL/OBJ/PLY/3MF (the extension picks the format), "
                "into the project's exports/ folder."))

# --- mesh booleans (manifold3d, Apache-2.0) --------------------------------
# Same shape as the B-Rep boolean nodes above, so the two lanes read alike: a
# collector Union, a Subtract whose `b` swallows a whole list of tools. Inputs
# must be closed volumes — an open mesh raises a message pointing at Mesh Fix.
register(NodeDef("MeshUnion", "mesh", "Mesh Union",
    inputs=[Socket("shapes", WIRE_MESH, multiple=True)],
    outputs=_mesh(),
    code_template={"algebra": "_mesh_bool('union', {shapes})"},
    description="Boolean union of meshes — fuses everything wired in. 0.1s on a "
                "147k-triangle part, where the B-Rep lane needs 81s. Inputs must be "
                "watertight (use Mesh Fix)."))

register(NodeDef("MeshSubtract", "mesh", "Mesh Subtract",
    inputs=[Socket("a", WIRE_MESH), Socket("b", WIRE_MESH, list_access=True)],
    outputs=_mesh(),
    code_template={"algebra": "_mesh_bool('subtract', {a}, {b})"},
    description="Boolean difference A - B on meshes. `b` may be a single mesh or a "
                "whole list of cutters (all subtracted)."))

register(NodeDef("MeshIntersect", "mesh", "Mesh Intersect",
    inputs=[Socket("a", WIRE_MESH), Socket("b", WIRE_MESH)],
    outputs=_mesh(),
    code_template={"algebra": "_mesh_bool('intersect', {a}, {b})"},
    description="Boolean intersection A & B on meshes."))

register(NodeDef("Voronoi3D", "mesh", "Voronoi 3D",
    inputs=[Socket("points", WIRE_VECTOR, list_access=True),
            Socket("body", WIRE_MESH, required=False)],
    params=[_f("scale", 0.9, 0.05, 1.0, 0.05, label="scale")],
    outputs=[Socket("cells", WIRE_MESH)],
    code_template={"algebra": "_voronoi3d({points}, {body}, {scale})"},
    description="TRUE 3D Voronoi: each point becomes a closed convex mesh CELL, "
                "clipped to `body` if wired (a solid casts in automatically). "
                "`scale` shrinks every cell toward its centre — Mesh Subtract "
                "the shrunk cells from the body and the walls between cells "
                "become the part: a voronoi lattice. Feed `points` from "
                "Populate with the same body in its region (a volume fill). "
                "List output — fans out downstream. Cap 2000 points."))

register(NodeDef("MeshSimplify", "mesh", "Mesh Simplify",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[_f("tolerance", 0.05, 0.001, 5.0, 0.01, label="tolerance (mm)"),
            _f("max_error", 5.0, 0.1, 100.0, 0.5, label="max error (% vol)")],
    outputs=_mesh(),
    code_template={"algebra": "_mesh_simplify({mesh}, {tolerance}, {max_error})"},
    description="Reduce triangle count within a BOUNDED deviation: `tolerance` is the "
                "most any surface may move, in mm — say how much error you accept, not "
                "how many triangles you want. 147k -> 35k triangles at 0.05mm costs "
                "0.07s and 0.1% of the volume. Keep the tolerance BELOW the part's wall "
                "thickness: above it, simplify tears thin walls apart. The node checks "
                "its own result and fails loudly (volume moved past `max error`, or the "
                "part came out in more pieces) rather than handing back a broken mesh."))

register(NodeDef("MeshToSolid", "mesh", "Mesh to Solid",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[_i("max_tris", 20000, 100, 200000, label="max tris", soft_max=50000)],
    outputs=_geo(),
    code_template={"algebra": "_mesh_to_solid({mesh}, {max_tris})"},
    description="The bridge BACK to the B-Rep lane: sew the triangles into a solid you "
                "can Fillet/Shell/export as STEP. Know what you get — every triangle "
                "becomes a planar FACE, so it is a faceted solid, not a reconstructed "
                "CAD model: 35k triangles took 54s to sew and wrote a 92MB STEP; 147k "
                "takes ~300s and cripples every boolean after it. Simplify first, and "
                "the guard refuses above `max tris` rather than hanging. To get a REAL "
                "parametric model out of a mesh, rebuild it with nodes (retroeng, "
                "PLAN_RETROENG.md) — don't sew it."))


# ===========================================================================
# 12c. Print physics — how it lands, what it costs, where it breaks
# ===========================================================================
# A printed part is ANISOTROPIC: the bond between two layers is roughly a third to
# two thirds as strong as the material within a layer. So the orientation is not a
# convenience — it decides where the part breaks, and by how much. These four nodes
# measure that (PLAN_PRINT_PHYSICS.md). They are heuristics, not an FEA: they catch
# the dominant failure mode (the part splits at the glued interface with the least
# area) and say nothing about a stress riser around a hole.

register(NodeDef("PlaceOnBed", "print", "Place on Bed",
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_SURFACE, WIRE_MESH])],
    params=[Param("center", "bool", "centre in XY", True, widget="checkbox"),
            _f("clearance", 0.0, 0.0, 10, label="clearance")],
    outputs=_geo(),
    output_follows="shape",
    code_template={"algebra": "_bed_drop({shape}, {center}, {clearance})"},
    description="Sit the part on the bed: its lowest point goes to z=0. Works on both "
                "lanes — a solid stays a solid, a mesh stays a mesh. It measures on the "
                "tessellation rather than the bounding box on purpose: the fast OCCT box "
                "is oversized (the live view marks its bbox `approx`), so a part dropped "
                "by it hovers above the bed by up to 1% of its size — invisible on "
                "screen, and a failed first layer."))

register(NodeDef("Drop", "print", "Drop",
    aliases=["Drop on Plane", "Fall", "Gravity", "Bounce", "Settle"],
    inputs=[Socket("shape", WIRE_SOLID, accepts=[WIRE_SURFACE, WIRE_MESH]),
            Socket("container", WIRE_SOLID, accepts=[WIRE_SURFACE, WIRE_MESH],
                   required=False),
            Socket("motion", WIRE_DATA, required=False),
            Socket("plane", WIRE_PLANE, required=False)] + _pin("t"),
    params=[_f("t", 1.0, 0.0, 1.0, step=0.01, label="timeline"),
            Param("material", "select", "material", "plastic", widget="select",
                  options=["plastic", "rubber", "steel", "wood", "lead", "clay"]),
            Param("settle", "bool", "settle (topple)", True, widget="checkbox"),
            Param("collide", "bool", "collide (stack)", False, widget="checkbox"),
            _f("grip", 1.0, 0.0, 2.0, step=0.05, label="grip (friction)")],
    outputs=_geo(),
    output_follows="shape",
    gizmo={"kind": "timeline", "binds": ["t"], "anchor": "preview", "lock": ["t"]},
    code_template={"algebra": "_drop({shape}, {plane}, {t}, {material}, {settle}, "
                              "{collide}, {container}, {grip}, {motion})"},
    description="Place on Bed, but as a FALL you can scrub: drag `timeline` from 0 "
                "(where the part is now) to 1 (at rest on the plane). The part drops "
                "under gravity, BOUNCES — each impact keeps a fixed fraction of the "
                "speed, set by `material`: rubber keeps going (e=0.85), plastic "
                "clatters (0.55), lead lands with one dead thud (0.08), clay just "
                "stops — and then, with `settle`, TOPPLES for real: if its centre of "
                "mass is not over the contact patch it tips about the support edge, "
                "rolls onto the next face of its hull, and repeats until it rests in "
                "a genuinely stable pose (a cube dropped on its edge falls flat; a "
                "sphere is left alone — rolling releases no energy and is not a "
                "topple). t=1 is always fully at rest — material and topples change "
                "the SHAPE of the journey, not its length. Falls along the wired "
                "plane's normal (default: the bed, z=0). Wire a Number Slider into "
                "`t` to drive several Drops from ONE timeline, or a list of times to "
                "scatter the trajectory as a motion trail. With `collide` on, SEVERAL "
                "shapes wired into this one node fall as ONE SCENE instead of a fan: "
                "real rigid-body dynamics (pybullet), so they fall TOGETHER — colliding "
                "in mid-air, pushing each other over, tumbling and stacking — simulated "
                "once and recorded as keyframes the slider scrubs (and the browser "
                "replays live). It costs real compute, hence the toggle. Each body is "
                "its convex HULL, and the pile is "
                "deterministic for a given scene but chaotic like real falling — nudge "
                "a part and it lands differently. Both lanes — a solid stays a solid. "
                "Wire a shape into `container` and it becomes an IMMOVABLE collider that "
                "the falling parts land in: a bowl, a tray, a crate. Unlike the falling "
                "parts it is NOT hulled — it keeps its true concave surface, so a bowl "
                "really does cradle what you pour into it, and its inner wall stops the "
                "pile spreading. It never moves and is not an output (preview the bowl "
                "node itself); wiring one turns scene mode on by itself, so a single part "
                "still falls into it. `grip` scales the friction of the whole scene: 1 is "
                "the default world, where parts settle and stay put. Turn it DOWN and "
                "everything gets slippery — which is not a detail. On a sloped face high "
                "friction grabs a part and flings it sideways instead of letting it slide "
                "off, so a Galton board built at grip 1 throws its balls to the walls and "
                "the bell collapses into two lumps; at 0.3 it comes out normal. Measured, "
                "not guessed."))

register(NodeDef("ContainerMotion", "print", "Container Motion",
    aliases=["Shake", "Tilt", "Pour", "Stir", "Agitate", "Spin", "Vibrate",
             "Tumble", "Shaker"],
    inputs=[Socket("offset", WIRE_VECTOR, required=False),
            Socket("pivot", WIRE_VECTOR, required=False)],
    params=[_f("x", 0, -500, 500, label="move x"),
            _f("y", 0, -500, 500, label="move y"),
            _f("z", 0, -500, 500, label="move z"),
            _f("rx", 0, -720, 720, step=1, label="rotate x"),
            _f("ry", 0, -720, 720, step=1, label="rotate y"),
            _f("rz", 0, -720, 720, step=1, label="rotate z"),
            _f("cycles", 0, 0, 30, step=0.5, label="cycles (0=one way)"),
            _f("duration", 1.0, 0.05, 20, step=0.05, label="duration (s)"),
            _f("delay", 0.0, 0.0, 20, step=0.05, label="delay (s)"),
            Param("easing", "select", "easing", "smooth", widget="select",
                  options=["smooth", "linear"])],
    outputs=_data("motion"),
    code_template={"algebra": "_container_motion({offset}, {x}, {y}, {z}, "
                              "{rx}, {ry}, {rz}, {pivot}, {cycles}, {duration}, "
                              "{delay}, {easing})"},
    description="MOVE the thing that was holding still: wire this into a Drop's "
                "`motion` and its `container` — the bowl, the tray, the crate — "
                "tilts, shakes or spins on the same timeline instead of just "
                "sitting there. This is not gravity and not a fall. You dictate "
                "the motion; the parts inside answer to it through contact and "
                "friction alone, which is why they lag, slide, climb the wall and "
                "spill rather than following it rigidly. `cycles` picks the shape "
                "of the motion and everything else falls out of it: 0 is a RAMP — "
                "go there once and stay, which is a tilt, a pour, a crate tipped "
                "over; above 0 it OSCILLATES about the starting pose that many "
                "times, which is a shake, a stir, a vibration, a tap. So: pour = "
                "rotate x/y ~110 (past the wall, or nothing comes out) with cycles "
                "0; shake = move 10 with cycles 8; settle a powder = move z 3, "
                "cycles 20; centrifuge = rotate z 720. `delay` waits before it "
                "starts — fill the bowl first, THEN tilt it. Rotation is about the "
                "container's own centre unless you wire a `pivot` (the hinge of a "
                "hopper, the lip a crate tips over). Motion needs a container: on "
                "its own it does nothing. Costs almost nothing to drive (~5ms per "
                "simulated second), but it keeps the scene awake for its whole "
                "duration — a shaker never settles, so it runs the full length."))

register(NodeDef("PrintCheck", "print", "Print Check",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[_f("angle", 45, 20, 80, label="overhang angle"),
            _f("layer", 0.2, 0.05, 1.0, label="layer height", step=0.05),
            _f("clearance", 0.2, 0, 2, label="support gap", step=0.05)],
    outputs=_data("report"),
    code_template={"algebra": "_print_check({mesh}, {angle}, {layer}, {clearance})"},
    description="The print report, as text — wire it into a Panel. Height and layer "
                "count, bed contact, the support material in cm3 and grams (the REAL "
                "volume — prisms under every overhang down to the bed, minus the part — "
                "not an area), and WHERE IT WILL BREAK: the weak plane is the smallest "
                "glued cross-section in the part, and a printed part comes apart at a "
                "layer line before it breaks anywhere else. Put the part on the bed "
                "first."))

register(NodeDef("SupportVolume", "print", "Support Volume",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[_f("angle", 45, 20, 80, label="overhang angle"),
            _f("layer", 0.2, 0.05, 1.0, label="layer height", step=0.05),
            _f("clearance", 0.2, 0, 2, label="support gap", step=0.05)],
    outputs=_mesh(),
    code_template={"algebra": "_support_body({mesh}, {angle}, {layer}, {clearance})"},
    description="The support material itself, as a BODY — drop a prism from every "
                "overhanging triangle to the bed, union them, subtract the part (and the "
                "part shifted down by `support gap`, which carves the clearance the "
                "support must leave or it welds itself on). Preview it to see what you "
                "are about to pay for, wire it into a Mesh Inspect for the volume, or "
                "export it. This is the honest number that `area x height` only "
                "gestures at: on a sphere of r=20 sitting on the bed it returns 1.63 cm3 "
                "against 1.73 worked out with a pencil. ~0.6s on a 20k-triangle part."))

register(NodeDef("OverhangFaces", "print", "Overhang Faces",
    inputs=[Socket("mesh", WIRE_MESH)],
    params=[_f("angle", 45, 20, 80, label="overhang angle"),
            _f("layer", 0.2, 0.05, 1.0, label="layer height", step=0.05)],
    outputs=_mesh(),
    code_template={"algebra": "_overhang_faces({mesh}, {angle}, {layer})"},
    description="Just the faces that will need support, as a mesh of their own — so the "
                "viewer gives them their own colour and you SEE them on the part. The "
                "faces resting ON the bed are excluded: a flat base points straight down "
                "too, and calling that an overhang is the classic way to get this wrong. "
                "It is an open patch, not a body — MeshInspect will say so, and it is "
                "right to."))

register(NodeDef("OrientForPrint", "print", "Orient for Print",
    inputs=[Socket("mesh", WIRE_MESH),
            Socket("load", WIRE_VECTOR, required=False)],
    params=[_f("strength", 1.0, 0, 3, label="strength", step=0.1),
            _f("supports", 1.0, 0, 3, label="fewer supports", step=0.1),
            _f("speed", 0.3, 0, 3, label="speed (height)", step=0.1),
            _f("angle", 45, 20, 80, label="overhang angle"),
            _f("layer", 0.2, 0.05, 1.0, label="layer height", step=0.05),
            _i("exact_below", 25000, 0, 200000, label="exact below (tris)",
               soft_max=50000)],
    outputs=[Socket("result", WIRE_MESH), Socket("report", WIRE_DATA)],
    code_template={"algebra": ""},   # handled by the transpiler (_emit_orient), not a template
    description="Try every stable resting pose (the faces of the convex hull the centre "
                "of mass sits over — let go of the part and it stays), score each, and "
                "return the winner already sitting on the bed. `report` is the table of "
                "the top five, so you can see why. Wire a vector into `load` — the "
                "direction the part will actually be pulled — and strength becomes the "
                "real question: how much of that load crosses the layers instead of "
                "running along them. With no load declared it falls back to maximising "
                "the smallest glued cross-section. Support is the TRUE volume (a boolean "
                "per pose) while the part is under `exact below` triangles, and the "
                "area x height proxy above it — the report says which it used, all-or-"
                "nothing, because ranking one pose by volume and the next by a proxy "
                "would compare two different quantities and call it a decision. The "
                "weights are a taste, not a law."))


# ===========================================================================
# 12d. Fasteners — threads that actually screw together
# ===========================================================================
# build123d 0.11 has no thread primitive, so noodle builds its own, and builds
# it on the MESH lane. That is not a shortcut: fusing a helical rib to its core
# through OCCT was measured at 2-8s AND WRONG WITHOUT RAISING (M6x1 came back as
# the bare core with the thread silently gone; M20x2.5 came back with volume 0),
# while manifold3d does it in 0.01s, watertight, with the major diameter exact.
# The pair was verified the only way that means anything — by measuring the
# boolean interference of a male thread inside its own nut. See PLAN_THREADS.md.

_THREAD_SIZE_OPTIONS = [
    "M1.6", "M2", "M2.5", "M3", "M3.5", "M4", "M5", "M6", "M8", "M10", "M12",
    "M14", "M16", "M18", "M20", "M22", "M24", "M27", "M30", "M33", "M36",
    "Tr8x1.5", "Tr10x2", "Tr12x3", "Tr14x3", "Tr16x4", "Tr18x4", "Tr20x4",
    "Tr22x5", "Tr24x5", "Tr28x5", "Tr30x6", "Tr32x6", "Tr36x6", "Tr40x7",
    "Tr44x7", "Tr48x8", "Tr50x8",
    "#4-40 UNC", "#6-32 UNC", "#8-32 UNC", "#10-24 UNC", "1/4-20 UNC",
    "5/16-18 UNC", "3/8-16 UNC", "7/16-14 UNC", "1/2-13 UNC", "5/8-11 UNC",
    "3/4-10 UNC", "1-8 UNC",
    "#4-48 UNF", "#6-40 UNF", "#8-36 UNF", "#10-32 UNF", "1/4-28 UNF",
    "5/16-24 UNF", "3/8-24 UNF", "7/16-20 UNF", "1/2-20 UNF", "5/8-18 UNF",
    "3/4-16 UNF", "1-12 UNF",
    "1/4-16 ACME", "5/16-14 ACME", "3/8-12 ACME", "1/2-10 ACME", "5/8-8 ACME",
    "3/4-6 ACME", "1-5 ACME", "1.1/4-5 ACME", "1.1/2-4 ACME",
    "1/8 NPT", "1/4 NPT", "3/8 NPT", "1/2 NPT", "3/4 NPT", "1 NPT",
    "1.1/4 NPT", "1.1/2 NPT", "2 NPT",
    "custom",
]

register(NodeDef("Thread", "fastener", "Thread",
    # `at`, not `origin`: an `origin` socket is wrapped around the node's whole
    # result by the emitter, which with `shape` wired would move the finished
    # assembly instead of putting the thread somewhere inside the part.
    inputs=[Socket("at", WIRE_VECTOR, required=False),
            Socket("shape", WIRE_SOLID, required=False,
                   accepts=[WIRE_MESH, WIRE_SURFACE])]
           + _pin("length", "clearance", "starts"),
    params=[_choice("kind", "external", ["external", "internal"], label="kind"),
            _choice("size", "M6", _THREAD_SIZE_OPTIONS, label="size"),
            _f("length", 10.0, 0.5, 500, label="length (mm)", soft_max=100),
            _f("clearance", 0.3, 0.0, 2.0, 0.05, label="clearance (mm)"),
            _i("starts", 1, 1, 8, label="starts"),
            _f("pitch", 0.0, 0.0, 20.0, 0.05, label="pitch (0 = standard)"),
            _f("diameter", 0.0, 0.0, 200.0, 0.5, label="diameter (0 = standard)"),
            _choice("profile", "ISO metric",
                    ["ISO metric", "Trapezoidal", "ACME", "NPT"],
                    label="profile (custom only)"),
            Param("lefthand", "bool", "left-hand", False, widget="checkbox"),
            Param("lead_in", "bool", "lead-in", True, widget="checkbox"),
            _i("segments", 64, 12, 256, label="segments/turn", soft_max=128)],
    outputs=_mesh(),
    aliases=["filetto", "filettatura", "vite", "bullone", "madrevite", "dado",
             "screw", "bolt", "nut", "tap", "helix thread", "iso", "metric",
             "unc", "unf", "acme", "npt", "trapezoidal", "lead screw"],
    code_template={"algebra":
        "_thread({size}, {length}, {kind} == 'internal', {clearance}, {starts}, "
        "{lefthand}, {lead_in}, {segments}, {profile}, {diameter}, {pitch}, "
        "{shape}, {at})"},
    description="A real screw thread — ISO metric (M), trapezoidal lead screw (Tr), "
                "UNC/UNF, ACME, or tapered NPT pipe — as a mesh you can print. `kind` "
                "external gives a threaded rod; internal gives the TAP: subtract it "
                "from a body and it drills the hole and cuts the thread in one go, "
                "which is why wiring `shape` is the easy path — the node picks the "
                "boolean (external adds, internal cuts) so you cannot get the "
                "direction backwards. Leave `shape` unwired to get the bare thread.\n"
                "AN EXTERNAL THREAD ALREADY BRINGS ITS OWN CORE, so `shape` is for "
                "what you are threading ONTO — a head, a flange, a boss. Wire a shank "
                "as fat as the nominal diameter and it FILLS THE GROOVES: the union is "
                "a plain cylinder, no error, and it looks fine until you zoom in. Any "
                "body overlapping the threaded length must stay under the minor "
                "diameter.\n"
                "CLEARANCE is the whole game on a printer. It loosens THIS thread — "
                "the male shrinks, the female grows — so set it on ONE half of a "
                "mating pair, not both, or you get double the gap. Measured on M6x1: "
                "at 0 the pair interferes by 0.011mm^3 (the theoretical line-to-line "
                "fit, which will bind), at 0.2mm and above the interference is exactly "
                "zero. 0.3 is a sane FDM default; go up for a coarse nozzle, down for "
                "resin.\n"
                "`pitch` overrides the table, which is how you ask for a fine pitch "
                "(M8 + pitch 1.0). `starts` > 1 gives a multi-start thread — the "
                "fast-action kind on a jar lid or a bottle cap. Internal threads print "
                "best with the axis vertical; feed the result to Place on Bed or "
                "Orient for Print.\n"
                "`at` puts the thread somewhere other than the axis, BEFORE the "
                "boolean — that is how a tapped hole goes where you want it. Wire a "
                "LIST of points into it and one node drills the whole pattern."))


# ===========================================================================
# 13. Export / IO
# ===========================================================================
register(NodeDef("ExportSTEP", "export", "Export STEP",
    inputs=[Socket("shape", WIRE_SOLID)],
    params=[Param("path", "str", "path", "output.step", widget="input")],
    outputs=[],
    code_template={"algebra": "export_step({shape}, _out({path}))"},
    description="Write the shape to a STEP file (into the project's exports/ folder)."))

register(NodeDef("ExportSTL", "export", "Export STL",
    inputs=[Socket("shape", WIRE_SOLID)],
    params=[Param("path", "str", "path", "output.stl", widget="input")],
    outputs=[],
    code_template={"algebra": "export_stl({shape}, _out({path}))"},
    description="Write the shape to an STL file (into the project's exports/ folder)."))

register(NodeDef("Export3MF", "export", "Export 3MF",
    inputs=[Socket("shape", WIRE_SOLID)],
    params=[Param("path", "str", "path", "output.3mf", widget="input")],
    outputs=[],
    code_template={"algebra": "_export_3mf({shape}, _out({path}))"},
    description="Write the shape to a 3MF file (the modern 3D-printing format: "
                "mesh + units in one file)."))

register(NodeDef("ExportGLTF", "export", "Export glTF",
    inputs=[Socket("shape", WIRE_SOLID)],
    params=[Param("path", "str", "path", "output.gltf", widget="input")],
    outputs=[],
    code_template={"algebra": "export_gltf({shape}, _out({path}))"},
    description="Write the shape to a glTF file (web/AR viewers, three.js)."))

register(NodeDef("ExportSVG", "export", "Export SVG",
    inputs=[Socket("shape", WIRE_SURFACE, accepts=[WIRE_CURVE])],
    params=[Param("path", "str", "path", "output.svg", widget="input")],
    outputs=[],
    code_template={"algebra": "_export_2d({shape}, _out({path}), 'svg')"},
    description="Write 2D geometry (a sketch, curve, or a Section of a solid) "
                "to an SVG drawing — the XY projection. Laser cutting, plotting, "
                "documentation."))

register(NodeDef("ExportDXF", "export", "Export DXF",
    inputs=[Socket("shape", WIRE_SURFACE, accepts=[WIRE_CURVE])],
    params=[Param("path", "str", "path", "output.dxf", widget="input")],
    outputs=[],
    code_template={"algebra": "_export_2d({shape}, _out({path}), 'dxf')"},
    description="Write 2D geometry (a sketch, curve, or a Section of a solid) "
                "to a DXF drawing — the XY projection. CNC/laser toolchains."))

# ===========================================================================
# 14. Group nodes (builder mode)
# ===========================================================================
register(NodeDef("BuildPart", "group", "Build Part",
    outputs=_geo("part"),
    is_group=True, group_kind="part",
    code_template={"builder": "with BuildPart() as {ctx}:"},
    description="Builder-mode part context; children run inside it."))

register(NodeDef("BuildSketch", "group", "Build Sketch",
    inputs=[Socket("plane", WIRE_PLANE, required=False)],
    outputs=_sk("sketch"),
    is_group=True, group_kind="sketch",
    code_template={"builder": "with BuildSketch({plane}) as {ctx}:"},
    description="Builder-mode sketch context; children run inside it."))

# ===========================================================================
# 15. CodeBlock — the universal node
# ===========================================================================
register(NodeDef("CodeBlock", "code", "Code Block",
    inputs=[Socket(f"in_{i}", WIRE_DATA, required=False) for i in range(6)],
    params=[Param("code", "str", "code",
                  "result = Box(in_0 or 10, in_1 or 10, 5)", widget="text", raw=True)],
    outputs=_data("result"),
    code_template={},  # handled specially by the transpiler
    description="Arbitrary build123d code; must assign `result`. Inputs in_0..in_5 "
                "(all optional) are available as variables; unconnected ones are None. "
                "Declare parameters with a `#@param` annotation — e.g. "
                "`teeth = 12  #@param int min=6 max=40` — to get a live knob on the "
                "node, an editable span in the code view, and a same-named input "
                "socket that fans out (Range -> CodeBlock.teeth = one result each). "
                "Overrides live in the `_cb` param namespace; the source is never rewritten."))


# ===========================================================================
# Output sub-types (advisory tags) — split the opaque `data` bus and the `curve`
# family for legend/disambiguation in the editor. Applied to the first output of
# each producer. Compatibility is unaffected (gates on wire_type only).
# See PLAN_DATA_PROTOCOL.md §4d.
# ===========================================================================
_OUTPUT_SUBTYPES = {
    # data -> number / integer / boolean / text
    "NumberSlider": "number", "NumberInput": "number", "IntegerSlider": "integer",
    "BooleanToggle": "boolean", "StringInput": "text",
    "Add": "number", "SubtractNum": "number", "Multiply": "number",
    "Divide": "number", "Power": "number", "Clamp": "number", "Expression": "number",
    "Remap": "number", "CurveLength": "number", "ListLength": "integer",
    "Sin": "number", "Cos": "number", "Tan": "number",
    "ArcSin": "number", "ArcCos": "number", "ArcTan": "number",
    "Sqrt": "number", "Abs": "number", "Round": "number",
    "Modulo": "number", "Min": "number", "Max": "number",
    "Floor": "integer", "Ceil": "integer",
    "DotProduct": "number", "Distance": "number", "VectorLength": "number",
    "Volume": "number", "Area": "number",
    # data -> list / domain
    "ListCreate": "list", "ListRange": "list", "ListReverse": "list",
    "ListSort": "list", "ListSlice": "list", "ListFlatten": "list",
    "Concat": "list", "Series": "list", "DivideDomain": "list",
    "ListRepeat": "list", "ListShift": "list", "ListFilter": "list",
    "ListUnique": "list", "Random": "list",
    "ConstructDomain": "domain", "Bounds": "domain",
    # curve -> line / polyline / spline / arc
    "Line": "line", "Polyline": "polyline", "Spline": "spline", "Bezier": "spline",
    "Helix": "spline", "Arc3pt": "arc", "ArcCenter": "arc",
}
for _t, _st in _OUTPUT_SUBTYPES.items():
    _d = REGISTRY.get(_t)
    if _d and _d.outputs:
        _d.outputs[0].subtype = _st
