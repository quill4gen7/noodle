import time, json, numpy as np, trimesh, os
from build123d import *
t = time.time()
G = {}
exec("from build123d import *\nimport math\n" + open('/tmp/cavi/dito.py').read(), G)
print("dito in", round(time.time() - t, 1), "s")
os.makedirs('/tmp/cavi/mesh', exist_ok=True)
def save(name, shp):
    vs, fs = shp.tessellate(0.05, 0.3)
    V = np.array([[v.X, v.Y, v.Z] for v in vs]); F = np.array(fs)
    trimesh.Trimesh(V, F, process=True).export(f'/tmp/cavi/mesh/{name}.stl')
    print(name, len(F), "tri", np.round(V.min(0), 1), np.round(V.max(0), 1))
save('vasca', G['corpo']); save('lid', G['coperchio_p']); save('gabbia', G['gabbia_p'])
names = ['pcb', 'scr', 'usb', 'jst', 't1', 't2', 't3', 't4', 't5', 't6', 'batt', 'servo', 'alette', 'torretta', 'pernino', 'albero', 'fili1', 'fili2', 'imu']
for n, s in zip(names, G['ghost']):
    if n.startswith('t') and n[1:].isdigit(): continue
    save('g_' + n, s)
keys = "x_rear x_b0 x_b1 xm z_pcb_top pend sx0 sx1 s_bot s_top zJ z_servo z_sb fili_y icx icy z_soff imu_l imu_w bx0 bx1 zfi batt_l batt_w batt_h xc0 x_md z_taglio gioco scheda_w x_lid_f".split()
json.dump({k: G[k] for k in keys if k in G}, open('/tmp/cavi/mesh/quote.json', 'w'), indent=1)
print({k: round(G[k], 2) for k in keys if k in G})
