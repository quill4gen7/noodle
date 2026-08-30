"""
The 🎨 Aspetto modal — colour + finish + wireframe in one place.

Pure-Python source assertions, like the other frontend contracts in tests/: what
matters is that the three display-only properties still land on the same node
fields (so graph.json is untouched by this UI change), that the modal it replaced
three separate menu entries with really did replace them, and that cancelling
puts everything back.
"""

import pathlib

NODES = (pathlib.Path(__file__).resolve().parent.parent / "webui" / "nodes.html").read_text()

# The modal's own source, bounded by the function that follows it — slicing a
# fixed number of characters silently stopped covering the tail as it grew.
_i = NODES.index("async function editAppearance")
MODAL = NODES[_i:NODES.index("async function renameGroup", _i)]


def test_the_three_old_entries_are_gone():
    """A Wireframe toggle and two submenus, whose common flaw was that you could
    not see the result without closing them first."""
    assert "'✨ Finish'" not in NODES
    assert "'🎨 Preview colour'" not in NODES
    assert "Preview colour'})" not in NODES


def test_one_entry_replaces_them():
    assert "opts.push({content: '🎨 Aspetto…', callback: ()=> editAppearance(self)});" in NODES


def test_it_is_still_gated_on_nodes_that_draw():
    """A data/math node has nothing to colour."""
    i = NODES.index("editAppearance(self)")
    assert "w.isEye" in NODES[i - 400:i]


def test_the_custom_colour_prompt_is_gone():
    """`prompt()` blocks the thread and shows nothing — the hex field is live."""
    assert "prompt('Hex colour" not in NODES


def test_it_writes_the_same_three_node_fields():
    """UI-only change: the state model and graph.json keys are untouched, so old
    graphs need no migration and nothing else in the app has to know."""
    for field in ("previewColor", "previewFinish", "wireframe"):
        assert f"t.{field} = st." in NODES, field


def test_cancelling_restores_what_was_captured_on_open():
    for field in ("previewColor", "previewFinish", "wireframe"):
        assert f"b.n.{field} = b." in NODES, field
    assert "const before = touch.map(n => ({n, color: n.previewColor," in MODAL


def test_a_visit_is_one_undo_step():
    """Not one per click — the modal applies live, so a naive recordHistory in
    `apply` would bury the previous state under a dozen entries."""
    assert MODAL.count("recordHistory()") == 1
    assert MODAL.index("recordHistory()") > MODAL.index("const ok = await m.p;")


def test_it_applies_without_re_executing():
    """finish/color/wireframe never reach the transpiler (the generated source is
    byte-identical with and without them — see tests/test_print.py), so the modal
    re-renders the last view instead of paying for a run."""
    assert "refreshDisplay()" in MODAL
    assert "runGraph" not in MODAL and "scheduleLive" not in MODAL


def test_a_multi_selection_is_styled_together():
    assert "sel.length > 1 && sel.includes(node)" in NODES


def test_escape_reverts_rather_than_confirming():
    assert "if (e.key === 'Escape'){ e.stopPropagation(); m.close(false); }" in MODAL
    # …and the listener is removed again, or every later Esc hits a dead modal
    assert "document.removeEventListener('keydown', onKey, true);" in MODAL


# --- per PIECE: a scene's bodies name the node that drew them --------------
def test_a_scene_lists_its_pieces():
    """The jar is glass and the bolts metal because they are DIFFERENT nodes —
    already expressible, but you had to know to open this on the container's
    node, whose own preview is usually off. The modal finds them for you."""
    assert "function piecesOf(node)" in NODES
    assert "b.owner" in MODAL or "b.owner" in NODES
    assert "const pieces = targets.length === 1 ? piecesOf(node) : null;" in MODAL


def test_one_piece_offers_no_choice():
    """A preview with a single body has nothing to pick between."""
    i = NODES.index("function piecesOf(node)")
    body = NODES[i:NODES.index("async function editAppearance", i)]
    assert "return out.length > 1 ? out : null;" in body


def test_an_unresolvable_owner_is_dropped_not_crashed():
    """body.owner is an on-disk id; nodeFor may legitimately miss."""
    i = NODES.index("function piecesOf(node)")
    body = NODES[i:NODES.index("async function editAppearance", i)]
    assert "filter(e => e.node)" in body


def test_switching_piece_retargets_and_rereads():
    """Each piece is a different NODE, so switching must re-read that node's own
    values — showing the previous piece's colour would be a lie."""
    assert "cur = p; targets = [p.node];" in MODAL
    assert "st.color = p.node.previewColor ?? null;" in MODAL


def test_cancel_covers_every_piece_visited():
    """Not just the one showing when the modal closed."""
    assert "const touch = pieces ? pieces.map(p => p.node) : targets;" in MODAL


def test_no_new_state_is_introduced():
    """The per-piece UI is a VIEW onto the per-node fields that already exist —
    no per-body override map, so graph.json is untouched and nothing migrates."""
    for invented in ("pieceColors", "bodyColors", "pieceFinish", "bodyOverrides"):
        assert invented not in NODES, invented
