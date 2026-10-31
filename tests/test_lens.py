"""Retro-engineering inspection: robust mesh sections (scans) and the lens /
views-sheet montage helpers. The browser parts are covered by the live smoke
test in the deploy notes; here everything runs without Chromium."""

import math

import numpy as np
import pytest

ss = pytest.importorskip("cad_nodes.slice_summary")
from cad_nodes import lens  # noqa: E402


def _box_tris(x0, y0, z0, x1, y1, z1):
    """Closed axis-aligned box as an (12, 3, 3) triangle soup."""
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
         (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return v[np.array(f)]


def _cylinder_tris(r, h, n=64):
    """Open-ended cylinder wall (no caps) as a triangle soup."""
    t = []
    for k in range(n):
        a0, a1 = 2 * math.pi * k / n, 2 * math.pi * (k + 1) / n
        p0 = (r * math.cos(a0), r * math.sin(a0))
        p1 = (r * math.cos(a1), r * math.sin(a1))
        t.append([[*p0, 0], [*p1, 0], [*p1, h]])
        t.append([[*p0, 0], [*p1, h], [*p0, h]])
    return np.array(t, float)


def test_vertex_on_plane_keeps_the_loop():
    # z=0 is exactly the bottom face: every crossing used to be dropped there
    # when a vertex sat on the plane; the nudge keeps a closed loop
    tris = _box_tris(0, 0, -5, 20, 10, 5)
    tris[:, :, 2][tris[:, :, 2] == -5] = 0.0     # bottom at z=0
    loops, opens = ss._mesh_section(tris, "z", 0.0)
    assert len(loops) == 1 and not opens
    assert abs(abs(ss._poly_area(loops[0])) - 200.0) < 1.0


def test_scan_gap_is_stitched_and_reported():
    tris = _cylinder_tris(30.0, 20.0, n=90)
    tris = tris[2:]                               # a hole in the scan: 1 facet (2.1 mm) gone
    loops, opens = ss._mesh_section(tris, "z", 10.0)
    assert len(loops) == 1 and not opens
    assert ss._mesh_section.bridged               # the bridged gap is said
    desc = ss._mesh_loop_desc(loops[0])
    assert desc.startswith("circle r=")
    assert abs(float(desc.split("r=")[1].split()[0]) - 30.0) < 0.1


def test_noise_loops_are_dropped():
    big = _box_tris(0, 0, 0, 40, 40, 10)
    speck = _box_tris(50, 50, 4, 50.3, 50.3, 6)  # 0.09 mm2: scan noise
    loops, _ = ss._mesh_section(np.concatenate([big, speck]), "z", 5.0)
    assert len(loops) == 1


def test_lens_data_mesh(tmp_path):
    p = tmp_path / "box.stl"
    tris = _box_tris(-10, -5, 0, 10, 5, 8).astype("<f4")
    rec = np.zeros(len(tris), dtype=[("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")])
    rec["v"] = tris.reshape(-1, 9)
    p.write_bytes(b"\0" * 80 + len(tris).to_bytes(4, "little") + rec.tobytes())
    d = ss.lens_data(str(p), axis="y", n=3)
    assert d["success"] and d["source"] == "mesh" and len(d["sections"]) == 3
    assert d["coords"] == "(x,z)"
    s0 = d["sections"][0]
    assert s0["loops"] and s0["loops"][0]["desc"].startswith("rect 20x8")


def test_section_svg_and_overlay_are_svg():
    sec = {"pos": 1.0, "loops": [
        {"pts": [[0, 0], [10, 0], [10, 5], [0, 5]], "desc": "rect 10x5", "hole": False},
        {"pts": [[5 + 2 * math.cos(a), 2.5 + 2 * math.sin(a)]
                 for a in np.linspace(0, 2 * math.pi, 24, endpoint=False)],
         "desc": "circle r=2 @(5,2.5)", "hole": True}], "open": [[[0, 6], [3, 7]]]}
    svg = lens.section_svg(sec, "z", {"min": [0, 0, 0], "max": [10, 5, 3]}, "#e5484d")
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "Ø4" in svg and "stroke-dasharray" in svg
    data = {"axis": "z", "bbox": {"min": [0, 0, 0], "max": [10, 5, 3]}, "sections": [sec]}
    pts = lens._probe_points(data)
    assert len(pts) == 4 + 4 + 24 + 2
    px = [[float(i), float(i), 0.0] for i in range(len(pts))]
    layers = lens._layers(data, {0}, px, lens.PALETTE)
    assert len(layers[0]["loops_px"]) == 2 and len(layers[0]["open_px"]) == 1
    assert "<polygon" in lens._overlay_svg(800, 600, layers)


def test_triad_handles_axis_pointing_at_camera():
    svg = lens._triad_svg({"x": [10, 0], "y": [0, 0], "z": [0, -10]}, 400, 300)
    assert "Y⊙" in svg
