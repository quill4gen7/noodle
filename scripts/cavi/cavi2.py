# Cavi v2 (prototipo): fili singoli o accoppiati in piattina, estremi attaccati a corpi
# che si muovono (il coperchio che si chiude), contatto col pezzo per SDF, fra fili per sfere.
# Unita': mm, g, s.
import math, time, numpy as np
from scipy import ndimage as ndi
from scipy.spatial import cKDTree


def voxel_parity(V, F, lo, h, shape):
    """solido per parita' lungo z: vale anche per le cavita' chiuse."""
    nx, ny, nz = shape
    xc = lo[0] + (np.arange(nx) + 0.5) * h + 1.234e-5
    yc = lo[1] + (np.arange(ny) + 0.5) * h + 2.345e-5
    acc = np.zeros((nx, ny, nz + 1), np.int32)
    for a, b, c in V[F]:
        x0, x1 = min(a[0], b[0], c[0]), max(a[0], b[0], c[0])
        y0, y1 = min(a[1], b[1], c[1]), max(a[1], b[1], c[1])
        i0, i1 = np.searchsorted(xc, x0), np.searchsorted(xc, x1)
        j0, j1 = np.searchsorted(yc, y0), np.searchsorted(yc, y1)
        if i0 >= i1 or j0 >= j1:
            continue
        d = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(d) < 1e-12:
            continue
        X, Y = np.meshgrid(xc[i0:i1], yc[j0:j1], indexing='ij')
        l1 = ((b[1] - c[1]) * (X - c[0]) + (c[0] - b[0]) * (Y - c[1])) / d
        l2 = ((c[1] - a[1]) * (X - c[0]) + (a[0] - c[0]) * (Y - c[1])) / d
        l3 = 1 - l1 - l2
        ins = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if not ins.any():
            continue
        z = l1 * a[2] + l2 * b[2] + l3 * c[2]
        k = np.clip(np.ceil((z - lo[2]) / h - 0.5).astype(int), 0, nz)
        ii, jj = np.nonzero(ins)
        np.add.at(acc, (ii + i0, jj + j0, k[ins]), 1)
    return (np.cumsum(acc, axis=2)[:, :, :nz] % 2) == 1


class Grid:
    """SDF su griglia di un gruppo di mesh (nel loro sistema locale)."""
    def __init__(self, meshes, h=0.25, pad=2.5):
        allV = np.concatenate([V for V, F in meshes])
        self.h = h
        self.lo = allV.min(0) - pad
        hi = allV.max(0) + pad
        self.shape = tuple(int(s) for s in np.ceil((hi - self.lo) / h))
        t = time.time()
        solid = np.zeros(self.shape, bool)
        for V, F in meshes:
            solid |= voxel_parity(V, F, self.lo, h, self.shape)
        self.t_vox = time.time() - t
        t = time.time()
        dout = ndi.distance_transform_edt(~solid) * h
        din = ndi.distance_transform_edt(solid) * h
        self.g = np.where(solid, -(din - 0.5 * h), dout - 0.5 * h).astype(np.float32)
        self.grad = np.stack(np.gradient(self.g, h), -1).astype(np.float32)
        self.t_edt = time.time() - t
        self.solid_frac = solid.mean()

    def query(self, P):
        u = (P - self.lo) / self.h - 0.5
        out = ((u < 0) | (u > np.array(self.shape) - 1.001)).any(1)
        u = np.clip(u, 0, np.array(self.shape) - 1.001)
        i = np.floor(u).astype(int)
        f = u - i
        val = 0.0
        grad = 0.0
        for dx in (0, 1):
            wx = f[:, 0] if dx else 1 - f[:, 0]
            for dy in (0, 1):
                wy = f[:, 1] if dy else 1 - f[:, 1]
                for dz in (0, 1):
                    w = wx * wy * (f[:, 2] if dz else 1 - f[:, 2])
                    idx = (i[:, 0] + dx, i[:, 1] + dy, i[:, 2] + dz)
                    val = val + w * self.g[idx]
                    grad = grad + w[:, None] * self.grad[idx]
        val = np.where(out, 1e3, val)          # fuori dalla griglia = libero
        n = grad / np.maximum(np.linalg.norm(grad, axis=1, keepdims=True), 1e-9)
        return val, n


def rot_x(deg, pivot):
    a = math.radians(deg)
    R = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    pivot = np.asarray(pivot, float)
    return R, pivot - R @ pivot


def chaikin(P, it=3):
    for _ in range(it):
        Q = [P[0]]
        for a, b in zip(P[:-1], P[1:]):
            Q += [0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b]
        Q.append(P[-1])
        P = np.array(Q)
    return P


def plen(P):
    return np.linalg.norm(np.diff(P, axis=0), axis=1).sum()


def resample(P, n):
    cum = np.r_[0, np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))]
    s = np.linspace(0, cum[-1], n)
    return np.stack([np.interp(s, cum, P[:, k]) for k in range(3)], 1), s


def lift_path(s, ts, e, te, L, up=np.array([0, 0, 1.0]), lead=3.0):
    """percorso iniziale di lunghezza L: esce lungo le tangenti, sale di h, scende. h per bisezione."""
    def make(h):
        a, b = s + lead * ts, e + lead * te
        P = np.array([s, a, a + up * h, b + up * h, b, e])
        return chaikin(P)
    if plen(make(0)) > L:
        return None
    lo, hi = 0.0, L
    for _ in range(50):
        mid = (lo + hi) / 2
        if plen(make(mid)) > L:
            hi = mid
        else:
            lo = mid
    return make(lo)


def transport(P, w0):
    """frame che minimizza la rotazione lungo il percorso: la larghezza della piattina."""
    T = np.gradient(P, axis=0)
    T /= np.linalg.norm(T, axis=1, keepdims=True)
    W = np.zeros_like(P)
    w = w0 - T[0] * (w0 @ T[0])
    w /= np.linalg.norm(w)
    W[0] = w
    for i in range(1, len(P)):
        w = w - T[i] * (w @ T[i])
        nw = np.linalg.norm(w)
        w = w / nw if nw > 1e-9 else W[i - 1]
        W[i] = w
    return W, T


class Scene:
    def __init__(self, bodies, floor_z=None, g=9810.0):
        self.bodies = bodies          # name -> (Grid, pose(t)->(R,p))
        self.floor_z = floor_z
        self.g = g
        self.groups = []
        self.cap = 0.3

    def add(self, name, n, r, L, start, end, couple=True, strip=4.0, EI=2e6, rho=3e-3, t0=0.0):
        """n fili di raggio r lunghi L; start/end = dict(body, c, t, w, pitch) nel sistema locale del corpo."""
        self.groups.append(dict(name=name, n=n, r=r, L=L, start=start, end=end, couple=couple, strip=strip,
                                EI=EI, rho=rho))

    def plan(self, t, h=0.4, lead=1.5, pad=6.0):
        """percorso piu' corto nello spazio libero per ogni gruppo, nella posa al tempo t."""
        ends = []
        for G in self.groups:
            for w in ('start', 'end'):
                c, tt, _, _, _ = self._world(G[w], t)
                ends.append(c)
        pts = [np.array(ends)]
        for name, (grid, pose) in self.bodies.items():
            R, p = pose(t)
            corners = np.array([[grid.lo[0] + i * grid.shape[0] * grid.h, grid.lo[1] + j * grid.shape[1] * grid.h,
                                 grid.lo[2] + k * grid.shape[2] * grid.h] for i in (0, 1) for j in (0, 1) for k in (0, 1)])
            pts.append(corners @ R.T + p)
        allp = np.concatenate(pts)
        lo, hi = allp.min(0) - pad, allp.max(0) + pad
        if self.floor_z is not None:
            lo[2] = max(lo[2], self.floor_z - 1)
        t0 = time.time()
        sdf, xs = world_sdf(self, t, lo, hi, h)
        self.t_world = time.time() - t0
        self.paths, self.Lmin = {}, {}
        for G in self.groups:
            sc, st, _, _, _ = self._world(G['start'], t)
            ec, et, _, _, _ = self._world(G['end'], t)
            free = sdf > G['r'] + 0.05
            a, b = sc + st * lead, ec + et * lead
            P = geodesic(free, xs, a, b, h)
            if P is None:
                self.paths[G['name']] = None; self.Lmin[G['name']] = None
                continue
            def ok(Q, G=G):
                v = np.full(len(Q), 1e3)
                for name, (grid, pose) in self.bodies.items():
                    R, p = pose(t)
                    v = np.minimum(v, grid.query((Q - p) @ R)[0])
                return v > G['r'] + 0.02
            P = shorten(P, ok)
            P = np.vstack([sc, P, ec])
            self.paths[G['name']] = P
            self.Lmin[G['name']] = plen(P)
        return self.Lmin

    def _pen_at(self, P, r, t):
        tot = 0.0
        for name, (grid, pose) in self.bodies.items():
            R, p = pose(t)
            v, _ = grid.query((P - p) @ R)
            tot += np.maximum(r - v, 0).sum()
        if self.floor_z is not None:
            tot += np.maximum(self.floor_z + r - P[:, 2], 0).sum()
        if getattr(self, 'inside', None) is not None:          # la mano che infila: fuori dalla sagoma costa
            lo, hi = self.inside
            tot += 10 * (np.maximum(lo - P, 0) + np.maximum(P - hi, 0)).sum()
        return tot / len(P)

    def _world(self, end, t):
        R, p = self.bodies[end['body']][1](t)
        return R @ np.asarray(end['c'], float) + p, R @ np.asarray(end['t'], float), R @ np.asarray(end['w'], float), R, p

    def build(self, t0=0.0):
        X, rad, wid, grp, inv, fixed = [], [], [], [], [], []
        E, B, Balpha, links = [], [], [], []
        self.wires = []
        off = 0
        for gi, G in enumerate(self.groups):
            sc, st, sw, Rs, ps = self._world(G['start'], t0)
            ec, et, ew, Re, pe = self._world(G['end'], t0)
            r = G['r']
            npt = int(round(G['L'] / r)) + 1
            if getattr(self, 'paths', None) and G['name'] in self.paths:
                path = self.paths[G['name']]
                Lg = plen(path)
                if Lg > G['L']:
                    raise ValueError(f"{G['name']}: L={G['L']:.0f} < percorso minimo {Lg:.1f} in questa posa")
                # il lasco va in una gobba PERPENDICOLARE alla piattina (dove si piega facilmente): da dritta e
                # compressa il minimizzatore resta sul punto di sella
                P0, _ = resample(path, 4 * npt)
                W0, T0 = transport(P0, sw)
                N1 = np.cross(T0, W0)
                u = np.linspace(0, 1, len(P0))[:, None]
                best = None
                for lobes in (1, 2, 3, 4, 6):
                    for phi in np.radians(np.arange(0, 360, 30)):
                        # direzione della gobba: attorno al filo, partendo dalla normale della piattina
                        Nv = np.cos(phi) * N1 + np.sin(phi) * W0
                        bump = np.sin(np.pi * lobes * u)
                        lo_, hi_ = 0.0, G['L']
                        for _ in range(50):
                            A = (lo_ + hi_) / 2
                            if plen(P0 + A * bump * Nv) > G['L']:
                                hi_ = A
                            else:
                                lo_ = A
                        Pc = P0 + lo_ * bump * Nv
                        pen = self._pen_at(Pc, r, t0)
                        # la piattina piega di piatto: le direzioni nel suo piano costano (peso sul seno)
                        cost = pen + (0.05 * abs(math.sin(phi)) * r if G['n'] > 1 else 0.0)
                        if best is None or cost < best[0]:
                            best = (cost, Pc, lobes, math.degrees(phi), lo_)
                self.init_info = getattr(self, 'init_info', {})
                self.init_info[G['name']] = dict(lobi=best[2], phi=best[3], ampiezza=round(best[4], 2), pen=round(best[0], 3))
                P, s = resample(best[1], npt)
            else:
                path = lift_path(sc, st, ec, et, G['L'])
                if path is None:
                    raise ValueError(f"{G['name']}: L={G['L']:.0f} troppo corto per la posa iniziale")
                P, s = resample(path, npt)
            W, T = transport(P, sw)
            ewp = ew - T[-1] * (ew @ T[-1])
            ewp /= max(np.linalg.norm(ewp), 1e-9)
            u = np.clip((s - 0.6 * G['L']) / (0.4 * G['L']), 0, 1)[:, None]
            W = W * (1 - u) + ewp * u
            W /= np.linalg.norm(W, axis=1, keepdims=True)
            sp = G['L'] / (npt - 1)
            mass = G['rho'] * math.pi * r * r * sp
            pitch_mid = 2 * r
            first = []
            for k in range(G['n']):
                kk = k - (G['n'] - 1) / 2
                a_s = np.clip(1 - s / max(G['strip'], 1e-6), 0, 1)
                a_e = np.clip(1 - (G['L'] - s) / max(G['strip'], 1e-6), 0, 1)
                pitch = pitch_mid + a_s * (G['start']['pitch'] - pitch_mid) + a_e * (G['end']['pitch'] - pitch_mid)
                Pk = P + W * (kk * pitch)[:, None]
                idx = np.arange(off, off + npt)
                X.append(Pk); rad.append(np.full(npt, r)); wid.append(np.full(npt, len(self.wires)))
                grp.append(np.full(npt, gi)); inv.append(np.full(npt, 1 / mass))
                # tratti sfilati agli estremi: ogni filo va dritto al suo pin, quindi e' lungo quanto serve per allargarsi
                fs = math.hypot(G['strip'], abs(kk) * abs(G['start']['pitch'] - pitch_mid)) / max(G['strip'], 1e-6)
                fe = math.hypot(G['strip'], abs(kk) * abs(G['end']['pitch'] - pitch_mid)) / max(G['strip'], 1e-6)
                smid = 0.5 * (s[:-1] + s[1:])
                rest_k = np.where(smid < G['strip'], sp * fs, np.where(smid > G['L'] - G['strip'], sp * fe, sp))
                E += [(i, i + 1, rest_k[q]) for q, i in enumerate(idx[:-1])]
                al = sp ** 3 / G['EI']
                B += [(i, i + 1, i + 2) for i in idx[:-2]]
                Balpha += [al] * (npt - 2)
                for j, (body, R, p) in ((0, (G['start']['body'], Rs, ps)), (1, (G['start']['body'], Rs, ps)),
                                        (npt - 2, (G['end']['body'], Re, pe)), (npt - 1, (G['end']['body'], Re, pe))):
                    fixed.append((off + j, body, R.T @ (Pk[j] - p)))
                first.append(off)
                self.wires.append(dict(group=gi, name=f"{G['name']}[{k}]", idx=idx, r=r, sp=sp))
                off += npt
            if G['couple'] and G['n'] > 1:
                ins = np.nonzero((s >= G['strip']) & (s <= G['L'] - G['strip']))[0]
                for k in range(G['n'] - 1):
                    a0, b0 = first[k], first[k + 1]
                    for i in ins:
                        links.append((a0 + i, b0 + i, 2 * r))
                        if i + 1 in ins:
                            dg = math.hypot(2 * r, sp)
                            links.append((a0 + i, b0 + i + 1, dg))
                            links.append((a0 + i + 1, b0 + i, dg))
        self.X = np.concatenate(X); self.rad = np.concatenate(rad); self.wid = np.concatenate(wid)
        self.grp = np.concatenate(grp); self.w = np.concatenate(inv)
        E = np.array(E); self.E = E[:, :2].astype(int); self.Erest = E[:, 2]
        self.B = np.array(B, int); self.Balpha = np.array(Balpha)
        L_ = np.array(links) if links else np.zeros((0, 3))
        self.Lk = L_[:, :2].astype(int); self.Lrest = L_[:, 2]
        self.fixed = fixed
        self.fidx = np.array([f[0] for f in fixed])
        self.w[self.fidx] = 0
        # colori per Gauss-Seidel della lunghezza: archi pari / dispari dentro ogni filo
        par = np.array([(a - self.wires[self.wid[a]]['idx'][0]) % 2 for a in self.E[:, 0]])
        self.Ecol = [np.nonzero(par == 0)[0], np.nonzero(par == 1)[0]]
        self.V = np.zeros_like(self.X)
        self.t = t0
        return self

    def _pin(self, t):
        for i, body, loc in self.fixed:
            R, p = self.bodies[body][1](t)
            self.X[i] = R @ loc + p

    def step(self, dt, rscale=1.0, iters=3, record=None, friction=0.3):
        X, w = self.X, self.w
        Xp = X.copy()
        self.V *= 0.998
        self.V[w > 0, 2] -= self.g * dt
        X += self.V * dt
        self.t += dt
        self._pin(self.t)
        rad = self.rad * rscale
        for _ in range(iters):
            for col in self.Ecol:                          # lunghezza, Gauss-Seidel per colori
                e = self.E[col]; a, b = e[:, 0], e[:, 1]
                dv = X[b] - X[a]; ln = np.linalg.norm(dv, axis=1)
                C = ln - self.Erest[col]; ws = w[a] + w[b]
                m = ws > 0
                lam = np.zeros_like(C); lam[m] = -C[m] / ws[m]
                nr = dv / np.maximum(ln, 1e-9)[:, None]
                X[a] -= (w[a] * lam)[:, None] * nr
                X[b] += (w[b] * lam)[:, None] * nr
            if len(self.Lk):                                # piattina: fili affiancati
                a, b = self.Lk[:, 0], self.Lk[:, 1]
                dv = X[b] - X[a]; ln = np.linalg.norm(dv, axis=1)
                rest = self.Lrest * np.where(self.Lrest > 2 * self.rad[a] + 1e-6, 1.0, rscale)
                C = ln - rest; ws = w[a] + w[b]
                lam = np.where(ws > 0, -C / np.maximum(ws, 1e-12), 0)
                nr = dv / np.maximum(ln, 1e-9)[:, None]
                dx = np.zeros_like(X); cnt = np.zeros(len(X))
                np.add.at(dx, a, -(w[a] * lam)[:, None] * nr); np.add.at(dx, b, (w[b] * lam)[:, None] * nr)
                np.add.at(cnt, a, 1); np.add.at(cnt, b, 1)
                X += 0.8 * dx / np.maximum(cnt, 1)[:, None]
            # flessione: solo la parte di x0 - 2x1 + x2 perpendicolare al filo (quella lungo il filo lo stirava)
            a, b, c = self.B.T
            C = X[a] - 2 * X[b] + X[c]
            tt = X[c] - X[a]
            tt /= np.maximum(np.linalg.norm(tt, axis=1, keepdims=True), 1e-9)
            C -= np.einsum('ij,ij->i', C, tt)[:, None] * tt
            ws = w[a] + 4 * w[b] + w[c]
            lam = -C / (ws + self.Balpha / dt ** 2)[:, None]
            dx = np.zeros_like(X)
            np.add.at(dx, a, w[a, None] * lam); np.add.at(dx, b, -2 * w[b, None] * lam); np.add.at(dx, c, w[c, None] * lam)
            X += 0.3 * dx
        # contatto coi corpi
        pens = {}
        contact = np.zeros(len(X), bool)
        for name, (grid, pose) in self.bodies.items():
            R, p = pose(self.t)
            loc = (X - p) @ R
            val, n = grid.query(loc)
            pen = rad - val
            pens[name] = np.maximum(pen, 0)
            hit = (pen > 0) & (w > 0)
            X[hit] += (n[hit] @ R.T) * np.minimum(pen[hit], self.cap * rad[hit])[:, None]
            contact |= hit
        if self.floor_z is not None:
            pen = self.floor_z + rad - X[:, 2]
            hit = (pen > 0) & (w > 0)
            X[hit, 2] += pen[hit]
            contact |= hit
        # fra fili
        rmax = rad.max()
        pr = cKDTree(X).query_pairs(2 * rmax, output_type='ndarray')
        ppo = np.zeros(len(X))
        if len(pr):
            i, j = pr[:, 0], pr[:, 1]
            ok = (self.wid[i] != self.wid[j]) | (np.abs(i - j) > 2)
            i, j = i[ok], j[ok]
            dv = X[j] - X[i]; ln = np.linalg.norm(dv, axis=1)
            pen2 = rad[i] + rad[j] - ln; ws = w[i] + w[j]
            m = (pen2 > 0) & (ln > 1e-9) & (ws > 0)
            i, j, dv, ln, pen2, ws = i[m], j[m], dv[m], ln[m], pen2[m], ws[m]
            nr = dv / ln[:, None]; lam = pen2 / ws
            dx = np.zeros_like(X); cnt = np.zeros(len(X))
            np.add.at(dx, i, -(w[i] * lam)[:, None] * nr); np.add.at(dx, j, (w[j] * lam)[:, None] * nr)
            np.add.at(cnt, i, 1); np.add.at(cnt, j, 1)
            X += dx / np.maximum(cnt, 1)[:, None]
            np.maximum.at(ppo, i, pen2); np.maximum.at(ppo, j, pen2)
            contact[i] = True; contact[j] = True
        self._pin(self.t)
        self.V = (X - Xp) / dt
        self.V[contact] *= (1 - friction)
        self.last = dict(pens=pens, ppo=ppo)

    # ---- misure ----
    def residual(self):
        """sovrapposizioni a fine passo, ricalcolate senza correggere."""
        out = {}
        X = self.X
        tot = np.zeros(len(X))
        for name, (grid, pose) in self.bodies.items():
            R, p = pose(self.t)
            val, _ = grid.query((X - p) @ R)
            out[name] = np.maximum(self.rad - val, 0)
        pr = cKDTree(X).query_pairs(2 * self.rad.max(), output_type='ndarray')
        ppo = np.zeros(len(X))
        if len(pr):
            i, j = pr[:, 0], pr[:, 1]
            ok = (self.wid[i] != self.wid[j]) | (np.abs(i - j) > 2)
            i, j = i[ok], j[ok]
            o = self.rad[i] + self.rad[j] - np.linalg.norm(X[j] - X[i], axis=1)
            np.maximum.at(ppo, i, o); np.maximum.at(ppo, j, o)
        out['fili'] = np.maximum(ppo, 0)
        return out

    def bend_radius(self):
        X = self.X
        a, b, c = self.B.T
        v1 = X[b] - X[a]; v2 = X[c] - X[b]
        cosang = np.einsum('ij,ij->i', v1, v2) / np.maximum(np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1), 1e-12)
        ang = np.arccos(np.clip(cosang, -1, 1))
        sp = 0.5 * (np.linalg.norm(v1, axis=1) + np.linalg.norm(v2, axis=1))
        R = np.where(ang > 1e-6, sp / np.maximum(ang, 1e-9), np.inf)
        return R            # per tripla (centro = b)

    def stretch(self):
        X = self.X
        ln = np.linalg.norm(X[self.E[:, 1]] - X[self.E[:, 0]], axis=1)
        return ln / self.Erest - 1


# ---- percorso piu' corto nello spazio libero (onda BFS sulla griglia, 26-vicini, poi accorciato) ----
def world_sdf(scene, t, lo, hi, h):
    """SDF del mondo (min sui corpi + pavimento) su una griglia regolare."""
    xs = [np.arange(lo[k] + h / 2, hi[k], h) for k in range(3)]
    G = np.stack(np.meshgrid(*xs, indexing='ij'), -1).reshape(-1, 3)
    v = np.full(len(G), 1e3)
    for name, (grid, pose) in scene.bodies.items():
        R, p = pose(t)
        v = np.minimum(v, grid.query((G - p) @ R)[0])
    if scene.floor_z is not None:
        v = np.minimum(v, G[:, 2] - scene.floor_z)
    return v.reshape([len(x) for x in xs]), xs


def geodesic(free, xs, a, b, h):
    """BFS 26-connessa da b; poi discesa da a. Restituisce la polilinea o None."""
    from scipy.ndimage import binary_dilation
    def cell(p):
        return tuple(int(np.clip(round((p[k] - xs[k][0]) / h), 0, len(xs[k]) - 1)) for k in range(3))
    ca, cb = cell(a), cell(b)
    free = free.copy()
    free[ca] = free[cb] = True
    dist = np.full(free.shape, -1, np.int32)
    dist[cb] = 0
    front = np.zeros(free.shape, bool); front[cb] = True
    seen = front.copy()
    st = np.ones((3, 3, 3), bool)
    k = 0
    while not seen[ca]:
        k += 1
        nf = binary_dilation(front, st) & free & ~seen
        if not nf.any():
            return None
        dist[nf] = k
        seen |= nf
        front = nf
    # discesa: dal vicino con distanza minore, preferendo il passo piu' corto
    path = [ca]
    c = ca
    offs = [(i, j, l) for i in (-1, 0, 1) for j in (-1, 0, 1) for l in (-1, 0, 1) if (i, j, l) != (0, 0, 0)]
    while dist[c] > 0:
        best = None
        for o in offs:
            n = (c[0] + o[0], c[1] + o[1], c[2] + o[2])
            if min(n) < 0 or n[0] >= free.shape[0] or n[1] >= free.shape[1] or n[2] >= free.shape[2]:
                continue
            if dist[n] == dist[c] - 1:
                key = (abs(o[0]) + abs(o[1]) + abs(o[2]))
                if best is None or key < best[0]:
                    best = (key, n)
        c = best[1]
        path.append(c)
    P = np.array([[xs[k][c[k]] for k in range(3)] for c in path])
    P[0], P[-1] = a, b
    return P


def shorten(P, ok, iters=200):
    """accorcia la polilinea (media dei vicini) finche' resta nello spazio libero."""
    P = P.copy()
    for _ in range(iters):
        Q = P.copy()
        Q[1:-1] = 0.5 * P[1:-1] + 0.25 * (P[:-2] + P[2:])
        good = ok(Q)
        good[0] = good[-1] = True
        P[good] = Q[good]
    return P
