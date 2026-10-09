# creepyFinger v4: cavi di servo, batteria, GY-521 verso la scheda; il coperchio si chiude a libro sul lato −y.
import sys, json, math, time, pickle, os, numpy as np, trimesh
sys.path.insert(0, '/tmp/cavi')
from cavi2 import Grid, Scene, rot_x
from cavi3 import Energy, relax

M = '/tmp/cavi/mesh'
Q = json.load(open(f'{M}/quote.json'))
def mesh(n):
    m = trimesh.load(f'{M}/{n}.stl')
    return np.asarray(m.vertices, float), np.asarray(m.faces)

H = float(os.environ.get('H', 0.25))
cache = f'/tmp/cavi/grids_{H}.pkl'
if os.path.exists(cache):
    g_bot, g_lid = pickle.load(open(cache, 'rb'))
else:
    t = time.time()
    g_bot = Grid([mesh(n) for n in ('vasca', 'gabbia', 'g_batt', 'g_servo', 'g_alette', 'g_torretta', 'g_pernino', 'g_albero')], h=H)
    g_lid = Grid([mesh(n) for n in ('lid', 'g_pcb', 'g_scr', 'g_usb', 'g_jst', 'g_imu')], h=H)
    pickle.dump((g_bot, g_lid), open(cache, 'wb'))
    print(f"griglie h={H}: sotto {g_bot.shape} lid {g_lid.shape} in {time.time()-t:.1f}s")

# ---- tempi: aperto e fermo, poi si chiude, poi si assesta ----
T_OPEN, T_CLOSE, T_END = 0.6, 1.6, 0.6
ANG_OPEN = float(os.environ.get('ANG', 180))
HINGE = (0.0, -19.4, 12.0)                     # spigolo −y del piano di taglio
def theta(t):
    if t <= T_OPEN:
        return ANG_OPEN
    u = min(1.0, (t - T_OPEN) / T_CLOSE)
    u = u * u * (3 - 2 * u)
    return ANG_OPEN * (1 - u)
MODE = os.environ.get('MODE', 'libro')
def pose_lid(t):
    if MODE == 'alza':
        return np.eye(3), np.array([0, 0, theta(t)])
    return rot_x(theta(t), HINGE)              # attorno a x: il coperchio si apre verso −y senza passare nel corpo
def pose_bot(t):
    return np.eye(3), np.zeros(3)
# verifica il verso
R, p = rot_x(90.0, HINGE)
_q = R @ np.array([0, 0, 31.0]) + p; assert _q[1] < -30 and _q[2] > 25, f"a 90° il coperchio deve stare in piedi sul lato −y: {_q}"

sc = Scene({'sotto': (g_bot, pose_bot), 'coperchio': (g_lid, pose_lid)}, floor_z=-7.6)
if os.environ.get('INFILA', '1') == '1':
    sc.inside = (np.array([-70.0, -17.0, -1e3]), np.array([14.0, 17.0, 1e3]))   # dentro la sagoma del guscio (pareti 1,8)

tg = math.tan(math.radians(Q['pend']))
def z_pcb_sotto(x):
    return Q['z_pcb_top'] - 1.1 - (x - Q['xm']) * tg
down = np.array([-math.sin(math.radians(Q['pend'])), 0, -math.cos(math.radians(Q['pend']))])
yp = 9.5                                       # piazzole sotto la scheda, dentro le guide (ai bordi c'è la guida)
def pin_row(y, xs):
    xc = float(np.mean(xs))
    return dict(body='coperchio', c=[xc, y, z_pcb_sotto(xc) - 0.05], t=down.tolist(), w=[1, 0, 0], pitch=2.54)

L_SERVO = float(os.environ.get('LS', 60)); L_BATT = float(os.environ.get('LB', 70)); L_IMU = float(os.environ.get('LI', 40))
EI = float(os.environ.get('EI', 1e7))
front = [-24.0 - 2.54 * k for k in range(4)]   # fila sotto la scheda, davanti alla gabbia
# servo: 4 fili (3 + il blu) in piattina Ø0,9, esce dal lato corto davanti (x = sx1) larga lungo z
sc.add('servo', 4, 0.45, L_SERVO,
       start=dict(body='sotto', c=[Q['sx1'] + 0.05, Q['fili_y'], Q['zJ']], t=[1, 0, 0], w=[0, 0, 1], pitch=0.9),
       end=pin_row(-yp, front), strip=5.0, EI=EI)
# batteria: rosso/nero accoppiati Ø1,2, sale nella camera davanti alla batteria, va al JST sotto la scheda (dietro)
xch = (Q['xc0'] + Q['x_md']) / 2
sc.add('batteria', 2, 0.6, L_BATT,
       start=dict(body='sotto', c=[xch, 0.0, 11.0], t=[0, 0, 1], w=[0, 1, 0], pitch=1.2),
       end=dict(body='coperchio', c=[-59.3, 0.0, 22.2], t=[1, 0, 0], w=[0, 1, 0], pitch=2.0), strip=4.0, EI=EI)
# GY-521: 4 fili in piattina Ø1,0 dai pin sul lato −y del modulo alla fila +y della scheda (davanti)
icx, icy = Q['icx'], Q['icy']
imu_pins = [icx - 8.89 + 2.54 * k for k in range(4)]
sc.add('gy521', 4, 0.5, L_IMU,
       start=dict(body='coperchio', c=[float(np.mean(imu_pins)), icy - Q['imu_w'] / 2 + 1.27, 21.1], t=down.tolist(), w=[1, 0, 0], pitch=2.54),
       end=pin_row(+yp, front), strip=4.0, EI=EI)

# gli estremi devono stare nel vuoto: controlla la SDF un po' fuori da ogni attacco
for G in sc.groups:
    for which in ('start', 'end'):
        e = G[which]
        grid = g_bot if e['body'] == 'sotto' else g_lid
        P = np.array(e['c']) + np.array(e['t']) * 1.5
        v, _ = grid.query(P[None])
        print(f"  attacco {G['name']}.{which}: SDF a 1,5 mm fuori = {v[0]:.2f} (r {G['r']})")



# ---- cavo gia' steso lungo il suo percorso, guscio chiuso: quanta lunghezza in piu' ci sta? ----
import copy
Lc = dict(sc.plan(0.0))
paths = dict(sc.paths)
print("percorso minimo chiuso:", {k: round(v, 1) for k, v in Lc.items()}, flush=True)
rows = []
which = os.environ.get('CAVO', 'servo batteria gy521').split()
for name in which:
    gi = [G['name'] for G in sc.groups].index(name)
    for extra in [int(e) for e in os.environ.get("EXTRA", "2 5 10 15 20 30").split()]:
        sc2 = Scene(sc.bodies, floor_z=sc.floor_z)
        sc2.inside = getattr(sc, 'inside', None)
        G = copy.deepcopy(sc.groups[gi]); G['L'] = Lc[name] + extra
        sc2.groups = [G]
        sc2.paths = {name: paths[name]}
        try:
            sc2.build(0.0)
        except ValueError as e:
            print(name, extra, e); continue
        en = Energy(sc2)
        near = np.zeros(len(sc2.X), bool)
        for i, _, _ in sc2.fixed:
            near[max(0, i - 3):i + 4] = True
        relax(sc2, en, 0.0, maxiter=int(os.environ.get("MAXIT", 800)), rounds=int(os.environ.get("ROUNDS", 3)))
        meas = ~near
        pz = max(en.pen['sotto'][meas].max(), en.pen['coperchio'][meas].max()) / G['r']
        ff = (en.ppo * meas).max() / G['r']
        st = sc2.stretch()[meas[sc2.E[:, 0]] & meas[sc2.E[:, 1]]].max()
        Rb = sc2.bend_radius(); Rb = Rb[meas[sc2.B[:, 1]]].min()
        ok = pz < 0.25 and ff < 0.25 and st < 0.03
        print(f"{name:9s} L {G['L']:5.1f} (+{extra:2d}) | nel pezzo {pz*100:4.0f}%r | fra fili {ff*100:4.0f}%r | stir {st*100:5.2f}% | "
              f"R min {Rb:4.1f} mm | {'ci sta' if ok else 'SCHIACCIATO'}", flush=True)
        rows.append((name, extra, G['L'], pz, ff, st, Rb, ok))
        pickle.dump(dict(X=sc2.X, wires=sc2.wires, rad=sc2.rad), open(f'/tmp/cavi/chiuso_{name}_{extra}.pkl', 'wb'))
