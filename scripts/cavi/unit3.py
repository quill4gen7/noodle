import sys, numpy as np, time
sys.path.insert(0, '/tmp/cavi')
from cavi2 import Scene
from cavi3 import Energy, relax
I = lambda t: (np.eye(3), np.zeros(3))
class G0:
    def query(self, P): return np.full(len(P), 1e3), np.zeros_like(P)
def make(n, r, L, EI, ps, pe, couple=True, we=(0,0,1)):
    sc = Scene({'a': (G0(), I)}, floor_z=None)
    sc.add('x', n, r, L, start=dict(body='a', c=[0, 0, 0], t=[1, 0, 0], w=[0, 0, 1], pitch=ps),
           end=dict(body='a', c=[30, 0, 0], t=[-1, 0, 0], w=list(we), pitch=pe), couple=couple, EI=EI, strip=4)
    sc.paths = {'x': np.array([[0, 0, 0], [1.5, 0, 0], [28.5, 0, 0], [30, 0, 0.]])} if STRAIGHT else None
    sc.build(0.0)
    return sc
STRAIGHT = False
# controllo del gradiente
sc = make(2, 0.45, 40, 2.5e7, 0.9, 2.54)
en = Energy(sc); en.update_pairs(sc.X)
x = sc.X[en.free].ravel() + np.random.RandomState(0).normal(0, 0.05, en.free.sum() * 3)
E0, g = en(x, 0)
h = 1e-6; idx = np.random.RandomState(1).choice(len(x), 8, replace=False)
num = [(en(x + h * np.eye(len(x))[k], 0)[0] - en(x - h * np.eye(len(x))[k], 0)[0]) / (2 * h) for k in idx]
print("gradiente: rel err max", np.max(np.abs(np.array(num) - g[idx]) / (np.abs(g[idx]) + 1e-3)))
for STRAIGHT in (False, True):
  print('partenza dritta' if STRAIGHT else 'partenza ad arco')
  for EI in (2.5e7,):
    for label, args in (("1 filo", (1, 0.45, 40, EI, 0.9, 0.9)), ("4 piattina", (4, 0.45, 40, EI, 0.9, 0.9)),
                        ("4 piattina pin 2.54", (4, 0.45, 40, EI, 0.9, 2.54)),
                        ("4 piatt. ruotata 90°", (4, 0.45, 40, EI, 0.9, 0.9, True, (0, 1, 0)))):
        sc = make(*args); en = Energy(sc)
        t0 = time.time(); info = relax(sc, en, 0.0)
        st = sc.stretch()
        z = sc.X[:, 2]
        print(f"EI {EI:.0e} {label:22s} stir max {st.max()*100:6.3f}% min {st.min()*100:6.3f}% | z min {z.min():6.1f} max {z.max():5.1f} | R min {sc.bend_radius().min():5.1f} | {time.time()-t0:.2f}s it {info.nit}")
