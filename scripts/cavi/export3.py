import sys, os, pickle, json, numpy as np, trimesh, manifold3d as m3
sys.path.insert(0, '/tmp/cavi')
from cavi2 import rot_x
D = pickle.load(open(os.environ.get('RUN', '/tmp/cavi/run3.pkl'), 'rb'))
OUT = os.environ.get('DEST', '/tmp/cavi/out'); os.makedirs(OUT, exist_ok=True)
HINGE = (0.0, -19.4, 12.0)
M = '/tmp/cavi/mesh'
def load(n):
    m = trimesh.load(f'{M}/{n}.stl'); return np.asarray(m.vertices), np.asarray(m.faces)
bot = ['vasca', 'gabbia', 'g_batt', 'g_servo', 'g_alette', 'g_torretta', 'g_albero']
lid = ['lid', 'g_pcb', 'g_scr', 'g_usb', 'g_jst', 'g_imu']
def save(name, V, F):
    trimesh.Trimesh(V, F, process=False).export(f'{OUT}/{name}.stl')
def tubes(X, idxs, r):
    parts = []
    for idx in idxs:
        P = X[idx]
        for a, b in zip(P[:-1], P[1:]):
            parts.append(m3.Manifold.hull(m3.Manifold.sphere(r, 10).translate(a.tolist()) + m3.Manifold.sphere(r, 10).translate(b.tolist())))
    me = m3.Manifold.batch_boolean(parts, m3.OpType.Add).to_mesh()
    return np.asarray(me.vert_properties)[:, :3], np.asarray(me.tri_verts)
MODE = os.environ.get('MODE', 'libro')
poses = [('aperto', 180.0, 0.0), ('90', 90.0, 130.0), ('chiuso', 0.0, 260.0)] if MODE == 'libro' else \
        [('aperto', 30.0, 0.0), ('meta', 15.0, 110.0), ('chiuso', 0.0, 220.0)]
for tag, ang, dx in poses:
    off = np.array([dx, 0, 0])
    R, p = rot_x(ang, HINGE) if MODE == 'libro' else (np.eye(3), np.array([0, 0, ang]))
    for n in bot:
        V, F = load(n); save(f'{tag}_sotto_{n}', V + off, F)
    for n in lid:
        V, F = load(n); save(f'{tag}_lid_{n}', V @ R.T + p + off, F)
    X = D['snap'][tag] + off
    if tag not in D['snap']: continue
    for gi, g in enumerate(D['groups']):
        idxs = [w['idx'] for w in D['wires'] if w['group'] == gi]
        V, F = tubes(X, idxs, D['rad'][idxs[0][0]] * 0.98)
        save(f'{tag}_cavo_{g}', V, F)
print("ok", sorted(os.listdir(OUT))[:4], len(os.listdir(OUT)))
