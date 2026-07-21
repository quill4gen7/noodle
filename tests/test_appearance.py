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
    assert "const before = targets.map(n => ({n, color: n.previewColor," in NODES


def test_a_visit_is_one_undo_step():
    """Not one per click — the modal applies live, so a naive recordHistory in
    `apply` would bury the previous state under a dozen entries."""
    i = NODES.index("async function editAppearance")
    body = NODES[i:i + 5000]
    assert body.count("recordHistory()") == 1
    assert body.index("recordHistory()") > body.index("const ok = await m.p;")


def test_it_applies_without_re_executing():
    """finish/color/wireframe never reach the transpiler (the generated source is
    byte-identical with and without them — see tests/test_print.py), so the modal
    re-renders the last view instead of paying for a run."""
    i = NODES.index("async function editAppearance")
    body = NODES[i:i + 5000]
    assert "refreshDisplay()" in body
    assert "runGraph" not in body and "scheduleLive" not in body


def test_a_multi_selection_is_styled_together():
    assert "sel.length > 1 && sel.includes(node)" in NODES


def test_escape_reverts_rather_than_confirming():
    i = NODES.index("async function editAppearance")
    body = NODES[i:i + 5000]
    assert "if (e.key === 'Escape'){ e.stopPropagation(); m.close(false); }" in body
    # …and the listener is removed again, or every later Esc hits a dead modal
    assert "document.removeEventListener('keydown', onKey, true);" in body
