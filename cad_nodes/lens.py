"""Inspection pictures for retro-engineering (PLAN_RETROENG fase 2).

Two montages, both rendered by the same warm Chromium as the screenshots (so the
container needs no image library) and both returned as ONE png + JSON meta:

- **lens** (`lente`): the part in perspective with the cutting plane(s) drawn
  across it and the REAL section contour projected onto the 3D view, next to the
  2D section(s) with a mm grid and the symbolic description. A 2D section alone
  does not say where it is; a 3D view alone does not say what the profile is.
  Together they do — that is the whole point.
- **views sheet**: N camera angles of the same object on one sheet, each with
  its azimuth/elevation, camera position -> target, fov, and an XYZ triad drawn
  from the real projection, so "which way is up" is never a guess.

Async (they drive the browser); the section geometry comes from the execution
worker (`executor.lens_data_*` -> `slice_summary.lens_data`).
"""

from __future__ import annotations

import asyncio
import base64
import html as _html
import math
from typing import Optional

PALETTE = ["#e5484d", "#0090ff", "#30a46c", "#f76b15", "#8e4ec6", "#d6a100",
           "#12a594", "#d6409f", "#5b5bd6", "#978365"]

DEFAULT_ANGLES = [(-45.0, 35.264), (45.0, 35.264), (135.0, 35.264),
                  (-135.0, 35.264), (-90.0, 89.9), (-90.0, -89.9),
                  (-90.0, 0.0), (0.0, 0.0), (90.0, 0.0)]

_AXIS_I = {"x": 0, "y": 1, "z": 2}
_IN_PLANE = {"z": (0, 1), "x": (1, 2), "y": (0, 2)}
_NAMES = "xyz"

_CSS = """
*{box-sizing:border-box} body{margin:0;background:#f6f7f9;color:#1c2024;
font:13px/1.35 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.head{padding:10px 14px;background:#111418;color:#e8eaed;font-size:13px}
.head b{color:#fff} .wrap{padding:12px;display:flex;flex-wrap:wrap;gap:12px}
.card{background:#fff;border:1px solid #d9dde3;border-radius:6px;overflow:hidden}
.card .cap{padding:6px 9px;font-size:12px;border-top:1px solid #e6e8eb;
white-space:pre-wrap;word-break:break-word}
.persp{position:relative;background:#0c0f14}
.persp img{display:block} .persp svg{position:absolute;left:0;top:0}
.tag{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:6px;vertical-align:-1px}
"""


def _esc(t) -> str:
    return _html.escape(str(t), quote=True)


def _b64(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _nice_step(span: float, target: int = 8) -> float:
    raw = max(span, 1e-9) / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _to3d(axis: str, pos: float, uv) -> list:
    p = [0.0, 0.0, 0.0]
    p[_AXIS_I[axis]] = pos
    i, j = _IN_PLANE[axis]
    p[i], p[j] = uv[0], uv[1]
    return p


def _plane_quad(axis: str, pos: float, bbox: dict, pad: float = 0.05) -> list:
    i, j = _IN_PLANE[axis]
    lo, hi = bbox["min"], bbox["max"]
    du, dv = (hi[i] - lo[i]) * pad, (hi[j] - lo[j]) * pad
    u0, u1, v0, v1 = lo[i] - du, hi[i] + du, lo[j] - dv, hi[j] + dv
    return [_to3d(axis, pos, uv) for uv in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))]


def _poly_attr(pts) -> str:
    return " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in pts)


# --- the 2D section plot ------------------------------------------------------
def _circle_r(desc: str) -> Optional[float]:
    if desc.startswith("circle r="):
        try:
            return float(desc.split("r=")[1].split()[0])
        except (IndexError, ValueError):
            return None
    return None


def section_svg(sec: dict, axis: str, bbox: dict, color: str,
                w: int = 520, h: int = 420) -> str:
    """One section as an SVG plot: mm grid with labels, loops (holes cut out),
    open chains dashed, circle diameters labelled. Scale = the part's in-plane
    bbox, the same for every section of a sweep, so they compare by eye."""
    i, j = _IN_PLANE[axis]
    lo, hi = bbox["min"], bbox["max"]
    u0, u1, v0, v1 = lo[i], hi[i], lo[j], hi[j]
    pad = 0.04 * max(u1 - u0, v1 - v0, 1e-6)
    u0, u1, v0, v1 = u0 - pad, u1 + pad, v0 - pad, v1 + pad
    ml, mb, mt, mr = 46, 30, 10, 10
    pw, ph = w - ml - mr, h - mt - mb
    s = min(pw / max(u1 - u0, 1e-9), ph / max(v1 - v0, 1e-9))
    ox = ml + (pw - s * (u1 - u0)) / 2
    oy = mt + (ph - s * (v1 - v0)) / 2

    def X(u):
        return ox + (u - u0) * s

    def Y(v):
        return oy + (v1 - v) * s

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}" style="background:#fff">']
    step = _nice_step(max(u1 - u0, v1 - v0))
    minor = step / 5
    g = math.ceil(u0 / minor) * minor
    while g <= u1 + 1e-9:
        out.append(f'<line x1="{X(g):.1f}" y1="{Y(v0):.1f}" x2="{X(g):.1f}" '
                   f'y2="{Y(v1):.1f}" stroke="#f1f3f5" stroke-width="1"/>')
        g += minor
    g = math.ceil(v0 / minor) * minor
    while g <= v1 + 1e-9:
        out.append(f'<line x1="{X(u0):.1f}" y1="{Y(g):.1f}" x2="{X(u1):.1f}" '
                   f'y2="{Y(g):.1f}" stroke="#f1f3f5" stroke-width="1"/>')
        g += minor
    g = math.ceil(u0 / step) * step
    while g <= u1 + 1e-9:
        out.append(f'<line x1="{X(g):.1f}" y1="{Y(v0):.1f}" x2="{X(g):.1f}" '
                   f'y2="{Y(v1):.1f}" stroke="{"#9aa3ad" if abs(g) < 1e-9 else "#e3e6ea"}" '
                   f'stroke-width="1"/>')
        out.append(f'<text x="{X(g):.1f}" y="{h - 12}" font-size="10" fill="#6b7480" '
                   f'text-anchor="middle">{_fmt(g)}</text>')
        g += step
    g = math.ceil(v0 / step) * step
    while g <= v1 + 1e-9:
        out.append(f'<line x1="{X(u0):.1f}" y1="{Y(g):.1f}" x2="{X(u1):.1f}" '
                   f'y2="{Y(g):.1f}" stroke="{"#9aa3ad" if abs(g) < 1e-9 else "#e3e6ea"}" '
                   f'stroke-width="1"/>')
        out.append(f'<text x="{ml - 5}" y="{Y(g) + 3:.1f}" font-size="10" fill="#6b7480" '
                   f'text-anchor="end">{_fmt(g)}</text>')
        g += step
    out.append(f'<text x="{w - mr}" y="{h - 1}" font-size="10" fill="#1c2024" '
               f'text-anchor="end">{_NAMES[i]} → mm (griglia {_fmt(step)} / {_fmt(minor)})</text>')
    out.append(f'<text x="4" y="{mt + 9}" font-size="10" fill="#1c2024">{_NAMES[j]} ↑</text>')

    loops = sec.get("loops") or []
    for lp in loops:                      # outers first, holes knocked out after
        if lp.get("hole"):
            continue
        pts = [(X(p[0]), Y(p[1])) for p in lp["pts"]]
        out.append(f'<polygon points="{_poly_attr(pts)}" fill="{color}" '
                   f'fill-opacity="0.16" stroke="{color}" stroke-width="1.6"/>')
    for lp in loops:
        if not lp.get("hole"):
            continue
        pts = [(X(p[0]), Y(p[1])) for p in lp["pts"]]
        out.append(f'<polygon points="{_poly_attr(pts)}" fill="#fff" '
                   f'stroke="{color}" stroke-width="1.3"/>')
    for ch in sec.get("open") or []:
        pts = [(X(p[0]), Y(p[1])) for p in ch]
        out.append(f'<polyline points="{_poly_attr(pts)}" fill="none" '
                   f'stroke="#f76b15" stroke-width="1.4" stroke-dasharray="4 3"/>')
    labels = 0
    for lp in loops:
        r = _circle_r(lp.get("desc", ""))
        if r is None or labels >= 40:
            continue
        cu = sum(p[0] for p in lp["pts"]) / len(lp["pts"])
        cv = sum(p[1] for p in lp["pts"]) / len(lp["pts"])
        out.append(f'<text x="{X(cu) + r * s + 3:.1f}" y="{Y(cv) + 3:.1f}" font-size="9" '
                   f'fill="#1c2024">Ø{_fmt(2 * r)}</text>')
        labels += 1
    if not loops and not sec.get("open"):
        out.append(f'<text x="{w / 2}" y="{h / 2}" font-size="14" fill="#8b95a1" '
                   f'text-anchor="middle">(sezione vuota)</text>')
    out.append("</svg>")
    return "".join(out)


# --- overlays on the perspective -----------------------------------------------
def _overlay_svg(w: float, h: float, layers: list) -> str:
    """layers: [{quad_px, loops_px: [(pts, hole)], open_px, color, label}]"""
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.1f} {h:.1f}">']
    for L in layers:
        c = L["color"]
        q = L["quad_px"]
        out.append(f'<polygon points="{_poly_attr(q)}" fill="{c}" fill-opacity="0.10" '
                   f'stroke="{c}" stroke-opacity="0.75" stroke-width="1.2" '
                   f'stroke-dasharray="6 4"/>')
    for L in layers:
        c = L["color"]
        for pts, hole in L["loops_px"]:
            out.append(f'<polygon points="{_poly_attr(pts)}" fill="none" stroke="#000" '
                       f'stroke-opacity="0.55" stroke-width="4.2"/>')
            out.append(f'<polygon points="{_poly_attr(pts)}" fill="none" stroke="{c}" '
                       f'stroke-width="{1.6 if hole else 2.4}"/>')
        for pts in L["open_px"]:
            out.append(f'<polyline points="{_poly_attr(pts)}" fill="none" stroke="{c}" '
                       f'stroke-width="2" stroke-dasharray="4 3"/>')
        q = L["quad_px"]
        lx, ly = min(q, key=lambda p: p[0] + p[1])[:2]
        out.append(f'<text x="{lx + 4:.1f}" y="{ly - 5:.1f}" font-size="13" '
                   f'font-weight="700" fill="{c}" stroke="#000" stroke-width="3" '
                   f'paint-order="stroke">{_esc(L["label"])}</text>')
    out.append("</svg>")
    return "".join(out)


def _triad_svg(axes: dict, w: float, h: float) -> str:
    """XYZ triad from the real projected axis directions, bottom-left."""
    if not axes:
        return ""
    ox, oy, R = 34.0, h - 34.0, 24.0
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.1f} {h:.1f}" style="position:absolute;left:0;top:0">',
           f'<circle cx="{ox}" cy="{oy}" r="{R + 8}" fill="#000" fill-opacity="0.35"/>']
    for name, col in (("x", "#ff5a5f"), ("y", "#3ddc84"), ("z", "#4aa3ff")):
        d = axes.get(name) or [0, 0]
        n = math.hypot(d[0], d[1])
        if n < 1e-6:
            out.append(f'<circle cx="{ox}" cy="{oy}" r="4" fill="{col}"/>'
                       f'<text x="{ox + 6}" y="{oy - 6}" font-size="11" fill="{col}">{name.upper()}⊙</text>')
            continue
        k = R * min(1.0, n / max(n, 1e-6))
        ex, ey = ox + d[0] / n * k, oy + d[1] / n * k
        out.append(f'<line x1="{ox}" y1="{oy}" x2="{ex:.1f}" y2="{ey:.1f}" '
                   f'stroke="{col}" stroke-width="2.6"/>'
                   f'<text x="{ox + d[0] / n * (k + 9):.1f}" y="{oy + d[1] / n * (k + 9) + 4:.1f}" '
                   f'font-size="11" font-weight="700" fill="{col}" text-anchor="middle">'
                   f'{name.upper()}</text>')
    out.append("</svg>")
    return "".join(out)


def _cam_caption(meta: dict) -> str:
    cam = meta.get("camera") or {}
    pos = cam.get("position") or []
    tgt = cam.get("target") or []
    proj = cam.get("projection", "?")
    fov = f" fov {_fmt(cam['fov'])}°" if cam.get("fov") else ""
    return (f"az {_fmt(meta.get('azim', 0))}° el {_fmt(meta.get('elev', 0))}° · {proj}{fov}\n"
            f"cam ({', '.join(_fmt(v) for v in pos)}) → ({', '.join(_fmt(v) for v in tgt)})"
            f" d={_fmt(cam.get('distance', 0))}")


def _layers(data: dict, positions_idx, px: list, colors) -> list:
    """Cut the flat probe_px list back into per-section overlay layers."""
    layers, k = [], 0
    for n_sec, sec in enumerate(data["sections"]):
        if n_sec not in positions_idx:
            continue
        quad = [px[k + m][:2] for m in range(4)]
        k += 4
        loops_px = []
        for lp in sec.get("loops") or []:
            n = len(lp["pts"])
            loops_px.append(([p[:2] for p in px[k:k + n]], lp.get("hole", False)))
            k += n
        open_px = []
        for ch in sec.get("open") or []:
            open_px.append([p[:2] for p in px[k:k + len(ch)]])
            k += len(ch)
        layers.append({"quad_px": quad, "loops_px": loops_px, "open_px": open_px,
                       "color": colors[n_sec % len(colors)],
                       "label": f"#{n_sec + 1} {data['axis']}={_fmt(sec['pos'])}"})
    return layers


def _probe_points(data: dict, which=None) -> list:
    pts = []
    for n_sec, sec in enumerate(data["sections"]):
        if which is not None and n_sec not in which:
            continue
        ax, pos = data["axis"], sec["pos"]
        pts += _plane_quad(ax, pos, data["bbox"])
        for lp in sec.get("loops") or []:
            pts += [_to3d(ax, pos, p) for p in lp["pts"]]
        for ch in sec.get("open") or []:
            pts += [_to3d(ax, pos, p) for p in ch]
    return pts


# --- entry points -------------------------------------------------------------
async def _sections(store, graph_id: str, path: str, node: str, axis: str,
                    positions, n: int) -> dict:
    from . import executor
    from .api import _resolve_asset
    workdir = store.dir(graph_id)
    if path:
        f = _resolve_asset(workdir, path)
        data = await asyncio.to_thread(executor.lens_data_file, f, workdir,
                                       axis, positions, n)
    else:
        data = await asyncio.to_thread(executor.lens_data_graph,
                                       store.load(graph_id), workdir, axis,
                                       positions, n, 120, node or None)
    if not data.get("success"):
        err = data.get("error") or "section failed"
        if not path and not node and "timed out" in str(err):
            err += (" — the graph's combined result is too heavy to section (an"
                    " imported scan mesh?): pass node=<the part's node> or"
                    " path=<the STL/STEP in assets/>")
        raise ValueError(err)
    return data


async def lens(store, graph_id: str, *, path: str = "", node: str = "",
               axis: str = "z", positions: Optional[list] = None, n: int = 1,
               view: str = "iso", azim: Optional[float] = None,
               elev: Optional[float] = None, zoom: float = 1.0,
               width: int = 1000, height: int = 700, run: bool = False,
               isolate: bool = False, cols: int = 3) -> tuple[bytes, dict]:
    """The lens: perspective + cutting plane(s) + projected section contour(s)
    beside the 2D section plot(s). Returns (png, meta)."""
    from . import api
    from .screenshot import render_html
    axis = str(axis).lower()
    if axis not in _AXIS_I:
        raise ValueError(f"axis must be x, y or z, not {axis!r}")
    if positions:
        positions = [float(p) for p in positions][:12]
    n = max(1, min(int(n), 12))
    data = await _sections(store, graph_id, path, node, axis, positions, n)
    secs = data["sections"]
    probe = _probe_points(data)
    png, meta = await api.screenshot(
        store, graph_id, view=view, azim=azim, elev=elev, zoom=zoom,
        width=width, height=height, node=node, isolate=isolate, run=run,
        probe=probe, scale=2)
    cw, ch = meta.get("canvas_css") or [width, height]
    layers = _layers(data, set(range(len(secs))), meta.get("probe_px") or [],
                     PALETTE)

    persp = (f'<div class="card"><div class="persp" style="width:{cw:.0f}px;height:{ch:.0f}px">'
             f'<img src="{_b64(png)}" width="{cw:.0f}" height="{ch:.0f}">'
             f'{_overlay_svg(cw, ch, layers)}{_triad_svg(meta.get("axes_px"), cw, ch)}</div>'
             f'<div class="cap">{_esc(_cam_caption(meta))}</div></div>')
    src = path or (f"nodo {node}" if node else "risultato del grafo")
    head = (f'<div class="head"><b>LENTE</b> · {_esc(graph_id)} · sorgente: {_esc(src)}'
            f' ({_esc(data.get("source", "?"))}) · taglio ⟂ {axis.upper()},'
            f' coordinate nel piano {_esc(data["coords"])} · {len(secs)} sezione/i</div>')
    pw = 520 if len(secs) > 1 else max(420, int(ch))
    phh = 420 if len(secs) > 1 else int(ch)
    cards = []
    for k, sec in enumerate(secs):
        col = PALETTE[k % len(PALETTE)]
        cards.append(f'<div class="card">{section_svg(sec, axis, data["bbox"], col, pw, phh)}'
                     f'<div class="cap" style="width:{pw}px"><span class="tag" style="background:{col}"></span>'
                     f'<b>#{k + 1} {axis}={_fmt(sec["pos"])} mm</b>\n{_esc(sec.get("text", ""))[:600]}</div></div>')
    if len(secs) == 1:
        body = f'<div class="wrap">{persp}{cards[0]}</div>'
        page_w = int(cw + pw + 50)
    else:
        cols = max(1, min(int(cols), 4))
        body = (f'<div class="wrap">{persp}</div>'
                f'<div class="wrap" style="width:{cols * (pw + 14) + 12}px">{"".join(cards)}</div>')
        page_w = int(max(cw + 26, cols * (pw + 14) + 12))
    html = (f'<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}</style></head>'
            f'<body style="width:{page_w}px">{head}{body}</body></html>')
    out = await render_html(html, width=page_w, height=600, scale=1)
    info = {"graph": graph_id, "source": src, "axis": axis, "coords": data["coords"],
            "bbox": data["bbox"], "camera": meta.get("camera"),
            "azim": meta.get("azim"), "elev": meta.get("elev"),
            "sections": [{"n": k + 1, "pos": s["pos"], "color": PALETTE[k % len(PALETTE)],
                          "loops": len(s.get("loops") or []),
                          "open": len(s.get("open") or []), "text": s.get("text", "")}
                         for k, s in enumerate(secs)],
            "bytes": len(out)}
    return out, info


async def views_sheet(store, graph_id: str, *, angles: Optional[list] = None,
                      node: str = "", isolate: bool = False, zoom: float = 1.0,
                      cell_w: int = 560, cell_h: int = 420, cols: int = 3,
                      run: bool = True, cut_axis: str = "", cut_pos: Optional[float] = None,
                      path: str = "") -> tuple[bytes, dict]:
    """N camera angles on one sheet, each captioned with its camera and an XYZ
    triad; optional cut plane + section contour drawn in every view."""
    from . import api
    from .screenshot import render_html
    angs = []
    for a in (angles or DEFAULT_ANGLES)[:16]:
        if isinstance(a, dict):
            angs.append((float(a.get("azim", 0)), float(a.get("elev", 0))))
        else:
            angs.append((float(a[0]), float(a[1])))
    data, probe = None, None
    if cut_axis and cut_pos is not None:
        data = await _sections(store, graph_id, path, node, cut_axis.lower(),
                               [float(cut_pos)], 1)
        probe = _probe_points(data)
    cells, cams = [], []
    for k, (az, el) in enumerate(angs):
        png, meta = await api.screenshot(
            store, graph_id, azim=az, elev=el, zoom=zoom, width=cell_w,
            height=cell_h, node=node, isolate=isolate, run=(run and k == 0),
            probe=probe, scale=1)
        cw, chh = meta.get("canvas_css") or [cell_w, cell_h]
        over = ""
        if data is not None:
            over = _overlay_svg(cw, chh, _layers(data, {0}, meta.get("probe_px") or [], PALETTE))
        cells.append(f'<div class="card"><div class="persp" style="width:{cw:.0f}px;height:{chh:.0f}px">'
                     f'<img src="{_b64(png)}" width="{cw:.0f}" height="{chh:.0f}">{over}'
                     f'{_triad_svg(meta.get("axes_px"), cw, chh)}'
                     f'<div style="position:absolute;right:8px;top:6px;color:#fff;font-weight:700;'
                     f'font-size:15px;text-shadow:0 0 3px #000">#{k + 1}</div></div>'
                     f'<div class="cap" style="width:{cw:.0f}px">#{k + 1} {_esc(_cam_caption(meta))}</div></div>')
        cams.append({"n": k + 1, "azim": meta.get("azim"), "elev": meta.get("elev"),
                     "camera": meta.get("camera"), "axes_px": meta.get("axes_px")})
    cols = max(1, min(int(cols), 4))
    page_w = cols * (cell_w + 14) + 12
    cut = (f" · taglio {cut_axis.upper()}={_fmt(cut_pos)}" if data is not None else "")
    head = (f'<div class="head"><b>VISTE</b> · {_esc(graph_id)} · {len(angs)} angoli'
            f'{cut} · azimut nel piano XY da +X, elevazione dal piano XY, Z in alto</div>')
    html = (f'<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}</style></head>'
            f'<body style="width:{page_w}px">{head}<div class="wrap">{"".join(cells)}</div>'
            f'</body></html>')
    out = await render_html(html, width=page_w, height=600, scale=1)
    return out, {"graph": graph_id, "views": cams, "bytes": len(out),
                 "cut": ({"axis": cut_axis, "pos": cut_pos,
                          "text": data["sections"][0].get("text")} if data else None)}
