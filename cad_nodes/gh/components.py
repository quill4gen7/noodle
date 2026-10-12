"""Grasshopper component -> noodle node: the table the .gh importer reads.

Keyed by the component's TYPE GUID, never by its name: one name can hide two
components (ikeaLack has two different "Offset Curve"s). Each entry says

  gh       the component's name in Grasshopper
  noodle   the noodle node type it becomes
  kind     alias     a core noodle node already does the same job (its search
                     aliases carry the GH name — catalog_gh.ALIASES)
           new       a node written for it (category `grasshopper`, catalog_gh)
           slider / panel / container   handled by the importer itself
           skip      nothing to build (Custom Preview…)
  inputs   {GH input name -> noodle input socket}
  outputs  {GH output name -> noodle output socket}
  params   {GH input name -> noodle param} for inputs that are widgets in noodle
           (persistent values in the .gh land there)
  note     what is NOT carried over, or differs

An input/output missing from its map is unsupported: the importer reports it.
For `new` nodes the map is the snake_case of the GH name by construction
(`_snake`), written out so the table can be read without running anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class GhMap:
    gh: str
    noodle: str | None
    kind: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    note: str = ""


def _snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    return s


def _new(gh, noodle, ins, outs, note="", rename=None):
    """A `new` node: sockets named as GH names them, snake_case — except where
    `rename` says otherwise (a name that is not an identifier: '2D Domain')."""
    rename = rename or {}
    return GhMap(gh, noodle, "new",
                 inputs={n: rename.get(n, _snake(n)) for n in ins},
                 outputs={n: rename.get(n, _snake(n)) for n in outs},
                 note=note)


COMPONENTS: dict[str, GhMap] = {
    # --- handled by the importer ------------------------------------------
    "57da07bd-ecab-415d-9d86-af36d7073abc": GhMap("Number Slider", "NumberSlider", "slider",
        outputs={"Number": "result"}, note="IntegerSlider when Digits=0"),
    "59e0b89a-e487-49f8-bab8-b5bab16be14c": GhMap("Panel", "Input", "panel"),
    "c552a431-af5b-46a9-a8a4-0fcbc27ef596": GhMap("Group", None, "skip",
        note="becomes a graph group"),
    "7f5c6c55-f846-4a08-9c9a-cfdc285cc6fe": GhMap("Scribble", "Note", "skip",
        note="becomes a Note"),
    "537b0419-bbc2-4ff4-bf08-afe526367b2c": GhMap("Custom Preview", None, "skip",
        note="eye + colour on the upstream node"),
    "919e146f-30ae-4aae-be34-4d72f555e7da": GhMap("Brep", "Geometry", "container"),
    "d5967b9f-e8ee-436b-a8ad-29fdcecf32d5": GhMap("Curve", "Curve", "container"),
    "deaf8653-5528-4286-807c-3de8b8dad781": GhMap("Surface", "Surface", "container"),
    "fbac3e32-f100-4292-8692-77240a42fd1a": GhMap("Point", "Point", "container"),
    "ac2bc2cb-70fb-4dd5-9c78-7e1ea97fe278": GhMap("Geometry", "Geometry", "container"),

    # --- aliases: a core node does the same job ---------------------------
    "d1a28e95-cf96-4936-bf34-8bf142d731bf": GhMap("Construct Domain", "ConstructDomain", "alias",
        inputs={"Domain start": "min", "Domain end": "max"}, outputs={"Domain": "domain"},
        params={"Domain start": "min", "Domain end": "max"}),
    "f44b92b0-3b5b-493a-86f4-fd7408c3daf3": GhMap("Bounds", "Bounds", "alias",
        inputs={"Numbers": "list"}, outputs={"Domain": "domain"}),
    "2fcc2743-8339-4cdf-a046-a1f17439191d": GhMap("Remap Numbers", "Remap", "alias",
        inputs={"Value": "value", "Source": "source", "Target": "target"},
        outputs={"Mapped": "result"},
        note="Clipped output not built; persistent Source/Target [a,b] -> "
             "source_min/source_max, target_min/target_max (GH default target is 0..1)"),
    "a0d62394-a118-422d-abb3-6af115c75b25": GhMap("Addition", "Add", "alias",
        inputs={"A": "a", "B": "b"}, outputs={"Result": "result"}),
    "9c007a04-d0d9-48e4-9da3-9ba142bc4d46": GhMap("Subtraction", "SubtractNum", "alias",
        inputs={"A": "a", "B": "b"}, outputs={"Result": "result"}),
    "9c85271f-89fa-4e9f-9f4a-d75802120ccc": GhMap("Division", "Divide", "alias",
        inputs={"A": "a", "B": "b"}, outputs={"Result": "result"}),
    "3581f42a-9592-4549-bd6b-1c0fc39d067b": GhMap("Construct Point", "ConstructPoint", "alias",
        inputs={"X coordinate": "x", "Y coordinate": "y", "Z coordinate": "z"},
        outputs={"Point": "point"},
        params={"X coordinate": "x", "Y coordinate": "y", "Z coordinate": "z"}),
    "4c4e56eb-2f04-43f9-95a3-cc46a14f495a": GhMap("Line", "Line", "alias",
        inputs={"Start Point": "start", "End Point": "end"}, outputs={"Line": "result"}),
    "c75b62fa-0a33-4da7-a5bd-03fd0068fd93": GhMap("Length", "CurveLength", "alias",
        inputs={"Curve": "curve"}, outputs={"Length": "length"}),
    "10434a15-da85-4281-bb64-a2b3a995b9c6": GhMap("Solid Union", "Union", "alias",
        inputs={"Breps": "shapes"}, outputs={"Result": "result"}),
    "fab11c30-2d9c-4d15-ab3c-2289f1ae5c21": GhMap("Solid Difference", "Subtract", "alias",
        inputs={"Breps A": "a", "Breps B": "b"}, outputs={"Result": "result"}),
    "8073a420-6bec-49e3-9b18-367f6fd76ac3": GhMap("Join Curves", "Join", "alias",
        inputs={"Curves": "shapes"}, outputs={"Curves": "result"},
        note="Preserve direction is ignored"),
    "60e7defa-8b21-4ee1-99aa-a9223d6134ff": GhMap("Mesh Brep", "ToMesh", "alias",
        inputs={"Brep": "shape"}, outputs={"Mesh": "result"},
        note="Settings ignored (noodle's tolerance)"),
    "c8cb6a5c-2ffd-4095-ba2a-5c35015e09e4": GhMap("Populate Geometry", "PopulateGeometry", "alias",
        inputs={"Geometry": "region", "Count": "count", "Seed": "seed"},
        outputs={"Population": "points"}, params={"Count": "count", "Seed": "seed"},
        note="Points (existing points to avoid) not built; another RNG, so other "
             "points for the same seed"),
    "0bb3d234-9097-45db-9998-621639c87d3b": GhMap("Bounding Box", "BoundingBox", "alias",
        inputs={"Content": "shape"}, outputs={"Box": "box"},
        note="world-aligned only: the Plane input is ignored"),
    "e9eb1dcf-92f6-4d4d-84ae-96222d60f56b": GhMap("Move", "Move", "alias",
        inputs={"Geometry": "shape", "Motion": "offset"}, outputs={"Geometry": "result"},
        note="Transform output not built"),
    "f12daa2f-4fd5-48c1-8ac3-5dea476912ca": GhMap("Mirror", "Mirror", "alias",
        inputs={"Geometry": "shape", "Plane": "plane"}, outputs={"Geometry": "result"},
        note="Transform output not built; an unwired Plane = YZ in GH, set the "
             "`plane` dropdown to YZ"),
    "e2996e6c-e067-42fa-8f44-2192c6763262": GhMap("Rich Graph Mapper", "GraphMapper", "alias",
        inputs={"Values": "t"}, outputs={"Values": "result"},
        note="plugin RichedGraphMapper. Source/Target other than [0,1] need a Remap "
             "on each side; the curve goes into the `curve` param"),

    # --- new nodes (category grasshopper) ------------------------------------
    "9103c240-a6a9-4223-9b42-dbd19bf38e2b": _new("Unit Z", "GhUnitZ", ["Factor"], ["Unit vector"]),
    "79f9fbb3-8f1d-4d9a-88a9-f7961b1012cd": _new("Unit X", "GhUnitX", ["Factor"], ["Unit vector"]),
    "d3d195ea-2d59-4ffa-90b1-8b7ff3369f69": _new("Unit Y", "GhUnitY", ["Factor"], ["Unit vector"]),
    "17b7152b-d30d-4d50-b9ef-c9fe25576fc2": _new("XY Plane", "GhXYPlane", ["Origin"], ["Plane"]),
    "8cc3a196-f6a0-49ea-9ed9-0cb343a3ae64": _new("XZ Plane", "GhXZPlane", ["Origin"], ["Plane"]),
    "fad344bc-09b1-4855-a2e6-437ef5715fe3": _new("YZ Plane", "GhYZPlane", ["Origin"], ["Plane"]),
    "9abae6b7-fa1d-448c-9209-4a8155345841": _new("Deconstruct", "GhDeconstruct",
        ["Point"], ["X component", "Y component", "Z component"]),
    "7986486c-621a-48fb-8f27-a28a22c91cc9": _new("Average", "GhAverage",
        ["Input"], ["Arithmetic mean"]),
    "8555a743-36c1-42b8-abcc-06d9cb94519f": _new("Construct Domain²", "GhConstructDomain2",
        ["Domain U", "Domain V"], ["2D Domain"], rename={"2D Domain": "domain2"}),
    "47c30f9d-b685-4d4d-9b20-5b60e48d5af8": _new("Deconstruct Domain²", "GhDeconstructDomain2",
        ["Domain"], ["U min", "U max", "V min", "V max"],
        note="a wired SURFACE gives OCCT's UV domain for it, not Rhino's numbers"),
    "d93100b6-d50b-40b2-831a-814659dc38e3": _new("Rectangle", "GhRectangle",
        ["Plane", "X Size", "Y Size", "Radius"], ["Rectangle", "Length"]),
    "807b86e3-be8d-4970-92b5-f8cdcb45b06b": _new("Circle", "GhCircle",
        ["Plane", "Radius"], ["Circle"]),
    "1a38d325-98de-455c-93f1-bca431bc1243": _new("Offset Curve", "GhOffsetCurve",
        ["Curve", "Distance", "Plane", "Corners"], ["Curve"],
        note="the older Offset Curve; Corners is the 2nd 'C' input"),
    "a6129be8-efbd-4039-8128-07a601d50af4": GhMap("Offset Curve", "GhOffsetCurve", "new",
        inputs={"Curve": "curve", "Plane": "plane", "Distance": "distance",
                "Both Sides": "both_sides"},
        outputs={"Offset": "curve"},
        note="the Rhino 8 Offset Curve: Kinks and Cap Type are not built"),
    "424eb433-2b3a-4859-beaf-804d8af0afd7": _new("Control Points", "GhControlPoints",
        ["Curve"], ["Points", "Weights", "Knots"]),
    "dde71aef-d6ed-40a6-af98-6b0673983c82": _new("Nurbs Curve", "GhNurbsCurve",
        ["Vertices", "Degree", "Periodic"], ["Curve", "Length", "Domain"]),
    "ccc7b468-e743-4049-891f-299432545898": _new("Curve Middle", "GhCurveMiddle",
        ["Curve"], ["Midpoint"]),
    "11bbd48b-bb0a-4f1b-8167-fa297590390d": _new("End Points", "GhEndPoints",
        ["Curve"], ["Start", "End"]),
    "d7e8bbd8-c7fd-4640-a594-4554c6a4670f": GhMap("Close Curve", "GhCloseCurve", "new",
        inputs={"Curve": "curve"},
        outputs={"Closed": "closed", "Result Boolean": "result_boolean"},
        note="Close Type/Bulge/Tolerance not built: always a straight closing segment"),
    "269eaa85-9997-4d77-a9ba-4c58cb45c9d3": _new("Discontinuity", "GhDiscontinuity",
        ["Curve", "Level"], ["Points", "Parameters"]),
    "62cc9684-6a39-422e-aefa-ed44643557b9": _new("Extend Curve", "GhExtendCurve",
        ["Curve", "Type", "Start", "End"], ["Curve"],
        note="only the line extension (Type 0) is built"),
    "2162e72e-72fc-4bf8-9459-d4d82fa8aa14": _new("Divide Curve", "GhDivideCurve",
        ["Curve", "Count", "Kinks"], ["Points", "Tangents", "Parameters"],
        note="Kinks ignored; parameters are normalised (0..1), not Rhino's"),
    "4d2a06bd-4b0f-4c65-9ee0-4220e4c01703": GhMap("Scale", "GhScale", "new",
        inputs={"Geometry": "geometry", "Center": "center", "Factor": "factor"},
        outputs={"Geometry": "geometry"}, note="Transform output not built"),
    "290f418a-65ee-406a-a9d0-35699815b512": GhMap("Scale NU", "GhScaleNU", "new",
        inputs={"Geometry": "geometry", "Plane": "plane", "Scale X": "scale_x",
                "Scale Y": "scale_y", "Scale Z": "scale_z"},
        outputs={"Geometry": "geometry"}, note="Transform output not built"),
    "97aabe15-99df-452d-ab10-fc3303107bed": GhMap("Move To Point", "GhMoveToPoint", "new",
        inputs={"Geometry": "geometry", "Point A": "point_a", "Point B": "point_b"},
        outputs={"Moved": "moved", "Vector": "vector"}, note="Transform output not built"),
    "d51e9b65-aa4e-4fd6-976c-cef35d421d05": _new("Boundary Surfaces", "GhBoundarySurfaces",
        ["Edges"], ["Surfaces"]),
    "962034e9-cc27-4394-afc4-5c16e3447cf9": _new("Extrude", "GhExtrude",
        ["Base", "Direction"], ["Extrusion"]),
    "14801f92-4837-4791-b992-f3c8bab65c45": _new("Unsplit Revolve Surface", "GhUnsplitRevolve",
        ["Profile", "Axis", "Revolution"], ["Unsplit"],
        note="UseDegrees on Revolution -> param `degrees`"),
    "6a9ccaab-1b03-484e-bbda-be9c81584a66": _new("Isotrim", "GhIsotrim",
        ["Surface", "Domain"], ["Surface"],
        note="domain in the face's OCCT parameters; Reparameterize not built"),
    "7db14002-c09c-4d7b-9f80-e4e2b00dfa1d": _new("Surface Split", "GhSurfaceSplit",
        ["Surface", "Curves"], ["Fragments"],
        note="fragment ORDER is OCCT's: a List Item by fixed index may pick another piece"),
    "6b5812f5-bb36-4d74-97fc-5a1f2f77452d": _new("Pull Curve", "GhPullCurve",
        ["Curve", "Surface"], ["Curve"],
        note="a curve crossing the seam of a closed surface comes back cut at the seam"),
    "902289da-28dc-454b-98d4-b8f8aa234516": _new("Pull Point", "GhPullPoint",
        ["Point", "Geometry"], ["Closest Point", "Distance"]),
    "904e4b56-484a-4814-b35f-aa4baf362117": _new("Brep | Brep", "GhBrepBrep",
        ["Brep A", "Brep B"], ["Curves", "Points"], note="Points output is always empty"),
    "3eba04bc-00e8-416d-b58f-a3dc8b3e22e2": GhMap("Trim with Brep", "GhTrimWithBrep", "new",
        inputs={"Curve": "curve", "Brep": "brep"},
        outputs={"Inside": "inside", "Outside": "outside"}),
    "8d372bdc-9800-45e9-8a26-6e33c5253e21": _new("Deconstruct Brep", "GhDeconstructBrep",
        ["Brep"], ["Faces", "Edges", "Vertices"]),
    "1addcc85-b04e-46e6-bd4a-6f6c93bf7efd": _new("Brep Join", "GhBrepJoin",
        ["Breps"], ["Breps", "Closed"]),
    "b2345234-fae4-4aea-9a12-f96abe075d70": _new("Offset Surface", "GhOffsetSurface",
        ["Surface", "Distance", "Both Sides", "Create Solid"], ["Offset"],
        note="Both Sides not built"),
    "86b28a7e-94d9-4791-8306-e13e10d5f8d5": _new("Area", "GhArea",
        ["Geometry"], ["Area", "Centroid"]),
    "ba9bb57a-61cf-4207-a1c4-994e371ba4f9": _new("Voronoi 3D", "GhVoronoi3D",
        ["Points", "Box"], ["Cells", "Boundary"]),
    "59daf374-bc21-4a5e-8282-5504fb7ae9ae": _new("List Item", "GhListItem",
        ["List", "Index", "Wrap"], ["Item"],
        note="one output: extra indexed outputs (+1, +2…) are not built"),
    "d8332545-21b2-4716-96e3-8559a9876e17": _new("Dispatch", "GhDispatch",
        ["List", "Dispatch pattern"], ["List A", "List B"]),
    "6f93d366-919f-4dda-a35e-ba03dd62799b": _new("Sort List", "GhSortList",
        ["Keys", "Values A"], ["Keys", "Values A"],
        note="only one values list (A)"),
}


def lookup(type_guid: str) -> GhMap | None:
    return COMPONENTS.get(type_guid)
