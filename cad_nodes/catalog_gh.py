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


def _plane_in(name="plane"):
    """A GH plane input: a Plane, a point (the XY plane through it), or a closed
    planar curve / planar face (the plane it lies in) — see _gh_plane. A `data`
    wire, because GH casts all of those to a plane."""
    return Socket(name, WIRE_DATA, required=False, subtype="plane")


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
    # NOT `origin`: the emitter wraps a socket of that name around the node's
    # whole result (_at), and the plane would be moved by its origin twice.
    _gh(f"Gh{_pl}Plane", f"{_pl} Plane", [Socket("origin_point", WIRE_VECTOR, required=False)],
        [Socket("plane", WIRE_PLANE)], f"_gh_world_plane('{_pl}', {{origin_point}})",
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
    "_gh_deconstruct_domain2({domain}, {reparameterize})", multi=True,
    params=[_bool("reparameterize")],
    description="The four bounds of a 2D domain. Wire a SURFACE and you get its "
                "own UV domain, as in GH — OCCT's parameters for that face, which "
                "are not always Rhino's numbers for the same surface.")

# ---------------------------------------------------------------------------
# Curves
# ---------------------------------------------------------------------------
_gh("GhRectangle", "Rectangle",
    [_plane_in(),
     _in("x_size", list_access=True), _in("y_size", list_access=True), _in("radius")],
    [Socket("rectangle", WIRE_CURVE), Socket("length", WIRE_DATA)],
    "_gh_rectangle({plane}, {x_size}, {y_size}, {radius})",
    params=[_num("x_size", 10.0), _num("y_size", 5.0), _num("radius", 0.0)],
    multi=True,
    description="A rectangle on a plane, its sides given as DOMAINS of the plane's "
                "x and y (a number n means 0..n). Wire Construct Domain −a..a to "
                "centre it. Drawn counter-clockwise, as Rhino draws it.")

_gh("GhCircle", "Circle",
    [_plane_in(), _in("radius")],
    [Socket("circle", WIRE_CURVE)], "_gh_circle({plane}, {radius})",
    params=[_num("radius", 1.0)],
    description="A circle in a plane (a point gives the XY plane through it).")

_gh("GhOffsetCurve", "Offset Curve",
    [_in("curve"), _in("distance"),
     _plane_in(),
     _in("corners"), _in("both_sides"), _in("cap_type")],
    [Socket("curve", WIRE_CURVE)],
    "_gh_offset_curve({curve}, {distance}, {plane}, {corners}, {both_sides}, {cap_type})",
    params=[_num("distance", 1.0), _int("corners", 1), _bool("both_sides", False),
            _int("cap_type", 0)],
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

_gh("GhLength", "Length", [_in("curve")], [Socket("length", WIRE_DATA)],
    "_gh_length_of({curve})",
    description="Length of a curve — or the perimeter of a surface, which GH "
                "reads as its boundary curve.")

_gh("GhJoinCurves", "Join Curves", [_in("curves", list_access=True), _in("preserve")],
    [Socket("curves", WIRE_CURVE)], "_gh_join_curves({curves}, {preserve})",
    params=[_bool("preserve")],
    description="Chain curves that touch end to end; every chain comes back, and "
                "a curve touching nothing comes back alone (noodle's Join refuses "
                "pieces that do not touch).")
_lists("GhJoinCurves")

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
    [_in("geometry"), _plane_in(),
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
    [Socket("surface", WIRE_SURFACE)], "_gh_isotrim({surface}, {domain}, {reparameterize})",
    params=[_bool("reparameterize")],
    description="The piece of a surface inside a 2D (UV) domain. The domain is in "
                "the face's own parameters (read them with Deconstruct Domain²).")

_gh("GhSurfaceSplit", "Surface Split", [_in("surface"), _in("curves", list_access=True)],
    [Socket("fragments", WIRE_SURFACE)], "_gh_surface_split({surface}, {curves})",
    params=[_bool("reparameterize")],   # accepted from the .gh, changes nothing here
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

_gh("GhBoundingBox", "Bounding Box",
    [_in("content"), _plane_in()],
    [Socket("box", WIRE_SOLID)], "_gh_bounding_box({content}, {plane})",
    description="The box aligned with a plane (world XY by default) holding the "
                "content. Flat content gives a flat box: the rectangle FACE.")

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


# ===========================================================================
# barattolo.gh — math, logic, lists, trees
# ===========================================================================
_gh("GhNegative", "Negative", [_in("value")], [Socket("result", WIRE_DATA)],
    "_gh_negative({value})", params=[_num("value", 0.0)],
    description="−x.")

_gh("GhListLength", "List Length", [_in("list", list_access=True)],
    [Socket("length", WIRE_DATA)], "_gh_list_length({list})",
    description="Number of items — a single item counts as one, nothing as zero "
                "(noodle's List Length wants a real list).")

_gh("GhRadians", "Radians", [_in("degrees")], [Socket("radians", WIRE_DATA)],
    "_gh_radians({degrees})", params=[_num("degrees", 0.0)],
    description="Degrees to radians — GH angles are radians.")

_gh("GhConcatenate", "Concatenate", [_in("fragment_a"), _in("fragment_b")],
    [Socket("result", WIRE_DATA, subtype="text")],
    "_gh_concatenate({fragment_a}, {fragment_b})",
    description="Join two texts.")

_gh("GhRepeatData", "Repeat Data", [_in("data", list_access=True), _in("length")],
    [Socket("data", WIRE_DATA)], "_gh_repeat_data({data}, {length})",
    params=[_int("length", 10)],
    description="Repeat a list CYCLICALLY until it is `length` long (List Repeat "
                "repeats a whole list n times instead).")
_lists("GhRepeatData")

_gh("GhListIndices", "List Indices", [_in("list", list_access=True)],
    [Socket("indices", WIRE_DATA)], "_gh_list_indices({list})",
    description="0, 1, … one index per item.")
_lists("GhListIndices")

_gh("GhMerge", "Merge", [_in(f"data_{i}", list_access=True) for i in range(1, 7)],
    [Socket("result", WIRE_DATA)],
    "_gh_merge(" + ", ".join(f"{{data_{i}}}" for i in range(1, 7)) + ")",
    description="Merge streams into one list (up to six; unwired ones are skipped).")
_lists("GhMerge")

_gh("GhRange", "Range", [_in("domain", list_access=True), _in("steps")],
    [Socket("range", WIRE_DATA)], "_gh_range({domain}, {steps})",
    params=[_int("steps", 10)],
    description="steps+1 numbers spanning a domain (a number n = 0..n), ends included.")
_lists("GhRange")

_gh("GhLargerThan", "Larger Than", [_in("first_number"), _in("second_number")],
    [Socket("larger_than", WIRE_DATA), Socket("or_equal_to", WIRE_DATA)],
    "_gh_larger({first_number}, {second_number})", multi=True,
    params=[_num("first_number", 0.0), _num("second_number", 0.0)],
    description="A > B, and A ≥ B.")

_gh("GhSmallerThan", "Smaller Than", [_in("first_number"), _in("second_number")],
    [Socket("smaller_than", WIRE_DATA), Socket("or_equal_to", WIRE_DATA)],
    "_gh_smaller({first_number}, {second_number})", multi=True,
    params=[_num("first_number", 0.0), _num("second_number", 0.0)],
    description="A < B, and A ≤ B.")

_gh("GhGateOr", "Gate Or", [_in("a"), _in("b")], [Socket("result", WIRE_DATA)],
    "_gh_gate_or({a}, {b})", params=[_bool("a"), _bool("b")],
    description="A or B.")

_gh("GhInvertBoolean", "Invert Boolean", [_in("boolean")],
    [Socket("inverted", WIRE_DATA), Socket("number", WIRE_DATA)],
    "_gh_invert_boolean({boolean})", multi=True, params=[_bool("boolean")],
    description="not B — also as a number (1/0).")

_gh("GhWeave", "Weave",
    [_in("pattern", list_access=True)] + [_in(f"stream_{i}", list_access=True) for i in range(4)],
    [Socket("weave", WIRE_DATA)],
    "_gh_weave({pattern}, " + ", ".join(f"{{stream_{i}}}" for i in range(4)) + ")",
    description="Interleave streams by an index pattern (0,1,… by default), until "
                "the stream the pattern asks for runs out.")
_lists("GhWeave")

_gh("GhSplitList", "Split List", [_in("list", list_access=True), _in("index")],
    [Socket("list_a", WIRE_DATA), Socket("list_b", WIRE_DATA)],
    "_gh_split_list({list}, {index})", multi=True, params=[_int("index", 0)],
    description="The items before `index` and the rest.")
_lists("GhSplitList")

_gh("GhReplaceNulls", "Replace Nulls",
    [_in("items", list_access=True), _in("replacements", list_access=True)],
    [Socket("items", WIRE_DATA), Socket("count", WIRE_DATA)],
    "_gh_replace_nulls({items}, {replacements})", multi=True,
    description="Fill the empty items of a list.")
_lists("GhReplaceNulls")

_gh("GhGraftTree", "Graft Tree", [_in("tree", list_access=True)],
    [Socket("tree", WIRE_DATA, subtype="tree")], "_gh_graft({tree})",
    aliases=["graft"],
    description="Every item into a branch of its own (a list of one-item lists). "
                "What an input's [graft] flag does in GH: the next node then runs "
                "once per item against the WHOLE of its other inputs, instead of "
                "pairing item with item.")
_lists("GhGraftTree")

_gh("GhCreateTree", "Create Tree", [_in("data", list_access=True), _in("path", list_access=True)],
    [Socket("data", WIRE_DATA, subtype="tree")], "_gh_create_tree({data}, {path})",
    description="A data tree: each item in the branch its path names ({0}, {1}… "
                "or plain integers). It travels as ONE value — take a branch out "
                "with Tree Branch.")

_gh("GhTreeBranch", "Tree Branch", [_in("tree", list_access=True), _in("path")],
    [Socket("branch", WIRE_DATA)], "_gh_tree_branch({tree}, {path})",
    params=[Param("path", "str", "path", "{0}", widget="input")],
    description="The items of one branch of a tree (several trees wired in are "
                "merged first) — barattolo's way of choosing one profile of three.")
_lists("GhTreeBranch")

# ===========================================================================
# barattolo.gh — curves
# ===========================================================================
_gh("GhBoundingRectangle", "Bounding Rectangle",
    [_in("geometry", list_access=True),
     _plane_in(), _in("union"), _in("slide")],
    [Socket("bounding", WIRE_CURVE), Socket("dimension_x", WIRE_DATA),
     Socket("dimension_y", WIRE_DATA)],
    "_gh_bounding_rectangle({geometry}, {plane}, {union}, {slide})", multi=True,
    params=[_bool("union", False), _num("slide", 0.0)],
    description="The rectangle aligned with a plane that holds the geometry, and "
                "its sides. `union`: one for everything, else one per item. "
                "`slide` sets its height along the plane normal: 0 at the "
                "bottom of the geometry, 1 at its top.")
_lists("GhBoundingRectangle")

_gh("GhPolygon", "Polygon",
    [_plane_in(),
     _in("radius"), _in("segments"), _in("fillet_radius")],
    [Socket("polygon", WIRE_CURVE), Socket("length", WIRE_DATA)],
    "_gh_polygon({plane}, {radius}, {segments}, {fillet_radius})", multi=True,
    params=[_num("radius", 1.0), _int("segments", 6), _num("fillet_radius", 0.0)],
    description="A regular polygon inscribed in a circle, first corner on the "
                "plane's x axis, corners optionally rounded.")

_gh("GhInterpolate", "Interpolate",
    [_in("vertices", list_access=True), _in("degree"), _in("periodic"), _in("knotstyle")],
    [Socket("curve", WIRE_CURVE), Socket("length", WIRE_DATA),
     Socket("domain", WIRE_DATA, subtype="domain")],
    "_gh_interpolate({vertices}, {degree}, {periodic})", multi=True,
    params=[_int("degree", 3), _bool("periodic", False), _int("knotstyle", 0)],
    description="A smooth curve THROUGH the points (closed and smooth when "
                "periodic). Degree and knot style are OCCT's choice.")

_gh("GhRebuildCurve", "Rebuild Curve",
    [_in("curve"), _in("degree"), _in("count"), _in("tangents")],
    [Socket("curve", WIRE_CURVE)], "_gh_rebuild_curve({curve}, {degree}, {count})",
    params=[_int("degree", 3), _int("count", 10)],
    description="Refit a curve as a B-spline of `degree` with `count` control points.")

_gh("GhSimplifyCurve", "Simplify Curve", [_in("curve"), _in("tolerance"), _in("angle_tolerance")],
    [Socket("curve", WIRE_CURVE), Socket("simplified", WIRE_DATA)],
    "_gh_simplify_curve({curve}, {tolerance}, {angle_tolerance})", multi=True,
    description="Merge collinear segments; `simplified` says if anything changed.")

_gh("GhIsArcCircleEllipse", "Is Arc/Circle/Ellipse", [_in("curve"), _in("tolerance")],
    [Socket("arc", WIRE_DATA), Socket("circle", WIRE_DATA), Socket("ellipse", WIRE_DATA)],
    "_gh_is_arc_circle_ellipse({curve}, {tolerance})", multi=True,
    description="Whether a curve is an arc, a full circle or an ellipse.")

_gh("GhArcCircleEllipse", "Arc/Circle/Ellipse", [_in("curve"), _in("get_type"), _in("tolerance")],
    [Socket("arc_circle_ellipse", WIRE_CURVE)],
    "_gh_arc_circle_ellipse({curve}, {get_type}, {tolerance})",
    description="The curve if it is an arc/circle/ellipse, else nothing.")

_gh("GhFilletCurve", "Fillet", [_in("curve"), _in("parameter"), _in("radius")],
    [Socket("curve", WIRE_CURVE), Socket("parameter", WIRE_DATA)],
    "_gh_fillet_curve({curve}, {parameter}, {radius})", multi=True,
    params=[_num("parameter", 0.0), _num("radius", 1.0)],
    description="Round the corner of a curve nearest a parameter (a polyline's "
                "parameters are its vertex indices, as in Rhino).")

_gh("GhProject", "Project",
    [_in("geometry"), _plane_in()],
    [Socket("geometry", WIRE_CURVE)], "_gh_project({geometry}, {plane})",
    description="Flatten curves (or points) onto a plane along its normal.")

_gh("GhRegionUnion", "Region Union",
    [_in("curves", list_access=True),
     _plane_in()],
    [Socket("result", WIRE_CURVE)], "_gh_region_union({curves}, {plane})",
    description="The outline of the union of closed planar regions.")
_lists("GhRegionUnion")

_gh("GhVectorStar", "Vector Star",
    [_plane_in(),
     _in("steps"), _in("subdivision_type"), _in("dimensionality")],
    [Socket("vectors", WIRE_DATA), Socket("lines", WIRE_CURVE)],
    "_gh_vector_star({plane}, {steps}, {subdivision_type}, {dimensionality})",
    multi=True, params=[_int("steps", 4)],
    description="Unit vectors fanned evenly round the plane's z axis (2D only).")
_lists("GhVectorStar")

# ===========================================================================
# barattolo.gh — surfaces & breps
# ===========================================================================
_gh("GhRotate", "Rotate",
    [_in("geometry"), _in("angle"), Socket("plane", WIRE_PLANE, required=False,
                                            accepts=[WIRE_VECTOR])],
    [Socket("geometry", WIRE_SOLID)],
    "_gh_rotate({geometry}, {angle}, {plane}, {degrees})",
    params=[_num("angle", 0.0), _bool("degrees", False)], follows="geometry",
    description="Rotate about the plane's normal through its origin, in RADIANS "
                "(set `degrees` for degrees).")

_gh("GhMoveToPlane", "Move To Plane",
    [_in("geometry"), _plane_in(),
     _in("above"), _in("below")],
    [Socket("geometry", WIRE_SOLID)],
    "_gh_move_to_plane({geometry}, {plane}, {above}, {below})",
    params=[_bool("above", True), _bool("below", True)], follows="geometry",
    description="Slide geometry along the plane normal until it touches the plane "
                "(from above and/or from below).")

_gh("GhPlaneSurface", "Plane Surface",
    [_plane_in(),
     _in("x_size", list_access=True), _in("y_size", list_access=True)],
    [Socket("plane", WIRE_SURFACE)], "_gh_plane_surface({plane}, {x_size}, {y_size})",
    params=[_num("x_size", 10.0), _num("y_size", 10.0)],
    description="A flat rectangular surface on a plane, sides as domains (n = 0..n).")

_gh("GhBrepEdges", "Brep Edges", [_in("brep")],
    [Socket("naked", WIRE_CURVE), Socket("interior", WIRE_CURVE),
     Socket("non_manifold", WIRE_CURVE)],
    "_gh_brep_edges({brep})", multi=True,
    description="Edges by how many faces use them: naked (1), interior (2), "
                "non-manifold (3+).")
_lists("GhBrepEdges")

_gh("GhFilletEdge", "Fillet Edge",
    [_in("shape"), _in("blend"), _in("metric"), _in("edges", list_access=True),
     _in("radii", list_access=True)],
    [Socket("result", WIRE_SOLID)],
    "_gh_fillet_edge({shape}, {edges}, {radii}, {blend}, {metric})",
    description="Round the edges given BY INDEX. The index is into OCCT's edge "
                "order, not Rhino's: a .gh index can land on another edge — "
                "look at the result.")

_gh("GhCapHoles", "Cap Holes", [_in("brep")], [Socket("brep", WIRE_SOLID)],
    "_gh_cap_holes({brep})",
    description="Close the planar openings of an open brep into a solid.")

_gh("GhIsPlanar", "Is Planar", [_in("surface"), _in("interior")],
    [Socket("planar", WIRE_DATA), Socket("plane", WIRE_PLANE)],
    "_gh_is_planar({surface}, {interior})", multi=True,
    description="Whether a surface is flat, and its plane.")

_gh("GhIsSurfaceTrimmed", "Is Surface Trimmed", [_in("surface")],
    [Socket("trimmed", WIRE_DATA)], "_gh_is_surface_trimmed({surface})",
    description="Whether a face covers less than its whole underlying surface.")

_gh("GhTrimWithPlane", "Trim With Plane",
    [_in("brep"), _plane_in(),
     _in("plane_direction"), _in("cap"), _in("tolerance")],
    [Socket("trimmed_breps", WIRE_SOLID)],
    "_gh_trim_with_plane({brep}, {plane}, {plane_direction}, {cap})",
    params=[_bool("plane_direction", True), _bool("cap", True)],
    description="Cut a brep with a plane and keep the side the normal points to "
                "(`plane_direction` off: the other side); `cap` closes the cut.")

_gh("GhCollisionOneMany", "Collision One|Many",
    [_in("collider"), _in("obstacles", list_access=True)],
    [Socket("collision", WIRE_DATA), Socket("index", WIRE_DATA)],
    "_gh_collision_one_many({collider}, {obstacles})", multi=True,
    description="Does the collider touch or overlap any obstacle, and which one first.")

_gh("GhExtrudeToPlane", "Extrude to Plane",
    [_in("shape_to_extrude"), _in("direction"), _in("plane_1"), _in("plane_2"),
     _in("tolerance")],
    [Socket("extrusion", WIRE_SOLID)],
    "_gh_extrude_to_plane({shape_to_extrude}, {direction}, {plane_1}, {plane_2})",
    description="Extrude along a direction from plane 1 to plane 2 (planes or planar "
                "faces). With one plane: from the shape up to it.")

_gh("GhExtrudeTapered", "Extrude Tapered Polysurface",
    [_in("profile"), _in("direction"), _in("angle"), _in("corner_type"), _in("face_merge")],
    [Socket("extruded", WIRE_SOLID)],
    "_gh_extrude_tapered({profile}, {direction}, {angle}, {corner_type}, {face_merge}, {degrees})",
    params=[_num("angle", 0.0), _bool("degrees", False)],
    description="Extrude a closed planar profile along a vector with the walls "
                "drafted by `angle` (radians unless `degrees`).")

_gh("GhBrepPlane", "Brep | Plane",
    [_in("brep"), _plane_in()],
    [Socket("curves", WIRE_CURVE), Socket("points", WIRE_VECTOR)],
    "_gh_brep_plane({brep}, {plane})", multi=True,
    description="The section curves of a brep cut by a plane, joined into loops.")
_lists("GhBrepPlane")

_gh("GhVolume", "Volume", [_in("geometry")],
    [Socket("volume", WIRE_DATA), Socket("centroid", WIRE_VECTOR)],
    "_gh_volume({geometry})", multi=True,
    description="Volume and volume centroid of closed breps.")

_gh("GhLoftOptions", "Loft Options",
    [_in("closed"), _in("adjust"), _in("rebuild"), _in("refit"), _in("type")],
    [Socket("options", WIRE_DATA)],
    "_gh_loft_options({closed}, {adjust}, {rebuild}, {refit}, {type})",
    params=[_bool("closed"), _int("type", 0)],
    description="Loft settings: `closed` loops back to the first curve; type 2 "
                "(Straight) gives a ruled loft. Adjust/Rebuild/Refit are not built.")

_gh("GhLoft", "Loft", [_in("curves", list_access=True), _in("options")],
    [Socket("loft", WIRE_SURFACE)], "_gh_loft({curves}, {options})",
    description="A surface through section curves (noodle's Loft makes a solid "
                "from profiles; this one is GH's, a skin).")

_gh("GhSweep2", "Sweep2",
    [_in("rail_1"), _in("rail_2"), _in("sections", list_access=True), _in("same_height")],
    [Socket("brep", WIRE_SURFACE)],
    "_gh_sweep2({rail_1}, {rail_2}, {sections}, {same_height})",
    description="Sections swept along rail 1, rail 2 steering their twist.")

_gh("GhUnrollPolysurface", "Unroll Polysurface",
    [_in("polysurface"), _plane_in(),
     _in("points", list_access=True), _in("curves", list_access=True)],
    [Socket("unrolled", WIRE_SURFACE), Socket("unrolled_points", WIRE_VECTOR),
     Socket("unrolled_curves", WIRE_CURVE)],
    "_gh_unroll({polysurface}, {plane}, {points}, {curves})", multi=True,
    description="Lay a developable face flat (plane, cylinder, cone), carrying "
                "points and curves on it along. One face at a time.")

_gh("GhSurfaceMorph", "Surface Morph",
    [_in("geometry"), _in("reference"), _in("surface"), _in("u_domain", list_access=True),
     _in("v_domain", list_access=True), _in("w_domain", list_access=True)],
    [Socket("geometry", WIRE_DATA)],
    "_gh_surface_morph({geometry}, {reference}, {surface}, {u_domain}, {v_domain}, {w_domain})",
    params=[_bool("reparameterize")],   # U/V are read as 0..1 of the face already
    description="Carry geometry from a reference box onto a surface (box x/y → "
                "surface u/v, z → along the normal). Curves come back as splines, "
                "faces and solids as a MESH: a bent B-Rep is not exact.")

# ===========================================================================
# Text — Squid (ShapeDiver edition): Font, Font List, Paragraph, Text Curves
# ===========================================================================
_gh("GhFont", "Font", [_in("family"), _in("size"), _in("bold"), _in("italic"), _in("underline")],
    [Socket("font", WIRE_DATA)],
    "_gh_font({family}, {size}, {bold}, {italic}, {underline})",
    params=[Param("family", "str", "family", "Liberation Sans", widget="input"),
            _num("size", 10.0), _bool("bold"), _bool("italic"), _bool("underline")],
    description="A font: family, size (mm of text height), bold/italic.")

_gh("GhFontList", "Font List", [], [Socket("families", WIRE_DATA)], "_gh_font_list()",
    description="The font families installed where noodle runs.")
_lists("GhFontList")

_gh("GhParagraph", "Paragraph", [_in("halign"), _in("valign"), _in("rightleft")],
    [Socket("paragraph", WIRE_DATA)], "_gh_paragraph({halign}, {valign}, {rightleft})",
    params=[_int("halign", 0), _int("valign", 0), _bool("rightleft")],
    description="Text alignment: h 0/1/2 = left/centre/right, v 0/1/2 = "
                "top/middle/bottom.")

_gh("GhTextCurves", "Text Curves",
    [_plane_in(), _in("text"),
     _in("font"), _in("paragraph")],
    [Socket("curves", WIRE_CURVE)], "_gh_text_curves({plane}, {text}, {font}, {paragraph})",
    params=[Param("text", "str", "text", "noodle", widget="input")],
    description="The outlines of a text on a plane: each glyph's outer and inner "
                "loops as closed curves (Boundary Surfaces fills them, holes kept).")
_lists("GhTextCurves")

# ===========================================================================
# Export & bake — Pancake (Export As, STEP Export Option, Filepath Series),
# eleFront (Attributes, Bake Objects)
# ===========================================================================
_gh("GhStepExportOption", "STEP Export Option",
    [_in("schema"), _in("export_parameter_space_curves"), _in("split_closed_surfaces"),
     _in("omit_colors_of_black_objects")],
    [Socket("option", WIRE_DATA)], "_gh_step_export_option({schema})",
    description="Tells Export As to write STEP. (Schema and the other switches are "
                "OCCT's defaults.)")

_gh("GhFilepathSeries", "Filepath Series",
    [_in("pattern_of_filepath"), _in("starting_index"), _in("quantity_of_filenames"),
     _in("generate_next"), _in("reset")],
    [Socket("filepath", WIRE_DATA), Socket("list_of_filepath", WIRE_DATA),
     Socket("counter_of_first_file", WIRE_DATA)],
    "_gh_filepath_series({pattern_of_filepath}, {starting_index}, {quantity_of_filenames})",
    multi=True,
    params=[Param("pattern_of_filepath", "str", "pattern", "{0}.stp", widget="input"),
            _int("starting_index", 0), _int("quantity_of_filenames", 1)],
    description="File names from a pattern: '{0}.stp' → 0.stp, 1.stp…")

_gh("GhExportAs", "Export As",
    [_in("geometry"), _in("layer"), _in("object_attributes"), _in("file_location"),
     _in("overwrite"), _in("options"), _in("pre_options"), _in("export")],
    [Socket("ok", WIRE_DATA), Socket("export_command", WIRE_DATA)],
    "_gh_export_as({geometry}, {file_location}, {overwrite}, {options}, {export}, "
    "{node_id!r}, {object_attributes})",
    multi=True,
    params=[Param("file_location", "str", "file", "export.step", widget="input"),
            _bool("overwrite", True), _bool("export", False)],
    description="Write the geometry to the project's exports/ when `export` is on "
                "(only the file NAME of a path is kept). STEP or STL, from the "
                "options or the extension.")

_gh("GhEleFrontAttributes", "EleFront Attributes",
    [_in("attributes"), _in("keys"), _in("values"), _in("name"), _in("layer"),
     _in("colour_source"), _in("object_colour"), _in("material_source"),
     _in("material_name"), _in("linetype"), _in("print_colour"), _in("print_width")],
    [Socket("attributes", WIRE_DATA), Socket("name", WIRE_DATA), Socket("layer", WIRE_DATA),
     Socket("display_colour", WIRE_DATA), Socket("material_name", WIRE_DATA)],
    "_gh_elefront_attributes({name}, {layer}, {object_colour}, {material_name})",
    multi=True,
    description="Object attributes (name, layer, colour, material) as one value for "
                "Bake Objects / Export As. Keys/values, linetype and print settings "
                "are not built.")

_gh("GhBakeObjects", "Bake Objects",
    [_in("geometry"), _in("attributes"), _in("bakename"), _in("bake_settings"), _in("bake")],
    [Socket("message", WIRE_DATA), Socket("ghosts", WIRE_DATA)],
    "_gh_bake_objects({geometry}, {attributes}, {bake}, {node_id!r})", multi=True,
    params=[_bool("bake", False)],
    description="noodle has no Rhino document: baking writes the geometry to "
                "exports/ as STEP, named after the attributes' name, when `bake` "
                "is on.")

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
    "Union": ["Solid Union"],
    "Subtract": ["Solid Difference"],
    "ToMesh": ["Mesh Brep"],
    "PopulateGeometry": ["Populate Geometry"],
    "Move": ["Move"],
    "Mirror": ["Mirror"],
    "GraphMapper": ["Rich Graph Mapper", "Graph Mapper"],
    "Geometry": ["Brep", "Geometry"],
    "Input": ["Panel"],
    "Note": ["Scribble"],
    # barattolo.gh
    "Multiply": ["Multiplication"],
    "Series": ["Series"],
    "Max": ["Maximum"],
    "Intersect": ["Solid Intersection"],
    "BooleanToggle": ["Boolean Toggle"],
    "ImportSVG": ["Import SVG"],
    "NumberInput": ["Number"],
    "StringInput": ["Text", "File Path"],
    "ListFlatten": ["Flatten Tree", "flatten"],
}
for _t, _names in ALIASES.items():
    _d = REGISTRY[_t]
    for _n in _names:
        if _n not in _d.aliases and _n.lower() != (_d.label or "").lower():
            _d.aliases.append(_n)
