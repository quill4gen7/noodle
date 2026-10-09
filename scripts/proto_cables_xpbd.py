# PLAN_CABLES.md — prototipo, NON codice dell'app. Gira nel container:
#   docker exec -i noodle python - < scripts/proto_cables_xpbd.py 2>/dev/null
# Prototipo XPBD: cavo = particelle a passo r, vincoli di lunghezza (rigidi), flessione
# (curvatura discreta, cedevolezza s^3/EI), contatto col pezzo (SDF su griglia) e fra cavi
# (coppie di sfere, cKDTree). Unita': mm, g, s -> forza in uN (1e-6 N).
import time, math, numpy as np, manifold3d as m3
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

def scene(slot_w, slot_h):
    box = m3.Manifold.cube([140, 60, 40], True).translate([0, 0, 20])
    cav = m3.Manifold.cube([136, 56, 40], True).translate([0, 0, 22])
    wall = m3.Manifold.cube([3, 56, 38], True).translate([0, 0, 21])
    slot = m3.Manifold.cube([5, slot_w, slot_h], True).translate([0, 0, 2 + slot_h / 2])
    me = ((box - cav) + (wall - slot)).to_mesh()
    return np.asarray(me.vert_properties)[:, :3], np.asarray(me.tri_verts)

def voxel_parity(V, F, lo, h, shape):
    """solido per parita' lungo z: chiude anche le cavita' (il flood-fill di _voxelize no)."""
    nx, ny, nz = shape
    xc = lo[0] + (np.arange(nx) + 0.5) * h + 1.234e-4; yc = lo[1] + (np.arange(ny) + 0.5) * h + 2.345e-4
    acc = np.zeros((nx, ny, nz + 1), np.int32)
    T = V[F]
    for a, b, c in T:
        x0, x1 = min(a[0], b[0], c[0]), max(a[0], b[0], c[0]); y0, y1 = min(a[1], b[1], c[1]), max(a[1], b[1], c[1])
        i0, i1 = np.searchsorted(xc, x0), np.searchsorted(xc, x1); j0, j1 = np.searchsorted(yc, y0), np.searchsorted(yc, y1)
        if i0 >= i1 or j0 >= j1: continue
        X, Y = np.meshgrid(xc[i0:i1], yc[j0:j1], indexing='ij')
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12: continue
        l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Y - c[1])) / d
        l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Y - c[1])) / d
        l3 = 1 - l1 - l2; ins = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if not ins.any(): continue
        z = l1 * a[2] + l2 * b[2] + l3 * c[2]
        k = np.clip(np.ceil((z - lo[2]) / h - 0.5).astype(int), 0, nz)
        ii, jj = np.nonzero(ins)
        np.add.at(acc, (ii + i0, jj + j0, k[ins]), 1)
    return (np.cumsum(acc, axis=2)[:, :, :nz] % 2) == 1

class SDF:
    def __init__(self, V, F, h=0.75, pad=4):
        self.h = h; self.lo = V.min(0) - pad; hi = V.max(0) + pad
        self.shape = tuple(np.ceil((hi - self.lo) / h).astype(int))
        t = time.time(); solid = voxel_parity(V, F, self.lo, h, self.shape); self.t_vox = time.time() - t
        t = time.time()
        dout = ndi.distance_transform_edt(~solid) * h; din = ndi.distance_transform_edt(solid) * h
        self.g = np.where(solid, -(din - 0.5 * h), dout - 0.5 * h).astype(np.float32); self.t_edt = time.time() - t
        self.grad = np.stack(np.gradient(self.g, h), -1).astype(np.float32)
    def __call__(self, P):
        u = (P - self.lo) / self.h - 0.5
        u = np.clip(u, 0, np.array(self.shape) - 1.001); i = np.floor(u).astype(int); f = u - i
        val = 0; grad = 0
        for dx in (0, 1):
            for dy in (0, 1):
                for dz in (0, 1):
                    w = (f[:, 0] if dx else 1 - f[:, 0]) * (f[:, 1] if dy else 1 - f[:, 1]) * (f[:, 2] if dz else 1 - f[:, 2])
                    idx = (i[:, 0] + dx, i[:, 1] + dy, i[:, 2] + dz)
                    val = val + w * self.g[idx]; grad = grad + w[:, None] * self.grad[idx]
        n = grad / np.maximum(np.linalg.norm(grad, axis=1, keepdims=True), 1e-9)
        return val, n

def run(slot_w, slot_h, n_cab=4, d=6.0, slack=1.06, EI=2e9, rho=1.5e-3, T=1.2, fps=60, sub=24, label=""):
    t_all = time.time()
    V, F = scene(slot_w, slot_h); sdf = SDF(V, F); r = d / 2; r_full = r
    X = []; cab = []; fixed = []
    for c in range(n_cab):
        cols = min(n_cab, 4); y0 = (c % cols - (cols - 1) / 2) * 1.6 * d; ze = 12 + (c // cols) * 1.6 * d
        P = np.array([[-60, y0, ze], [-25, y0 * 0.5, 2 + r + 0.5], [0, 0, 2 + slot_h / 2], [25, y0 * 0.5, 2 + r + 0.5], [60, y0, ze]], float)
        t = np.linspace(0, 1, 600); seg = np.linspace(0, 1, len(P))
        path = np.stack([np.interp(t, seg, P[:, i]) for i in range(3)], 1)
        cum = np.r_[0, np.cumsum(np.linalg.norm(np.diff(path, axis=0), axis=1))]
        L = cum[-1] * slack; n = int(round(L / r)); s = L / n
        # la catena e' piu' lunga del percorso: parte con un'onda verticale che recupera il lasco
        ss = np.linspace(0, cum[-1], n + 1); pts = np.stack([np.interp(ss, cum, path[:, i]) for i in range(3)], 1)
        X.append(pts); cab.append(np.full(n + 1, c)); fixed += [sum(len(x) for x in X) - n - 1, sum(len(x) for x in X) - 1]
    X = np.concatenate(X); cab = np.concatenate(cab); N = len(X)
    idx = np.arange(N); same_next = np.r_[cab[1:] == cab[:-1], False]
    E = np.stack([idx[same_next], idx[same_next] + 1], 1)                 # coppie di lunghezza
    B = np.stack([idx[:-2], idx[:-2] + 1, idx[:-2] + 2], 1); B = B[(cab[B[:, 0]] == cab[B[:, 2]])]
    rest = np.full(len(E), 0.0)
    # passo di riposo per cavo
    for c in range(n_cab):
        m = cab[E[:, 0]] == c
        Lc = np.linalg.norm(np.diff(X[cab == c], axis=0), axis=1).sum() * slack
        rest[m] = Lc / m.sum()
    s_mean = rest.mean()
    mass = rho * math.pi * r * r * s_mean; w = np.full(N, 1 / mass); w[fixed] = 0
    Vel = np.zeros_like(X); dt = 1 / fps / sub; g = np.array([0, 0, -9810.])
    alpha_b = s_mean ** 3 / EI / dt ** 2
    seg_id = cab * 100000 + idx
    fc_part = np.zeros(N); fc_cab = np.zeros(N)
    t_sim = time.time(); steps = int(T * fps) * sub
    for k in range(steps):
        Xp = X.copy(); Vel *= 0.995; Vel[w > 0] += g * dt; X = X + Vel * dt
        last = k >= steps - sub
        r = r_full * min(1.0, 0.3 + 0.7 * k / (0.6 * steps)) if INFLATE else r_full
        # lunghezza (Jacobi, rigido)
        for _ in range(2):
            a, b = E[:, 0], E[:, 1]; dv = X[b] - X[a]; ln = np.linalg.norm(dv, axis=1); C = ln - rest
            ws = w[a] + w[b]; nrm = dv / ln[:, None]; lam = -C / np.maximum(ws, 1e-12)
            dx = np.zeros_like(X); np.add.at(dx, a, -(w[a] * lam)[:, None] * nrm); np.add.at(dx, b, (w[b] * lam)[:, None] * nrm)
            X = X + 0.5 * dx
        # flessione: C = x0 - 2x1 + x2 (vettore), cedevolezza s^3/EI
        a, b, c = B.T; C = X[a] - 2 * X[b] + X[c]; ws = w[a] + 4 * w[b] + w[c]
        lam = (-C / (ws + alpha_b)[:, None])
        dx = np.zeros_like(X); np.add.at(dx, a, w[a, None] * lam); np.add.at(dx, b, -2 * w[b, None] * lam); np.add.at(dx, c, w[c, None] * lam)
        X = X + 0.5 * dx
        # contatto col pezzo
        val, n = sdf(X); pen = r - val; hit = (pen > 0) & (w > 0)
        X[hit] += n[hit] * pen[hit, None]
        if last: fc_part = np.maximum(fc_part, np.where(hit, pen * mass / dt ** 2, 0))
        # contatto fra cavi (e lo stesso cavo se lontano lungo la catena)
        pr = cKDTree(X).query_pairs(2 * r, output_type='ndarray')
        if len(pr):
            i, j = pr[:, 0], pr[:, 1]; ok = (cab[i] != cab[j]) | (np.abs(i - j) > 3); i, j = i[ok], j[ok]
            dv = X[j] - X[i]; ln = np.linalg.norm(dv, axis=1); pen2 = 2 * r - ln; ws = w[i] + w[j]
            m = (ln > 1e-9) & (ws > 0); i, j, dv, ln, pen2, ws = i[m], j[m], dv[m], ln[m], pen2[m], ws[m]
            nrm = dv / ln[:, None]; lam = pen2 / ws
            dx = np.zeros_like(X); cnt = np.zeros(N)
            np.add.at(dx, i, -(w[i] * lam)[:, None] * nrm); np.add.at(dx, j, (w[j] * lam)[:, None] * nrm)
            np.add.at(cnt, i, 1); np.add.at(cnt, j, 1)
            X = X + dx / np.maximum(cnt, 1)[:, None]
            if last:
                f = lam / dt ** 2; np.maximum.at(fc_cab, i, f); np.maximum.at(fc_cab, j, f)
        Vel = (X - Xp) / dt
    t_sim = time.time() - t_sim; r = r_full
    # misure finali
    val, _ = sdf(X); over_part = np.maximum(r - val, 0)
    pr = cKDTree(X).query_pairs(2 * r, output_type='ndarray'); over_cab = np.zeros(N)
    if len(pr):
        i, j = pr[:, 0], pr[:, 1]; ok = (cab[i] != cab[j]) | (np.abs(i - j) > 3)
        o = 2 * r - np.linalg.norm(X[j[ok]] - X[i[ok]], axis=1); np.maximum.at(over_cab, i[ok], o); np.maximum.at(over_cab, j[ok], o)
    stretch = (np.linalg.norm(X[E[:, 1]] - X[E[:, 0]], axis=1) / rest - 1).max()
    kap = np.linalg.norm(X[B[:, 0]] - 2 * X[B[:, 1]] + X[B[:, 2]], axis=1) / s_mean ** 2
    Rmin = 1 / kap.max() if kap.max() > 0 else float('inf')
    wseg = mass * 9810
    print(f"{label:>10} feritoia {slot_w}x{slot_h}: N={N} | vox {sdf.t_vox:.2f}s edt {sdf.t_edt:.2f}s sim {t_sim:.1f}s ({steps} sub) | "
          f"dentro al pezzo max {over_part.max():.2f}mm  cavo-cavo max {over_cab.max():.2f}mm ({over_cab.max()/d*100:.0f}% Ø) | "
          f"stiramento {stretch*100:.1f}% | R min {Rmin:.1f}mm | F contatto max {max(fc_part.max(), fc_cab.max())/wseg:.0f}x peso del tratto")
    return X, cab

for INFLATE in (False, True):
    print('gonfia' if INFLATE else 'diametro pieno da subito')
    for n_cab, sw, sh, d in [(4, 14, 14, 6.0), (4, 10, 10, 6.0), (16, 26, 26, 6.0), (16, 22, 22, 6.0), (32, 50, 30, 4.0)]:
        run(sw, sh, n_cab=n_cab, d=d, label=f"{n_cab}xØ{d:g}")
