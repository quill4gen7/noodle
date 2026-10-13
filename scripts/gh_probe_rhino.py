#! python3
# Runs INSIDE Rhino 8 (RhinoCode), not in noodle. Builds tiny Grasshopper
# definitions component by component (no .gh file), solves them and writes
# what each output holds — to pin down a component's semantics where noodle
# has to reproduce it. Reads <BASE>/probe_job.json {"out": name}; the cases
# are below. Every number is GH's own answer.
import json, os, traceback
import System
import Rhino
import Rhino.Geometry as G
import clr

clr.AddReference("Grasshopper")
import Grasshopper as gh

BASE = os.path.join(os.path.expanduser("~"), "noodle-ref")
job = json.load(open(os.path.join(BASE, "probe_job.json")))
OUT = os.path.join(BASE, job["out"])

ghp = Rhino.RhinoApp.GetPlugInObject("Grasshopper")
if ghp is not None and not ghp.IsEditorLoaded():
    ghp.RunHeadless()

GUID = {
    "Discontinuity": "269eaa85-9997-4d77-a9ba-4c58cb45c9d3",
    "Voronoi 3D": "ba9bb57a-61cf-4207-a1c4-994e371ba4f9",
    "Bounding Rectangle": "22f0d53c-5673-456d-bdef-1c7085693481",
    "Offset Curve PF": "a6129be8-efbd-4039-8128-07a601d50af4",
    "Trim With Plane": "a725d81b-2e91-429f-af46-152f07c50617",
}


def describe(v):
    d = {"type": type(v).__name__}
    try:
        if isinstance(v, G.Curve):
            d["length"] = v.GetLength()
            d["closed"] = v.IsClosed
            b = v.GetBoundingBox(True)
            d["bbox"] = [b.Min.X, b.Min.Y, b.Min.Z, b.Max.X, b.Max.Y, b.Max.Z]
        elif isinstance(v, G.Brep):
            a = G.AreaMassProperties.Compute(v)
            d["area"] = a.Area if a else None
            d["solid"] = v.IsSolid
            if v.IsSolid:
                vp = G.VolumeMassProperties.Compute(v)
                d["volume"] = vp.Volume if vp else None
            d["faces"] = v.Faces.Count
            b = v.GetBoundingBox(True)
            d["bbox"] = [b.Min.X, b.Min.Y, b.Min.Z, b.Max.X, b.Max.Y, b.Max.Z]
        elif isinstance(v, G.Box):
            d["bbox"] = [v.BoundingBox.Min.X, v.BoundingBox.Min.Y, v.BoundingBox.Min.Z,
                         v.BoundingBox.Max.X, v.BoundingBox.Max.Y, v.BoundingBox.Max.Z]
            d["volume"] = v.Volume
        elif isinstance(v, G.Rectangle3d):
            b = v.BoundingBox
            d["bbox"] = [b.Min.X, b.Min.Y, b.Min.Z, b.Max.X, b.Max.Y, b.Max.Z]
            d["normal"] = [v.Plane.ZAxis.X, v.Plane.ZAxis.Y, v.Plane.ZAxis.Z]
        elif isinstance(v, G.Point3d):
            d["point"] = [v.X, v.Y, v.Z]
        elif isinstance(v, (int, float, bool, str)):
            d["value"] = v
        elif isinstance(v, G.Mesh):
            d["faces"] = v.Faces.Count
    except Exception as e:
        d["err"] = str(e)
    return d


def run(name, inputs):
    """Emit one component, give it persistent inputs, solve, read outputs."""
    doc = gh.Kernel.GH_Document()
    comp = gh.Instances.ComponentServer.EmitObject(System.Guid(GUID[name]))
    if comp is None:
        return {"error": "component not found: " + name}
    comp.CreateAttributes()
    doc.AddObject(comp, False)
    for p in comp.Params.Input:
        if p.Name in inputs:
            vals = inputs[p.Name]
            vals = vals if isinstance(vals, list) else [vals]
            p.PersistentData.Clear()
            for v in vals:
                p.PersistentData.Append(gh.Kernel.GH_Convert.ToGoo(v))
    doc.Enabled = True
    doc.NewSolution(True, gh.Kernel.GH_SolutionMode.Silent)
    res = {"inputs": [p.Name for p in comp.Params.Input], "outputs": {}}
    msgs = []
    for lvl in (gh.Kernel.GH_RuntimeMessageLevel.Error, gh.Kernel.GH_RuntimeMessageLevel.Warning):
        msgs += [str(lvl) + ": " + m for m in comp.RuntimeMessages(lvl)]
    if msgs:
        res["messages"] = msgs
    for p in comp.Params.Output:
        items = []
        for pa in p.VolatileData.Paths:
            for goo in p.VolatileData.get_Branch(pa):
                if goo is None:
                    items.append(None)
                    continue
                items.append(dict(path=str(pa), **describe(goo.ScriptVariable())))
        res["outputs"][p.Name] = items
    doc.Dispose()
    return res


def L(*pts, closed=False):
    ps = [G.Point3d(*p) for p in pts]
    if closed:
        ps.append(ps[0])
    return G.PolylineCurve(ps)


cases = {}
try:
    # (a) Discontinuity: how many points, which parameters, levels 1 and 2
    square = L((0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0), closed=True)
    rounded = G.Curve.CreateFilletCornersCurve(square, 2.0, 0.001, 0.001)
    circle = G.ArcCurve(G.Circle(G.Plane.WorldXY, 5))
    circle_nurbs = G.Circle(G.Plane.WorldXY, 5).ToNurbsCurve()
    ell = L((0, 0, 0), (10, 0, 0), (10, 5, 0))
    for cname, crv in (("square", square), ("rounded", rounded), ("circle_arc", circle),
                       ("circle_nurbs", circle_nurbs), ("L_open", ell)):
        for lvl in (1, 2):
            cases[f"disc/{cname}/level{lvl}"] = run("Discontinuity", {"Curve": crv, "Level": lvl})

    # (c) Voronoi 3D in a FLAT box (as ikeaLack #104: zero thickness along x)
    pts = [G.Point3d(220, y, z) for y, z in ((-200, 60), (-150, 120), (-60, 80), (-30, 170), (-120, 180))]
    flat = G.Box(G.BoundingBox(G.Point3d(220, -215, 52), G.Point3d(220, -5, 187)))
    thick = G.Box(G.BoundingBox(G.Point3d(218, -215, 52), G.Point3d(222, -5, 187)))
    cases["voronoi/flat_box"] = run("Voronoi 3D", {"Points": pts, "Box": flat})
    cases["voronoi/thick_box"] = run("Voronoi 3D", {"Points": pts, "Box": thick})

    # (d) Bounding Rectangle of a box z 10..30: Slide and Axis
    box = G.Box(G.BoundingBox(G.Point3d(0, 0, 10), G.Point3d(20, 10, 30))).ToBrep()
    for axis in (0, 1, 2):
        for slide in (0.0, 0.5, 1.0):
            cases[f"brect/axis{axis}/slide{slide}"] = run(
                "Bounding Rectangle", {"Geometry": box, "Plane": G.Plane.WorldXY,
                                       "Axis": axis, "Slide": slide, "Union": True})

    # (e) Pufferfish Offset Curve on a line: Both Sides and Cap Type
    line = G.LineCurve(G.Point3d(0, 0, 0), G.Point3d(100, 0, 0))
    for both in (0.0, -5.0):
        for cap in (0, 1, 2):
            cases[f"pfoffset/both{both}/cap{cap}"] = run(
                "Offset Curve PF", {"Curve": line, "Plane": G.Plane.WorldXY, "Distance": 5.0,
                                    "Both Sides": both, "Kinks": 0, "Cap Type": cap})
    # and on a closed square, to see the side
    cases["pfoffset/square/d5"] = run("Offset Curve PF", {"Curve": square, "Plane": G.Plane.WorldXY,
                                                          "Distance": 5.0, "Both Sides": 0.0,
                                                          "Kinks": 0, "Cap Type": 0})

    # (f) Trim With Plane: a 20 mm cube centred at the origin, plane XY
    cube = G.Box(G.BoundingBox(G.Point3d(-10, -10, -10), G.Point3d(10, 10, 10))).ToBrep()
    for direction in (True, False):
        for cap in (True, False):
            cases[f"trim/dir{direction}/cap{cap}"] = run(
                "Trim With Plane", {"Brep": cube, "Plane": G.Plane.WorldXY,
                                    "Plane Direction": direction, "Cap": cap, "Tolerance": 0.001})
except Exception:
    cases["fatal"] = traceback.format_exc()

with open(OUT, "w") as f:
    json.dump(cases, f, indent=1)
