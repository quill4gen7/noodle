#! python3
# Runs INSIDE Rhino 8 (RhinoCode / ScriptEditor), not in noodle: the reference
# side of scripts/gh_compare.py. Reads <BASE>/job.json {file, out, detail}.
# Open a .gh in Grasshopper, solve it, and dump per-object measures to JSON:
# for each output parameter: branch counts, item types, bbox, sum of
# length/area/volume, and the first items in detail. Edges of Breps feeding
# the objects listed in DETAIL are dumped one by one (edge order matters).
import json, os, sys, traceback
import System
import Rhino
import Rhino.Geometry as G
import clr

BASE = os.path.join(os.path.expanduser("~"), "noodle-ref")
open(os.path.join(BASE, "started.txt"), "w").write("ok")
job = json.load(open(os.path.join(BASE, "job.json")))
GH_FILE = os.path.join(BASE, job["file"])
OUT = os.path.join(BASE, job["out"])
DETAIL = set(job.get("detail", []))          # instance guids of objects to detail
log = []


def save(res):
    with open(OUT, "w") as f:
        json.dump(res, f)


try:
    ghp = Rhino.RhinoApp.GetPlugInObject("Grasshopper")
    # no LoadEditor(): it can raise a modal window that blocks the script
    # server until someone clicks. Headless is enough for GH_DocumentIO.
    if ghp is not None and not ghp.IsEditorLoaded():
        ghp.RunHeadless()
    clr.AddReference("Grasshopper")
    import Grasshopper as gh
    # GH_DocumentIO.Open pops a modal "IO generated N messages" window on any
    # archive warning, which blocks the script server until someone clicks.
    # Reading the archive directly reports nothing to the UI.
    clr.AddReference("GH_IO")
    from GH_IO.Serialization import GH_Archive
    arch = GH_Archive()
    if not arch.ReadFromFile(GH_FILE):
        raise RuntimeError("cannot read " + GH_FILE)
    doc = gh.Kernel.GH_Document()
    if not arch.ExtractObject(doc, "Definition"):
        raise RuntimeError("cannot extract the definition from " + GH_FILE)
    io_messages = [str(m.Message) for m in arch.Messages] if hasattr(arch, "Messages") else []
    # Geometry REFERENCED from a .3dm we do not have: GH read its cached copy
    # from the file but looks the object up in the Rhino document at solve time
    # and comes back empty. Dropping the reference makes it internalised, i.e.
    # the cached copy is used — the same geometry noodle's importer rebuilds.
    unref = 0
    for obj in doc.Objects:
        if isinstance(obj, gh.Kernel.IGH_Param):
            try:
                pd = obj.PersistentData
            except Exception:
                continue
            for goo in pd.AllData(True):
                if isinstance(goo, gh.Kernel.Types.IGH_GeometricGoo) and goo.IsReferencedGeometry:
                    try:
                        goo.ReferenceID = System.Guid.Empty
                        unref += 1
                    except Exception:
                        pass
    gh.Instances.DocumentServer.AddDocument(doc)
    doc.Enabled = True
    t0 = System.DateTime.Now
    doc.NewSolution(True, gh.Kernel.GH_SolutionMode.Silent)
    solve_s = (System.DateTime.Now - t0).TotalSeconds
except Exception:
    save({"fatal": traceback.format_exc()})
    raise


def geo_of(goo):
    try:
        v = goo.ScriptVariable()
    except Exception:
        v = None
    return v


def bbox_of(v):
    try:
        if isinstance(v, G.Point3d):
            return G.BoundingBox(v, v)
        if isinstance(v, G.GeometryBase):
            return v.GetBoundingBox(True)
        if isinstance(v, G.Plane):
            return G.BoundingBox(v.Origin, v.Origin)
        for t in ("BoundingBox",):
            if hasattr(v, t):
                return getattr(v, t)
    except Exception:
        pass
    return None


def metric(v):
    m = {}
    try:
        if isinstance(v, G.Curve):
            m["length"] = v.GetLength()
            m["closed"] = v.IsClosed
        elif isinstance(v, G.Brep):
            m["area"] = G.AreaMassProperties.Compute(v).Area if v.Faces.Count else 0
            if v.IsSolid:
                vp = G.VolumeMassProperties.Compute(v)
                m["volume"] = vp.Volume if vp else None
            m["faces"] = v.Faces.Count
            m["edges"] = v.Edges.Count
            m["solid"] = v.IsSolid
        elif isinstance(v, G.Surface):
            a = G.AreaMassProperties.Compute(v)
            m["area"] = a.Area if a else None
        elif isinstance(v, G.Mesh):
            m["faces"] = v.Faces.Count
            if v.IsClosed:
                m["volume"] = v.Volume()
        elif isinstance(v, G.Extrusion):
            b = v.ToBrep()
            vp = G.VolumeMassProperties.Compute(b)
            m["volume"] = vp.Volume if vp else None
        elif isinstance(v, (int, float, bool, str)):
            m["value"] = v
        elif isinstance(v, G.Point3d):
            m["point"] = [v.X, v.Y, v.Z]
        elif isinstance(v, G.Vector3d):
            m["vector"] = [v.X, v.Y, v.Z]
        elif isinstance(v, G.Interval):
            m["domain"] = [v.T0, v.T1]
        elif isinstance(v, G.Plane):
            m["plane"] = [v.Origin.X, v.Origin.Y, v.Origin.Z, v.ZAxis.X, v.ZAxis.Y, v.ZAxis.Z]
    except Exception as e:
        m["err"] = str(e)
    return m


def edges_of(b):
    out = []
    for i in range(b.Edges.Count):
        e = b.Edges[i]
        c = e.EdgeCurve
        d = {"i": i, "length": e.GetLength(), "closed": e.IsClosed,
             "start": [e.PointAtStart.X, e.PointAtStart.Y, e.PointAtStart.Z],
             "mid": list((lambda p: (p.X, p.Y, p.Z))(e.PointAt(e.Domain.Mid))),
             "faces": list(e.AdjacentFaces())}
        ok, circ = e.TryGetCircle(0.001)
        if ok:
            d["circle"] = {"r": circ.Radius, "c": [circ.Center.X, circ.Center.Y, circ.Center.Z]}
        out.append(d)
    return out


def dump_param(p, detail):
    data = p.VolatileData
    paths = list(data.Paths)
    counts = [data.get_Branch(pa).Count for pa in paths]
    types, tot = {}, {}
    bb = G.BoundingBox.Empty
    first = []
    n = 0
    for pa in paths:
        for goo in data.get_Branch(pa):
            if goo is None:
                types["null"] = types.get("null", 0) + 1
                continue
            v = geo_of(goo)
            tn = type(v).__name__ if v is not None else goo.TypeName
            types[tn] = types.get(tn, 0) + 1
            b = bbox_of(v)
            if b is not None and b.IsValid:
                bb.Union(b)
            m = metric(v)
            for k in ("length", "area", "volume"):
                if m.get(k) is not None:
                    tot[k] = tot.get(k, 0.0) + m[k]
            if n < (50 if detail else 4):
                item = {"path": str(pa), **m}
                if detail and isinstance(v, G.Brep):
                    item["edge_list"] = edges_of(v)
                first.append(item)
            n += 1
    r = {"name": p.Name, "nick": p.NickName, "guid": str(p.InstanceGuid),
         "branches": len(paths), "counts": counts[:40], "items": n,
         "types": types, "sum": tot, "first": first}
    if bb.IsValid:
        r["bbox"] = [bb.Min.X, bb.Min.Y, bb.Min.Z, bb.Max.X, bb.Max.Y, bb.Max.Z]
    return r


res = {"file": job["file"], "solve_s": solve_s, "unreferenced": unref, "io_messages": io_messages, "objects": []}
for k, obj in enumerate(doc.Objects):
    try:
        e = {"k": k, "guid": str(obj.InstanceGuid), "name": obj.Name, "nick": obj.NickName,
             "type_guid": str(obj.ComponentGuid)}
        detail = str(obj.InstanceGuid) in DETAIL
        msgs = []
        try:
            for lvl in (gh.Kernel.GH_RuntimeMessageLevel.Error, gh.Kernel.GH_RuntimeMessageLevel.Warning):
                for mm in obj.RuntimeMessages(lvl):
                    msgs.append(str(lvl) + ": " + mm)
        except Exception:
            pass
        if msgs:
            e["messages"] = msgs
        if isinstance(obj, gh.Kernel.IGH_Component):
            e["outputs"] = [dump_param(p, detail) for p in obj.Params.Output]
            if detail:
                e["inputs"] = [dump_param(p, True) for p in obj.Params.Input]
        elif isinstance(obj, gh.Kernel.IGH_Param):
            e["outputs"] = [dump_param(obj, detail)]
        res["objects"].append(e)
    except Exception:
        res["objects"].append({"k": k, "error": traceback.format_exc()})
save(res)
try:
    gh.Instances.DocumentServer.RemoveDocument(doc)
    doc.Dispose()
except Exception:
    pass
