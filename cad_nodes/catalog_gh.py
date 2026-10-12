"""Grasshopper components as noodle nodes (category `grasshopper`).

One node per Grasshopper component that noodle did not already have, named as
in Grasshopper (label = the GH name, type = `Gh` + CamelCase) and behaving as
Grasshopper does: planes as frames, domains, vectors whose length is the
distance, Rhino's offset sign, radians. Where a core noodle node already does
the same job the GH name is an ALIAS on it instead (see ALIASES below and the
mapping table cad_nodes/gh/components.py, which the .gh importer reads).

Socket names are the GH parameter names in snake_case ("Start Point" ->
start_point), so a .gh wire maps onto a socket by name. Numeric inputs are
params-as-inputs with NO hard bounds: a value from a .gh file must arrive as
written, never clamped to a slider range.

Runtime: cad_nodes/gh/runtime.py (`_gh_*` helpers appended to the PREAMBLE).
"""
from __future__ import annotations

from .catalog import (REGISTRY, NodeDef, Param, Socket, register, WIRE_CURVE,
                      WIRE_DATA, WIRE_PLANE, WIRE_SOLID, WIRE_SURFACE, WIRE_VECTOR)

CAT = "grasshopper"


def _in(name, list_access=False, required=False, subtype=""):
    """A GH input: the universal `data` wire (GH is loosely typed — the helper
    does the cast GH would do), optional unless said otherwise."""
    return Socket(name, WIRE_DATA, required=required, list_access=list_access,
                  subtype=subtype)


def _num(name, default=0.0):
    return Param(name, "float", name, default, widget="input")


def _int(name, default=0):
    return Param(name, "int", name, default, step=1, widget="input")


def _bool(name, default=False):
    return Param(name, "bool", name, default, widget="checkbox")


def _gh(type_, label, inputs, outputs, template, params=(), description="",
        multi=False, follows=None, aliases=()):
    return register(NodeDef(type_, CAT, label, inputs=list(inputs), params=list(params),
                            outputs=list(outputs),
                            code_template={"algebra": template},
                            description=description, multi_out=multi,
                            output_follows=follows, aliases=list(aliases)))


# Nodes whose output (some socket of it) is a list: downstream item-access
# inputs must fan out over it. Read by the transpiler (_LIST_PRODUCERS).
LIST_PRODUCERS: set[str] = set()


def _lists(*types):
    LIST_PRODUCERS.update(types)


# ---------------------------------------------------------------------------
# Vectors & planes
# ---------------------------------------------------------------------------
for _ax in "XYZ":
    _gh(f"GhUnit{_ax}", f"Unit {_ax}", [_in("factor")],
        [Socket("unit_vector", WIRE_VECTOR)],
        f"_gh_unit('{_ax}', {{factor}})", params=[_num("factor", 1.0)],
        description=f"A vector along world {_ax}, `factor` long (negative flips it). "
                    "Wired into Move it is the motion; into Extrude, the direction "
                    "AND the distance.")

for _pl, _desc in (("XY", "normal +Z"), ("XZ", "x = X, y = Z, normal −Y"),
                   ("YZ", "x = Y, y = Z, normal +X")):
    _gh(f"Gh{_pl}Plane", f"{_pl} Plane", [Socket("origin", WIRE_VECTOR, required=False)],
        [Socket("plane", WIRE_PLANE)], f"_gh_world_plane('{_pl}', {{origin}})",
        description=f"The world {_pl} plane ({_desc}, as in Rhino) through `origin` "
                    "(world origin when unwired).")

_gh("GhDeconstruct", "Deconstruct", [_in("point")],
    [Socket("x_component", WIRE_DATA), Socket("y_component", WIRE_DATA),
     Socket("z_component", WIRE_DATA)],
    "_gh_deconstruct({point})", multi=True,
    description="A point's X, Y and Z. (noodle's Deconstruct (points) explodes a "
                "SHAPE into its vertices — a different operation.)")

_gh("GhAverage", "Average", [_in("input", list_access=True)],
    [Socket("arithmetic_mean", WIRE_DATA)], "_gh_average({input})",
    description="Arithmetic mean of numbers — or of points (their centroid).")

# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------
_gh("GhConstructDomain2", "Construct Domain²",
    [_in("domain_u", list_access=True), _in("domain_v", list_access=True)],
    [Socket("domain2", WIRE_DATA, subtype="domain")],
    "_gh_construct_domain2({domain_u}, {domain_v})",
    description="A 2D (UV) domain from two domains — feed Isotrim.")

_gh("GhDeconstructDomain2", "Deconstruct Domain²", [_in("domain")],
    [Socket("u_min", WIRE_DATA), Socket("u_max", WIRE_DATA),
     Socket("v_min", WIRE_DATA), Socket("v_max", WIRE_DATA)],
    "_gh_deconstruct_domain2({domain})", multi=True,
    description="The four bounds of a 2D domain. Wire a SURFACE and you get its "
                "own UV domain, as in GH — OCCT's parameters for that face, which "
                "are not always Rhino's numbers for the same surface.")

# ---------------------------------------------------------------------------
# Curves
# ---------------------------------------------------------------------------
_gh("GhRectangle", "Rectangle",
    [Socket("plane", WIRE_PLANE, required=False, accepts=[WIRE_VECTOR]),
     _in("x_size", list_access=True), _in("y_size", list_access=True), _in("radius")],
    [Socket("rectangle", WIRE_CURVE), Socket("length", WIRE_DATA)],
    "_gh_rectangle({plane}, {x_size}, {y_size}, {radius})",
    params=[_num("x_size", 10.0), _num("y_size", 5.0), _num("radius", 0.0)],
    multi=True,
    description="A rectangle on a plane, its sides given as DOMAINS of the plane's "
                "x and y (a number n means 0..n). Wire Construct Domain −a..a to "
                "centre it. Drawn counter-clockwise, as Rhino draws it.")

_gh("GhCircle", "Circle",
    [Socket("plane", WIRE_PLANE, required=False, accepts=[WIRE_VECTOR]), _in("radius")],
    [Socket("circle", WIRE_CURVE)], "_gh_circle({plane}, {radius})",
    params=[_num("radius", 1.0)],
    description="A circle in a plane (a point gives the XY plane through it).")

_gh("GhOffsetCurve", "Offset Curve",
    [_in("curve"), _in("distance"),
     Socket("plane", WIRE_PLANE, required=False, accepts=[WIRE_VECTOR]),
     _in("corners"), _in("both_sides")],
    [Socket("curve", WIRE_CURVE)],
    "_gh_offset_curve({curve}, {distance}, {plane}, {corners}, {both_sides})",
    params=[_num("distance", 1.0), _int("corners", 1), _bool("both_sides", False)],
    description="Offset a planar curve in a plane. Rhino's sign: a positive distance "
                "goes to the RIGHT of the curve's direction about the plane normal — "
                "outward on a counter-clockwise loop. corners: 0 none, 1 sharp, "
                "2 round, 3 smooth, 4 chamfer.")

_gh("GhControlPoints", "Control Points", [_in("curve")],
    [Socket("points", WIRE_VECTOR), Socket("weights", WIRE_DATA),
     Socket("knots", WIRE_DATA)],
    "_gh_control_points({curve})", multi=True,
    description="A curve's control points, weights and knots. A closed polyline "
                "repeats its first point at the end, as in Rhino (a rectangle has 5).")
_lists("GhControlPoints")

_gh("GhNurbsCurve", "Nurbs Curve",
    [_in("vertices", list_access=True), _in("degree"), _in("periodic")],
    [Socket("curve", WIRE_CURVE), Socket("length", WIRE_DATA),
     Socket("domain", WIRE_DATA, subtype="domain")],
    "_gh_nurbs_curve({vertices}, {degree}, {periodic})",
    params=[_int("degree", 3), _bool("periodic", False)], multi=True,
    description="A NURBS curve FROM control points (it does not pass through "
                "them): clamped uniform knots, or periodic (closed and smooth).")

_gh("GhCurveMiddle", "Curve Middle", [_in("curve")],
    [Socket("midpoint", WIRE_VECTOR)], "_gh_curve_middle({curve})",
    description="The point halfway along a curve (by length).")

_gh("GhEndPoints", "End Points", [_in("curve")],
    [Socket("start", WIRE_VECTOR), Socket("end", WIRE_VECTOR)],
    "_gh_end_points({curve})", multi=True,
    description="A curve's start and end points, as two outputs.")

_gh("GhCloseCurve", "Close Curve", [_in("curve")],
    [Socket("closed", WIRE_CURVE), Socket("result_boolean", WIRE_DATA)],
    "_gh_close_curve({curve})", multi=True,
    description="Close an open curve with a straight segment from its end back to "
                "its start (an already-closed curve passes through).")

_gh("GhDiscontinuity", "Discontinuity", [_in("curve"), _in("level")],
    [Socket("points", WIRE_VECTOR), Socket("parameters", WIRE_DATA)],
    "_gh_discontinuity({curve}, {level})", params=[_int("level", 1)], multi=True,
    description="The kinks of a curve — the corners of a polyline/polycurve. A "
                "closed curve's seam counts when it is a corner; an open curve's "
                "ends never do.")
_lists("GhDiscontinuity")

_gh("GhExtendCurve", "Extend Curve",
    [_in("curve"), _in("type"), _in("start"), _in("end")],
    [Socket("curve", WIRE_CURVE)], "_gh_extend_curve({curve}, {start}, {end})",
    params=[_int("type", 0), _num("start", 0.0), _num("end", 0.0)],
    description="Lengthen a curve at either end along its tangent — or SHORTEN it "
                "with a negative length. (type: only the line extension is built.)")

_gh("GhDivideCurve", "Divide Curve", [_in("curve"), _in("count"), _in("kinks")],
    [Socket("points", WIRE_VECTOR), Socket("tangents", WIRE_DATA),
     Socket("parameters", WIRE_DATA)],
    "_gh_divide_curve({curve}, {count}, {kinks})",
    params=[_int("count", 10), _bool("kinks", False)], multi=True,
    description="Divide a curve into `count` equal-length segments: the points "
                "(count+1 on an open curve, count on a closed one), tangents and "
                "normalised parameters. (noodle's Divide Curve returns FRAMES.)")
_lists("GhDivideCurve")

# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------
_gh("GhScale", "Scale", [_in("geometry"), _in("center"), _in("factor")],
    [Socket("geometry", WIRE_SOLID)], "_gh_scale({geometry}, {center}, {factor})",
    params=[_num("factor", 0.5)], follows="geometry",
    description="Uniform scale about a centre point (world origin by default). "
                "Any factor, negative included.")

_gh("GhScaleNU", "Scale NU",
    [_in("geometry"), Socket("plane", WIRE_PLANE, required=False, accepts=[WIRE_VECTOR]),
     _in("scale_x"), _in("scale_y"), _in("scale_z")],
    [Socket("geometry", WIRE_SOLID)],
    "_gh_scale_nu({geometry}, {plane}, {scale_x}, {scale_y}, {scale_z})",
    params=[_num("scale_x", 1.0), _num("scale_y", 1.0), _num("scale_z", 1.0)],
    follows="geometry",
    description="Non-uniform scale along a plane's axes (a point gives the XY "
                "plane through it). Negative factors mirror.")

_gh("GhMoveToPoint", "Move To Point", [_in("geometry"), _in("point_a"), _in("point_b")],
    [Socket("moved", WIRE_SOLID), Socket("vector", WIRE_VECTOR)],
    "_gh_move_to_point({geometry}, {point_a}, {point_b})", multi=True,
    description="Translate geometry by the vector from point A to point B.")

# ---------------------------------------------------------------------------
# Surfaces & breps
# ---------------------------------------------------------------------------
_gh("GhBoundarySurfaces", "Boundary Surfaces", [_in("edges", list_access=True)],
    [Socket("surfaces", WIRE_SURFACE)], "_gh_boundary_surfaces({edges})",
    description="Planar faces from closed planar curves; a curve inside another "
                "on the same plane becomes a HOLE in it.")
_lists("GhBoundarySurfaces")

_gh("GhExtrude", "Extrude", [_in("base"), _in("direction")],
    [Socket("extrusion", WIRE_SOLID)], "_gh_extrude({base}, {direction})",
    description="Extrude along a vector — its length is the distance. A curve "
                "becomes a surface, a planar face a closed solid.")

_gh("GhUnsplitRevolve", "Unsplit Revolve Surface",
    [_in("profile"), _in("axis"), _in("revolution", list_access=True)],
    [Socket("unsplit", WIRE_SURFACE)],
    "_gh_revolve_unsplit({profile}, {axis}, {revolution}, {degrees})",
    params=[_bool("degrees", False)],
    description="Revolve a profile curve about an axis LINE over an angle domain "
                "(radians; set `degrees` when the .gh input had Degrees on) into "
                "one surface, not split at the seam.")

_gh("GhIsotrim", "Isotrim", [_in("surface"), _in("domain")],
    [Socket("surface", WIRE_SURFACE)], "_gh_isotrim({surface}, {domain})",
    description="The piece of a surface inside a 2D (UV) domain. The domain is in "
                "the face's own parameters (read them with Deconstruct Domain²).")

_gh("GhSurfaceSplit", "Surface Split", [_in("surface"), _in("curves", list_access=True)],
    [Socket("fragments", WIRE_SURFACE)], "_gh_surface_split({surface}, {curves})",
    description="Cut a surface into fragments along curves lying on it (Pull Curve "
                "them first). The ORDER of the fragments is OCCT's, not Rhino's: "
                "pick one by a measure (Sort List on area), not by an index.")
_lists("GhSurfaceSplit")

_gh("GhPullCurve", "Pull Curve", [_in("curve"), _in("surface")],
    [Socket("curve", WIRE_CURVE)], "_gh_pull_curve({curve}, {surface})",
    description="Project a curve onto a surface along the surface normals.")

_gh("GhPullPoint", "Pull Point", [_in("point"), _in("geometry", list_access=True)],
    [Socket("closest_point", WIRE_VECTOR), Socket("distance", WIRE_DATA)],
    "_gh_pull_point({point}, {geometry})", multi=True,
    description="The closest point on a geometry, and how far it is.")

_gh("GhBrepBrep", "Brep | Brep", [_in("brep_a"), _in("brep_b")],
    [Socket("curves", WIRE_CURVE), Socket("points", WIRE_VECTOR)],
    "_gh_brep_brep({brep_a}, {brep_b})", multi=True,
    description="The intersection curves of two breps, joined into loops.")
_lists("GhBrepBrep")

_gh("GhTrimWithBrep", "Trim with Brep", [_in("curve"), _in("brep")],
    [Socket("inside", WIRE_CURVE), Socket("outside", WIRE_CURVE)],
    "_gh_trim_with_brep({curve}, {brep})", multi=True,
    description="Split a curve by a brep and sort the pieces into the parts inside "
                "and outside it.")
_lists("GhTrimWithBrep")

_gh("GhDeconstructBrep", "Deconstruct Brep", [_in("brep")],
    [Socket("faces", WIRE_SURFACE), Socket("edges", WIRE_CURVE),
     Socket("vertices", WIRE_VECTOR)],
    "_gh_deconstruct_brep({brep})", multi=True,
    description="A brep's faces, edges and vertices.")
_lists("GhDeconstructBrep")

_gh("GhBrepJoin", "Brep Join", [_in("breps", list_access=True)],
    [Socket("breps", WIRE_SOLID), Socket("closed", WIRE_DATA)],
    "_gh_brep_join({breps})", multi=True,
    description="Join surfaces/breps that share edges; a closed result is a solid.")

_gh("GhOffsetSurface", "Offset Surface",
    [_in("surface"), _in("distance"), _in("both_sides"), _in("create_solid")],
    [Socket("offset", WIRE_SOLID)],
    "_gh_offset_surface({surface}, {distance}, {both_sides}, {create_solid})",
    params=[_num("distance", 1.0), _bool("both_sides", False),
            _bool("create_solid", False)],
    description="Offset a surface along its normals; `create_solid` closes the gap "
                "into a solid wall.")

_gh("GhArea", "Area", [_in("geometry")],
    [Socket("area", WIRE_DATA), Socket("centroid", WIRE_VECTOR)],
    "_gh_area({geometry})", multi=True,
    description="Area and area centroid. A closed planar curve counts as the region "
                "it bounds.")

_gh("GhVoronoi3D", "Voronoi 3D",
    [_in("points", list_access=True), _in("box")],
    [Socket("cells", WIRE_SOLID), Socket("boundary", WIRE_SOLID)],
    "_gh_voronoi3d({points}, {box})", multi=True,
    description="One convex cell per point, clipped to a box, as B-Rep SOLIDS (so "
                "Brep | Brep and booleans work on them exactly). noodle's own "
                "Voronoi 3D works on meshes clipped to any body.")
_lists("GhVoronoi3D")

# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------
_gh("GhListItem", "List Item", [_in("list", list_access=True), _in("index"), _in("wrap")],
    [Socket("item", WIRE_DATA)], "_gh_list_item({list}, {index}, {wrap})",
    params=[_int("index", 0), _bool("wrap", True)],
    description="The item at `index`. `wrap` folds an index past the end back to "
                "the start (GH's default). Wire a list of indices to pick several.")

_gh("GhDispatch", "Dispatch",
    [_in("list", list_access=True), _in("dispatch_pattern", list_access=True)],
    [Socket("list_a", WIRE_DATA), Socket("list_b", WIRE_DATA)],
    "_gh_dispatch({list}, {dispatch_pattern})", multi=True,
    description="Send each item to list A or list B by a repeating boolean "
                "pattern (default True, False: even items to A).")
_lists("GhDispatch")

_gh("GhSortList", "Sort List",
    [_in("keys", list_access=True), _in("values_a", list_access=True)],
    [Socket("keys", WIRE_DATA), Socket("values_a", WIRE_DATA)],
    "_gh_sort_list({keys}, {values_a})", multi=True,
    description="Sort the keys, and reorder the values the same way — sort shapes "
                "by a measure (Length, Area) wired as keys.")
_lists("GhSortList")


# ---------------------------------------------------------------------------
# GH names on core noodle nodes that already do the same job
# ---------------------------------------------------------------------------
ALIASES = {
    "NumberSlider": ["Number Slider"],
    "ConstructDomain": ["Construct Domain"],
    "Remap": ["Remap Numbers"],
    "Bounds": ["Bounds"],
    "Add": ["Addition"],
    "SubtractNum": ["Subtraction"],
    "Divide": ["Division"],
    "ConstructPoint": ["Construct Point"],
    "Line": ["Line"],
    "CurveLength": ["Length"],
    "Union": ["Solid Union"],
    "Subtract": ["Solid Difference"],
    "Join": ["Join Curves"],
    "ToMesh": ["Mesh Brep"],
    "PopulateGeometry": ["Populate Geometry"],
    "BoundingBox": ["Bounding Box"],
    "Move": ["Move"],
    "Mirror": ["Mirror"],
    "GraphMapper": ["Rich Graph Mapper", "Graph Mapper"],
    "Geometry": ["Brep", "Geometry"],
    "Input": ["Panel"],
    "Note": ["Scribble"],
}
for _t, _names in ALIASES.items():
    _d = REGISTRY[_t]
    for _n in _names:
        if _n not in _d.aliases and _n.lower() != (_d.label or "").lower():
            _d.aliases.append(_n)
