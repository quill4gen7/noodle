# Cavi v3: forma d'EQUILIBRIO per minimizzazione d'energia (L-BFGS), quasi-statica.
# Energia = flessione (EI/s)(1 - cos θ) + lunghezza (molla rigida) + piattina (molle laterali) + gravità
#         + contatto col pezzo (penalità su SDF) + contatto fra fili (penalità sfera-sfera).
# Riusa la scena di cavi2 (attacchi, fili, legami, geodetica) solo per costruire lo stato.
import numpy as np, time
from scipy.optimize import minimize
from scipy.spatial import cKDTree


class Energy:
    def __init__(self, sc, k_len=300.0, k_con=300.0, g=9810.0):
        self.sc = sc
        r = sc.rad
        self.free = sc.w > 0
        # passo e EI per particella (dai gruppi)
        EI = np.zeros(len(sc.X)); rho = np.zeros(len(sc.X))
        for gi, G in enumerate(sc.groups):
            EI[sc.grp == gi] = G['EI']; rho[sc.grp == gi] = G['rho']
        s = sc.Erest
        a, b, c = sc.B.T
        self.kb = EI[b] / s[0] if False else EI[b] / np.array([sc.wires[sc.wid[i]]['sp'] for i in b])
        sp = np.array([sc.wires[sc.wid[i]]['sp'] for i in range(len(sc.X))])
        self.sp = sp
        base = EI / sp ** 3                                     # rigidezza di flessione "per spostamento"
        self.ks = k_len * base[sc.E[:, 0]]                      # lunghezza
        self.kl = k_len * base[sc.Lk[:, 0]] if len(sc.Lk) else np.zeros(0)
        self.kc = k_con * base                                  # contatto
        self.mg = rho * np.pi * r * r * sp * g
        self.pairs = np.zeros((0, 2), int)

    def update_pairs(self, X, margin=1.0):
        rmax = self.sc.rad.max()
        pr = cKDTree(X).query_pairs(2 * rmax + margin, output_type='ndarray')
        if len(pr):
            i, j = pr[:, 0], pr[:, 1]
            ok = (self.sc.wid[i] != self.sc.wid[j]) | (np.abs(i - j) > 2)
            pr = pr[ok]
        self.pairs = pr

    def __call__(self, xf, t):
        sc = self.sc
        X = sc.X.copy()
        X[self.free] = xf.reshape(-1, 3)
        gX = np.zeros_like(X)
        E = 0.0
        # lunghezza
        a, b = sc.E[:, 0], sc.E[:, 1]
        d = X[b] - X[a]; ln = np.linalg.norm(d, axis=1); ex = ln - sc.Erest
        E += 0.5 * (self.ks * ex * ex).sum()
        f = (self.ks * ex / np.maximum(ln, 1e-12))[:, None] * d
        np.add.at(gX, b, f); np.add.at(gX, a, -f)
        # piattina
        if len(sc.Lk):
            a, b = sc.Lk[:, 0], sc.Lk[:, 1]
            d = X[b] - X[a]; ln = np.linalg.norm(d, axis=1); ex = ln - sc.Lrest
            E += 0.5 * (self.kl * ex * ex).sum()
            f = (self.kl * ex / np.maximum(ln, 1e-12))[:, None] * d
            np.add.at(gX, b, f); np.add.at(gX, a, -f)
        # flessione: (EI/s)(1 - t1·t2)
        a, b, c = sc.B.T
        e1 = X[b] - X[a]; e2 = X[c] - X[b]
        l1 = np.linalg.norm(e1, axis=1); l2 = np.linalg.norm(e2, axis=1)
        t1 = e1 / l1[:, None]; t2 = e2 / l2[:, None]
        cs = np.einsum('ij,ij->i', t1, t2)
        E += (self.kb * (1 - cs)).sum()
        g1 = -(self.kb / l1)[:, None] * (t2 - cs[:, None] * t1)        # dE/de1
        g2 = -(self.kb / l2)[:, None] * (t1 - cs[:, None] * t2)        # dE/de2
        np.add.at(gX, a, -g1); np.add.at(gX, b, g1 - g2); np.add.at(gX, c, g2)
        # gravita'
        E += (self.mg * X[:, 2]).sum()
        gX[:, 2] += self.mg
        # contatto coi corpi
        self.pen = {}
        for name, (grid, pose) in sc.bodies.items():
            R, p = pose(t)
            val, n = grid.query((X - p) @ R)
            pen = np.maximum(sc.rad - val, 0)
            self.pen[name] = pen
            E += 0.5 * (self.kc * pen * pen).sum()
            gX -= (self.kc * pen)[:, None] * (n @ R.T)
        if sc.floor_z is not None:
            pen = np.maximum(sc.floor_z + sc.rad - X[:, 2], 0)
            E += 0.5 * (self.kc * pen * pen).sum()
            gX[:, 2] -= self.kc * pen
        # la mano che infila: una scatola (x, y) da cui i cavi non escono; molla morbida (1/10 del contatto)
        if getattr(sc, 'inside', None) is not None:
            lo, hi = sc.inside
            o = np.maximum(lo - X, 0) - np.maximum(X - hi, 0)
            o[:, 2] = 0
            k = 0.1 * self.kc
            E += 0.5 * (k[:, None] * o * o).sum()
            gX -= k[:, None] * o
        # fra fili
        self.ppo = np.zeros(len(X))
        if len(self.pairs):
            i, j = self.pairs[:, 0], self.pairs[:, 1]
            d = X[j] - X[i]; ln = np.linalg.norm(d, axis=1)
            pen = np.maximum(sc.rad[i] + sc.rad[j] - ln, 0)
            k = np.minimum(self.kc[i], self.kc[j])
            E += 0.5 * (k * pen * pen).sum()
            f = (k * pen / np.maximum(ln, 1e-12))[:, None] * d
            np.add.at(gX, i, f); np.add.at(gX, j, -f)
            np.maximum.at(self.ppo, i, pen); np.maximum.at(self.ppo, j, pen)
        return E, gX[self.free].ravel()


def relax(sc, en, t, maxiter=400, rounds=3):
    sc.t = t
    sc._pin(t)
    info = None
    for _ in range(rounds):
        en.update_pairs(sc.X)
        x0 = sc.X[en.free].ravel()
        res = minimize(en, x0, args=(t,), jac=True, method='L-BFGS-B',
                       options=dict(maxiter=maxiter, maxcor=20, gtol=1e-6, ftol=1e-12))
        sc.X[en.free] = res.x.reshape(-1, 3)
        info = res
    en(sc.X[en.free].ravel(), t)          # aggiorna pen / ppo allo stato finale
    return info
