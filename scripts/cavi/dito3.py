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


# ---- 1. lunghezza minima per angolo d'apertura (solo geodetica) ----
if os.environ.get('ANGOLI'):
    rows = []
    for a in ((0, 10, 20, 30, 40) if MODE == 'alza' else (0, 30, 60, 90, 120, 150, 180)):
        ANG_OPEN = float(a)
        Lm = sc.plan(0.0)
        rows.append((a, {k: (round(v, 1) if v else None) for k, v in Lm.items()}))
        print(f"aperto {a:3d}{'mm' if MODE == 'alza' else '°'}: " + "  ".join(f"{k} {v}" for k, v in rows[-1][1].items()), flush=True)
    sys.exit(0)

# ---- 2. chiusura quasi-statica ----
Lo = sc.plan(0.0)
print("percorso minimo aperto:", {k: round(v, 1) for k, v in Lo.items()}, flush=True)
sc.build(0.0)
en = Energy(sc)
near = np.zeros(len(sc.X), bool)
for i, _, _ in sc.fixed:
    near[max(0, i - 3):i + 4] = True
meas = ~near
STEP = float(os.environ.get('STEP', 1.5))
angs = list(np.arange(ANG_OPEN, -1e-9, -STEP))
if angs[-1] != 0: angs.append(0.0)
def t_of(a):                           # tempo finto per avere la posa all'angolo a
    return T_OPEN + T_CLOSE * (1 - (a / ANG_OPEN)) if a < ANG_OPEN else 0.0
# theta(t) usa lo smoothstep: invertiamolo a mano facendo passare l'angolo diretto
def pose_at(a):
    if MODE == 'alza':
        return np.eye(3), np.array([0, 0, a])
    return rot_x(a, HINGE)
sc.bodies['coperchio'] = (g_lid, lambda t: pose_at(t))     # qui "t" E' l'angolo
t0 = time.time()
info = relax(sc, en, ANG_OPEN, maxiter=600, rounds=2)
print(f"aperto, assestato: {time.time()-t0:.1f}s", flush=True)
hist = []
snap = {'aperto': sc.X.copy()}
print("gobba iniziale:", sc.init_info, flush=True)
prev = ANG_OPEN
for a in angs[1:]:
    # partenza calda: le particelle vicine al coperchio si muovono con lui (peso dalla distanza dai due corpi)
    R0, p0 = pose_at(prev); R1, p1 = pose_at(a)
    vb, _ = g_bot.query(sc.X)
    vl, _ = g_lid.query((sc.X - p0) @ R0)
    wl = 1 / (1 + np.exp(-(vb - vl) / 0.5))
    moved = ((sc.X - p0) @ R0) @ R1.T + p1
    fr = en.free
    sc.X[fr] = (1 - wl[fr, None]) * sc.X[fr] + wl[fr, None] * moved[fr]
    prev = a
    info = relax(sc, en, a, maxiter=int(os.environ.get('ITER', 150)), rounds=1)
    rec = dict(ang=a)
    pb, pl = en.pen['sotto'] * meas, en.pen['coperchio'] * meas
    for gi, G in enumerate(sc.groups):
        m = sc.grp == gi
        rec[G['name']] = dict(pezzo=float(np.maximum(pb, pl)[m].max() / G['r']),
                              pinza=float(np.minimum(pb, pl)[m].max() / G['r']),
                              fili=float((en.ppo * meas)[m].max() / G['r']))
    hist.append(rec)
    if abs(a - ANG_OPEN / 2) < STEP / 2 and 'meta' not in snap: snap['meta'] = sc.X.copy()
snap['chiuso'] = sc.X.copy()
el = time.time() - t0
st = sc.stretch(); Rb = sc.bend_radius()
print(f"chiusura in {len(angs)} passi, {el:.0f}s")
print(f"L servo {L_SERVO:.0f}  batteria {L_BATT:.0f}  gy521 {L_IMU:.0f} mm  (EI {EI:g})")
pb, pl = en.pen['sotto'] * meas, en.pen['coperchio'] * meas
for gi, G in enumerate(sc.groups):
    m = sc.grp == gi
    mb = m[sc.B[:, 1]] & meas[sc.B[:, 1]]
    j = np.argmin(np.where(mb, Rb, np.inf))
    first = next((h for h in hist if max(h[G['name']]['pinza'], h[G['name']]['pezzo'], h[G['name']]['fili']) > 0.25), None)
    worst = max(hist, key=lambda h: h[G['name']]['pezzo'])
    print(f"  {G['name']:9s} chiuso: nel pezzo {np.maximum(pb, pl)[m].max()/G['r']*100:4.0f}%r (fra le metà {np.minimum(pb, pl)[m].max()/G['r']*100:3.0f}%) | "
          f"fra fili {(en.ppo*meas)[m].max()/G['r']*100:4.0f}%r | stir {st[m[sc.E[:,0]]].max()*100:4.2f}% | "
          f"R min {Rb[j]:.1f} mm a {np.round(sc.X[sc.B[j,1]],1)} | si schiaccia (>25% r): {'a ' + format(first['ang'], '.0f') + '°' if first else 'mai'}"
          f" | peggio a {worst['ang']:.0f}°: {worst[G['name']]['pezzo']*100:.0f}%r")
pickle.dump(dict(snap=snap, wires=sc.wires, rad=sc.rad, groups=[G['name'] for G in sc.groups], grp=sc.grp,
                 hist=hist, L=(L_SERVO, L_BATT, L_IMU), meas=meas, Lo=Lo), open(os.environ.get('OUT', '/tmp/cavi/run3.pkl'), 'wb'))
