"""Point 1 of PLAN_MECHANISM: kinematic mechanism feasibility probe.

Pure numpy + manifold3d (no build123d): involute spur gears, automatic mesh
detection + tooth phasing, per-frame propagation from drivers, interference
check by boolean. Run inside the container:  docker exec -i noodle python - < this
"""
import math, time
import numpy as np
from manifold3d import CrossSection, Manifold


# ---------------------------------------------------------------- the gear
def inv(a):
    return math.tan(a) - a


def gear_profile(z, m, pa_deg=20.0, backlash=0.05, n_flank=8, n_arc=3):
    """Closed 2D involute outline, tooth 0 centred on +X. backlash = mm of
    circumferential play taken off each tooth (at the pitch circle)."""
    pa = math.radians(pa_deg)
    rp = m * z / 2.0
    rb = rp * math.cos(pa)
    ra = rp + m
    rf = rp - 1.25 * m
    half = math.pi / (2 * z) - (backlash / 2.0) / rp   # half tooth angle @ pitch
    def flank_ang(r):          # angle from tooth centre to the flank at radius r
        r = max(r, rb)
        return half + inv(pa) - inv(math.acos(rb / r))
    r0 = max(rb, rf)
    rs = [r0 + (ra - r0) * (i / n_flank) for i in range(n_flank + 1)]
    pts = []
    pitch = 2 * math.pi / z
    for k in range(z):
        c = k * pitch
        right = [(r, c - flank_ang(r)) for r in rs]
        left = [(r, c + flank_ang(r)) for r in reversed(rs)]
        if rf < r0:
            right.insert(0, (rf, c - flank_ang(r0)))
            left.append((rf, c + flank_ang(r0)))
        tip = [(ra, right[-1][1] + (left[0][1] - right[-1][1]) * i / (n_arc + 1))
               for i in range(1, n_arc + 1)]
        a_end = left[-1][1]
        a_next = c + pitch - flank_ang(r0)
        root = [(rf, a_end + (a_next - a_end) * i / (n_arc + 1))
                for i in range(1, n_arc + 1)]
        for r, a in right + tip + left + root:
            pts.append((r * math.cos(a), r * math.sin(a)))
    return pts


def spur_gear(z, m, width, bore=0.0, **kw):
    cs = CrossSection([gear_profile(z, m, **kw)])
    if bore > 0:
        cs = cs - CrossSection.circle(bore / 2, 48)
    man = cs.extrude(width)            # z in [0, width], axis +Z through origin
    meta = {"z": z, "m": m, "width": width}
    return man, meta


# ------------------------------------------------------------ the mechanism
class Body:
    """A rigid body on a revolute joint about a world axis (here always
    parallel to Z, through `center`), optionally sliding along it (prismatic,
    `slide`). It may carry several gears (a cluster / a shaft)."""
    def __init__(self, name, center, parts, gears):
        self.name, self.center = name, np.asarray(center, float)
        self.parts = parts          # list of Manifold in body frame
        self.gears = gears          # list of (meta, z0) : gear face at z0..z0+w
        self.theta = 0.0
        self.slide = 0.0


def rot_z(th):
    c, s = math.cos(th), math.sin(th)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def mesh_pairs(bodies, tol=0.05):
    """Every gear pair currently in mesh: parallel axes (all Z here), centre
    distance = m(z1+z2)/2, axial overlap of the two faces > 0."""
    out = []
    for i, a in enumerate(bodies):
        for j in range(i + 1, len(bodies)):
            b = bodies[j]
            d = np.linalg.norm((b.center - a.center)[:2])
            for ga, za in a.gears:
                for gb, zb in b.gears:
                    if abs(ga["m"] - gb["m"]) > 1e-9:
                        continue
                    if abs(d - ga["m"] * (ga["z"] + gb["z"]) / 2) > tol:
                        continue
                    lo = max(a.center[2] + a.slide + za, b.center[2] + b.slide + zb)
                    hi = min(a.center[2] + a.slide + za + ga["width"],
                             b.center[2] + b.slide + zb + gb["width"])
                    if hi - lo > 1e-6:
                        out.append((i, j, ga, gb, hi - lo))
    return out


def phase_ok(a, b, ga, gb, theta_a):
    """theta_b that meshes with a at theta_a (tooth of a on the line of centres
    <-> space of b on it), as the family theta_b0 + k*2pi/zb."""
    phi = math.atan2(*(b.center - a.center)[1::-1])
    za, zb = ga["z"], gb["z"]
    # when theta_a = phi a tooth of a points at b; b must then show a space:
    # theta_b = phi + pi + pi/zb.  The ratio carries it on from there.
    return (phi + math.pi + math.pi / zb) - (za / zb) * (theta_a - phi)


ENGAGING = {}     # pair key -> phase error (rad) still to recover, "clash" mode


def solve_frame(bodies, drivers, couplings_fixed, prev_pairs, engage="clash",
                first=False):
    """drivers: {body index: theta}. couplings_fixed: list of (i, j) that turn
    together (spline / shaft). Propagate by BFS. A body no driver reaches keeps
    its last angle. Returns (pairs, snaps, pairs_full).

    A pair that comes INTO mesh while moving is out of phase by up to half a
    pitch. engage="snap" corrects it at once (b jumps). engage="clash" keeps b
    continuous and recovers the error in proportion to the axial overlap, so
    the teeth grind while they enter — the interference check reports it — and
    are exactly in phase once fully engaged. The frame-0 alignment is ASSEMBLY
    (first=True) and always snaps."""
    pairs = mesh_pairs(bodies)
    adj = {k: [] for k in range(len(bodies))}
    for (i, j, ga, gb, ov) in pairs:
        full = min(ga["width"], gb["width"])
        adj[i].append((j, ("gear", ga, gb, ov / full)))
        adj[j].append((i, ("gear", gb, ga, ov / full)))
    for (i, j) in couplings_fixed:
        adj[i].append((j, ("fix",)))
        adj[j].append((i, ("fix",)))
    seen, queue, snaps = set(), [], []
    for k, th in drivers.items():
        bodies[k].theta = th
        seen.add(k); queue.append(k)
    keys = {(p[0], p[1]) for p in pairs}
    for gone in set(ENGAGING) - keys:          # pulled out mid-entry: forget it
        ENGAGING.pop(gone)
    newkeys = keys - prev_pairs
    while queue:
        i = queue.pop(0)
        for j, c in adj[i]:
            if j in seen:
                continue
            a, b = bodies[i], bodies[j]
            if c[0] == "fix":
                b.theta = a.theta
            else:
                key = (min(i, j), max(i, j))
                tgt = phase_ok(a, b, c[1], c[2], a.theta)
                pitch = 2 * math.pi / c[2]["z"]
                frac = min(1.0, c[3])
                if key in newkeys:
                    k = round((b.theta - tgt) / pitch)
                    err = b.theta - (tgt + k * pitch)    # b ahead of the phase
                    if abs(err) > 1e-6:
                        snaps.append((a.name, b.name, round(math.degrees(-err), 3)))
                        if engage == "clash" and not first:
                            ENGAGING[key] = (k, err)
                if key in ENGAGING:
                    k, err = ENGAGING[key]
                    b.theta = tgt + k * pitch + err * (1.0 - frac)
                    if frac >= 1.0:
                        ENGAGING.pop(key)
                else:
                    k = round((b.theta - tgt) / pitch)
                    b.theta = tgt + k * pitch
            seen.add(j); queue.append(j)
    return keys, snaps, pairs


def posed(body):
    M = np.zeros((3, 4))
    M[:, :3] = rot_z(body.theta)
    M[:, 3] = body.center + np.array([0, 0, body.slide])
    return [p.transform(M) for p in body.parts]


def interference(bodies, skip=()):
    """Pairwise boolean volume between posed bodies (bbox prefilter)."""
    P = [Manifold.batch_boolean(posed(b), __import__("manifold3d").OpType.Add)
         if len(b.parts) > 1 else posed(b)[0] for b in bodies]
    hits = []
    for i in range(len(P)):
        bi = P[i].bounding_box()
        for j in range(i + 1, len(P)):
            if (i, j) in skip:
                continue
            bj = P[j].bounding_box()
            if any(bi[k] > bj[k + 3] or bj[k] > bi[k + 3] for k in range(3)):
                continue
            v = (P[i] ^ P[j]).volume()
            if v > 1e-4:
                hits.append((bodies[i].name, bodies[j].name, v))
    return hits


# ------------------------------------------------------------------ probes
def probe_train():
    print("== 3 gears in a row (z 20 -> 30 -> 15, m 2) ==")
    m = 2.0
    t0 = time.perf_counter()
    g1, M1 = spur_gear(20, m, 8, bore=6)
    g2, M2 = spur_gear(30, m, 8, bore=6)
    g3, M3 = spur_gear(15, m, 8, bore=6)
    print(f"build 3 gears: {1000*(time.perf_counter()-t0):.1f} ms, tris",
          g1.num_tri(), g2.num_tri(), g3.num_tri())
    c1 = (0, 0, 0)
    c2 = (m * (20 + 30) / 2, 0, 0)
    a3 = math.radians(60)        # third gear off-axis: phase must handle any angle
    d23 = m * (30 + 15) / 2
    c3 = (c2[0] + d23 * math.cos(a3), d23 * math.sin(a3), 0)
    B = [Body("g20", c1, [g1], [(M1, 0)]), Body("g30", c2, [g2], [(M2, 0)]),
         Body("g15", c3, [g3], [(M3, 0)])]
    prev = set()
    N = 120
    ts, tf = [], []
    worst = 0
    for f in range(N + 1):
        th = 2 * math.pi * f / N        # one input revolution
        t = time.perf_counter()
        prev, snaps, pairs = solve_frame(B, {0: th}, [], prev, first=(f == 0))
        ts.append(time.perf_counter() - t)
        t = time.perf_counter()
        hits = interference(B)
        tf.append(time.perf_counter() - t)
        worst = max([worst] + [h[2] for h in hits])
        if f == 0:
            print("pairs in mesh:", [(B[p[0]].name, B[p[1]].name) for p in pairs],
                  "snaps:", snaps)
    print(f"output turns: g30 {math.degrees(B[1].theta):.2f} deg (expect "
          f"{-360*20/30:.2f}), g15 {math.degrees(B[2].theta):.2f} deg (expect "
          f"{360*20/15:.2f} mod phase)")
    print(f"solve {1000*np.mean(ts):.3f} ms/frame, interference "
          f"{1000*np.mean(tf):.1f} ms/frame, worst overlap {worst:.4f} mm3")
    # negative control: shift g30 by half a tooth -> must clash
    B[1].theta += math.pi / 30
    print("half-tooth off, interference:",
          [(a, b, round(v, 2)) for a, b, v in interference(B)])
    B[1].theta -= math.pi / 30
    # clearance with min_gap
    P = [posed(b)[0] for b in B]
    print("min_gap g20-g30 within 1mm:", round(P[0].min_gap(P[1], 1.0), 4))


def probe_gearbox(ENGAGE="clash"):
    """Sliding-mesh 2-speed: input shaft with 20t + 30t fixed, output shaft
    with a 40t/30t cluster sliding on its spline. Centre distance 60 = m(z1+z2)/2
    for both pairs with m=2: (20+40)=60, (30+30)=60."""
    print(f"\n== 2-speed sliding-mesh gearbox, engage={ENGAGE} ==")
    ENGAGING.clear()
    m, w = 2.0, 8.0
    a20, A20 = spur_gear(20, m, w, bore=6)
    a30, A30 = spur_gear(30, m, w, bore=6)
    b40, B40 = spur_gear(40, m, w, bore=6)
    b30, B30 = spur_gear(30, m, w, bore=6)
    # input: 20t at z 0..8, 30t at z 20..28
    inp = Body("input", (0, 0, 0), [a20, a30.translate((0, 0, 30))],
               [(A20, 0), (A30, 30)])
    # cluster: 40t at z 0..8, 30t at z 20..28 when slide = 0 -> BOTH would mesh!
    # so its pitch is 12 instead: 40t at 0..8, 30t at 12..20. Positions:
    #   slide = 0  : 40t over 20t (1st, 2:1),   30t at 12..20 meshes nothing
    #   slide = 8.5: 40t at 8.5..16.5 nothing, 30t at 20.5..28.5 over 30t (2nd, 1:1)
    #   slide = 4  : 40t at 4..12 over 20t still (0..8 -> 4mm) ... neutral needs
    #   a gap: 40t clears 0..8 once slide >= 8, 30t reaches 20 once slide > 8.
    clu = Body("cluster", (60, 0, 0), [b40, b30.translate((0, 0, 12))],
               [(B40, 0), (B30, 12)])
    out = Body("output", (60, 0, -10), [Manifold.cylinder(40, 3, 3, 24)], [])
    B = [inp, clu, out]
    spline = [(1, 2)]                      # cluster turns with the output shaft
    # fork program: 1st -> neutral -> 2nd. Neutral window = slide exactly 8.0
    def fork(t):
        # input 20t 0..8 / 30t 30..38 ; cluster 40t 0..8 / 30t 12..20
        # slide 0 = 1st, 9 = neutral (nothing overlaps), 18 = 2nd
        keys = [(0.0, 0.0), (0.35, 0.0), (0.45, 9.0), (0.55, 9.0), (0.65, 18.0),
                (1.0, 18.0)]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t <= t1:
                u = 0 if t1 == t0 else (t - t0) / (t1 - t0)
                return v0 + (v1 - v0) * u
        return keys[-1][1]
    prev = set()
    N = 240
    log, clashes, tt = [], [], []
    for f in range(N + 1):
        t = f / N
        clu.slide = fork(t)
        th_in = 4 * math.pi * t          # input turns twice
        t1 = time.perf_counter()
        prev, snaps, pairs = solve_frame(B, {0: th_in}, spline, prev,
                                          engage=ENGAGE, first=(f == 0))
        hits = interference(B, skip={(1, 2)})
        tt.append(time.perf_counter() - t1)
        gear = ("1st" if any(p[2]["z"] == 20 or p[3]["z"] == 20 for p in pairs)
                else "2nd" if pairs else "N")
        log.append((t, gear, math.degrees(out.theta)))
        if snaps:
            print(f"  t={t:.3f} engage {snaps}")
        if hits:
            clashes.append((round(t, 3), [(a, b, round(v, 2)) for a, b, v in hits]))
    for t, g, o in log[::24]:
        print(f"  t={t:.2f} gear={g:3s} output={o:9.2f} deg")
    # ratios over the steady windows
    def rate(ta, tb):
        ia, ib = int(ta * N), int(tb * N)
        din = 4 * math.pi * (tb - ta)
        return -(math.radians(log[ib][2] - log[ia][2])) / din
    print(f"ratio 1st {rate(0.05, 0.3):.4f} (expect 0.5), "
          f"neutral moves {log[int(.54*N)][2]-log[int(.46*N)][2]:.3f} deg (expect 0), "
          f"2nd {rate(0.7, 0.95):.4f} (expect 1.0)")
    print(f"frame {1000*np.mean(tt):.1f} ms incl. interference; "
          f"{len(clashes)} frames in clash")
    if clashes:
        peak = max(clashes, key=lambda c: c[1][0][2])
        print(f"  clash t={clashes[0][0]}..{clashes[-1][0]}, peak {peak[1][0][2]} mm3 "
              f"at t={peak[0]}; after t={clashes[-1][0]} the mesh is clean")


probe_train()
probe_gearbox("snap")
probe_gearbox("clash")
