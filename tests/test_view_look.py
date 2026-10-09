"""🔍 Aspetto in /view (webui/view-look.js) and the 👻 ghost finish (viewer.js).

Pure-Python contract checks; the rendering itself was verified in the browser
(PLAN_VIEW_SECTION §2). The look is a VIEW of a frozen generation: it lives in
the URL hash and is never written anywhere."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIEW = (ROOT / "webui" / "view.html").read_text()
LOOK = (ROOT / "webui" / "view-look.js").read_text()
VIEWER = (ROOT / "webui" / "viewer.js").read_text()
NODES = (ROOT / "webui" / "nodes.html").read_text()


def test_ghost_is_a_finish_of_the_shared_viewer_and_of_the_editor():
    mm = VIEWER.split("export function makeMaterial(")[1].split("\nexport ")[0]
    ghost = mm.split("finish === 'ghost'")[1].split("return m;")[0]
    assert "transparent: true" in ghost and "depthWrite: false" in ghost
    assert "GHOST_OPACITY" in ghost and "export const GHOST_OPACITY = 0.15" in VIEWER
    edges = VIEWER.split("export function ghostEdges(")[1].split("\n}")[0]
    assert "new THREE.EdgesGeometry(src, 30)" in edges
    assert "lines.raycast = () => {}" in edges          # edges never take a pick
    # both editor paths draw the edges: a fresh mesh and an in-place restyle
    assert "if (finish === 'ghost') syncGhostEdges(mesh" in VIEWER
    assert "syncGhostEdges(obj, color);" in VIEWER.split("function restylePreview(")[1].split("\nexport class")[0]
    assert "['ghost'," in NODES.split("const FINISHES = [")[1].split("];")[0]


def test_the_look_is_view_only_and_rides_the_hash():
    assert "fetch(" not in LOOK and "localStorage" not in LOOK
    write = VIEW.split("function writeHash(")[1].split("\n}")[0]
    assert "LOOK.hashValue()" in write and "'look=' + look" in write
    boot = VIEW.split("async function boot(")[1]
    # read ONCE at load, before apply() writes the hash back
    assert boot.index("LOOK.fromHash(hashParam('look'))") < boot.index("\n  apply();")


def test_every_leaf_row_and_the_selection_bar_reach_the_menu():
    rows = VIEW.split("function renderList(")[1].split("\n}\n")[0]
    assert rows.count('class="ib lk') == 2                # a node row and a leaf row
    assert rows.count("LOOK.openMenu(") == 2
    assert 'id="sel-look"' in VIEW and "$('sel-look').onclick" in VIEW
    keys = VIEW.split("addEventListener('keydown', e => {")[1].split("\n});")[0]
    assert "k === 'g' && selected" in keys


def test_glow_is_resynced_after_a_change():
    refresh = LOOK.split("function refresh(")[1].split("\n}")[0]
    assert "markGlow(" in refresh and "syncGlow()" in refresh
    assert "syncGlow() {" in VIEWER


def test_a_piece_of_a_fanout_is_restyled_through_its_own_object():
    """The glow pass draws whole OBJECTS: an emissive group swapped in place
    would light every piece of the shared buffer. A proxy mesh is its own."""
    paint = LOOK.split("function paint(")[1].split("\nfunction refresh(")[0]
    assert "proxy" in paint and "base.visible = false" in paint
    assert "face.materialIndex = i" in paint             # a pick names the piece


def test_look_inside_is_one_finish_for_the_whole_envelope():
    inside = LOOK.split("export function lookInside(")[1].split("\n}")[0]
    assert "finish: mode" in inside and "coverOf(key)" in inside
    assert 'data-in="glass"' in LOOK and 'data-in="ghost"' in LOOK
    assert 'data-in="emissive"' not in LOOK               # never a mix, never glass+ghost


def test_the_pick_goes_through_a_ghost():
    fh = VIEW.split("function firstHit(")[1].split("\nfunction labelOf(")[0]
    assert "hitOf(hits, true) || hitOf(hits, false)" in fh
